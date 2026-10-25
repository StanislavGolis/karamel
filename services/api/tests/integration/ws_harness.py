"""Обвязка WS-тестов (Chore P-6, O1; TD 6.5 §«Требования к обвязке»).

**Почему `starlette.testclient.TestClient`, а не `httpx.AsyncClient`.**
`httpx.ASGITransport` WebSocket не поддерживает вообще: он умеет только
HTTP-запросы и на `websocket_connect` падает. `TestClient` поднимает
приложение через портал в отдельном потоке и даёт настоящий WS-обмен
фреймами внутри процесса — без портов, без сети, без новых зависимостей
(`starlette==1.9.0` уже в venv).

**Почему файл синхронный.** `TestClient` синхронный по построению: его
`websocket_connect`/`receive_json` блокирующие. Модули, которые им
пользуются, обязаны быть `def test_...`, а не `async def` — иначе
`asyncio_mode = "auto"` обернёт тест в event loop, а внутри портала
TestClient крутит СВОЙ loop, и два loop'а в одном потоке дают
`RuntimeError: This event loop is already running`.

**Почему таймаут получения — через поток.** У `WebSocketTestSession`
нет параметра таймаута: `receive_json()` ждёт вечно. Для инварианта
«посторонний НЕ получил событие» это означало бы зависший тест вместо
падения. Таймаут реализован потоком-демоном: он зовёт `receive_json`,
результат кладётся в очередь, основной поток ждёт `queue.get(timeout)`.
Поток-демон не мешает завершению теста; при закрытии сессии он получает
`WebSocketDisconnect` и умирает сам.
"""

from __future__ import annotations

import contextlib
import queue
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

__all__ = [
    "ACCESS_COOKIE_NAME",
    "DEFAULT_TIMEOUT",
    "WS_PATH",
    "connect_ws",
    "envelope",
    "expect_close",
    "expect_silence",
    "message_created",
    "receive_envelope",
    "send_envelope",
    "subscribe",
    "unsubscribe",
]

#: Имя cookie сессии — единый источник с `session_service.ACCESS_COOKIE_NAME`.
ACCESS_COOKIE_NAME = "karamel_access"

#: Путь WS-эндпоинта (Task 1.1 стори 6.5).
WS_PATH = "/api/v1/ws"

DEFAULT_TIMEOUT = 5.0

#: NFR60: `type` — `сущность.действие`, нижний регистр, точка.
_TYPE_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")


def envelope(
    type_: str, payload: dict[str, Any], ts: str = "2026-10-24T00:00:00Z"
) -> dict[str, Any]:
    """Собирает конверт `{type, payload, ts}` (NFR60).

    Хелпер существует, чтобы тест не собирал конверт руками в десяти местах
    и не разошёлся с контрактом: `type` проверяется тем же регексом, что
    валидатор сервера, `payload` — словарь, `ts` — строка с `Z`.
    """
    if not _TYPE_RE.match(type_):
        raise ValueError(f"type не соответствует NFR60: {type_!r}")
    if not isinstance(payload, dict):
        raise TypeError("payload обязан быть объектом (NFR60)")
    if not ts.endswith("Z"):
        raise ValueError(f"ts обязан быть ISO-8601 UTC с Z: {ts!r}")
    return {"type": type_, "payload": payload, "ts": ts}


def message_created(
    *,
    message_uuid: str,
    chat_uuid: str,
    sender_uuid: str,
    body: str,
    created_at: str = "2026-10-24T00:00:00Z",
) -> dict[str, Any]:
    """Конверт `message.created` с полями из §«Конверт» стори 6.5."""
    return envelope(
        "message.created",
        {
            "message_uuid": message_uuid,
            "chat_uuid": chat_uuid,
            "sender_uuid": sender_uuid,
            "body": body,
            "created_at": created_at,
        },
    )


@contextmanager
def connect_ws(
    app: FastAPI,
    *,
    token: str | None = None,
    path: str = WS_PATH,
    headers: dict[str, str] | None = None,
) -> Iterator[WebSocketTestSession]:
    """Открывает WS-соединение с cookie и ГАРАНТИРОВАННО закрывает его.

    Соединение открывается только после успешного handshake: если сервер
    отверг попытку до `accept()` (Task 2.2 — отказ ДО accept), исключение
    `WebSocketDisconnect` вылетит уже из `websocket_connect`, и `yield` не
    выполнится — тест обязан ловить это снаружи через `expect_close` или
    `pytest.raises`.

    `with TestClient(app)` обязателен, а не опционален: без него не
    выполняется `lifespan` (подписка `PSUBSCRIBE`, коннект к Redis, dispose),
    и тест падал бы по ложной причине «брокер не подписан».
    """
    client_kwargs: dict[str, Any] = {}
    if token is not None:
        client_kwargs["cookies"] = {ACCESS_COOKIE_NAME: token}
    ws_kwargs: dict[str, Any] = {}
    if headers is not None:
        ws_kwargs["headers"] = headers

    # `WebSocketTestSession` ОБЯЗАН войти в контекст: `__enter__` создаёт
    # `self.portal`, запускает задачу `_run` и шлёт `websocket.connect`.
    # Без этого `send_json` падает `AttributeError: no attribute 'portal'`
    # (проверено исходником starlette 1.9 и вживую).
    with contextlib.ExitStack() as stack:
        client = stack.enter_context(TestClient(app, **client_kwargs))
        session = stack.enter_context(client.websocket_connect(path, **ws_kwargs))
        yield session


def send_envelope(session: WebSocketTestSession, type_: str, payload: dict[str, Any]) -> None:
    """Отправляет конверт, проверенный на соответствие NFR60."""
    session.send_json(envelope(type_, payload))


def subscribe(session: WebSocketTestSession, chat_uuid: str) -> None:
    """Подписка на комнату `chat:{uuid}` (Task 3.2)."""
    send_envelope(session, "chat.subscribe", {"chat_uuid": chat_uuid})


def unsubscribe(session: WebSocketTestSession, chat_uuid: str) -> None:
    """Отписка от комнаты без разрыва соединения (Task 3.3)."""
    send_envelope(session, "chat.unsubscribe", {"chat_uuid": chat_uuid})


class _Reader:
    """ОДИН постоянный читающий поток на WS-сессию (CR 7.2 M-4).

    **Почему не поток на каждый вызов.** Прежняя реализация поднимала демона
    на `receive_json()` и бросала его при таймауте: поток оставался висеть на
    блокирующем чтении и перехватывал СЛЕДУЮЩИЙ кадр. Из-за этого
    `expect_silence` нельзя было звать иначе как последним в тесте, и тест на
    «события нет» становился фиктивным (`test_attendance_ws.py`).

    Здесь читатель живёт столько же, сколько сессия: он непрерывно читает
    кадры и складывает их в очередь. Таймаут — это таймаут НА ОЧЕРЕДИ, а не
    на сокете, поэтому после таймаута следующий кадр не теряется: он уже
    лежит в очереди или будет туда положен тем же потоком.
    """

    def __init__(self, session: WebSocketTestSession) -> None:
        self._frames: queue.Queue[object] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, args=(session,), daemon=True, name="ws-receive"
        )
        self._thread.start()

    def _run(self, session: WebSocketTestSession) -> None:
        """Читает кадры до закрытия сессии, складывая их (и исключения) в очередь."""
        while True:
            try:
                frame = session.receive_json()
            except BaseException as exc:  # noqa: BLE001 - пробрасываем как есть
                self._frames.put(exc)
                return
            self._frames.put(frame)

    def next(self, timeout: float) -> Any:
        """Следующий кадр или `TimeoutError`. Исключение сессии поднимается как есть."""
        try:
            item = self._frames.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"WS: событие не пришло за {timeout} с") from exc
        if isinstance(item, BaseException):
            raise item
        return item


#: Читатели по идентификатору сессии. Живут до конца процесса-теста: сессия
#: `TestClient` короткая, а демон сам завершится на `WebSocketDisconnect`.
_READERS: dict[int, _Reader] = {}
_READERS_LOCK = threading.Lock()


def _reader_for(session: WebSocketTestSession) -> _Reader:
    """Возвращает (создавая при первом обращении) читателя этой сессии."""
    key = id(session)
    with _READERS_LOCK:
        reader = _READERS.get(key)
        if reader is None:
            reader = _Reader(session)
            _READERS[key] = reader
        return reader


def _receive_with_timeout(session: WebSocketTestSession, timeout: float) -> Any:
    """Принимает один JSON-фрейм или поднимает `TimeoutError` (см. `_Reader`)."""
    return _reader_for(session).next(timeout)


def receive_envelope(
    session: WebSocketTestSession,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Принимает событие и проверяет конверт NFR60.

    Проверка конверта встроена, а не вынесена в отдельный ассерт: каждое
    полученное событие обязано соответствовать NFR60, и делать это вручную
    в 20 тестах — гарантированная забывчивость.
    """
    raw = _receive_with_timeout(session, timeout)
    if not isinstance(raw, dict):
        raise AssertionError(f"WS: ожидался объект конверта, получено {type(raw).__name__}")
    missing = {"type", "payload", "ts"} - raw.keys()
    if missing:
        raise AssertionError(f"WS: в конверте нет полей {sorted(missing)}: {raw!r}")
    if not _TYPE_RE.match(str(raw["type"])):
        raise AssertionError(f"WS: type не по NFR60: {raw['type']!r}")
    if not isinstance(raw["payload"], dict):
        raise AssertionError(f"WS: payload не объект: {type(raw['payload']).__name__}")
    if not str(raw["ts"]).endswith("Z"):
        raise AssertionError(f"WS: ts без суффикса Z: {raw['ts']!r}")
    if expected_type is not None and raw["type"] != expected_type:
        raise AssertionError(f"WS: ожидался тип {expected_type!r}, получен {raw['type']!r}")
    return raw


def expect_close(
    session: WebSocketTestSession,
    *,
    code: int = 1008,
    timeout: float = DEFAULT_TIMEOUT,
) -> WebSocketDisconnect:
    """Ждёт закрытия соединения сервером и проверяет код (AC-06).

    Возвращает исключение, чтобы вызывающий мог дополнительно проверить
    `reason is None or reason == ""` — отказ БЕЗ деталей.
    """
    try:
        _receive_with_timeout(session, timeout)
    except WebSocketDisconnect as exc:
        if exc.code != code:
            raise AssertionError(f"WS: ожидался код закрытия {code}, получен {exc.code}") from exc
        return exc
    raise AssertionError(f"WS: соединение не закрыто кодом {code} (пришло событие)")


def expect_silence(session: WebSocketTestSession, *, timeout: float = 0.3) -> None:
    """Проверяет, что за `timeout` НЕ пришло ни одного события.

    Нужен для инварианта «посторонний не получает события»: без него тест
    либо зависает, либо проверяет только факт закрытия и пропускает утечку.
    """
    try:
        received = _receive_with_timeout(session, timeout)
    except TimeoutError:
        return
    raise AssertionError(f"WS: ожидалась тишина, получено событие {received!r}")
