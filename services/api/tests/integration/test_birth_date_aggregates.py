"""Возраст из `birth_date` в агрегатах: состав компании и «мои заявки».

**Что нашёл CR (H-3, класс 11.8 H-2).** Мутации M22/M23 выжили:

    M22 chat_participants_service  age=compute_age(...) -> age=age      SURVIVED
    M23 join_request_service      author_age=compute_age(...) -> ...   SURVIVED

Замер причины: хелперы обоих тестовых файлов вставляют анкеты С ЧИСЛОМ
(`age=28`), без `birth_date`. Замена `compute_age(birth_date, age, ...)` на
прямое `age` на таких данных невидима: оба источника совпадают по смыслу,
и разница проявляется только у учётки «нового типа» (`birth_date` есть,
`age = NULL`) — то есть у КАЖДОГО, кто зарегистрировался после 12.2.

Проверяются два пути, ранее не покрытые:

1. `GET /cards/{uuid}/participants` — возраст участника;
2. `GET /api/v1/my/requests` — возраст автора карточки.

Краснеет при:
- возврате `age=age` (M22) — у участника будет `null` вместо 25;
- возврате `author_age=row.author_age` (M23) — у автора `null` вместо 25.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

ACCESS_COOKIE_NAME = "karamel_access"
CITY = "tomsk"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


def _birth_date_for_age(years: int) -> date:
    today = date.today()
    return date(today.year - years, today.month, today.day)


async def _user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7902{uuid_lib.uuid4().int % 10**7:07d}"
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
    user.gender = "female"
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _profile_with_birth_date(session: AsyncSession, user: User, years: int) -> None:
    """Анкета «нового типа»: дата рождения есть, `age` — `NULL` (AC-2)."""
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
            "VALUES (:user, 'Аня', 'Томск', :birth_date, :gender, 'о себе', 'complete')"
        ),
        {"user": user.id, "birth_date": _birth_date_for_age(years), "gender": user.gender},
    )
    await session.flush()


async def _card(
    session: AsyncSession, author_id: int, *, status: str = "published"
) -> tuple[str, int]:
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": CITY},
    )
    place_id = int(place.scalar_one())
    starts = datetime.now(UTC) + timedelta(days=2)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 2, 60, "
            ":expires, :status, 1, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": CITY,
            "starts": starts,
            "expires": starts - timedelta(hours=1),
            "status": status,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    await session.flush()
    return str(card_uuid), int(card_id)


async def _selected_request(session: AsyncSession, card_id: int, applicant_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:card, :applicant, '', 'selected', :submitted, "
            "now() + interval '1 day', now())"
        ),
        {"card": card_id, "applicant": applicant_id, "submitted": datetime.now(UTC)},
    )
    await session.flush()


async def test_participant_age_comes_from_birth_date(
    client: AsyncClient, session: AsyncSession
) -> None:
    """H-3: в составе компании возраст вычисляется из `birth_date`.

    Зритель — участник компании (иначе `404`, ADR-13). Участник с датой
    рождения и `age = NULL`: прямой `age=age` вернул бы `null`.
    """
    author, _author_token = await _user(session)
    card_uuid, card_id = await _card(session, author.id)
    member, member_token = await _user(session)
    await _profile_with_birth_date(session, member, 25)
    await _selected_request(session, card_id, member.id)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/participants",
        cookies={ACCESS_COOKIE_NAME: member_token},
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    ages = {item["uuid"]: item["age"] for item in items}
    assert ages[str(member.uuid)] == 25, f"ожидалось 25, получено {ages}"


async def test_my_requests_author_age_comes_from_birth_date(
    client: AsyncClient, session: AsyncSession
) -> None:
    """H-3: в «Моих заявках» возраст автора вычисляется из `birth_date`.

    Автор карточки — «нового типа» (`birth_date` есть, `age = NULL`).
    Замена `compute_age(...)` на `row.author_age` вернула бы `null`.
    """
    author, _ = await _user(session)
    await _profile_with_birth_date(session, author, 25)
    _card_uuid, card_id = await _card(session, author.id)
    applicant, applicant_token = await _user(session)
    await _selected_request(session, card_id, applicant.id)

    response = await client.get(
        "/api/v1/my/requests", cookies={ACCESS_COOKIE_NAME: applicant_token}
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "список заявок пуст — тест ничего не проверяет"
    assert items[0]["author"]["age"] == 25, f"ожидалось 25, получено {items[0]['author']}"
