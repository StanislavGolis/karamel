"""Доставка в комнату `user:{uuid}` без клиентской подписки (стори 9.2; Task 1.5).

**Что доказывается.** Соединение попадает в `ws_hub._users` при `accept()`
(`api/v1/ws.py`), и адресное событие приходит БЕЗ единого `chat.subscribe`.
Это и есть контракт комнаты пользователя: `request.received` создаётся до
открытия чата (ADR-06), значит подписаться на чат клиент физически не может.

**Почему мутация M5 краснит этот тест.** Если убрать `ws_hub.register` из
эндпоинта, `connections_for_user` вернёт пустой набор, и `fan_out_user_local`
доставит 0 кадров — при этом `notifications` заполнится, то есть тест на БД
остался бы зелёным. Именно поэтому проверка идёт через живой WS-обмен.

**Почему один `TestClient` на тест.** `ws_hub` и `broker` — модульные
синглтоны: второй клиент даёт второй event loop и отменяет задачу брокера
чужого loop'а (`test_ws_publish.py`, модульный докстринг).
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import TestClient, WebSocketTestSession

from app.core.config import settings
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import issue_session
from app.services.ws_hub import ws_hub
from tests.integration.ws_harness import ACCESS_COOKIE_NAME, WS_PATH, receive_envelope

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="нужны реальные PostgreSQL и Redis: DATABASE_URL + REDIS_URL",
    ),
]

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


class _RequestSettings:
    """`JoinRequestSettings` без чтения `.env` (Protocol не инстанцируется)."""

    default_city_slug = settings.default_city_slug
    join_request_active_limit = 2
    s3_signed_url_ttl = 900


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    place_id = int(row.scalar_one())
    _CREATED_PLACES.append(place_id)
    return place_id


async def _insert_card(session: AsyncSession, *, author_id: int, place_id: int) -> str:
    """Карточка режима заявок: `capacity=1`, `published`, окно открыто, чата нет."""
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at, NULL,"
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
        },
    )
    return str(row.scalar_one())


def _seed() -> dict[str, str]:
    """Синхронная обёртка: `TestClient` требует sync-тестов и sync-фикстур.

    `async def` фикстура не видна синхронному тесту, а `async def` тест упал бы
    `RuntimeError: This event loop is already running` (см. `ws_harness.py`).
    Данные пишутся РЕАЛЬНЫМ коммитом: приложение под `TestClient` открывает
    свои соединения и savepoint-строк теста не видит.
    """
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _go() -> dict[str, str]:
        async with factory() as session:
            author, author_token = await _new_user(session)
            _applicant, applicant_token = await _new_user(session)
            place_id = await _place(session)
            card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
            await session.commit()
        return {
            "author_token": author_token,
            "applicant_token": applicant_token,
            "card_uuid": card_uuid,
        }

    async def _main() -> dict[str, str]:
        try:
            return await _go()
        finally:
            await engine.dispose()

    return asyncio.run(_main())


async def _cleanup_seed() -> None:
    """Уборка после теста: строки удаляются в порядке, обратном FK.

    Это КОРУТИНА: `asyncio.run` вызывается СНАРУЖИ (в фикстуре). Вложенный
    `asyncio.run` внутри корутины падает `RuntimeError: asyncio.run() cannot
    be called from a running event loop` — и уборка не выполняется вовсе,
    строки текут в следующий тест (P-12).
    """
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            if _CREATED_USERS:
                await session.execute(
                    text("DELETE FROM notifications WHERE user_id = ANY(:ids)"),
                    {"ids": _CREATED_USERS},
                )
                await session.execute(
                    text(
                        "DELETE FROM chats WHERE event_card_id IN "
                        "(SELECT id FROM event_cards WHERE author_id = ANY(:ids))"
                    ),
                    {"ids": _CREATED_USERS},
                )
                await session.execute(
                    text(
                        "DELETE FROM join_requests WHERE card_id IN "
                        "(SELECT id FROM event_cards WHERE author_id = ANY(:ids))"
                    ),
                    {"ids": _CREATED_USERS},
                )
                await session.execute(
                    text("DELETE FROM event_cards WHERE author_id = ANY(:ids)"),
                    {"ids": _CREATED_USERS},
                )
            if _CREATED_PLACES:
                await session.execute(
                    text("DELETE FROM places WHERE id = ANY(:ids)"),
                    {"ids": _CREATED_PLACES},
                )
            if _CREATED_USERS:
                await session.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
                )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def seeded() -> Iterator[dict[str, str]]:
    """Автор и заявитель с валидными сессиями + карточка. Реальный коммит."""
    data = _seed()
    yield data
    # `_cleanup_seed` — корутина: без `asyncio.run` она НЕ выполняется, и
    # строки текут в следующий тест (P-12). `RuntimeWarning: coroutine was
    # never awaited` в прогоне — ровно этот дефект.
    asyncio.run(_cleanup_seed())


@pytest.fixture
def server() -> Iterator[TestClient]:
    ws_hub.clear()
    with TestClient(app) as client:
        yield client
    ws_hub.clear()


def _headers(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


@contextmanager
def _ws_open(server: TestClient, token: str) -> Iterator[WebSocketTestSession]:
    with server.websocket_connect(WS_PATH, headers=_headers(token)) as ws:
        yield ws


def test_author_receives_request_received_without_any_subscribe(
    server: TestClient, seeded: dict[str, str]
) -> None:
    """AC-1 + Task 1.5: событие приходит в `user:{uuid}` БЕЗ `chat.subscribe`.

    Мутация M5 (не регистрировать соединение в `_users` при `accept`)
    краснит этот тест: кадр не дойдёт ни до одного соединения автора.
    Мутация M1 (`ws:chat:` вместо `ws:user:`) — тоже: автор не подписан ни на
    один чат, потому что чата на момент заявки ещё нет (ADR-06).
    """
    with _ws_open(server, seeded["author_token"]) as ws_author:
        # Ни одного `chat.subscribe`: это и есть предмет проверки.
        response = server.post(
            f"/api/v1/cards/{seeded['card_uuid']}/requests",
            json={"message": "хочу присоединиться"},
            headers=_headers(seeded["applicant_token"]),
        )
        assert response.status_code == 201, response.text

        frame = receive_envelope(ws_author, expected_type="request.received")

    assert frame["payload"]["card_uuid"] == seeded["card_uuid"]
    assert "applicant_id" not in frame["payload"]
    assert "author_id" not in frame["payload"]


def test_two_connections_of_same_user_both_receive(
    server: TestClient, seeded: dict[str, str]
) -> None:
    """Два соединения автора — оба получают событие.

    Комната пользователя, а не первое соединение: человек может держать
    продукт в двух вкладках, и «уведомление пришло в одну» — это дефект
    доставки, который не виден на одном сокете.
    """
    with (
        _ws_open(server, seeded["author_token"]) as first,
        _ws_open(server, seeded["author_token"]) as second,
    ):
        response = server.post(
            f"/api/v1/cards/{seeded['card_uuid']}/requests",
            json={"message": "ещё раз"},
            headers=_headers(seeded["applicant_token"]),
        )
        assert response.status_code == 201, response.text

        receive_envelope(first, expected_type="request.received")
        receive_envelope(second, expected_type="request.received")


def test_events_are_not_delivered_to_other_user(server: TestClient, seeded: dict[str, str]) -> None:
    """Адресность: посторонний не получает чужое уведомление.

    Краснеет, если `fan_out_user_local` начнёт рассылать всем соединениям
    процесса (или если канал превратится в широковещательный).
    """
    from tests.integration.ws_harness import expect_silence

    with _ws_open(server, seeded["applicant_token"]) as ws_applicant:
        response = server.post(
            f"/api/v1/cards/{seeded['card_uuid']}/requests",
            json={"message": "подаю"},
            headers=_headers(seeded["applicant_token"]),
        )
        assert response.status_code == 201, response.text
        expect_silence(ws_applicant, timeout=1.0)
