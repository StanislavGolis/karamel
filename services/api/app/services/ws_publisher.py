"""Публикация WS-событий после commit (стори 6.5; AC-04, AC-05; Task 4).

**Контракт ADR-11.** Внутри транзакции сетевых вызовов нет. Порядок на
каждом пути: `INSERT/UPDATE → session.commit() → publish()`. Событие о
незакоммиченных данных не публикуется НИКОГДА: клиент увидел бы сообщение,
которого ещё нет в БД, а следующий запрос вернул бы пустоту (гонка чтения).

**Почему публикация не может бросить.** HTTP-ответ уже сформирован и
успешен: `201` на отправленное сообщение. Падение Redis — это НЕ ошибка
клиента, источник истины один (БД), а событие лишь ускоряет обновление.
`raise` здесь превратил бы недоступность Redis в `500` на исправной работе
с данными (R8 TD 6.5).

**Почему локальная доставка при сбое Redis.** Один инстанс без Redis —
штатная конфигурация разработки и деградация прода (Task 4.2/5.2). Чат
работает, событие доходит до соединений ЭТОГО процесса, в лог идёт
`ws.publish.degraded`. При нескольких инстансах без шины связь между ними
недостижима по определению — это не деградация, а отсутствие масштабирования.
"""

from __future__ import annotations

import json
import uuid as uuid_lib
from typing import Any

import structlog
from redis.exceptions import RedisError

from app.core.redis import get_redis
from app.schemas.ws import WsEnvelope

__all__ = [
    "WS_CHANNEL_PREFIX",
    "WS_USER_CHANNEL_PREFIX",
    "chat_channel",
    "publish_to_chat",
    "publish_to_user",
    "user_channel",
]

logger = structlog.get_logger(__name__)

#: Канал Redis на чат (ADR-06/§8.3 архитектуры).
WS_CHANNEL_PREFIX = "ws:chat:"

#: Канал Redis на пользователя (ADR-06, §8.3): адресные события, у которых
#: нет чата на момент возникновения (`request.received` создаётся до открытия
#: чата — ADR-06 запрещает рассылку в чат, которого ещё нет).
WS_USER_CHANNEL_PREFIX = "ws:user:"


def chat_channel(chat_uuid: uuid_lib.UUID) -> str:
    """`ws:chat:{uuid}` — строка канала; формат один для publish и psubscribe."""
    return f"{WS_CHANNEL_PREFIX}{chat_uuid}"


def user_channel(user_uuid: uuid_lib.UUID) -> str:
    """`ws:user:{uuid}` — личный канал пользователя (комната `user:{uuid}`).

    Отдельная функция, а не `chat_channel`: префикс — часть контракта брокера
    (`CHANNEL_PATTERN`), и разбор канала на стороне подписчика идёт по нему.
    Одна опечатка в префиксе сделала бы событие невидимым для всех
    подписчиков молча (мутация M1).
    """
    return f"{WS_USER_CHANNEL_PREFIX}{user_uuid}"


async def publish_to_chat(
    *,
    chat_uuid: uuid_lib.UUID,
    envelope: WsEnvelope,
) -> bool:
    """Публикует событие в канал чата. НЕ бросает (ADR-11, R8).

    Вызывается ТОЛЬКО после `await session.commit()`. Порядок проверяется
    перехватом в тестах (W4/W5 TD 6.5): ассерт состояния его не доказывает.

    Дедуп по `(type, message_uuid)` живёт в брокере, а не здесь: публикатор
    не знает, сколько раз Redis доставит сообщение, а брокер видит поток.

    **Возвращает `True`, только если `PUBLISH` реально прошёл в шину.**
    `False` — сбой Redis и локальная деградация. Это не косметика: в 9.2
    `delivered_at` уведомления ставится ИСКЛЮЧИТЕЛЬНО по `True`, иначе
    событие, дошедшее только до соединений этого процесса, помечалось бы
    доставленным всем (долг L-2, D-9.2-5). Прежние вызывающие возврат не
    использовали — изменение совместимо.
    """
    payload = json.dumps(envelope.to_frame(), ensure_ascii=False, separators=(",", ":"))
    channel = chat_channel(chat_uuid)
    try:
        client = get_redis()
        await client.publish(channel, payload)
    except (RedisError, OSError, ConnectionError) as exc:
        # R8: HTTP уже успешен, соединения не рвём каскадом.
        logger.error(
            "ws.publish.failed",
            chat_uuid=str(chat_uuid),
            event_type=envelope.type,
            error=type(exc).__name__,
        )
        await _deliver_locally_chat(chat_uuid=chat_uuid, envelope=envelope)
        return False
    return True


async def publish_to_user(
    *,
    user_uuid: uuid_lib.UUID,
    envelope: WsEnvelope,
) -> bool:
    """Публикует адресное событие в личный канал пользователя. НЕ бросает.

    Контракт идентичен `publish_to_chat` (ADR-11: после `commit`, сбой шины —
    не ошибка клиента) и отличается только адресатом: комната `user:{uuid}`
    вместо `chat:{uuid}`. Возврат `bool` нужен 9.2 для честного `delivered_at`
    (D-9.2-5, долг L-2).
    """
    payload = json.dumps(envelope.to_frame(), ensure_ascii=False, separators=(",", ":"))
    channel = user_channel(user_uuid)
    try:
        client = get_redis()
        await client.publish(channel, payload)
    except (RedisError, OSError, ConnectionError) as exc:
        # CR 9.2 r2, LOW-1: `user_uuid` в лог НЕ пишется (ADR-13/FR81):
        # идентификатор пользователя позволял бы связать все неудачные
        # доставки одного человека в цепочку — ровно тот оракул, который
        # безличность запрещает. Тип события и класс ошибки достаточны для
        # диагностики; адрес канала восстанавливается из него при нужде.
        logger.error(
            "ws.publish.failed",
            event_type=envelope.type,
            error=type(exc).__name__,
        )
        await _deliver_locally_user(user_uuid=user_uuid, envelope=envelope)
        return False
    return True


async def _deliver_locally_chat(*, chat_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> None:
    """Деградация без Redis: доставляем соединениям чата ЭТОГО процесса.

    Импорт `ws_broker` внутри функции: модуль брокера импортирует публикатор
    не напрямую, но циклический импорт через `ws_hub` сорвал бы старт
    приложения при первом же чтении схем.
    """
    from app.services.ws_broker import fan_out_local

    logger.warning(
        "ws.publish.degraded",
        chat_uuid=str(chat_uuid),
        event_type=envelope.type,
    )
    frame: dict[str, Any] = envelope.to_frame()
    await fan_out_local(chat_uuid=chat_uuid, frame=frame)


async def _deliver_locally_user(*, user_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> None:
    """Деградация без Redis для личного канала: соединения пользователя.

    Отдельная ветка, а не параметризованная общая: адресация идёт по разным
    словарям реестра (`_chats` / `_users`), и «универсальный» фан-аут
    пришлось бы разбирать по префиксу внутри — то есть та же ветвь, но
    спрятанная и без явного лога.
    """
    from app.services.ws_broker import fan_out_user_local

    logger.warning(
        # CR 9.2 r3, MEDIUM-3: `user_uuid` в лог НЕ пишется (FR81/ADR-13,
        # Task 1.2/7.2). Это тот же оракул, что в `ws.publish.failed`:
        # идентификатор человека позволил бы связать все неудачные доставки
        # одного пользователя в цепочку. Chat-ветка пишет `chat_uuid` —
        # общий ресурс, там это допустимо; user-ветка — личность.
        "ws.publish.degraded",
        event_type=envelope.type,
    )
    frame: dict[str, Any] = envelope.to_frame()
    await fan_out_user_local(user_uuid=user_uuid, frame=frame)
