"""Гард выхода из режима заявок через API (стори 6.1; AC-08, OQ-5b.3-3).

CR 6.1: unit-тест (`test_card_capacity_pending_guard.py`) подменяет
`JoinRequestRepository` целиком, поэтому ни `has_pending_for_card`, ни
маппинг `CardAlreadyJoinedError` → `409 CARD_ALREADY_JOINED` не проверялись
против БД и роутера. Этот файл закрывает обе дыры: реальная заявка в
PostgreSQL и настоящий `PATCH /api/v1/cards/{uuid}`.

§18.11: проверяются ОБА значения признака — «есть `pending`» (отказ) и
«нет `pending`» (успех).
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
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой зависимостью сессии."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    """Автор с согласием на ПД и живой сессией."""
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _request_mode_card(session: AsyncSession, *, author_id: int) -> str:
    """Опубликованная карточка режима заявок (`capacity=1`) — сырым SQL."""
    now = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    place_id = int(row.scalar_one())
    card = await session.execute(
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
            "starts": now + timedelta(days=3),
            "expires": now + timedelta(hours=2),
            "published_at": now,
        },
    )
    return str(card.scalar_one())


async def _applicant(session: AsyncSession) -> int:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    return int(user.id)


async def _add_request(session: AsyncSession, *, card_uuid: str, applicant_id: int) -> None:
    """Живая `pending`-заявка: ровно то состояние, которое блокирует `PATCH`.

    Вставляется сырым SQL: смысл теста — гард сервиса, а не правила подачи
    (они проверены в 5а.2).
    """
    await session.execute(
        text(
            "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
            "submitted_at, expires_at) "
            "SELECT gen_random_uuid(), id, :applicant, 'хочу', 'pending', now(), "
            "expires_at FROM event_cards WHERE uuid = :card"
        ),
        {"applicant": applicant_id, "card": card_uuid},
    )


async def _card_state(session: AsyncSession, card_uuid: str) -> dict[str, Any]:
    result = await session.execute(
        text("SELECT capacity, filled_count, status FROM event_cards WHERE uuid = :uuid"),
        {"uuid": card_uuid},
    )
    return dict(result.mappings().one())


async def test_patch_capacity_one_to_two_with_pending_returns_409(
    session: AsyncSession, client: AsyncClient
) -> None:
    """AC-08: `1 → 2` при живой `pending` → `409 CARD_ALREADY_JOINED`, строка не меняется.

    Краснеет, если: гард не смотрит на `pending` (уходило бы `200`), роутер
    не маппит `CardAlreadyJoinedError` (`500`/`422` вместо `409`), или
    изменение применяется до проверки (`capacity` стал бы 2 при отказе).
    """
    author, token = await _new_user(session)
    card_uuid = await _request_mode_card(session, author_id=int(author.id))
    await _add_request(session, card_uuid=card_uuid, applicant_id=await _applicant(session))
    await session.flush()

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"capacity": 2},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CARD_ALREADY_JOINED"
    state = await _card_state(session, card_uuid)
    assert state["capacity"] == 1
    assert state["filled_count"] == 0


async def test_patch_capacity_one_to_two_without_pending_succeeds(
    session: AsyncSession, client: AsyncClient
) -> None:
    """AC-08, §18.11: `1 → 2` без `pending` → `200`, вместимость изменена.

    Краснеет, если гард запрещает выход из режима заявок всегда: карточка
    без заявителей — законный случай смены режима (FR70).
    """
    author, token = await _new_user(session)
    card_uuid = await _request_mode_card(session, author_id=int(author.id))

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"capacity": 2},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200, response.text
    state = await _card_state(session, card_uuid)
    assert state["capacity"] == 2


async def test_patch_title_with_pending_still_succeeds(
    session: AsyncSession, client: AsyncClient
) -> None:
    """AC-08: правка БЕЗ смены режима не блокируется живой заявкой.

    Краснеет, если гард расширить до «есть `pending` → любой `PATCH` нельзя»:
    автор не смог бы поправить опечатку, пока заявитель ждёт решения.
    """
    author, token = await _new_user(session)
    card_uuid = await _request_mode_card(session, author_id=int(author.id))
    await _add_request(session, card_uuid=card_uuid, applicant_id=await _applicant(session))
    await session.flush()

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        # AC-7 стори 13.2: `title` из контракта удалён — правим описание.
        json={"description": "Новое описание"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200, response.text
    state = await _card_state(session, card_uuid)
    assert state["capacity"] == 1


async def test_has_pending_for_card_reads_database_not_a_mock(
    session: AsyncSession,
) -> None:
    """AC-08: `has_pending_for_card` — ИСТИННЫЙ SQL, оба значения (§18.11).

    CR 6.1: unit-тест подменяет репозиторий целиком, поэтому ошибка в
    предикате (`status == PENDING` без `.value`, забытый `card_id`,
    `scalar_one()` вместо `first()`) не ловилась ни разу.
    Краснеет при: снятом фильтре по `card_id` (True на чужой карточке),
    сравнении со статусом отличным от `pending`, и при подсчёте
    терминальных заявок как активных (ADR-09, NFR29).
    """
    author, _ = await _new_user(session)
    card_uuid = await _request_mode_card(session, author_id=int(author.id))
    other_uuid = await _request_mode_card(session, author_id=int(author.id))
    repo = JoinRequestRepository(session)
    card_id = int(
        (
            await session.execute(
                text("SELECT id FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
            )
        ).scalar_one()
    )
    other_id = int(
        (
            await session.execute(
                text("SELECT id FROM event_cards WHERE uuid = :uuid"), {"uuid": other_uuid}
            )
        ).scalar_one()
    )

    # Нет заявок — False; заявка на ДРУГОЙ карточке не считается.
    assert await repo.has_pending_for_card(card_id=card_id) is False
    await _add_request(session, card_uuid=other_uuid, applicant_id=await _applicant(session))
    await session.flush()
    assert await repo.has_pending_for_card(card_id=card_id) is False

    # Живая заявка на нужной карточке — True.
    await _add_request(session, card_uuid=card_uuid, applicant_id=await _applicant(session))
    await session.flush()
    assert await repo.has_pending_for_card(card_id=card_id) is True
    assert other_id != card_id

    # Терминальный статус освобождает «занятость»: гард смотрит ТОЛЬКО `pending`.
    await session.execute(
        text("UPDATE join_requests SET status = 'cancelled' WHERE card_id = :card"),
        {"card": card_id},
    )
    await session.flush()
    assert await repo.has_pending_for_card(card_id=card_id) is False
