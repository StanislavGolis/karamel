"""X-36: скрытое место в выдаче карточки (стори 5а.4, Task 10.6; AC-08).

Решение пользователя от 2026-10-24 (пункт 3 ER эпика 4): карточка скрытого
места НЕ попадает в ленту (витрина не показывает встречу, на которую нельзя
прийти), но остаётся доступной по прямой ссылке с `place: null` — заявки и
участники живы, и действие модератора не отнимает у автора данные
(FR68 «скрыть ≠ удалить», R-04 — физического удаления нет).

Каждый тест проверяет ОБЕ стороны: скрытие убирает карточку из витрины
И НЕ ломает прямой доступ. Односторонняя проверка прошла бы на половинном
решении.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7911{uuid_lib.uuid4().int % 10**7:07d}"
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
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession, *, is_active: bool) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, :active) RETURNING id"
        ),
        {
            "name": f"Кафе {uuid_lib.uuid4().hex[:8]}",
            "slug": settings.default_city_slug,
            "active": is_active,
        },
    )
    return int(row.scalar_one())


async def _insert_card(session: AsyncSession, *, author_id: int, place_id: int) -> str:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at,"
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


async def _feed(client: AsyncClient, token: str) -> Any:
    return await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})


async def test_hidden_place_card_is_absent_from_feed_and_count(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (X-36): карточка скрытого места НЕ в ленте и НЕ в `total`.

    Краснеет, если убрать `Place.is_active.is_(True)` из
    `build_feed_statement` или из `build_feed_count_statement` (мутация M3):
    участник увидел бы афишу встречи с местом, которое модератор скрыл.
    Две проверки сразу — потому что фильтр есть в ДВУХ запросах, и
    расхождение дало бы «5 всего, 0 на странице».
    """
    author, token = await _new_user(session)
    visible_place = await _place(session, is_active=True)
    hidden_place = await _place(session, is_active=False)
    visible_uuid = await _insert_card(session, author_id=author.id, place_id=visible_place)
    hidden_uuid = await _insert_card(session, author_id=author.id, place_id=hidden_place)
    await session.flush()

    response = await _feed(client, token)

    assert response.status_code == 200
    body = response.json()
    uuids = [item["uuid"] for item in body["items"]]
    assert visible_uuid in uuids
    assert hidden_uuid not in uuids
    # `total` считается тем же предикатом: скрытая карточка не увеличивает его.
    assert body["total"] == len(uuids)


async def test_hidden_place_card_is_still_reachable_with_null_place(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (X-36, FR68): прямая ссылка работает, `place` равен `null`.

    Краснеет, если скрывать карточку целиком (`404`): автор и уже заявившиеся
    потеряли бы доступ к своим данным из-за действия модератора. Краснеет
    и в обратную сторону — если отдавать место как обычно: скрытие перестало
    бы что-либо значить.
    """
    author, token = await _new_user(session)
    hidden_place = await _place(session, is_active=False)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=hidden_place)
    await session.flush()

    response = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    body = response.json()
    assert body["uuid"] == card_uuid
    # `null`, а не объект с пустыми строками или именем скрытого места.
    assert body["place"] is None


async def test_active_place_still_returns_place_object(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08: активное место отдаётся как прежде — X-36 не «сломал» норму.

    Краснеет, если сделать `place` всегда `null` (например, потеряв метку
    `place_is_active` при распаковке строки): лента осталась бы без адресов,
    и участник не узнал бы, куда идти.
    """
    author, token = await _new_user(session)
    active_place = await _place(session, is_active=True)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=active_place)
    await session.flush()

    response = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    place = response.json()["place"]
    assert place is not None
    assert place["address_text"] == "ул. Мира, 1"
