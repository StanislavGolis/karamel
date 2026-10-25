"""Гонка лимита заявок и порядок блокировок (§18.3, стори 5а.2; AC-08, AC-09).

§18.3: гонку в одной сессии доказать нельзя — второй запрос видит
незакоммиченные изменения первого. Поэтому фикстура `session` из `conftest.py`
(одна транзакция, §18.8) здесь НЕ используется: данные готовятся собственным
движком и коммитятся по-настоящему, а подачи идут через реальный сервис,
каждая — в своей сессии со своим соединением.

**Доказательство — `count(*)` строк заявителя, а не коды ответов** (AC-08,
урок CR 2.2): коды не различают ветки, а число строк показывает, сколько
подач реально прошло.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repositories.users import UserRepository
from app.services.join_request_service import submit_join_request

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"


class _Settings:
    """Минимум настроек сервиса: протокол `JoinRequestSettings` без чтения `.env`."""

    default_community_slug = COMMUNITY
    join_request_active_limit = 2


# Коммиты здесь НАСТОЯЩИЕ (§18.3): строки видны остальным тестам и обязаны
# исчезнуть до их старта, иначе прогон пачки даёт лишние карточки в ленте и
# «Multiple results found» в тестах схемы.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные.

    Пул ограничен четырьмя соединениями: нужны ДВЕ параллельные транзакции и
    одна для проверки состояния после гонки.
    """
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям.

    `join_requests` уходит каскадом от `event_cards`; `event_cards` → `places`
    (там `ON DELETE RESTRICT`) → `users`.
    """
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_USERS and not _CREATED_PLACES:
        return
    async with factory() as session:
        if _CREATED_USERS:
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


async def _new_user(factory: async_sessionmaker[AsyncSession], *, register: bool = True) -> int:
    """Пользователь с настоящим коммитом (нужен для FK и для блокировки)."""
    async with factory() as session:
        phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(session).create(
            provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
        )
        await session.commit()
        if register:
            _CREATED_USERS.append(int(user.id))
        return int(user.id)


async def _seed_card(
    factory: async_sessionmaker[AsyncSession],
    *,
    author_id: int,
    capacity: int = 1,
) -> uuid_lib.UUID:
    """Место и опубликованная карточка режима «заявки» — с настоящим коммитом."""
    async with factory() as session:
        place_row = await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug, is_active) "
                "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": COMMUNITY},
        )
        place_id = int(place_row.scalar_one())
        starts = datetime.now(UTC) + timedelta(days=3)
        card_row = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
                "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
                "filled_count, verification_status, published_at) "
                "VALUES (:author, :place, :slug, 'Гонка', NULL, 'cafe', :starts, :capacity, "
                "60, :expires, 'published', 0, 'approved', :published_at) RETURNING uuid"
            ),
            {
                "author": author_id,
                "place": place_id,
                "slug": COMMUNITY,
                "starts": starts,
                "capacity": capacity,
                "expires": starts - timedelta(hours=1),
                "published_at": starts - timedelta(days=1),
            },
        )
        card_uuid = card_row.scalar_one()
        await session.commit()
        _CREATED_PLACES.append(place_id)
        return uuid_lib.UUID(str(card_uuid))


async def _pending_count(factory: async_sessionmaker[AsyncSession], applicant_id: int) -> int:
    async with factory() as session:
        row = await session.execute(
            text(
                "SELECT count(*) FROM join_requests WHERE applicant_id = :id AND status = 'pending'"
            ),
            {"id": applicant_id},
        )
        return int(row.scalar_one())


async def test_limit_race_on_two_different_cards(factory: async_sessionmaker[AsyncSession]) -> None:
    """AC-08 (NFR29, §12.2): 1 `pending` + две РАЗНЫЕ карточки → проходит одна подача.

    **Карточки РАЗНЫЕ намеренно (D-5a.2-4):** на одной карточке вторую вставку
    отбил бы `uq_join_requests_card_applicant_active`, и снятая `FOR UPDATE`
    осталась бы непроверенной — тест зеленел бы при полностью отсутствующей
    блокировке. На двух карточках различитель только блокировка актора.

    Доказательство — `count(*)` заявителя: ожидается 2 (одна засеянная + одна
    прошедшая). Без `FOR UPDATE` обе прочли бы `active_count = 1`, обе прошли бы
    проверку `< 2`, и строк стало бы 3. Краснеет ровно от снятия
    `.with_for_update()` в `UserRepository.build_lock_statement`.
    """
    author_id = await _new_user(factory)
    applicant_id = await _new_user(factory)
    card_a = await _seed_card(factory, author_id=author_id)
    card_b = await _seed_card(factory, author_id=author_id)
    seeded = await _seed_card(factory, author_id=author_id)

    # Одна активная заявка уже есть: свободен ровно один слот из двух.
    async with factory() as session:
        await submit_join_request(
            session,
            applicant_id=applicant_id,
            card_uuid=seeded,
            message="первая",
            settings=_Settings(),
        )

    async def attempt(card_uuid: uuid_lib.UUID) -> str:
        async with factory() as session:
            try:
                await submit_join_request(
                    session,
                    applicant_id=applicant_id,
                    card_uuid=card_uuid,
                    message="гонка",
                    settings=_Settings(),
                )
            except Exception as error:  # noqa: BLE001 — коды не различитель, важен факт
                return type(error).__name__
            return "ok"

    results = await asyncio.gather(attempt(card_a), attempt(card_b))

    total = await _pending_count(factory, applicant_id)
    assert total == 2, f"активных заявок: {total} (ожидалось 2); результаты: {results}"
    assert results.count("ok") == 1, f"успешных подач: {results.count('ok')} (ожидалось 1)"


async def test_lock_order_users_first_and_no_event_cards_lock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-09 (ADR-09): первый `FOR UPDATE` — `users`, на `event_cards` блокировок нет.

    Перехват `before_cursor_execute` на реальном соединении: текст SQL виден
    в том виде, в каком его получил драйвер (текст билдера это не доказывает —
    `FOR UPDATE` мог бы быть снят на уровне сессии).

    Краснеет двумя правками: (1) убрать блокировку актора — список блокировок
    станет пустым; (2) добавить `.with_for_update()` в запрос карточки —
    `event_cards` попадёт в список, и порядок `users → cards` будет нарушен
    (дедлок с 4.4, где `cards` блокируется после `users`).
    """
    author_id = await _new_user(factory)
    applicant_id = await _new_user(factory)
    card = await _seed_card(factory, author_id=author_id)

    locks: list[str] = []
    engine = create_async_engine(DATABASE_URL)

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FOR UPDATE" in statement.upper():
            locks.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        maker = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with maker() as session:
            await submit_join_request(
                session,
                applicant_id=applicant_id,
                card_uuid=card,
                message="порядок",
                settings=_Settings(),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
        await engine.dispose()

    assert locks, "блокировка актора не выполнялась"
    assert "users" in locks[0], f"первая блокировка не на users: {locks[0]}"
    assert all("event_cards" not in sql for sql in locks), f"event_cards блокируется: {locks}"
