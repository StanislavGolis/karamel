"""Чат компании: отправка и чтение сообщений (стори 6.4; AC-01…AC-06).

Роут не содержит бизнес-логики: гард участия, шифрование и идемпотентность
живут в `services/chat_message_service.py`, запросы — в репозиториях
(project-context §6). Здесь только HTTP-форма: статусы, конверт ошибки и
единица работы.

**Почему `404`, а не `403`** (FR86, ADR-13): `403` подтвердил бы
существование чата — тот же класс утечки, что скрытый диалог в v1.
Посторонний и несуществующий чат дают побайтово один ответ, включая число
SQL-запросов (D-6.4-6).

**Шифрование и ответ.** Тело сообщения расшифровывается только для ответа:
в БД и в логах его нет (NFR31, D-11). `MessageRead` не содержит ни
`chat_id`, ни `sender_id`, ни шифртекста (D-2).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ConsentUser
from app.core.db import get_session
from app.schemas.chat import (
    ChatListItem as ChatListItemResponse,
)
from app.schemas.chat import (
    ChatListLastMessage as ChatListLastMessageResponse,
)
from app.schemas.chat import (
    ChatListPage,
    ChatReadResponse,
    MessageCreate,
    MessagePage,
    MessageRead,
)
from app.services.chat_list_service import (
    CHATS_PAGE_SIZE,
    list_chats,
    mark_chat_read,
)
from app.services.chat_message_service import (
    MESSAGES_PAGE_SIZE,
    ChatMessagePage,
    ChatNotFoundError,
    MessageCorruptedError,
    list_messages,
    send_message,
)
from app.services.place_cursor import InvalidCursorError

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/chats", tags=["chats"])

_NO_STORE = {"cache-control": "no-store"}

# AC-05 (NFR19): явный код отказа при недоступной БД. `_STATUS_CODES` в
# `main.py` даёт `service_unavailable` для «сырых» `HTTPException(503)`, но
# доменная ошибка обязана нести код сама: иначе клиент ветвился бы по тексту.
SERVICE_UNAVAILABLE_CODE = "service_unavailable"


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": …}` с `no-store` — как у остальных приватных ответов."""
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


def _domain_error(error: Exception) -> JSONResponse:
    """AC-03, AC-06: доменные ошибки чата → HTTP.

    - `404 chat_not_found` — чата нет ИЛИ зритель не участник (ADR-13);
    - `500 message_corrupted` — шифртекст в БД не расшифровывается (D-6.4-5);
    - `422 invalid_cursor` — битый курсор (ошибка ввода, не сервера).
    """
    if isinstance(error, ChatNotFoundError):
        return _error(error.code, 404)
    if isinstance(error, MessageCorruptedError):
        return _error(error.code, 500)
    if isinstance(error, InvalidCursorError):
        return _error(error.code, 422)
    raise error


def _to_read(message: Any) -> MessageRead:
    """Доменный `ChatMessage` → схема ответа (одна точка сборки на два маршрута)."""
    return MessageRead(
        uuid=message.uuid,
        sender_uuid=message.sender_uuid,
        body=message.body,
        created_at=message.created_at,
        # Стори 6.7 (AC-04): признак приходит из доменного объекта, а не
        # выводится в схеме из `sender_uuid is None` — вывод разошёлся бы
        # с БД при первой же правке инварианта.
        is_system=message.is_system,
    )


@router.post("/{chat_uuid}/messages", status_code=201, response_model=MessageRead)
async def send_message_endpoint(
    chat_uuid: uuid_lib.UUID,
    payload: MessageCreate,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MessageRead | JSONResponse:
    """AC-01, AC-04, AC-05: отправка сообщения участником компании.

    `201` при ПЕРВОЙ отправке и тот же `201` с тем же `uuid` при ретрае с
    тем же `client_message_id` (NFR18): для клиента это одно и то же
    «сообщение доставлено», и различать их он не должен.

    `ConsentUser`: переписка — персональные данные (FR9), без согласия
    маршрут недоступен.

    `OperationalError` (БД недоступна) → `503` с МАШИННЫМ кодом (AC-05,
    NFR19): «тихий» сбой или голый `500` без кода заставил бы клиент
    показывать «сообщение отправлено», которого нет.
    """
    try:
        message = await send_message(
            session,
            viewer_id=user.id,
            chat_uuid=chat_uuid,
            body=payload.body,
            client_message_id=payload.client_message_id,
            settings=_settings_like(),
        )
    except ChatNotFoundError as error:
        return _domain_error(error)
    except MessageCorruptedError as error:  # pragma: no cover - защитный путь
        return _domain_error(error)
    except OperationalError:
        # NFR19: явная ошибка вместо «сообщение будто отправлено». Лог без
        # тела и без идентификатора устройства.
        logger.error("chat_message.db_unavailable", chat_uuid=str(chat_uuid))
        return _error(SERVICE_UNAVAILABLE_CODE, 503)
    return _to_read(message)


@router.get("/{chat_uuid}/messages", response_model=MessagePage)
async def list_messages_endpoint(
    chat_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = MESSAGES_PAGE_SIZE,
) -> MessagePage | JSONResponse:
    """AC-02, AC-03, AC-05: страница истории компании.

    Курсор — непрозрачная строка; битое значение даёт `422 invalid_cursor`,
    а не `500` (ошибка ввода). `limit` ограничен `Query(ge=1, le=100)`:
    верхняя граница объявлена в ДВУХ местах намеренно — схема запроса
    отбивает мусор до сервиса, сервис держит тот же предел для вызовов
    в обход HTTP (воркеры, тесты).

    FR31: «без лимита по количеству» — история листается до `next_cursor =
    null`; страница фиксирована (D-6.4-3).
    """
    try:
        page: ChatMessagePage = await list_messages(
            session,
            viewer_id=user.id,
            chat_uuid=chat_uuid,
            cursor=cursor,
            limit=limit,
            settings=_settings_like(),
        )
    except ChatNotFoundError as error:
        return _domain_error(error)
    except InvalidCursorError as error:
        return _domain_error(error)
    except MessageCorruptedError as error:
        return _domain_error(error)
    except OperationalError:
        logger.error("chat_message.db_unavailable", chat_uuid=str(chat_uuid))
        return _error(SERVICE_UNAVAILABLE_CODE, 503)

    return MessagePage(
        items=[_to_read(item) for item in page.items],
        next_cursor=page.next_cursor,
        # X-6.5-5 (chore P-7): курсор хвоста страницы для догрузки после
        # реконнекта WS. Отдельное поле, потому что `next_cursor = None` на
        # последней странице — клиент не смог бы спросить «что нового».
        last_cursor=page.last_cursor,
    )


# ——— стори 6.6: список чатов и непрочитанные (AC-01, AC-02) ———


@router.get("", response_model=ChatListPage)
async def list_chats_endpoint(
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = CHATS_PAGE_SIZE,
) -> ChatListPage | JSONResponse:
    """AC-01, AC-04, AC-05: список чатов зрителя с непрочитанными.

    Путь `/api/v1/chats` (без завершающего слеша) — конверт §4
    `{items, total, cursor}`. Порядок — по свежести активности: новое
    сообщение поднимает чат наверх.

    `cursor` — непрозрачная строка `(created_at, uuid)`; битое значение даёт
    `422 invalid_cursor`, а не `500` (ошибка ввода). `limit` ограничен
    `Query(ge=1, le=100)`: верхняя граница объявлена в схеме запроса И в
    сервисе — мусор отбивается до БД, а вызовы в обход HTTP (тесты, воркеры)
    держат тот же предел (тот же приём, что в истории 6.4).

    `ConsentUser`: список содержит превью переписки — персональные данные
    (FR9), без согласия маршрут недоступен.
    """
    try:
        page = await list_chats(
            session,
            viewer_id=user.id,
            cursor=cursor,
            limit=limit,
            settings=_settings_like(),
        )
    except InvalidCursorError as error:
        return _domain_error(error)
    except OperationalError:
        logger.error("chat_list.db_unavailable")
        return _error(SERVICE_UNAVAILABLE_CODE, 503)

    return ChatListPage(
        items=[
            ChatListItemResponse(
                chat_uuid=item.chat_uuid,
                card_uuid=item.card_uuid,
                card_title=item.card_title,
                last_message=None
                if item.last_message is None
                else ChatListLastMessageResponse(
                    uuid=item.last_message.uuid,
                    sender_uuid=item.last_message.sender_uuid,
                    body=item.last_message.body,
                    created_at=item.last_message.created_at,
                    # Стори 6.7 (AC-04): признак берётся из доменного объекта,
                    # а не выводится в схеме: вывод разошёлся бы с БД.
                    is_system=item.last_message.is_system,
                ),
                unread_count=item.unread_count,
                last_activity_at=item.last_activity_at,
            )
            for item in page.items
        ],
        total=page.total,
        cursor=page.cursor,
    )


@router.post("/{chat_uuid}/read", response_model=ChatReadResponse)
async def mark_chat_read_endpoint(
    chat_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChatReadResponse | JSONResponse:
    """AC-02: отметка прочтения — обнуляет непрочитанные, идемпотентно.

    Повторный вызов возвращает `200` с тем же телом: для клиента это одно и
    то же «непрочитанных нет», и различать первый вызов от повтора он не
    должен (NFR18-подобная логика для чтения).

    Посторонний получает `404 chat_not_found` — побайтово тот же ответ и с
    тем же числом SQL-запросов, что «чата нет» (ADR-13, анти-оракул D-6.4-6).
    """
    try:
        result = await mark_chat_read(session, viewer_id=user.id, chat_uuid=chat_uuid)
    except ChatNotFoundError as error:
        return _domain_error(error)
    except OperationalError:
        logger.error("chat_list.db_unavailable", chat_uuid=str(chat_uuid))
        return _error(SERVICE_UNAVAILABLE_CODE, 503)

    return ChatReadResponse(chat_uuid=result.chat_uuid, unread_count=result.unread_count)


def _settings_like() -> Any:
    """Настройки приложения как `ChatSettings` (ключ шифрования).

    Отдельная функция, а не импорт `settings` на уровне модуля: тест
    подменяет ключ через `settings.chat_message_key`, и общий объект должен
    оставаться тем же (иначе расшифровка в тесте пошла бы другим ключом).
    """
    from app.core.config import settings

    return settings
