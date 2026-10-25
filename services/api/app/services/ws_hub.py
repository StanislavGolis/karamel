"""Реестр WS-соединений и комнат (стори 6.5; AC-01, AC-06; Task 3).

**Почему реестр в памяти процесса, а не в Redis.** Реестр отвечает на вопрос
«в каком соединении сидит участник этого чата» — это факт ОДНОГО процесса.
Сокет живёт в памяти того процесса, который его принял, и переезжать между
процессами не может. Мультиинстанс решается pub/sub'ом, а не общим реестром.

**Почему два словаря, а не один с префиксами.** Комнаты `chat:{uuid}` и
`user:{uuid}` адресуют разные аудитории (ADR-06). Один словарь с ключами
`chat:{uuid}` схлопнул бы их и потребовал разбора префикса на каждом фан-ауте.

**Почему у соединения замок.** Фан-аут идёт из задачи брокера, а подтверждение
подписки — из обработчика соединения. Оба пишут в один сокет; без замка это
гонка двух корутин на одном `WebSocket`, и Starlette падает
`RuntimeError: cannot call send once a close message has been sent`.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_lib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import structlog
from starlette.websockets import WebSocket

__all__ = ["WsConnection", "WsHub", "ws_hub"]

logger = structlog.get_logger(__name__)

#: CR 9.4 r1 (HIGH-1): сколько последних событий помнит ОДНО соединение.
#:
#: Окно на соединение, а не на процесс, как у брокера (`DEDUP_WINDOW`):
#: соединений много, и общий кольцевой буфер вытеснял бы ключи чужих сокетов —
#: при большом числе клиентов защита от дубля деградировала бы ровно тогда,
#: когда нужна. 64 с запасом покрывают всплеск адресных событий одному
#: человеку (уведомления по нескольким карточкам подряд).
WS_EVENT_DEDUP_WINDOW = 64


def _event_key(frame: dict[str, Any]) -> tuple[str, str] | None:
    """Ключ дедупа ДОСТАВКИ на сокет: `(type, event_uuid)` или `None`.

    **Почему только `event_uuid`, без fallback на `message_uuid`.** Брокер уже
    дедупит повторную доставку ОДНОГО канала, включая legacy-кадры с
    `message_uuid` (D-7.2-6). Здесь решается другая задача: одно событие,
    пришедшее одному сокету ИЗ РАЗНЫХ каналов (`ws:chat:{chat}` и
    `ws:user:{uuid}`). Различить такие доставки можно только по ключу, который
    публикатор поставил ОДИН раз на всё событие, — это `event_uuid`.

    **`None` — дедуп неприменим** (D-2). События состояния соединения
    (`chat.subscribed`, `chat.restored`) не несут `event_uuid`, и их повтор —
    легитимный факт: `chat.subscribed` обязан дойти дважды при двух подписках
    на один чат (регрессия 6.5 `W-SUBSCRIBED-01`). Дедуп по `type` схлопнул бы
    оба подтверждения.
    """
    type_ = frame.get("type")
    if not isinstance(type_, str):
        return None
    event_uuid = frame.get("event_uuid")
    if not isinstance(event_uuid, str) or not event_uuid:
        return None
    return (type_, event_uuid)


@dataclass(eq=False)
class WsConnection:
    """Одно открытое соединение: сокет, владелец и набор комнат.

    `eq=False` — идентичность по объекту: два соединения одного пользователя
    не равны друг другу, и `set` должен хранить оба.
    """

    websocket: WebSocket
    user_uuid: uuid_lib.UUID
    chats: set[uuid_lib.UUID] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False
    #: CR 9.4 r1, HIGH-1: окно последних доставленных `(type, event_uuid)`.
    #: Одно соединение сидит И в комнате чата, И в своём личном канале
    #: (ADR-06): событие, опубликованное в оба, пришло бы ему дважды. Дедуп
    #: в брокере этого не решает и решать не должен — он отбрасывает повторы
    #: ОДНОГО канала. Здесь отбрасывается повторное вручение ОДНОГО события
    #: ОДНОМУ сокету, независимо от того, сколькими каналами оно пришло.
    _delivered_events: OrderedDict[tuple[str, str], None] = field(default_factory=OrderedDict)

    async def send_frame(self, frame: dict[str, Any]) -> bool:
        """Отправляет кадр; `False` — соединение закрыто, умерло или это дубль.

        Ошибка отправки НЕ пробрасывается: событие — ускоритель, а не источник
        истины (ADR-11). Мёртвое соединение обязано исчезнуть из реестра, а не
        уронить фан-аут остальным.

        **Дедуп по `(type, event_uuid)` на сокете** (CR 9.4 r1, HIGH-1).
        `False` для дубля — это «не доставлено», и вызывающий фан-аут
        посчитает его нулём. Дедуп применяется ТОЛЬКО к кадрам с
        `event_uuid`: события без него (`chat.subscribed`, `chat.restored`)
        повторяемы по контракту (D-2), и `chat.subscribed` обязан дойти
        дважды при двух подписках на один чат (регрессия 6.5
        `W-SUBSCRIBED-01`).

        **Почему не в клиенте.** Фронт обрабатывает только `message.created`
        и не знает `event_uuid` (CR MEDIUM-1). Дедуп на сервере закрывает
        дубль независимо от версии клиента — старый клиент тоже не увидит
        два одинаковых приглашения.
        """
        if self._closed:
            return False
        event_key = _event_key(frame)
        if event_key is not None and event_key in self._delivered_events:
            return False
        async with self._lock:
            if self._closed:
                return False
            try:
                await self.websocket.send_json(frame)
            except (RuntimeError, OSError, ConnectionError):
                # `RuntimeError` — Starlette на закрытом сокете.
                self._closed = True
                return False
        if event_key is not None:
            self._remember_event(event_key)
        return True

    def _remember_event(self, key: tuple[str, str]) -> None:
        """Помнит доставленный ключ; окно ограничено, чтобы не течь памятью.

        То же ограничение, что у брокера (D-6.5-6): неограниченный `set` —
        утечка под видом защиты от дубля. `WS_EVENT_DEDUP_WINDOW` — на
        соединение, а не на процесс: соединений много, и общий счётчик
        вытеснял бы ключи чужих сокетов.
        """
        self._delivered_events[key] = None
        while len(self._delivered_events) > WS_EVENT_DEDUP_WINDOW:
            self._delivered_events.popitem(last=False)

    def mark_closed(self) -> None:
        """Помечает соединение мёртвым без попытки отправки."""
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class WsHub:
    """Реестр комнат процесса. Один экземпляр на приложение (`ws_hub`)."""

    def __init__(self) -> None:
        self._chats: dict[uuid_lib.UUID, set[WsConnection]] = {}
        self._users: dict[uuid_lib.UUID, set[WsConnection]] = {}

    def register(self, connection: WsConnection) -> None:
        """Добавляет соединение в комнату пользователя (ADR-06: при открытии)."""
        self._users.setdefault(connection.user_uuid, set()).add(connection)

    def subscribe(self, connection: WsConnection, chat_uuid: uuid_lib.UUID) -> None:
        """Подписывает соединение на чат.

        Проверку участия делает вызывающий код ДО вызова (Task 3.1): гард
        идёт первым, иначе посторонний оказался бы в комнате, а отказ
        пришлось бы «откатывать» удалением из реестра.
        """
        connection.chats.add(chat_uuid)
        self._chats.setdefault(chat_uuid, set()).add(connection)

    def unsubscribe(self, connection: WsConnection, chat_uuid: uuid_lib.UUID) -> None:
        """Снимает подписку на чат, не закрывая соединение (Task 3.3)."""
        connection.chats.discard(chat_uuid)
        bucket = self._chats.get(chat_uuid)
        if bucket is None:
            return
        bucket.discard(connection)
        if not bucket:
            # Пустой набор не держим: иначе реестр растёт числом когда-либо
            # открытых чатов, и «утечка соединений» подменяется утечкой ключей.
            self._chats.pop(chat_uuid, None)

    def drop(self, connection: WsConnection) -> None:
        """Удаляет соединение из ВСЕХ комнат (закрытие сокета).

        Без этого шага реестр хранит мёртвые соединения, и фан-аут пытается
        писать в закрытые сокеты: память растёт, а ошибки маскируются
        обработкой `send_frame` (AC-06, Task 3.3).
        """
        users = self._users.get(connection.user_uuid)
        if users is not None:
            users.discard(connection)
            if not users:
                self._users.pop(connection.user_uuid, None)
        for chat_uuid in list(connection.chats):
            self.unsubscribe(connection, chat_uuid)
        connection.mark_closed()

    def connections_for_chat(self, chat_uuid: uuid_lib.UUID) -> tuple[WsConnection, ...]:
        """Снимок соединений комнаты на момент фан-аута."""
        return tuple(self._chats.get(chat_uuid, ()))

    def connections_for_user(self, user_uuid: uuid_lib.UUID) -> tuple[WsConnection, ...]:
        """Снимок соединений пользователя (задел под `user:`-события)."""
        return tuple(self._users.get(user_uuid, ()))

    def clear(self) -> None:
        """Полная очистка — только для тестов и остановки приложения."""
        for bucket in self._users.values():
            for connection in bucket:
                connection.mark_closed()
        self._users.clear()
        self._chats.clear()

    def sizes(self) -> tuple[int, int, int]:
        """`(соединений, комнат_чатов, комнат_пользователей)` — для тестов утечек."""
        connections = sum(len(bucket) for bucket in self._users.values())
        return connections, len(self._chats), len(self._users)


#: Экземпляр на процесс. Не `Depends`: брокер и роут живут в разных жизненных
#: циклах и должны видеть ОДИН реестр, иначе фан-аут уйдёт в пустоту.
ws_hub = WsHub()
