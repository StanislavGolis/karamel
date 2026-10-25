"""Гард размера тела запроса (фикс OOM, дефект стенд-проверки 2.3).

Тесты вызывают middleware напрямую по ASGI-протоколу, без HTTP-клиента:
chunked-запрос (без `Content-Length`) через `httpx.ASGITransport` не
воспроизводится — транспорт сам решает, как передать тело, и отдаёт `400`
до нашего кода. Прямой вызов проверяет ровно то, что нужно: сколько раз
middleware дёрнула `receive()` и что вернула.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from app.core.body_limit import BODY_TOO_LARGE_CODE, BodySizeLimitMiddleware

LIMIT = 1024


def _scope(*, content_length: int | None = None) -> Scope:
    """HTTP-scope с телом и опциональным `Content-Length`."""
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/profiles",
        "raw_path": b"/api/v1/profiles",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


def _collector() -> tuple[list[Message], Send]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return sent, send


def _body_of(sent: list[Message]) -> dict[str, Any]:
    """JSON-тело ответа из отправленных ASGI-сообщений."""
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _status_of(sent: list[Message]) -> int:
    start = next(m for m in sent if m["type"] == "http.response.start")
    status: int = start["status"]
    return status


async def test_declared_length_over_limit_rejected_without_reading_body() -> None:
    """Рубеж 1: `Content-Length` сверх лимита — `413` до единого `receive()`.

    Это и есть фикс OOM: тело никогда не попадает в память. Счётчик вызовов
    `receive()` — прямая проверка, что приложение тело не читало.
    """
    receive_calls = 0

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"x" * LIMIT, "more_body": False}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        # Приложение не должно быть вызвано вообще.
        raise AssertionError("приложение вызвано при превышении лимита")

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(content_length=LIMIT * 10), receive, send)

    assert receive_calls == 0
    assert _status_of(sent) == 413
    assert _body_of(sent) == {"code": BODY_TOO_LARGE_CODE}


async def test_response_is_json_with_no_store() -> None:
    """Конверт ошибки единый: `application/json` и `no-store`, как в роутах.

    Без `content-type` клиент (BFF Next) не разобрал бы тело как JSON,
    а без `no-store` отказ мог бы закэшироваться на устройстве.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("не должен вызываться")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(content_length=LIMIT + 1), receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {name.decode(): value.decode() for name, value in start["headers"]}
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert int(headers["content-length"]) == len(
        json.dumps({"code": BODY_TOO_LARGE_CODE}).encode("utf-8")
    )


async def test_chunked_body_over_limit_aborted_on_counter() -> None:
    """Рубеж 2: без `Content-Length` считаются реально прочитанные байты.

    Chunked-запрос не раскрывает размер заранее: гард по заголовку его
    пропустил бы, и клиент прислал бы сколько угодно данных. Проверяется,
    что обрыв происходит на превышении и приложение не получает тело целиком.
    """
    chunks_served = 0
    chunk = b"x" * 256

    async def receive() -> Message:
        nonlocal chunks_served
        chunks_served += 1
        # 10 чанков по 256 = 2560 байт при лимите 1024.
        more = chunks_served < 10
        return {"type": "http.request", "body": chunk, "more_body": more}

    consumed = 0

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        nonlocal consumed
        while True:
            message = await receive_()
            consumed += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(), receive, send)

    assert _status_of(sent) == 413
    assert _body_of(sent) == {"code": BODY_TOO_LARGE_CODE}
    # Точные значения, а не `<=`: приложение получило РОВНО лимит (4 чанка
    # по 256), пятое чтение вернуло пустой финальный чанк. `<= LIMIT + chunk`
    # пропустил бы регресс «читаем ещё несколько чанков сверх лимита».
    assert consumed == LIMIT
    assert chunks_served == 5


async def test_response_started_before_limit_is_completed() -> None:
    """Ответ, начатый до превышения, закрывается — а не обрывается молча.

    Сценарий стримингового роута: `http.response.start` ушёл клиенту, и только
    потом дочитывается тело сверх лимита. `413` здесь уже невозможен, но
    выбрасывать финальный `more_body=False` нельзя: соединение висело бы до
    `timeout_keep_alive`, а клиент видел бы валидный `200` без тела.
    """
    state = {"ask": 0}

    async def receive() -> Message:
        state["ask"] += 1
        if state["ask"] == 1:
            return {"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}
        return {"type": "http.disconnect"}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        # Ответ начинается ДО чтения тела — так ведёт себя стриминг.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        await receive_()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(), receive, send)

    # 413 не отправлен (статус уже ушёл), но ответ завершён финальным чанком.
    assert _status_of(sent) == 200
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert bodies[-1]["more_body"] is False
    assert b"".join(m.get("body", b"") for m in bodies) == b"part"


async def test_non_digit_content_length_is_not_trusted() -> None:
    """`Content-Length: +99999999` не должен обойти рубеж 1.

    `int()` принимает знак и пробелы, RFC 9110 — только цифры. h11 такие
    заголовки отбивает сам, но гард не должен зависеть от парсера сервера:
    нестрогий разбор пропустил бы тело в приложение.
    """

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 10, "more_body": False}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        await receive_()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent, send = _collector()
    scope = _scope()
    scope["headers"] = [(b"content-length", b"+99999999")]
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(scope, receive, send)

    # Заголовок не распознан → решение принял счётчик (10 байт < лимита).
    assert _status_of(sent) == 200


async def test_body_below_limit_is_passed_through() -> None:
    """Граница: тело меньше лимита доходит до приложения без изменений.

    Без этого теста гард «413 на всё» считался бы успешным фиксом.
    """
    payload = b'{"name": "\xd0\x90\xd0\xbd\xd1\x8f"}'
    seen: list[bytes] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        message = await receive_()
        seen.append(message.get("body", b""))
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"created"})

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(content_length=len(payload)), receive, send)

    assert seen == [payload]
    assert _status_of(sent) == 201


async def test_body_exactly_at_limit_is_accepted() -> None:
    """Граница включительна: ровно лимит — валидный запрос, не `413`.

    `>` в сравнении, а не `>=`: иначе клиент, точно рассчитавший тело,
    получал бы отказ на корректном запросе.
    """

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * LIMIT, "more_body": False}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        message = await receive_()
        assert len(message.get("body", b"")) == LIMIT
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(content_length=LIMIT), receive, send)

    assert _status_of(sent) == 200


async def test_websocket_scope_is_not_intercepted() -> None:
    """WebSocket и lifespan не имеют тела запроса — гард их не трогает."""

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        await send({"type": "websocket.accept"})

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware({"type": "websocket", "path": "/ws", "headers": []}, receive, send)

    assert sent == [{"type": "websocket.accept"}]


async def test_lifespan_scope_is_not_intercepted() -> None:
    """Lifespan-сообщения гард не трогает: у них нет HTTP-тела."""

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        await send({"type": "lifespan.startup.complete"})

    sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware({"type": "lifespan"}, receive, send)

    assert sent == [{"type": "lifespan.startup.complete"}]


async def test_disconnect_is_propagated_after_limit_exceeded() -> None:
    """`http.disconnect` доходит до приложения даже после превышения лимита.

    Иначе приложение, ждущее разрыва (`request.is_disconnected`), не увидело
    бы его никогда: ветка `limit_exceeded` отдаёт только пустые
    `http.request`-сообщения, и цикл ожидания стал бы холостым.
    """
    state = {"ask": 0}

    async def receive() -> Message:
        state["ask"] += 1
        if state["ask"] == 1:
            return {"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": True}
        return {"type": "http.disconnect"}

    seen: list[str] = []

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        first = await receive_()
        seen.append(first["type"])
        second = await receive_()
        seen.append(second["type"])

    _sent, send = _collector()
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(_scope(), receive, send)

    # Первое сообщение — тело (превышение), второе — настоящий disconnect.
    assert seen == ["http.request", "http.disconnect"]


@pytest.mark.parametrize("bad_length", [b"not-a-number", b"-5"])
async def test_malformed_content_length_falls_back_to_counter(bad_length: bytes) -> None:
    """Битый `Content-Length` не повод отказывать: решает счётчик байт.

    `-5` и мусор парсером не разбираются — заголовку верить нельзя, но и
    `413` без оснований неверен: запрос проходит во второй рубеж.
    """

    async def receive() -> Message:
        return {"type": "http.request", "body": b"y" * 10, "more_body": False}

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        await receive_()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent, send = _collector()
    scope = _scope()
    scope["headers"] = [(b"content-length", bad_length)]
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=LIMIT)
    await middleware(scope, receive, send)

    assert _status_of(sent) == 200
