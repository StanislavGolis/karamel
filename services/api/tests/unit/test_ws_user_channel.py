"""Транспорт пользовательской комнаты `ws:user:{uuid}` (стори 9.2; Task 1).

**Почему unit, а не только integration.** Здесь проверяется ФОРМА контракта:
префикс канала, разбор префикса брокером, возврат `bool` публикатора и
удаление мёртвых соединений. Интеграционный тест проверил бы то же самое,
но через живой Redis и `TestClient`, где причина падения не видна: любая из
четырёх поломок даёт один и тот же симптом «событие не пришло».

**Почему без Redis.** `publish_to_user`/`publish_to_chat` берут клиент через
`get_redis()`, и подмена этой функции даёт ровно три исхода контракта:
`PUBLISH` прошёл, `RedisError`, локальная деградация. Живой Redis нужен
только для сквозного сценария (он в `test_ws_user_room.py`).

**Почему фейковый сокет, а не `TestClient`.** `WsConnection.send_frame`
работает с `WebSocket.send_json`: подделки из двух методов достаточно, чтобы
проверить и доставку, и обработку мёртвого соединения без event loop'а
TestClient'а (он у каждого свой — см. `test_ws_publish.py`).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

import pytest
from redis.exceptions import RedisError

from app.schemas.ws import WsEnvelope
from app.services import ws_broker, ws_publisher
from app.services.ws_hub import WsConnection, ws_hub


def _envelope() -> WsEnvelope:
    return WsEnvelope.build("request.received", {"card_uuid": str(uuid_lib.uuid4())})


class _FakeSocket:
    """Сокет, который запоминает отправленные кадры или падает по команде."""

    def __init__(self, *, broken: bool = False) -> None:
        self.frames: list[dict[str, Any]] = []
        self._broken = broken

    async def send_json(self, frame: dict[str, Any]) -> None:
        if self._broken:
            raise RuntimeError("сокет закрыт")
        self.frames.append(frame)


class _FakeRedis:
    """Клиент Redis с одним методом `publish`.

    Запоминает `(канал, payload)` — тесту нужен именно канал: адресация
    `ws:user:` против `ws:chat:` и есть предмет проверки.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self._fail = fail

    async def publish(self, channel: str, payload: str) -> int:
        if self._fail:
            raise RedisError("шина недоступна")
        self.published.append((channel, payload))
        return 1


@pytest.fixture(autouse=True)
def clean_hub() -> None:
    """P-11: `ws_hub` — модульный синглтон, без очистки тесты текут друг в друга."""
    ws_hub.clear()
    yield
    ws_hub.clear()


def _connection(user_uuid: uuid_lib.UUID, socket: _FakeSocket) -> WsConnection:
    return WsConnection(websocket=socket, user_uuid=user_uuid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Форма канала (M1)
# ---------------------------------------------------------------------------


def test_user_channel_uses_user_prefix_not_chat() -> None:
    """M1: `user_channel` даёт `ws:user:`, а не `ws:chat:`.

    Краснеет, если префикс скопирован из `chat_channel`: публикация ушла бы в
    комнату чата, которой на момент `request.received` ещё не существует
    (ADR-06), и автор не получил бы ни одного события.
    """
    user_uuid = uuid_lib.uuid4()

    assert ws_publisher.user_channel(user_uuid) == f"ws:user:{user_uuid}"
    assert ws_publisher.chat_channel(user_uuid) == f"ws:chat:{user_uuid}"


# ---------------------------------------------------------------------------
# Контракт возврата публикатора (M2, M3, M4)
# ---------------------------------------------------------------------------


async def test_publish_to_user_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Успешный `PUBLISH` → `True`: только тогда 9.2 ставит `delivered_at`.

    Краснеет, если публикатор начнёт возвращать `None`/`False` на успехе —
    `delivered_at` тогда не заполнился бы НИКОГДА (M4).
    """
    client = _FakeRedis()
    monkeypatch.setattr(ws_publisher, "get_redis", lambda: client)
    user_uuid = uuid_lib.uuid4()

    result = await ws_publisher.publish_to_user(user_uuid=user_uuid, envelope=_envelope())

    assert result is True
    assert [channel for channel, _ in client.published] == [f"ws:user:{user_uuid}"]


async def test_publish_to_user_returns_false_on_redis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2: сбой Redis → `False`, а не `True`.

    Мутация M2 (`return True` в ветке `except`) обязана красить этот тест:
    иначе `delivered_at` заполнялся бы при недоступной шине, и подписчик
    другого инстанса никогда бы не получил событие (долг L-2).
    """
    monkeypatch.setattr(ws_publisher, "get_redis", lambda: _FakeRedis(fail=True))

    result = await ws_publisher.publish_to_user(user_uuid=uuid_lib.uuid4(), envelope=_envelope())

    assert result is False


async def test_publish_to_chat_also_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """`publish_to_chat` тоже возвращает `bool` (Task 1.1).

    Прежние вызывающие возврат не использовали — совместимо. Но контракт
    обязан быть одинаковым у двух публикаторов: расхождение (один `bool`,
    другой `None`) сделало бы `delivered_at` зависящим от того, какой
    публикатор случайно вызвали.
    """
    ok = _FakeRedis()
    monkeypatch.setattr(ws_publisher, "get_redis", lambda: ok)
    assert (
        await ws_publisher.publish_to_chat(chat_uuid=uuid_lib.uuid4(), envelope=_envelope()) is True
    )

    monkeypatch.setattr(ws_publisher, "get_redis", lambda: _FakeRedis(fail=True))
    assert (
        await ws_publisher.publish_to_chat(chat_uuid=uuid_lib.uuid4(), envelope=_envelope())
        is False
    )


# ---------------------------------------------------------------------------
# Безличность логов доставки (M21, M22; CR 9.2 r3 MEDIUM-3)
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Перехватчик лога модуля: событие + kwargs, без глобального structlog.

    **Почему не `structlog.testing.capture_logs()`.** Он захватывает только
    через тот processor-chain, что настроен ГЛОБАЛЬНО на момент вызова, а
    integration-тесты переконфигурируют structlog. В полном прогоне
    `capture_logs()` вернул пустой список при ФАКТИЧЕСКИ записанных событиях
    (видно в Captured stdout) — тест краснел от порядка запуска, а не от
    кода. Подмена логгера модуля проверяет ФАКТ вызова и его поля.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, **kwargs: Any) -> None:  # noqa: ARG002
        self.entries.append((event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._record("error", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._record("warning", event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._record("info", event, **kwargs)

    def by_event(self, event: str) -> dict[str, Any]:
        for name, kwargs in self.entries:
            if name == event:
                return kwargs
        raise AssertionError(f"событие {event!r} не записано; есть {self.entries}")


async def test_user_delivery_logs_carry_no_user_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M21/M22: НИ ОДИН лог user-ветки не содержит идентификатора человека.

    **Дефект, который закрывает тест.** `ws.publish.failed` и
    `ws.publish.degraded` писали `user_uuid`: все неудачные доставки одного
    человека связывались в цепочку — ровно тот оракул, который FR81/ADR-13
    запрещает (Task 1.2/7.2 стори). Фикс LOW-1 закрыл только первый лог;
    второй остался, и НИ ОДИН тест этого не замечал (мутация «вернуть
    `user_uuid`» давала SURVIVED).

    Проверяются ОБА лога: сбой `PUBLISH` порождает `ws.publish.failed`, а
    следующий за ним путь деградации — `ws.publish.degraded`. Тест краснеет
    при возврате поля в ЛЮБОЙ из двух (M21, M22 — обе KILLED).
    """
    recorder = _LogRecorder()
    monkeypatch.setattr(ws_publisher, "logger", recorder)
    monkeypatch.setattr(ws_publisher, "get_redis", lambda: _FakeRedis(fail=True))

    published = await ws_publisher.publish_to_user(user_uuid=uuid_lib.uuid4(), envelope=_envelope())

    assert published is False
    # Оба события обязаны быть: иначе тест проверял бы пустой набор.
    assert {name for name, _ in recorder.entries} == {
        "ws.publish.failed",
        "ws.publish.degraded",
    }, recorder.entries
    for event in ("ws.publish.failed", "ws.publish.degraded"):
        fields = recorder.by_event(event)
        assert "user_uuid" not in fields, f"{event}: user_uuid в логе ({fields})"
        assert "user_id" not in fields, f"{event}: user_id в логе ({fields})"


async def test_chat_delivery_log_still_has_chat_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Контроль: chat-ветка СОХРАНЯЕТ `chat_uuid` — общий ресурс, не личность.

    Без этого теста «убрать личность из логов» можно было бы выполнить
    удалением всей диагностики: `chat_uuid` нужен, чтобы понять, какой чат
    не получил событие.
    """
    recorder = _LogRecorder()
    monkeypatch.setattr(ws_publisher, "logger", recorder)
    monkeypatch.setattr(ws_publisher, "get_redis", lambda: _FakeRedis(fail=True))
    chat_uuid = uuid_lib.uuid4()

    await ws_publisher.publish_to_chat(chat_uuid=chat_uuid, envelope=_envelope())

    assert recorder.by_event("ws.publish.failed").get("chat_uuid") == str(chat_uuid)


# ---------------------------------------------------------------------------
# Разбор префикса и фан-аут (M11, M12, M15)
# ---------------------------------------------------------------------------


def test_channel_patterns_include_user_room() -> None:
    """M11: `ws:user:*` обязан быть в шаблонах подписки.

    Краснеет, если шаблон остался один (`ws:chat:*`): события из Redis не
    дойдут до пользовательской комнаты вообще, и обнаружится это только
    в проде — на одном инстансе спасает локальная доставка, на двух нет.
    """
    patterns = ws_broker.WS_CHANNEL_PATTERNS

    assert "ws:user:*" in patterns
    assert "ws:chat:*" in patterns


async def test_handle_message_routes_user_channel_to_user_room() -> None:
    """Событие из `ws:user:` доходит до соединения пользователя.

    Краснеет, если `handle_message` оставит только чат-ветку: канал с
    префиксом `ws:user:` отбрасывался бы как «чужой», и адресное уведомление
    не появлялось бы ни у кого.
    """
    user_uuid = uuid_lib.uuid4()
    other_uuid = uuid_lib.uuid4()
    socket = _FakeSocket()
    connection = _connection(user_uuid, socket)
    ws_hub.register(connection)

    # Второе соединение ДРУГОГО пользователя: адресность обязана удержаться.
    stranger_socket = _FakeSocket()
    ws_hub.register(_connection(other_uuid, stranger_socket))

    frame = _envelope().to_frame()
    delivered = await ws_broker.broker.handle_message(
        f"ws:user:{user_uuid}", __import__("json").dumps(frame)
    )

    assert delivered == 1
    assert socket.frames == [frame]
    assert stranger_socket.frames == []


async def test_handle_message_does_not_raise_on_unknown_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M12: неизвестный префикс — лог и 0 доставок, НЕ исключение.

    Краснеет, если разборщик начнёт падать: одно чужое сообщение в общем
    Redis остановило бы фан-аут чата для всех соединений процесса.

    **Почему подменяется `logger.warning`, а не `caplog`/`capture_logs`.**
    `structlog` конфигурируется глобально при импорте приложения, и в полном
    прогоне (после integration-тестов) `capture_logs()` возвращает пустой
    список — тест краснел ровно от порядка запуска, а не от кода. Подмена
    метода на самом логгере модуля проверяет ФАКТ вызова, не завися от
    глобальной конфигурации.
    """
    events: list[str] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: Any) -> None:  # noqa: ARG002
            events.append(event)

        def info(self, event: str, **kwargs: Any) -> None:  # noqa: ARG002
            events.append(event)

    monkeypatch.setattr(ws_broker, "logger", _Recorder())

    delivered = await ws_broker.broker.handle_message(
        "ws:other:value",
        '{"type": "request.received", "payload": {}, "ts": "2026-10-25T00:00:00Z"}',
    )

    assert delivered == 0
    assert events == ["ws.broker.bad_channel"]


async def test_fan_out_user_local_drops_dead_connection() -> None:
    """M15: мёртвое соединение удаляется из `_users`, а не остаётся навсегда.

    Краснеет, если `fan_out_user_local` проглотит `False` без `ws_hub.drop`:
    реестр растёт, фан-аут пишет в закрытые сокеты, а `sizes()` показывает
    соединения, которых нет (P-11).
    """
    user_uuid = uuid_lib.uuid4()
    dead = _connection(user_uuid, _FakeSocket(broken=True))
    alive_socket = _FakeSocket()
    alive = _connection(user_uuid, alive_socket)
    ws_hub.register(dead)
    ws_hub.register(alive)
    before = ws_hub.sizes()[0]

    delivered = await ws_broker.fan_out_user_local(
        user_uuid=user_uuid, frame=_envelope().to_frame()
    )

    assert delivered == 1
    assert alive_socket.frames != []
    assert ws_hub.sizes()[0] == before - 1
    assert dead not in ws_hub.connections_for_user(user_uuid)
