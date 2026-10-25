"""Границы ядра автовыбора (стори 5б.4, Task 4; долги CR 5б.3).

Два долга, найденных ревью 5б.3, закрываются здесь:

- **X-5b.3-7.** Граница `expires_at == now` не была покрыта: все тесты
  строили окно с запасом (5 минут назад / 3 дня вперёд), поэтому мутация
  `>=` <-> `>` проходила зелёной. На настоящей границе карточка закрывается
  или не закрывается в зависимости от микросекунды.
- **X-5b.3-9.** ADR-05 («раньше заявился — раньше выбран») защищался только
  текстом SQL. Поведенческий тест 5б.3 на РАВНЫХ `submitted_at` оставался
  зелёным при снятии `submitted_at` из `ORDER BY`, потому что равные метки
  разрешались тем же `id ASC`. Нужны РАЗНЫЕ метки.

§18.1: оба теста обязаны краснеть от соответствующей мутации.
§18.3: `now` передаётся параметром, реальное время в утверждениях не участвует.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.card_decision_service import DecisionOutcome, close_due_card

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие."""
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям."""
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_USERS and not _CREATED_PLACES:
        return
    async with factory() as session:
        if _CREATED_USERS:
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
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    return user


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_community_slug},
    )
    place_id = int(row.scalar_one())
    _CREATED_PLACES.append(place_id)
    return place_id


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    expires_at: datetime,
    capacity: int = 1,
) -> int:
    """Карточка с ЯВНЫМ `expires_at`: граница задаётся бит в бит."""
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', 0, 'approved', :published_at) RETURNING id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": expires_at,
            "published_at": starts - timedelta(days=1),
        },
    )
    return int(row.scalar_one())


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, submitted_at: datetime
) -> int:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, '', 'pending', :submitted, "
            "now() + interval '1 day') RETURNING id"
        ),
        {"card": card_id, "applicant": applicant_id, "submitted": submitted_at},
    )
    return int(row.scalar_one())


async def _selected_request_id(session: AsyncSession, card_id: int) -> int | None:
    row = await session.execute(
        text("SELECT id FROM join_requests WHERE card_id = :c AND status = 'selected'"),
        {"c": card_id},
    )
    return row.scalar_one_or_none()


# --- X-5b.3-7: граница expires_at == now -----------------------------------


async def test_expires_at_equal_now_is_closed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """X-5b.3-7 (AC-01, AC-02 стори 5б.3): `expires_at == now` — окно УЖЕ закрыто.

    Мутация M8: `expires_at > now` -> `>=` в ядре `close_due_card` и
    `expires_at <= now` -> `<` в `build_due_decision_ids_statement`.
    Обе делают карточку «открытой» на точной границе: окно, которое обязано
    быть закрыто (NFR20 «пропущенных окон 0»), остаётся `published`.

    `microsecond=0` обязателен: `now` в данных и в прогоне должен совпадать
    бит в бит, иначе тест проверяет «примерно границу» и мутацию пропускает.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        applicant = await _new_user(seed)
        place_id = await _place(seed)
        card_id = await _insert_card(
            seed, author_id=author.id, place_id=place_id, expires_at=moment
        )
        request_id = await _insert_request(
            seed,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=moment - timedelta(hours=1),
        )
        await seed.commit()

    async with factory() as session:
        result = await close_due_card(
            session, card_id=card_id, author_id=int(author.id), now=moment
        )
        await session.commit()

    assert result.outcome is DecisionOutcome.SELECTED
    async with factory() as check:
        assert await _selected_request_id(check, card_id) == request_id


async def test_expires_at_one_second_after_now_stays_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """X-5b.3-7 (контроль): `expires_at == now + 1с` — окно ещё ОТКРЫТО.

    Симметричная мутация к M8: если границу сделать нестрогой в другую
    сторону (`expires_at >= now`), карточка закрывалась бы за секунду до
    истечения. Тест держит обе стороны границы.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        applicant = await _new_user(seed)
        place_id = await _place(seed)
        card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(seconds=1),
        )
        await _insert_request(
            seed,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=moment - timedelta(hours=1),
        )
        await seed.commit()

    async with factory() as session:
        result = await close_due_card(
            session, card_id=card_id, author_id=int(author.id), now=moment
        )
        await session.commit()

    assert result.outcome is DecisionOutcome.SKIPPED
    async with factory() as check:
        assert await _selected_request_id(check, card_id) is None


# --- X-5b.3-9: ADR-05 на РАЗНЫХ submitted_at -------------------------------


async def test_earliest_submitted_at_wins_not_smallest_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """X-5b.3-9 (AC-01, ADR-05): выбирается САМАЯ РАННЯЯ заявка.

    Три заявки с РАЗНЫМИ `submitted_at`, причём `id` вставки идут в
    ПРОТИВОПОЛОЖНОМ порядке: первая вставленная подана ПОЗЖЕ всех
    (10 минут назад), а самая ранняя (120 минут назад) вставлена последней.
    Мутация «убрать `submitted_at` из `ORDER BY`» (`build_first_pending_statement`)
    тогда выберет первую по `id`, то есть самую ПОЗДНЮЮ, и тест покраснеет.
    На равных метках такой тест был бы бесполезен: `id ASC` дал бы тот же
    ответ — ровно об этом долг X-5b.3-9.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment - timedelta(minutes=1),
        )
        late = await _new_user(seed)
        middle = await _new_user(seed)
        early = await _new_user(seed)
        late_id = await _insert_request(
            seed, card_id=card_id, applicant_id=late.id, submitted_at=moment - timedelta(minutes=10)
        )
        middle_id = await _insert_request(
            seed,
            card_id=card_id,
            applicant_id=middle.id,
            submitted_at=moment - timedelta(minutes=60),
        )
        early_id = await _insert_request(
            seed,
            card_id=card_id,
            applicant_id=early.id,
            submitted_at=moment - timedelta(minutes=120),
        )
        await seed.commit()

    async with factory() as session:
        result = await close_due_card(
            session, card_id=card_id, author_id=int(author.id), now=moment
        )
        await session.commit()

    assert result.outcome is DecisionOutcome.SELECTED
    async with factory() as check:
        selected = await _selected_request_id(check, card_id)
    assert selected == early_id
    assert selected != late_id
    assert selected != middle_id
