"""Гонка за последнее место (стори 6.2; AC-03, AC-04, риски R1/R2).

TD 6.2: два параллельных присоединения к последнему месту обязаны дать
РОВНО ОДИН чат и один `409 CARD_IS_FULL`. Доказательство — СЧЁТЧИК строк,
а не коды ответов (урок CR 2.2): коды можно получить и при двух чатах.

§18.3: гонки — только на ДВУХ независимых соединениях с настоящими
коммитами. Фикстура `session` из `conftest.py` держит одну транзакцию, и
второе соединение не увидело бы незакоммиченных изменений первого.

§18.12: строки убираются фикстурой, иначе трёхкратный прогон набора оставит
данные и сломает глобальные `count(*)` в посторонних тестах (X-5a.6-3).
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.card_join_service import CardIsFullError, join_open_card

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Собственный движок: два соединения с настоящими коммитами."""
    created = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield created
    await created.dispose()


@pytest.fixture
async def factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Уборка после гонки: чаты → заявки → карточки → места → пользователи."""
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    async with factory() as session:
        if _CREATED_USERS:
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
                text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
            )
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
            )
        await session.commit()


async def _new_user(session: AsyncSession) -> User:
    phone = f"+7903{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    return user


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


async def _insert_card(
    session: AsyncSession, *, author_id: int, place_id: int, capacity: int, filled_count: int
) -> tuple[uuid_lib.UUID, int]:
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', :filled, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "expires": now + timedelta(minutes=60),
            "capacity": capacity,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return uuid_lib.UUID(str(card_uuid)), int(card_id)


async def _insert_participation(session: AsyncSession, *, card_id: int, applicant_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "decided_at, expires_at) VALUES (:card, :applicant, '', 'selected', now(), now(), "
            "now() + interval '1 day')"
        ),
        {"card": card_id, "applicant": applicant_id},
    )


class _JoinSettings:
    default_city_slug = settings.default_city_slug


async def _join(session: AsyncSession, *, actor_id: int, card_uuid: uuid_lib.UUID) -> str:
    """Присоединение; возвращает исход строкой — гонка допускает любой порядок."""
    try:
        await join_open_card(
            session, actor_id=actor_id, card_uuid=card_uuid, settings=_JoinSettings()
        )
    except CardIsFullError:
        return "full"
    except Exception as error:  # noqa: BLE001 — важен исход, не класс
        return type(error).__name__
    return "ok"


async def _chat_count(session: AsyncSession, *, card_id: int) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM chats WHERE event_card_id = :id"), {"id": card_id}
    )
    return int(result.scalar_one())


async def test_race_last_seat_one_chat(factory: async_sessionmaker[AsyncSession]) -> None:
    """R1, AC-03: два присоединения к последнему месту → РОВНО ОДИН чат.

    Краснеет, если `chat_uuid` писать вторым `UPDATE` или создавать чат вне
    CAS: оба запроса увидели бы свободное место и вставили по строке `chats`
    (второй — `IntegrityError` по `uq_chats_event_card_id`, то есть `500`
    вместо чистого `409 CARD_IS_FULL`).
    """
    async with factory() as session:
        author = await _new_user(session)
        first_member = await _new_user(session)
        rival_a = await _new_user(session)
        rival_b = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=2, filled_count=1
        )
        await _insert_participation(session, card_id=card_id, applicant_id=int(first_member.id))
        await session.commit()

    async def _run(actor_id: int) -> str:
        async with factory() as session:
            return await _join(session, actor_id=actor_id, card_uuid=card_uuid)

    outcomes = await asyncio.gather(_run(int(rival_a.id)), _run(int(rival_b.id)))

    async with factory() as session:
        chats = await _chat_count(session, card_id=card_id)
        state = (
            await session.execute(
                text("SELECT filled_count, capacity, chat_uuid FROM event_cards WHERE id = :id"),
                {"id": card_id},
            )
        ).one()

    # §12.2: доказательство — СЧЁТЧИК, а не коды ответов.
    assert chats == 1, (outcomes, chats)
    assert sorted(outcomes) == ["full", "ok"], outcomes
    assert int(state.filled_count) == int(state.capacity) == 2
    assert state.chat_uuid is not None


async def test_race_last_seat_filled_count_not_exceeded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2, AC-03: `filled_count == capacity`, а не `capacity + 1`.

    Краснеет при снятом предикате `filled_count < capacity`: место утекло бы
    за пределы вместимости (NFR47), и `CHECK ck_event_cards_full_capacity`
    отверг бы строку уже на записи.
    """
    async with factory() as session:
        author = await _new_user(session)
        first_member = await _new_user(session)
        rival_a = await _new_user(session)
        rival_b = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=3, filled_count=2
        )
        await _insert_participation(session, card_id=card_id, applicant_id=int(first_member.id))
        await session.commit()

    async def _run(actor_id: int) -> str:
        async with factory() as session:
            return await _join(session, actor_id=actor_id, card_uuid=card_uuid)

    await asyncio.gather(_run(int(rival_a.id)), _run(int(rival_b.id)))

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT filled_count, capacity FROM event_cards WHERE id = :id"),
                {"id": card_id},
            )
        ).one()
        chats = await _chat_count(session, card_id=card_id)

    assert int(state.filled_count) == int(state.capacity) == 3
    assert chats == 1
