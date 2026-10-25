"""WS-авторизация, отказ и утечки (стори 6.5; TD 6.5: W7–W11, W14, W15, W20).

**Почему файл синхронный.** `starlette.testclient.TestClient` синхронный: он
поднимает приложение в отдельном потоке со своим event loop. `async def` тест
получил бы второй loop и упал `RuntimeError: This event loop is already running`
(см. докстринг `ws_harness.py` из Chore P-6).

**Почему посев идёт через `asyncio.run` с собственным движком.** Приложение под
`TestClient` открывает СВОИ соединения к БД; данные, записанные в
savepoint-сессию, ему не видны (доказано в P-6: savepoint-commit → 0 строк
снаружи). Синхронная фикстура с `asyncio.run` — тот же приём, что у
`clean_database` в `conftest.py`.

**Почему один `TestClient` на тест.** `ws_hub` и `broker` — модульные
синглтоны: два `TestClient` дали бы два loop'а и одну задачу брокера,
привязанную к первому (закрытому) loop. Два соединения открываются на ОДНОМ
клиенте.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_lib
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import pytest
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services.ws_hub import ws_hub
from tests.integration.ws_harness import (
    ACCESS_COOKIE_NAME,
    WS_PATH,
    envelope,
    expect_silence,
    receive_envelope,
    subscribe,
)
from tests.integration.ws_seed import WsCompany, seed_company

DATABASE_URL = __import__("os").environ.get("DATABASE_URL")

pytestmark = [
    pytest.mark.usefixtures("domain_cleanup"),
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="WS-тесты требуют реальный PostgreSQL: задайте DATABASE_URL",
    ),
]


def _cookie_headers(token: str) -> dict[str, str]:
    """Заголовок cookie для per-connection подмены участника.

    `TestClient(cookies=…)` задаёт cookie на КЛИЕНТ, а в тестах утечек нужен
    один клиент на много соединений разных пользователей.
    """
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


def _seed_company() -> WsCompany:
    """Создаёт компанию РЕАЛЬНЫМ коммитом и возвращает токены и uuid.

    Свой движок на вызов: `asyncio.run` создаёт новый loop, и движок,
    переиспользованный между тестами, дал бы `InterfaceError: another
    operation is in progress` (тот же класс, что в P-6 у `second_engine`).
    """
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> WsCompany:
        async with factory() as session:
            return await seed_company(session)

    try:
        return asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture
def company() -> WsCompany:
    """Компания с автором, участником и посторонним (AC-06)."""
    ws_hub.clear()
    return _seed_company()


@contextmanager
def ws_pair(
    company: WsCompany, *, first: str, second: str | None = None
) -> Iterator[tuple[WebSocketTestSession, WebSocketTestSession | None]]:
    """Два WS-соединения на ОДНОМ `TestClient`.

    Хелпер, а не `connect_ws` из P-6: тот создаёт новый `TestClient` на каждый
    вызов, а для фан-аута оба соединения обязаны жить в одном loop'е и одном
    `ws_hub`.
    """
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(app, cookies={ACCESS_COOKIE_NAME: first}))
        session_a = stack.enter_context(client.websocket_connect(WS_PATH))
        if second is None:
            yield session_a, None
            return
        # Второй клиент нужен со СВОЕЙ cookie: `TestClient.cookies` живёт на
        # клиенте, а не на соединении.
        client_b = stack.enter_context(TestClient(app, cookies={ACCESS_COOKIE_NAME: second}))
        session_b = stack.enter_context(client_b.websocket_connect(WS_PATH))
        yield session_a, session_b


# ——— W10: токен в query отвергается ——————————————————————————————————————


def test_token_in_query_is_rejected(company: WsCompany) -> None:
    """W10 (R7): `/ws?token=<валидный JWT>` без cookie — отказ 1008.

    Мутация M16: добавить fallback `websocket.query_params.get("token")` —
    тест краснеет. Токен в URL попадает в логи прокси и `Referer`.
    """
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(f"{WS_PATH}?token={company.member_token}") as ws,
    ):
        ws.receive_json()

    assert exc.value.code == 1008
    assert exc.value.reason == ""


# ——— W7, W8, W9: отказ без деталей ————————————————————————————————————————


def test_stranger_gets_1008_without_details(company: WsCompany) -> None:
    """W7 (R5, AC-06): посторонний при подписке закрыт кодом 1008 без причины.

    Мутация M4: добавить `reason="not a participant"` — тест краснеет.
    """
    with ws_pair(company, first=company.stranger_token) as (ws, _):
        subscribe(ws, company.chat_uuid)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()

    assert exc.value.code == 1008
    assert exc.value.reason == ""


def test_three_denial_branches_are_indistinguishable(company: WsCompany) -> None:
    """W8 (R5): нет cookie / чужой чат / несуществующий чат — ОДИН ответ.

    Три отдельные попытки сравниваются по `(code, reason)`: отличие хотя бы
    в одном раскрыло бы, что чат существует (ADR-13, NFR33).
    """
    results: list[tuple[int, str]] = []

    # (а) без cookie
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(WS_PATH) as ws,
    ):
        ws.receive_json()
    results.append((exc.value.code, exc.value.reason or ""))

    # (б) валидная сессия постороннего на существующий чат
    with ws_pair(company, first=company.stranger_token) as (ws, _):
        subscribe(ws, company.chat_uuid)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        results.append((exc.value.code, exc.value.reason or ""))

    # (в) посторонний на НЕсуществующий чат
    unknown = str(uuid_lib.uuid4())
    with ws_pair(company, first=company.stranger_token) as (ws, _):
        subscribe(ws, unknown)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        results.append((exc.value.code, exc.value.reason or ""))

    assert results == [(1008, ""), (1008, ""), (1008, "")], results


def test_participant_gets_events_stranger_does_not(company: WsCompany) -> None:
    """W9 (§18.11: оба значения признака).

    Участник подписан и получает `chat.subscribed`; посторонний за то же
    время не получает НИЧЕГО и закрыт кодом 1008. Без второй половины тест
    проходил бы при «пускаем всех, но молча».
    """
    with ws_pair(company, first=company.member_token) as (ws, _):
        subscribe(ws, company.chat_uuid)
        confirmation = receive_envelope(ws, expected_type="chat.subscribed")
        assert confirmation["payload"]["chat_uuid"] == company.chat_uuid

    with ws_pair(company, first=company.stranger_token) as (ws, _):
        subscribe(ws, company.chat_uuid)
        with pytest.raises(WebSocketDisconnect):
            expect_silence(ws, timeout=1.0)


# ——— W11: cookie не логируется ——————————————————————————————————————————


def test_no_cookie_in_logs(company: WsCompany) -> None:
    """W11, W20 (R14): значение cookie НЕ попадает в логи handshake.

    Мутация M20: `logger.info("ws.connect", cookies=dict(websocket.cookies))` —
    тест краснеет. Утечка сессии в лог равна её компрометации для того, у кого
    есть доступ к логам.
    """
    with (
        structlog.testing.capture_logs() as logs,
        ws_pair(company, first=company.member_token) as (ws, _),
    ):
        subscribe(ws, company.chat_uuid)
        receive_envelope(ws, expected_type="chat.subscribed")

    serialized = repr(logs)
    assert company.member_token not in serialized
    assert "karamel_access" not in serialized


# ——— W14, W15: утечки реестра —————————————————————————————————————————


def test_registry_empty_after_20_cycles(company: WsCompany) -> None:
    """W14 (R6, Task 3.3): после 20 циклов connect/subscribe/close реестр пуст.

    **Проверка идёт ВНУТРИ одного `TestClient`.** Первая версия открывала
    по циклу на каждый `TestClient`, и тест был тавтологией: выход из
    `TestClient` запускает lifespan shutdown → `ws_shutdown()` →
    `ws_hub.clear()`. Мутация M10 (убрать `ws_hub.drop` в `finally`) такой
    тест НЕ красила — проверено прогоном: `1 passed`. Здесь клиент один,
    цикл идёт по соединениям, реестр проверяется до остановки приложения.

    Мутация M10: убрать `ws_hub.drop(connection)` в `finally` обработчика —
    после 20 соединений реестр содержит 20 мёртвых записей.
    """
    with TestClient(app) as client:
        for _ in range(20):
            with client.websocket_connect(
                WS_PATH, headers=_cookie_headers(company.member_token)
            ) as ws:
                subscribe(ws, company.chat_uuid)
                receive_envelope(ws, expected_type="chat.subscribed")

        connections, chat_rooms, user_rooms = ws_hub.sizes()
        assert (connections, chat_rooms, user_rooms) == (0, 0, 0)


def test_unsubscribe_on_screen_change(company: WsCompany) -> None:
    """W15 (R6): `chat.unsubscribe` снимает подписку БЕЗ разрыва соединения.

    Отдельный класс утечки: соединение живо, но экран сменился. Мутация M10b
    (игнорировать `chat.unsubscribe`) — тест краснеет при зелёном W14.
    """
    with ws_pair(company, first=company.member_token) as (ws, _):
        subscribe(ws, company.chat_uuid)
        receive_envelope(ws, expected_type="chat.subscribed")
        assert ws_hub.sizes()[1] == 1

        ws.send_json(envelope("chat.unsubscribe", {"chat_uuid": company.chat_uuid}))

        # Соединение живо: следующий кадр доходит до сервера и не рвёт сокет.
        subscribe(ws, company.chat_uuid)
        receive_envelope(ws, expected_type="chat.subscribed")

    assert ws_hub.sizes() == (0, 0, 0)


# ——— NFR60: неизвестный тип клиента игнорируется ———————————————————————


def test_unknown_client_type_is_ignored_not_fatal(company: WsCompany) -> None:
    """AC-02/NFR60: неизвестный тип от клиента НЕ закрывает соединение.

    Клиент будущей версии может прислать тип, которого сервер не знает;
    рвать из-за этого связь — ломать чат при обновлении фронта.
    """
    with ws_pair(company, first=company.member_token) as (ws, _):
        ws.send_json(envelope("future.unknown", {"a": 1}))
        # Соединение обязано остаться рабочим: подписка после мусора проходит.
        subscribe(ws, company.chat_uuid)
        receive_envelope(ws, expected_type="chat.subscribed")
