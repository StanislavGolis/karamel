"""Фан-аут событий из Redis в локальные соединения (стори 6.5; AC-05; Task 5).

**Почему брокер подписывается на `psubscribe("ws:chat:*")`, а не на каналы
по одному.** Каналов столько же, сколько чатов: подписка на каждый при
открытии соединения означала бы `SUBSCRIBE`/`UNSUBSCRIBE` на каждую пару
«соединение × чат» и гонку с публикацией между ними. Шаблон подписывается
ОДИН раз на старте процесса (lifespan), а фильтрация идёт по реестру.

**Почему дедуп в брокере, а не в публикаторе.** Redis Pub/Sub не даёт
exactly-once: одно сообщение может быть доставлено дважды (переподписка,
реконнект клиента Redis, at-least-once семантика). Публикатор отправляет
один раз и не видит поток; брокер видит его и отбрасывает повтор.

**Почему дедуп по `(type, payload.message_uuid)`, а при отсутствии
`message_uuid` — без дедупа.** `chat.restored` не несёт `message_uuid`;
дедуп по `None` схлопнул бы два легитимных события подряд (D-6.5-6 TD 6.5).

**Почему память дедупа ограничена.** Неограниченный `set` растёт вместе с
числом событий за всё время работы процесса — это утечка под видом защиты
от дубля. Хранится окно последних ключей (кольцевой список + набор).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid as uuid_lib
from collections import OrderedDict, deque
from typing import Any

import structlog
from redis.exceptions import RedisError

from app.core.redis import get_redis
from app.services.ws_hub import ws_hub

__all__ = [
    "DEDUP_WINDOW",
    "WS_CHANNEL_PATTERNS",
    "WsBroker",
    "broker",
    "fan_out_local",
    "fan_out_user_local",
    "ws_startup",
    "ws_shutdown",
]

logger = structlog.get_logger(__name__)

#: Сколько последних ключей помнить для дедупа. 2048 с запасом покрывает
#: всплеск в чате и не растёт во времени (см. модульный докстринг).
DEDUP_WINDOW = 2048

#: Префиксы каналов: `ws:chat:{uuid}` и `ws:user:{uuid}` (ADR-06, §8.3).
#: Один `psubscribe` на каждый префикс: шаблон `ws:*` захватил бы и чужие
#: ключи того же Redis, а разбор префикса всё равно нужен.
_CHAT_PREFIX = "ws:chat:"
_USER_PREFIX = "ws:user:"

#: Шаблоны канала — то, что реально уходит в `psubscribe`/`punsubscribe`.
#: Список, а не строка: с 9.2 подписок две, и `punsubscribe` обязан снять обе,
#: иначе после рестарта в старом процессе осталась бы висящая подписка.
WS_CHANNEL_PATTERNS: tuple[str, ...] = ("ws:chat:*", "ws:user:*")


def _dedup_key(frame: dict[str, Any]) -> tuple[str, str] | None:
    """Ключ дедупа `(type, event_uuid)` или `(type, message_uuid)` (D-7.2-6).

    **Почему два ключа.** Решение 9 пакета ввело `event_uuid` в конверт
    событий-переходов (7.2), но `message.created`/`message.delivered` уже
    дедупились по `message_uuid` и несут его в payload (6.4/6.5). Менять ключ
    сообщений значило бы ломать работающий дедуп: переключение без fallback
    потеряло бы дубль сообщения ровно в момент деплоя, когда кадры в полёте
    ещё без `event_uuid`.

    **Порядок приоритета:**

    1. `event_uuid` верхнего уровня (его ставит `WsEnvelope.build` событиям-
       переходам) — для `attendance.done` и любых будущих переходов;
    2. `payload.message_uuid` — для сообщений и для кадров в полёте при
       деплое (старый сервер, новый брокер);
    3. иначе `None` — дедуп НЕПРИМЕНИМ. Это не ошибка: `chat.subscribed` и
       `chat.restored` описывают состояние соединения, и их повтор легитимен
       (D-2, регрессия 6.5 `W-SUBSCRIBED-01`).

    **Не-`str` и пустая строка → `None`, не исключение** (инвентаризация
    предикатов, строка 16): брокер получает кадры из Redis, где тип мог быть
    испорчен. Падение разборщика остановило бы фан-аут для всех соединений
    из-за одного кривого кадра.
    """
    type_ = frame.get("type")
    if not isinstance(type_, str):
        return None

    event_uuid = frame.get("event_uuid")
    if isinstance(event_uuid, str) and event_uuid:
        return (type_, event_uuid)

    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    message_uuid = payload.get("message_uuid")
    if not isinstance(message_uuid, str) or not message_uuid:
        return None
    return (type_, message_uuid)


async def fan_out_local(*, chat_uuid: uuid_lib.UUID, frame: dict[str, Any]) -> int:
    """Отправляет кадр всем соединениям комнаты ЭТОГО процесса.

    Возвращает число доставок — тесту нужно отличать «отправлено двоим» от
    «отправлено никому». Мёртвые соединения удаляются из реестра: иначе
    реестр растёт, а ошибки маскируются обработкой `send_frame`.
    """
    delivered = 0
    for connection in ws_hub.connections_for_chat(chat_uuid):
        if await connection.send_frame(frame):
            delivered += 1
        elif connection.closed:
            # CR 9.4 r1: `False` теперь означает И "умер", И "дубль этого
            # события на этом сокете". Дропать реестр можно только во втором
            # смысле — живое соединение, получившее дубль, обязано остаться в
            # комнатах, иначе одно событие тихо отписывало бы клиента от чата.
            ws_hub.drop(connection)
    return delivered


async def fan_out_user_local(*, user_uuid: uuid_lib.UUID, frame: dict[str, Any]) -> int:
    """Отправляет кадр всем соединениям ПОЛЬЗОВАТЕЛЯ в этом процессе.

    Тот же контракт, что у `fan_out_local` (D-9.2-1): `int` доставок и
    `ws_hub.drop` мёртвых соединений. Второй словарь реестра (`_users`)
    заполняется при `accept()`, поэтому клиенту не нужна подписка: адресное
    событие приходит на любое его соединение — мутация M5 (не регистрировать
    в `_users`) делает доставку нулевой при двух открытых вкладках.
    """
    delivered = 0
    for connection in ws_hub.connections_for_user(user_uuid):
        if await connection.send_frame(frame):
            delivered += 1
        elif connection.closed:
            # См. `fan_out_local`: дубль — не повод выкидывать живое соединение.
            ws_hub.drop(connection)
    return delivered


class WsBroker:
    """Подписка на `ws:chat:*` и раздача событий в локальный реестр."""

    def __init__(self, *, dedup_window: int = DEDUP_WINDOW) -> None:
        self._task: asyncio.Task[None] | None = None
        self._pubsub: Any = None
        self._started = False
        self._degraded = False
        # CR 9.4 HIGH-1: ключ включает канал — иначе намеренный фан-аут
        # одного события в разные каналы схлопывался как дубль.
        self._dedup_order: deque[tuple[str, str, str]] = deque()
        self._dedup_seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._dedup_window = dedup_window

    @property
    def started(self) -> bool:
        """Подписка активна (нужно тестам старта и диагностике)."""
        return self._started

    @property
    def degraded(self) -> bool:
        """Redis был недоступен на старте: работаем локально (Task 5.2)."""
        return self._degraded

    def _remember(self, key: tuple[str, str, str]) -> bool:
        """`True` — ключ новый; `False` — уже видели (дубль, отбросить)."""
        if key in self._dedup_seen:
            return False
        self._dedup_seen[key] = None
        self._dedup_order.append(key)
        while len(self._dedup_order) > self._dedup_window:
            oldest = self._dedup_order.popleft()
            self._dedup_seen.pop(oldest, None)
        return True

    def _accept(self, frame: dict[str, Any], channel: str) -> bool:
        """Решение «доставлять ли»: дедуп по ключу, если он применим.

        **Канал входит в ключ** (CR 9.4 HIGH-1). Дедуп защищает от повторной
        ДОСТАВКИ одного сообщения из Redis Pub/Sub (at-least-once,
        переподписка, реконнект клиента): повтор приходит В ТОТ ЖЕ канал.
        Один кадр, намеренно опубликованный в разные каналы, — это фан-аут,
        а не дубль: `attendance.request` уходит в `ws:chat:{chat}` и в
        `ws:user:{uuid}` каждого адресата с ОДНИМ `event_uuid` (ключ дедупа
        клиента). Без канала в ключе первая публикация резервировала ключ и
        все остальные каналы молча отбрасывались как `duplicate_dropped`:
        FR111 не доставлялся, а `publish_to_user` возвращал `True` (Redis
        принял сообщение), из-за чего `delivered_at` заполнялся ложно.

        Раньше ключ был `(type, event_uuid)`. Расширение БЕЗОПАСНО: все
        существующие события-переходы публикуются в ОДИН канал
        (`message.created`/`message.delivered` — в чат, `attendance.done` —
        в чат), поэтому внутри одного канала ключ ведёт себя как раньше и
        дубли по-прежнему отбрасываются. `chat.subscribed`/`chat.restored`
        дедупа не имеют вовсе (`_dedup_key` → `None`) — не меняется.
        """
        key = _dedup_key(frame)
        if key is None:
            return True
        return self._remember((key[0], key[1], channel))

    async def handle_message(self, channel: str, raw: str) -> int:
        """Разбирает сообщение канала и раздаёт его локально.

        Вынесено отдельным методом, а не спрятано в цикле: так тест дедупа
        подаёт два одинаковых сообщения напрямую, без реального Redis.
        Возвращает число доставок (0 для дубля или чужого канала).

        **Два префикса, один разборщик.** С 9.2 подписка двойная
        (`ws:chat:*` и `ws:user:*`), и адресат определяется префиксом канала.
        Неизвестный префикс — `ws.broker.bad_channel` и отброшенный кадр, а НЕ
        исключение (мутация M12): один чужой ключ в общем Redis не должен
        останавливать фан-аут чата для всех соединений.
        """
        if channel.startswith(_CHAT_PREFIX):
            target: uuid_lib.UUID | None = None
            suffix = channel[len(_CHAT_PREFIX) :]
            try:
                target = uuid_lib.UUID(suffix)
            except ValueError:
                logger.warning("ws.broker.bad_channel", channel=channel)
                return 0
            raw_uuid = target
            is_user_channel = False
        elif channel.startswith(_USER_PREFIX):
            suffix = channel[len(_USER_PREFIX) :]
            try:
                raw_uuid = uuid_lib.UUID(suffix)
            except ValueError:
                logger.warning("ws.broker.bad_channel", channel=channel)
                return 0
            is_user_channel = True
        else:
            logger.warning("ws.broker.bad_channel", channel=channel)
            return 0

        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ws.broker.bad_frame", channel=channel)
            return 0
        if not isinstance(frame, dict):
            logger.warning("ws.broker.bad_frame", channel=channel)
            return 0
        if not self._accept(frame, channel):
            logger.info(
                "ws.broker.duplicate_dropped",
                channel=channel,
                event_type=frame.get("type"),
            )
            return 0
        if is_user_channel:
            return await fan_out_user_local(user_uuid=raw_uuid, frame=frame)
        return await fan_out_local(chat_uuid=raw_uuid, frame=frame)

    async def _run(self) -> None:
        """Читает поток pub/sub до отмены задачи.

        Ошибка Redis внутри цикла НЕ завершает задачу: соединения обязаны
        пережить недоступность шины (Task 5.2 — деградация, не отказ).
        """
        assert self._pubsub is not None
        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except (RedisError, OSError, ConnectionError) as exc:
                logger.error("ws.broker.receive_failed", error=type(exc).__name__)
                await asyncio.sleep(0.5)
                continue
            if message is None:
                continue
            channel = message.get("channel")
            data = message.get("data")
            if isinstance(channel, bytes):
                channel = channel.decode()
            if isinstance(data, bytes):
                data = data.decode()
            if isinstance(channel, str) and isinstance(data, str):
                try:
                    await self.handle_message(channel, data)
                except Exception as exc:  # noqa: BLE001 - цикл не должен умирать
                    logger.error("ws.broker.fanout_failed", error=type(exc).__name__)

    async def start(self) -> None:
        """Подписывается на шаблон каналов (Task 5.1). Не бросает при отказе."""
        if self._started:
            return
        try:
            client = get_redis()
            pubsub = client.pubsub()
            await pubsub.psubscribe(*WS_CHANNEL_PATTERNS)
        except (RedisError, OSError, ConnectionError) as exc:
            # Task 5.2: один инстанс без Redis — чат работает локально.
            self._degraded = True
            logger.warning("ws.broker.degraded", error=type(exc).__name__)
            return
        self._pubsub = pubsub
        self._task = asyncio.create_task(self._run(), name="ws-broker")
        self._started = True
        logger.info("ws.broker.started", patterns=list(WS_CHANNEL_PATTERNS))

    async def stop(self) -> None:
        """Отменяет задачу и закрывает pub/sub (lifespan shutdown)."""
        self._started = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is not None:
            try:
                await pubsub.punsubscribe(*WS_CHANNEL_PATTERNS)
                await pubsub.aclose()
            except (RedisError, OSError, ConnectionError, RuntimeError):
                pass
        self._dedup_order.clear()
        self._dedup_seen.clear()

    def reset_dedup(self) -> None:
        """Сбрасывает окно дедупа — для изоляции тестов."""
        self._dedup_order.clear()
        self._dedup_seen.clear()


#: Один брокер на процесс (lifespan). Как и `ws_hub`, не `Depends`: подписка
#: живёт дольше запроса и должна быть видна роуту.
broker = WsBroker()


async def ws_startup() -> None:
    """Старт WS-подсистемы из `lifespan` приложения."""
    await broker.start()


async def ws_shutdown() -> None:
    """Остановка WS-подсистемы: брокер и реестр."""
    await broker.stop()
    ws_hub.clear()
