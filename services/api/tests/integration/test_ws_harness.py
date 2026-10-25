"""Тесты самой обвязки (Chore P-6; TD 6.5, O1/O2/O5).

**Зачем тестировать обвязку.** Обвязка — это тоже код, и он может врать:
хелпер, который «успешно подключился», но не передал cookie, сделал бы
все будущие WS-тесты 6.5 зелёными при неработающей авторизации. Правило
§18.2 проекта: ассерт, который не может упасть по делу, хуже отсутствующего.

**Почему на локальном приложении, а не на `app.main:app`.** WS-эндпоинта
`/api/v1/ws` в проекте ещё нет — он появится в стори 6.5. Тест обвязки на
несуществующем маршруте проверял бы 404, а не обвязку. Мини-приложение ниже
повторяет РОВНО те контракты, которые нужны хелперам: cookie в handshake,
конверт `{type,payload,ts}`, `close(1008)` без причины, тишина при отказе.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect as ClientDisconnect

from tests.integration.ws_harness import (
    ACCESS_COOKIE_NAME,
    WS_PATH,
    connect_ws,
    envelope,
    expect_close,
    expect_silence,
    receive_envelope,
    send_envelope,
    subscribe,
)

DATABASE_URL = os.environ.get("DATABASE_URL")

VALID_TOKEN = "valid-session-token"


def _build_stub_app() -> FastAPI:
    """Мини-приложение с контрактами, которые нужны обвязке.

    - без cookie `karamel_access` — `close(1008)` БЕЗ причины (AC-06);
    - с cookie — `chat.subscribed` в ответ на подписку (конверт NFR60);
    - `probe.close` — серверное закрытие кодом 1008 (для `expect_close`).
    """
    stub = FastAPI()

    @stub.websocket(WS_PATH)
    async def ws_endpoint(websocket: WebSocket) -> None:
        token = websocket.cookies.get(ACCESS_COOKIE_NAME)
        if token != VALID_TOKEN:
            # Отказ ДО accept(), без деталей: посторонний не должен
            # отличить «нет сессии» от «нет прав» (ADR-13).
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive_json()
                type_ = message.get("type")
                if type_ == "probe.close":
                    await websocket.close(code=1008)
                    return
                if type_ == "chat.subscribe":
                    await websocket.send_json(
                        {
                            "type": "chat.subscribed",
                            "payload": {"chat_uuid": message["payload"]["chat_uuid"]},
                            "ts": "2026-10-24T00:00:00Z",
                        }
                    )
                    continue
                await websocket.send_json(
                    {"type": "probe.echo", "payload": message, "ts": "2026-10-24T00:00:00Z"}
                )
        except WebSocketDisconnect:
            return

    return stub


# ——— O1: WS-клиент —————————————————————————————————————————————————————————


def test_connect_with_cookie_receives_envelope() -> None:
    """O1: cookie доезжает до сервера, конверт возвращается и валиден.

    Мутация, которая должна покраснить тест: убрать передачу cookie в
    `connect_ws` — тогда handshake получит отказ и `receive_envelope`
    поднимет `WebSocketDisconnect` вместо конверта.
    """
    app = _build_stub_app()
    with connect_ws(app, token=VALID_TOKEN) as session:
        subscribe(session, chat_uuid="c-1")
        received = receive_envelope(session, expected_type="chat.subscribed")
    assert received["payload"] == {"chat_uuid": "c-1"}


def test_send_envelope_roundtrip() -> None:
    """O1: отправленный конверт доходит до сервера без искажений.

    Проверяется, что `send_envelope` реально шлёт `type`/`payload`/`ts`,
    а не, например, только `payload`: сервер в заглушке возвращает принятое
    сообщение целиком.
    """
    app = _build_stub_app()
    with connect_ws(app, token=VALID_TOKEN) as session:
        send_envelope(session, "probe.ping", {"n": 1})
        received = receive_envelope(session, expected_type="probe.echo")
    assert received["payload"]["payload"] == {"n": 1}
    assert received["payload"]["type"] == "probe.ping"


def test_connect_without_cookie_is_closed_1008() -> None:
    """O1: без cookie соединение закрыто 1008 — и это видно через хелпер.

    Если бы `connect_ws` глотал отказ, тест на постороннего в 6.5 был бы
    неотличим от успешной подписки.
    """
    app = _build_stub_app()
    with (
        pytest.raises(ClientDisconnect) as exc,
        connect_ws(app, token=None) as session,
    ):
        receive_envelope(session)
    assert exc.value.code == 1008
    assert exc.value.reason == ""


def test_expect_close_checks_code_and_empty_reason() -> None:
    """O1: `expect_close` возвращает исключение с кодом и ПУСТОЙ причиной.

    Пустая причина — часть AC-06: отказ без деталей. Хелпер обязан отдавать
    исключение наружу, иначе тест не сможет проверить `reason`.
    """
    app = _build_stub_app()
    with connect_ws(app, token=VALID_TOKEN) as session:
        send_envelope(session, "probe.close", {})
        closed = expect_close(session, code=1008)
    assert closed.code == 1008
    assert closed.reason == ""


def test_expect_silence_returns_on_timeout() -> None:
    """O1: `expect_silence` НЕ зависает и не падает, когда событий нет.

    Без этого инвариант «посторонний не получил событие» либо висел бы
    вечно, либо проходил бы всегда (что ещё хуже — тавтология §18.2).
    """
    app = _build_stub_app()
    with connect_ws(app, token=VALID_TOKEN) as session:
        expect_silence(session, timeout=0.2)


def test_expect_silence_fails_when_event_arrives() -> None:
    """O1: `expect_silence` краснеет, если событие всё-таки пришло.

    Обратное значение признака (§18.11): проверка тишины обязана уметь
    падать, иначе она ничего не проверяет.
    """
    app = _build_stub_app()
    with connect_ws(app, token=VALID_TOKEN) as session:
        send_envelope(session, "probe.ping", {})
        with pytest.raises(AssertionError, match="ожидалась тишина"):
            expect_silence(session, timeout=2.0)


def test_envelope_rejects_broken_type_and_ts() -> None:
    """O1: хелпер не даёт собрать конверт, нарушающий NFR60.

    Проверяются оба значения (§18.11): корректный тип/ts проходят,
    некорректные — отвергаются с понятной ошибкой.
    """
    assert envelope("message.created", {"a": 1})["type"] == "message.created"
    with pytest.raises(ValueError, match="NFR60"):
        envelope("Message.Created", {})
    with pytest.raises(ValueError, match="NFR60"):
        envelope("messagecreated", {})
    with pytest.raises(TypeError):
        envelope("message.created", [1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ts"):
        envelope("message.created", {}, ts="2026-10-24T00:00:00+00:00")


def test_testclient_ws_reaches_stub_through_http_transport() -> None:
    """O1: фиксирует ФАКТ — `httpx.ASGITransport` не нужен и TestClient работает.

    Тест-страховка от «оптимизации» обвязки в сторону `httpx.AsyncClient`:
    он WebSocket не умеет, и такой рефакторинг должен краснеть здесь, а не
    в двадцати тестах стори 6.5.
    """
    app = _build_stub_app()
    cookies = {ACCESS_COOKIE_NAME: VALID_TOKEN}
    with (
        TestClient(app, cookies=cookies) as client,
        client.websocket_connect(WS_PATH) as session,
    ):
        session.send_json(envelope("probe.ping", {}))
        assert session.receive_json()["type"] == "probe.echo"


# ——— O2: независимая сессия ————————————————————————————————————————————————


# Служебной таблицы НЕТ: O2 проверяется на доменной `users`.
#
# **Почему не своя таблица.** Первая версия создавала `_p6_probe` (session-
# scoped, реальным коммитом). Это уронило `tests/unit/test_alembic_revisions.py`:
# `alembic check` идёт ПОСЛЕ integration-тестов и видит в базе таблицу,
# которой нет в моделях — «Detected removed table '_p6_probe'» (проверено,
# прогон: 1 failed, 1826 passed). Доменная таблица такой проблемы не создаёт:
# она и в моделях, и в TRUNCATE-списке `clean_database`.
#
# Идентификаторы строк разведены по тестам (901–903), чтобы тесты не зависели
# от порядка выполнения и от строк, оставшихся от соседей.

_USER_INSERTS = {
    901: (
        "INSERT INTO users (id, provider, provider_user_id, phone) "
        "VALUES (901, 'phone', 'p6-901', '+79070000901')"
    ),
    902: (
        "INSERT INTO users (id, provider, provider_user_id, phone) "
        "VALUES (902, 'phone', 'p6-902', '+79070000902')"
    ),
    903: (
        "INSERT INTO users (id, provider, provider_user_id, phone) "
        "VALUES (903, 'phone', 'p6-903', '+79070000903')"
    ),
}


async def test_second_session_does_not_see_uncommitted(
    session: AsyncSession, second_session: AsyncSession
) -> None:
    """O2: незакоммиченные данные НЕ видны из второго соединения.

    Без этого свойства доказательство «публикация видит закоммиченное» (W4)
    было бы тавтологией: тест «строка видна» проходил бы и при публикации
    до `commit`.
    """
    await session.execute(text(_USER_INSERTS[901]))
    await session.flush()

    visible = await second_session.execute(text("SELECT count(*) FROM users WHERE id = 901"))
    assert visible.scalar() == 0

    await second_session.rollback()


async def test_second_session_sees_committed(second_session: AsyncSession) -> None:
    """O2: настоящий `commit()` виден из второго соединения.

    Второе значение признака (§18.11): после commit строка обязана быть видна.
    Обе половины вместе доказывают, что фикстура различает состояния, а не
    «всегда возвращает 0». Строка убирается своим же коммитом, чтобы не
    попасть в чужие `count(users)`.
    """
    await second_session.execute(text(_USER_INSERTS[902]))
    await second_session.commit()

    visible = await second_session.execute(text("SELECT count(*) FROM users WHERE id = 902"))
    assert visible.scalar() == 1

    await second_session.execute(text("DELETE FROM users WHERE id = 902"))
    await second_session.commit()


async def test_main_session_commit_is_invisible_from_second_session(
    session: AsyncSession, second_session: AsyncSession
) -> None:
    """O2: `commit()` в фикстуре `session` НЕ виден извне — это SAVEPOINT.

    Тест фиксирует разницу между двумя фикстурами явно, чтобы будущий автор
    не доказывал видимость через `session`: это естественно и дало бы
    тавтологию (W4 был бы зелёным при публикации до commit).
    """
    await session.execute(text(_USER_INSERTS[903]))
    await session.commit()

    visible = await second_session.execute(text("SELECT count(*) FROM users WHERE id = 903"))
    assert visible.scalar() == 0

    await second_session.rollback()


# ——— O5: недоступный Redis —————————————————————————————————————————————————


async def test_dead_redis_raises_connection_error(dead_redis: None) -> None:
    """O5: подменённый клиент бросает `ConnectionError` на `publish`.

    Это условие риска R8: код публикации обязан поймать ошибку и залогировать
    `ws.publish.failed`, не отдавая `500` клиенту. Если фикстура вернёт
    рабочий клиент, тест 6.5 на деградацию станет тавтологией.
    """
    from app.core.redis import get_redis

    client = get_redis()
    with pytest.raises(ConnectionError, match="Redis недоступен"):
        await client.publish("ws:chat:probe", "{}")


async def test_dead_redis_psubscribe_raises(dead_redis: None) -> None:
    """O5: подписка на каналы тоже падает — брокер обязан деградировать.

    Второе значение признака: мёртвый Redis падает не только на публикации.
    Если бы `psubscribe` проходил, тест на деградацию проверял бы половину
    сценария (Task 5.2).
    """
    from app.core.redis import get_redis

    client = get_redis()
    with pytest.raises(ConnectionError):
        await client.psubscribe("ws:chat:*")
