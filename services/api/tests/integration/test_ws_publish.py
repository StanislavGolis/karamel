"""Доставка, порядок `commit → publish` и дедуп (стори 6.5; TD 6.5: W1–W6, W12, W13, W17, W19).

**Правило: РОВНО ОДИН `TestClient` жив на тест.** `ws_hub` и `broker` —
модульные синглтоны, а задача брокера привязана к event loop того
`TestClient`, который её запустил. Второй одновременный `TestClient`
запускает свой loop: `broker.start()` возвращается рано (`_started=True`),
а `stop()` отменяет задачу ЧУЖОГО loop'а — `RuntimeError: got Future attached
to a different loop` (проверено прогоном: 6 падений). Поэтому оба участника
живут на одном клиенте: cookie подставляется per-connection
(`websocket_connect(..., headers={"cookie": …})`) и per-request
(`client.post(..., cookies=…)`).

**Почему спай ставится на `chat_message_service.publish_to_chat`.** Сервис
импортирует имя по значению, поэтому подмена в модуле-источнике на него НЕ
влияет. То же правило действует для `get_redis` внутри `ws_publisher`.

**Почему порядок доказывается двумя способами.** Перехват последовательности
(W5) не ловит подмену `commit` на `flush`: вызов `commit()` остаётся в списке,
а данные не зафиксированы. Видимость из второго соединения (W4) ловит именно
это. Ни один из двух тестов по отдельности не покрывает ADR-11.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid as uuid_lib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import TestClient, WebSocketTestSession

from app.main import app
from app.repositories.messages import MessageRepository
from app.services.ws_hub import ws_hub
from tests.integration.ws_harness import (
    ACCESS_COOKIE_NAME,
    WS_PATH,
    envelope,
    receive_envelope,
    subscribe,
)
from tests.integration.ws_seed import WsCompany, seed_company

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")

pytestmark = [
    pytest.mark.usefixtures("domain_cleanup"),
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="нужны реальные PostgreSQL и Redis: DATABASE_URL + REDIS_URL",
    ),
]


def _seed() -> WsCompany:
    """Компания реальным коммитом — приложение под TestClient её увидит."""
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _go() -> WsCompany:
        async with factory() as session:
            return await seed_company(session)

    async def _main() -> WsCompany:
        try:
            return await _go()
        finally:
            await engine.dispose()

    return asyncio.run(_main())


@pytest.fixture
def company() -> WsCompany:
    """Компания и чистый реестр соединений перед тестом."""
    ws_hub.clear()
    return _seed()


@pytest.fixture
def server() -> Iterator[TestClient]:
    """ЕДИНСТВЕННЫЙ `TestClient` на тест (см. модульный докстринг)."""
    ws_hub.clear()
    with TestClient(app) as client:
        yield client
    ws_hub.clear()


def _cookie_headers(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


@contextmanager
def ws_open(
    server: TestClient, token: str, *, chat_uuid: str | None = None
) -> Iterator[WebSocketTestSession]:
    """Открывает WS с cookie нужного участника и (опционально) подписывает.

    Cookie передаётся заголовком, а не через `TestClient(cookies=…)`: клиент
    один на тест, а участников двое.
    """
    with server.websocket_connect(WS_PATH, headers=_cookie_headers(token)) as ws:
        if chat_uuid is not None:
            subscribe(ws, chat_uuid)
            receive_envelope(ws, expected_type="chat.subscribed")
        yield ws


def _post_message(server: TestClient, token: str, chat_uuid: str, body: str) -> Any:
    """Отправляет сообщение через HTTP-эндпоинт 6.4 от имени участника."""
    return server.post(
        f"/api/v1/chats/{chat_uuid}/messages",
        json={"body": body, "client_message_id": str(uuid_lib.uuid4())},
        cookies={ACCESS_COOKIE_NAME: token},
    )


# ——— W1, W2, W3: доставка второму участнику ————————————————————————————


def test_second_participant_receives_message(company: WsCompany, server: TestClient) -> None:
    """W1 (AC-01, NFR10): участник получает `message.created` с РАСШИФРОВАННЫМ текстом.

    Ключ не покидает сервер: в payload лежит текст, а не шифртекст (NFR31).
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        response = _post_message(server, company.author_token, company.chat_uuid, "привет")
        assert response.status_code == 201, response.text
        event = receive_envelope(ws, expected_type="message.created", timeout=3.0)

    assert event["payload"]["body"] == "привет"
    assert event["payload"]["chat_uuid"] == company.chat_uuid
    assert event["payload"]["message_uuid"] == response.json()["uuid"]


def test_payload_contains_no_key_or_ciphertext(company: WsCompany, server: TestClient) -> None:
    """W2 (NFR31, R8): в payload нет ключа и шифртекста.

    Проверяется СОСТАВ полей и совпадение `body` с отправленным текстом:
    если бы сервер отправил шифртекст, клиент показал бы мусор, а ключ
    оказался бы в браузере.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        _post_message(server, company.author_token, company.chat_uuid, "секрет")
        event = receive_envelope(ws, expected_type="message.created", timeout=3.0)

    payload = event["payload"]
    assert payload["body"] == "секрет"
    for forbidden in ("key", "ciphertext", "nonce", "tag", "chat_id", "sender_id"):
        assert forbidden not in payload


def test_payload_is_single_message_object(company: WsCompany, server: TestClient) -> None:
    """W3 (R18): payload — ОДНО сообщение, а не массив истории."""
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        _post_message(server, company.author_token, company.chat_uuid, "одно")
        event = receive_envelope(ws, expected_type="message.created", timeout=3.0)

    assert isinstance(event["payload"], dict)
    assert set(event["payload"]) == {
        "message_uuid",
        "chat_uuid",
        "sender_uuid",
        "body",
        "created_at",
        # Стори 6.7 (AC-04, D-6.7-4): признак системности есть ВСЕГДА (у
        # обычного сообщения `false`), чтобы клиент не угадывал его по
        # отсутствию ключа.
        "is_system",
    }
    assert event["payload"]["is_system"] is False
    assert event["payload"]["created_at"].endswith("Z")


# ——— W4, W5: порядок commit → publish ——————————————————————————————————


def test_publish_sees_committed_row(
    company: WsCompany, server: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4 (AC-04, ADR-11): в момент `publish` строка УЖЕ видна извне.

    Спай открывает СВОЁ соединение к БД. Подмена `commit` на `flush` (мутация
    M1) оставляет W5 зелёным, а этот тест красным: снаружи строки нет.
    """
    seen: list[Any] = []
    original = __import__(
        "app.services.chat_message_service", fromlist=["publish_to_chat"]
    ).publish_to_chat

    async def spy(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        engine = create_async_engine(DATABASE_URL, poolclass=None)
        try:
            async with engine.connect() as connection:
                row = await connection.execute(
                    text("SELECT uuid FROM messages WHERE uuid = :u"),
                    {"u": uuid_lib.UUID(envelope.payload["message_uuid"])},
                )
                seen.append(row.first())
        finally:
            await engine.dispose()
        await original(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr("app.services.chat_message_service.publish_to_chat", spy)

    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid):
        response = _post_message(server, company.author_token, company.chat_uuid, "видимость")

    assert response.status_code == 201, response.text
    assert seen == [(uuid_lib.UUID(response.json()["uuid"]),)], seen


def test_call_order_commit_then_publish(
    company: WsCompany, server: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5 (AC-04): последовательность вызовов — ровно `commit`, затем `publish`.

    Сравнение ПОЛНОЙ последовательности (`==`), а не вхождения: проверка
    `"publish" in calls` прошла бы и при обратном порядке (§18.11, мутация M1).
    """
    calls: list[str] = []
    original_commit = AsyncSession.commit
    original_publish = __import__(
        "app.services.chat_message_service", fromlist=["publish_to_chat"]
    ).publish_to_chat

    async def spy_commit(self: AsyncSession) -> None:
        result = await original_commit(self)
        calls.append("commit")
        return result

    async def spy_publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        calls.append("publish")
        await original_publish(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr(AsyncSession, "commit", spy_commit)
    monkeypatch.setattr("app.services.chat_message_service.publish_to_chat", spy_publish)

    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid):
        calls.clear()
        response = _post_message(server, company.author_token, company.chat_uuid, "порядок")

    assert response.status_code == 201, response.text
    assert calls == ["commit", "publish"], calls


# ——— W6, W12: сбой публикации и деградация —————————————————————————————


class _FailingRedis:
    """Клиент Redis, у которого `PUBLISH` падает (R8)."""

    async def publish(self, channel: str, payload: str) -> None:
        raise RedisError("Redis недоступен (тест)")


def test_publish_failure_does_not_break_http(
    company: WsCompany, server: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W6 (AC-04, R8): падение `PUBLISH` → HTTP `201`, лог `ws.publish.failed`.

    Мутация M3/M17 (проброс исключения из `publish`) — тест краснеет: клиент
    получил бы `500` на исправной записи в БД.
    """
    monkeypatch.setattr("app.services.ws_publisher.get_redis", lambda: _FailingRedis())

    with (
        structlog.testing.capture_logs() as logs,
        ws_open(server, company.member_token, chat_uuid=company.chat_uuid),
    ):
        response = _post_message(server, company.author_token, company.chat_uuid, "без Redis")

    assert response.status_code == 201, response.text
    assert any(entry.get("event") == "ws.publish.failed" for entry in logs)


def test_no_redis_local_delivery_still_works(
    company: WsCompany, server: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W12 (Task 5.2): без Redis событие доходит локально, соединение живо.

    Деградация, а не отказ: участник получает сообщение даже когда шина
    недоступна. Мутация «`raise` вместо локальной доставки» — тест краснеет.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        monkeypatch.setattr("app.services.ws_publisher.get_redis", lambda: _FailingRedis())
        response = _post_message(server, company.author_token, company.chat_uuid, "локально")
        assert response.status_code == 201, response.text
        event = receive_envelope(ws, expected_type="message.created", timeout=3.0)

    assert event["payload"]["body"] == "локально"


# ——— W13: дедуп дубля из Redis ————————————————————————————————————————


def test_duplicate_publish_delivered_once(company: WsCompany, server: TestClient) -> None:
    """W13 (AC-05, R3): двойной `PUBLISH` одного события → ОДНА доставка.

    Redis Pub/Sub не даёт exactly-once. Мутация M6 (убрать дедуп) — клиент
    получает событие дважды; мутация M7 (дедуп по одному `type`) — теряет
    следующие сообщения (проверяется W1).
    """
    message_uuid = str(uuid_lib.uuid4())
    frame = envelope(
        "message.created",
        {
            "message_uuid": message_uuid,
            "chat_uuid": company.chat_uuid,
            "sender_uuid": str(company.author.uuid),
            "body": "дубль",
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )

    async def _publish_twice() -> None:
        from redis.asyncio import Redis

        client = Redis.from_url(REDIS_URL, decode_responses=True)
        try:
            payload = json.dumps(frame, ensure_ascii=False)
            await client.publish(f"ws:chat:{company.chat_uuid}", payload)
            await client.publish(f"ws:chat:{company.chat_uuid}", payload)
        finally:
            await client.aclose()

    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        asyncio.run(_publish_twice())

        first = receive_envelope(ws, expected_type="message.created", timeout=3.0)
        assert first["payload"]["message_uuid"] == message_uuid
        with pytest.raises(TimeoutError):
            receive_envelope(ws, expected_type="message.created", timeout=1.0)


# ——— W17: догрузка после реконнекта ———————————————————————————————————


def test_missed_messages_loaded_after_reconnect(company: WsCompany, server: TestClient) -> None:
    """W17 (AC-03, R2): после обрыва пропущенное догружается по курсору.

    Соединение разрывается, пока автор отправляет два сообщения; после
    переподключения клиент запрашивает историю с курсора последнего
    полученного и получает РОВНО пропущенное — без потерь и дублей.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        first = _post_message(server, company.author_token, company.chat_uuid, "m1")
        assert first.status_code == 201, first.text
        event_first = receive_envelope(ws, expected_type="message.created", timeout=3.0)
        cursor_source = event_first["payload"]

        # Обрыв и два пропущенных сообщения.
        ws.close()
        missed = [
            _post_message(server, company.author_token, company.chat_uuid, "m2"),
            _post_message(server, company.author_token, company.chat_uuid, "m3"),
        ]
        assert [r.status_code for r in missed] == [201, 201]

        # Догрузка по курсору `(created_at, uuid)` — контракт 6.4.
        raw = json.dumps(
            {
                "created_at": cursor_source["created_at"],
                "uuid": cursor_source["message_uuid"],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        history = server.get(
            f"/api/v1/chats/{company.chat_uuid}/messages",
            params={"cursor": cursor},
            cookies={ACCESS_COOKIE_NAME: company.member_token},
        )

    assert history.status_code == 200, history.text
    loaded = {item["uuid"] for item in history.json()["items"]}
    expected = {r.json()["uuid"] for r in missed}
    assert loaded == expected, (loaded, expected)


# ——— W19: CAS delivered_at ——————————————————————————————————————————


def test_delivered_at_cas(company: WsCompany) -> None:
    """W19 (Task 6.2, R11): первый `mark_delivered` → 1, второй → 0.

    Мутация M11 (снять `WHERE delivered_at IS NULL`) — второй вызов тоже
    вернёт 1 и перезапишет время доставки.

    Синхронный тест со своим движком: `second_session` из conftest привязана
    к event loop pytest-asyncio, а WS-тесты здесь синхронные.
    """
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    message_uuid = uuid_lib.uuid4()

    async def _go() -> tuple[int, int, Any, Any]:
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO messages (chat_id, sender_id, body, uuid, created_at) "
                    "SELECT id, :sender, decode(repeat('ab', 40), 'hex'), :u, now() "
                    "FROM chats WHERE uuid = :chat"
                ),
                {
                    "sender": company.author.id,
                    "u": message_uuid,
                    "chat": company.chat_uuid_obj,
                },
            )
            await session.commit()
            chat_id = int(
                (
                    await session.execute(
                        text("SELECT id FROM chats WHERE uuid = :chat"),
                        {"chat": company.chat_uuid_obj},
                    )
                ).scalar_one()
            )
            repository = MessageRepository(session)
            first = await repository.mark_delivered(chat_id=chat_id, message_uuid=message_uuid)
            await session.commit()
            delivered_at = (
                await session.execute(
                    text("SELECT delivered_at FROM messages WHERE uuid = :u"),
                    {"u": message_uuid},
                )
            ).scalar_one()
            second = await repository.mark_delivered(chat_id=chat_id, message_uuid=message_uuid)
            await session.commit()
            delivered_at_after = (
                await session.execute(
                    text("SELECT delivered_at FROM messages WHERE uuid = :u"),
                    {"u": message_uuid},
                )
            ).scalar_one()
            return first, second, delivered_at, delivered_at_after

    async def _main() -> tuple[int, int, Any, Any]:
        try:
            return await _go()
        finally:
            await engine.dispose()

    first, second, delivered_at, delivered_at_after = asyncio.run(_main())

    assert first == 1
    assert delivered_at is not None
    assert second == 0
    assert delivered_at_after == delivered_at


# ——— W18: NFR10 ≤1 с (маркер perf, не блокирует PR) ——————————————————————


@pytest.mark.perf
def test_delivery_under_one_second(company: WsCompany, server: TestClient) -> None:
    """W18 (AC-01, NFR10): доставка укладывается в 1000 мс.

    **Тест НЕ блокирует PR** (маркер `perf`, TD D-6.5-1): тайминг в нагруженном
    CI флакует, а «зелёный» замер локально ничего не доказывает про прод за
    TLS и прокси. NFR10 закрывается СТРУКТУРНО (W4/W5: сетевых вызовов внутри
    транзакции нет — ADR-11), а реальная цифра снимается на стенде.

    Здесь замер всё равно полезен: он ловит грубую регрессию (публикация
    внутри транзакции, ожидание сети до commit) в сотни миллисекунд.
    """
    import time

    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        started = time.monotonic()
        response = _post_message(server, company.author_token, company.chat_uuid, "быстро")
        assert response.status_code == 201, response.text
        receive_envelope(ws, expected_type="message.created", timeout=3.0)
        elapsed_ms = (time.monotonic() - started) * 1000

    assert elapsed_ms < 1000, f"доставка заняла {elapsed_ms:.0f} мс (NFR10: ≤1000)"


# ——— W16: порядок commit → publish на пути card.full ————————————————————


async def test_no_publish_before_commit_on_card_full(
    second_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W16 (AC-04, «на всех путях»): `card.full` публикуется ПОСЛЕ commit.

    Второй путь сборки — `join_request_service.select_join_request` (выбор
    автора). Мутация M5 (перенести `publish` до `commit`) обязана красить
    этот тест: иначе событие о сборке компании уходило бы о данных, которых
    ещё нет в БД (гонка чтения, ADR-11).
    """
    from app.services import join_request_service

    calls: list[str] = []
    original_commit = AsyncSession.commit
    original_publish = join_request_service.publish_to_chat

    async def spy_commit(self: AsyncSession) -> None:
        result = await original_commit(self)
        calls.append("commit")
        return result

    async def spy_publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        calls.append("publish")
        await original_publish(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr(AsyncSession, "commit", spy_commit)
    monkeypatch.setattr(join_request_service, "publish_to_chat", spy_publish)

    company = await seed_company(second_session, capacity=2)
    # Заявка уже `selected` в `seed_company`; нужен `pending`, чтобы выбор
    # автора прошёл по контракту 5б.2 (иначе `REQUEST_NOT_PENDING`).
    await second_session.execute(
        text(
            "UPDATE join_requests SET status = 'pending', decided_at = NULL "
            "WHERE card_id = :c AND applicant_id = :a"
        ),
        {"c": company.card_id, "a": company.member.id},
    )
    # AC-04 (FR70, D-5b.2-2): выбор автора существует ТОЛЬКО при вместимости 1.
    # `seed_company` создаёт карточку на 2 места с уже собранным чатом, поэтому
    # для этого пути карточка приводится к состоянию «опубликована, место
    # свободно, чата ещё нет», а прежняя строка `chats` удаляется: иначе
    # `ensure_for_card` вернёт `None` (чат уже открыт) и публикации не будет.
    await second_session.execute(
        text("DELETE FROM chats WHERE event_card_id = :c"), {"c": company.card_id}
    )
    await second_session.execute(
        text(
            "UPDATE event_cards SET status = 'published', filled_count = 0, "
            "capacity = 1, chat_uuid = NULL WHERE id = :c"
        ),
        {"c": company.card_id},
    )
    await second_session.commit()

    request_uuid = (
        await second_session.execute(
            text("SELECT uuid FROM join_requests WHERE card_id = :c AND applicant_id = :a"),
            {"c": company.card_id, "a": company.member.id},
        )
    ).scalar_one()
    await second_session.commit()

    from app.core.config import settings as app_settings  # noqa: PLC0415

    calls.clear()
    result = await join_request_service.select_join_request(
        second_session,
        author_id=company.author.id,
        request_uuid=uuid_lib.UUID(str(request_uuid)),
        settings=app_settings,
    )

    assert result.chat_uuid is not None
    # Стори 9.2: после `commit` добавлены уведомления (`request.selected`,
    # `company.assembled`), каждое со своей короткой транзакцией. Точное
    # равенство `["commit", "publish"]` стало бы проверкой ЧИСЛА коммитов,
    # а не порядка — а порядок и есть контракт ADR-11. Инвариант сохранён и
    # усилен: первый `publish` обязан идти ПОСЛЕ первого `commit`, и ни один
    # `publish` не имеет права стоять раньше него.
    first_publish = calls.index("publish")
    assert first_publish > calls.index("commit"), calls
    assert calls[0] == "commit", calls
    assert "publish" not in calls[:first_publish], calls


# ——— W1b: два сообщения одного типа ————————————————————————————————————


def test_two_messages_of_same_type_both_delivered(company: WsCompany, server: TestClient) -> None:
    """W1b (R3, мутация M7): два `message.created` доходят ОБА.

    **Зачем отдельный тест.** W1 отправляет ОДНО сообщение, поэтому
    «слишком широкий дедуп» (ключ по одному `type` вместо
    `(type, message_uuid)`) им не проверяется — мутация M7 оставалась
    зелёной (проверено прогоном: `1 passed`). Здесь сообщений два, и
    схлопывание по `type` теряет второе.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        first = _post_message(server, company.author_token, company.chat_uuid, "первое")
        second = _post_message(server, company.author_token, company.chat_uuid, "второе")
        assert (first.status_code, second.status_code) == (201, 201)

        event_one = receive_envelope(ws, expected_type="message.created", timeout=3.0)
        event_two = receive_envelope(ws, expected_type="message.created", timeout=3.0)

    bodies = {event_one["payload"]["body"], event_two["payload"]["body"]}
    assert bodies == {"первое", "второе"}, bodies
