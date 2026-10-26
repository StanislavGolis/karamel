"""WebSocket-эндпоинт реального времени (стори 6.5; AC-01, AC-02, AC-06; Task 2, 3).

**Почему нативный FastAPI/Starlette, а не socket.io.** Проверено фактами
(Dev Notes стори): `socket.io`/`engineio` в проекте нет ни на бэке, ни на
фронте; `websockets` есть транзитивно через `uvicorn[standard]`; конверт
NFR60 `{type, payload, ts}` обязателен, а socket.io навязывает свой формат
`42[...]`, который пришлось бы разбирать и переупаковывать. Ноль новых
зависимостей и один протокол вместо двух.

**Почему cookie, а не токен в query.** Токен в URL попадает в логи прокси и
в `Referer` — то есть утекает туда, где его не ждут. Cookie
`karamel_access` (`HttpOnly`, `SameSite=Lax`) уже несёт сессию для HTTP, и
браузер прикладывает её к handshake same-origin автоматически.

**Почему отказ ДО `accept()` и без причины.** `403`/текст раскрыли бы
существование чата (ADR-13, NFR33): посторонний не должен отличать «нет
сессии» от «нет прав» и «чата не существует». Все три ветки — один
`close(code=1008)` с ПУСТЫМ `reason` и без записи `chat_uuid` в лог.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session_factory
from app.repositories.chats import ChatRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.users import UserRepository
from app.schemas.ws import WsEnvelope
from app.services.account_deletion_service import deletion_window_expired
from app.services.session_service import ACCESS_COOKIE_NAME
from app.services.ws_hub import WsConnection, ws_hub

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["ws"])

#: Код отказа по ADR-13: policy violation, без причины.
CLOSE_POLICY_VIOLATION = 1008

#: Заведомо отсутствующий `event_card_id` — как в `chat_message_service`:
#: запрос участия выполняется ВСЕГДА, чтобы «нет чата» не стоило на запрос
#: меньше «нет прав» (тайминг-оракул, урок F-1 стори 6.3).
_MISSING_CARD_ID = -1


async def _authenticate(websocket: WebSocket) -> tuple[int, uuid_lib.UUID] | None:
    """Резолвит пользователя по cookie. `None` — любой отказ, без деталей.

    Возвращает `(internal_id, public_uuid)`: внутренний нужен гарду участия
    (запросы ходят по `bigint`), публичный — в реестр соединений и в payload.

    **Токен в query НЕ принимается принципиально** (Task 2.1): `?token=…`
    попадает в логи прокси и Referer. Единственное исключение в проекте —
    заголовок `Authorization` для не-браузерных клиентов; здесь его тоже нет:
    WS-клиент продукта всегда браузерный.
    """
    token = websocket.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        return None

    # Импорт внутри функции: `deps` тянет HTTP-зависимости, и импорт на модуле
    # создал бы цикл `ws` → `deps` → `api.v1` при сборке роутеров.
    from app.api.deps import _is_revoked
    from app.core.security import decode_access_token

    try:
        payload = decode_access_token(token)
        user_uuid = uuid_lib.UUID(str(payload["sub"]))
    except Exception:  # noqa: BLE001 - битый токен = нет сессии, без деталей
        return None

    factory = get_session_factory()
    async with factory() as session:
        user = await UserRepository(session).get_by_uuid(user_uuid)
        if user is None:
            return None
        if _is_revoked(user, payload):
            return None
        # Стори 10.2, D-10.2-1: удаление блокирует WS только после истечения
        # 7-дневного окна. До этого доступ сохраняется (AC-01) — иначе
        # пользователь не смог бы отменить запрос. Тот же предикат, что в
        # `api/deps.py` (P-16): второго условия окна нет.
        if user.status == "banned" or deletion_window_expired(user, now=datetime.now(UTC)):
            return None
        return user.id, user.uuid


async def _is_participant(session: AsyncSession, *, chat_uuid: uuid_lib.UUID, user_id: int) -> bool:
    """Гард участия — ТОТ ЖЕ предикат, что в 6.4 (Task 3.1).

    Автор карточки — участник по определению (ADR-04); остальные должны быть
    `selected` в заявках. Второго предиката не заводим: он разошёлся бы с
    `chat_message_service` при первой правке, и человек либо видел бы события
    чужого чата, либо не получал свои.

    Запрос участия выполняется ВСЕГДА (даже при отсутствии чата) — иначе
    «нет чата» стоило бы меньше «нет прав» и существование утекало бы по
    времени ответа.
    """
    found = await ChatRepository(session).get_by_uuid(chat_uuid=chat_uuid)
    card_id_for_lookup = found[1] if found is not None else _MISSING_CARD_ID
    participating = await JoinRequestRepository(session).viewer_participation_card_ids(
        applicant_id=user_id, card_ids=[card_id_for_lookup]
    )
    if found is None:
        return False
    _chat_id, event_card_id, _card_uuid, author_id = found
    return author_id == user_id or event_card_id in participating


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Одно соединение на клиент; подписки — по комнатам `chat:{uuid}`.

    Порядок шагов контракта: авторизация по cookie → `accept` → цикл
    сообщений → отписка в `finally`. Отказ происходит ДО `accept`, поэтому
    посторонний не получает ни одного кадра.
    """
    identity = await _authenticate(websocket)
    if identity is None:
        # Отказ без деталей и до accept: посторонний не отличает причину.
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    user_id, user_uuid = identity
    await websocket.accept()

    connection = WsConnection(websocket=websocket, user_uuid=user_uuid)
    ws_hub.register(connection)
    logger.info("ws.connected", user_uuid=str(user_uuid))

    try:
        while True:
            raw = await websocket.receive_json()
            await _handle_client_message(
                websocket=websocket,
                connection=connection,
                user_id=user_id,
                raw=raw,
            )
    except WebSocketDisconnect:
        pass
    except (RuntimeError, OSError):
        # Разрыв транспорта: то же, что disconnect, без эскалации.
        pass
    finally:
        # Task 3.3: без этого шага реестр хранит мёртвые соединения, и фан-аут
        # пишет в закрытые сокеты (R6 TD 6.5).
        ws_hub.drop(connection)
        logger.info("ws.disconnected", user_uuid=str(user_uuid))


async def _handle_client_message(
    *,
    websocket: WebSocket,
    connection: WsConnection,
    user_id: int,
    raw: object,
) -> None:
    """Обрабатывает один кадр клиента: только `chat.subscribe`/`chat.unsubscribe`.

    Неизвестный тип ИГНОРИРУЕТСЯ с записью в лог и НЕ закрывает соединение:
    клиент будущей версии может прислать тип, которого этот сервер не знает,
    и рвать из-за этого связь — значит ломать чат при обновлении фронта
    (NFR60).
    """
    if not isinstance(raw, dict):
        logger.info("ws.unknown_type", type="not_an_object")
        return
    type_ = raw.get("type")
    payload = raw.get("payload")
    if not isinstance(type_, str) or not isinstance(payload, dict):
        logger.info("ws.unknown_type", type=str(type_))
        return

    if type_ == "chat.subscribe":
        await _subscribe(
            websocket=websocket, connection=connection, user_id=user_id, payload=payload
        )
        return
    if type_ == "chat.unsubscribe":
        chat_uuid = _parse_chat_uuid(payload)
        if chat_uuid is None:
            logger.info("ws.unknown_type", type=type_)
            return
        ws_hub.unsubscribe(connection, chat_uuid)
        return

    logger.info("ws.unknown_type", type=type_)


def _parse_chat_uuid(payload: dict[str, object]) -> uuid_lib.UUID | None:
    """Достаёт `chat_uuid` из payload; `None` — поле битое или отсутствует."""
    value = payload.get("chat_uuid")
    if not isinstance(value, str):
        return None
    try:
        return uuid_lib.UUID(value)
    except ValueError:
        return None


async def _subscribe(
    *,
    websocket: WebSocket,
    connection: WsConnection,
    user_id: int,
    payload: dict[str, object],
) -> None:
    """Подписка на `chat:{uuid}` с проверкой участия по БД (AC-06).

    Отказ — закрытие соединения кодом 1008 БЕЗ причины, как и при
    невалидной сессии: посторонний не должен узнать, что чат существует
    (ADR-13). Ответ `chat.unsubscribed`/`forbidden` был бы подсказкой.
    """
    chat_uuid = _parse_chat_uuid(payload)
    if chat_uuid is None:
        logger.info("ws.unknown_type", type="chat.subscribe")
        return

    factory = get_session_factory()
    async with factory() as session:
        allowed = await _is_participant(session, chat_uuid=chat_uuid, user_id=user_id)

    if not allowed:
        # В лог НЕ пишем `chat_uuid`: он раскрыл бы существование чата
        # тому, кто читает логи без прав на него.
        logger.info("ws.subscribe.denied")
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    ws_hub.subscribe(connection, chat_uuid)
    confirmation = WsEnvelope.build("chat.subscribed", {"chat_uuid": str(chat_uuid)})
    await connection.send_frame(confirmation.to_frame())
    logger.info("ws.subscribed", chat_uuid=str(chat_uuid))


def validate_envelope(frame: object) -> dict[str, object] | None:
    """Проверяет исходящий кадр по NFR60. Экспортируется для тестов."""
    if not isinstance(frame, dict):
        return None
    try:
        WsEnvelope.model_validate(frame)
    except ValidationError:
        return None
    return frame
