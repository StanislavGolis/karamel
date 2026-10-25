"""Гонки переходов состояния карточки на двух соединениях (§18.3, стори 4.6).

AC-10, ADR-03. §18.3: гонку в одной сессии доказать нельзя — второй запрос
видит незакоммиченные изменения первого. Поэтому здесь НЕ используется фикстура
`session` из `conftest.py` (она живёт в одной транзакции, §18.8): данные
готовятся собственным движком и коммитятся по-настоящему, а переходы идут
через реальный сервис, каждый — в своей сессии со своим соединением.

**Доказательство — счётчик успешных `UPDATE`, а не коды ответов** (AC-10,
урок CR 2.2). Коды не различают ветки: без CAS второй запрос тоже упал бы —
но упал бы уже ПОСЛЕ записи, перезаписав результат первого. Различитель —
сколько раз CAS вернул `rowcount == 1`: с CAS ровно один, без него — два.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.enums import EventCardStatus
from app.repositories.users import UserRepository
from app.services.event_card_service import (
    CardNotCancellableError,
    CardNotPublishedError,
    cancel_card,
    close_card,
    finish_started_cards,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"


class _Settings:
    """Минимум настроек сервиса: протокол `CardSettings` без чтения `.env`."""

    default_community_slug = COMMUNITY
    community_slugs = (COMMUNITY,)
    card_default_decision_window_minutes = 2880
    card_content_stop_words = ("казино", "займ")
    card_active_limit = 2


# Ключи созданного за тест: коммиты здесь НАСТОЯЩИЕ (§18.3), поэтому строки
# видны всем остальным тестам и обязаны исчезнуть до их старта. Иначе прогон
# пачки даёт «Multiple results found» в тестах схемы и лишние карточки в ленте
# — то есть падения, не связанные с проверяемым поведением.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture(autouse=True)
async def _cleanup_seed(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям.

    `event_cards` → `places` → `users`: у места `ON DELETE RESTRICT`, поэтому
    место нельзя удалить, пока на него ссылается карточка; учётку уносит
    `CASCADE` карточек, но удалять её явно дешевле, чем надеяться на каскад
    в чужой таблице (`profile`/`refresh_tokens` чистятся каскадом).
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


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные.

    Пул ограничен четырьмя соединениями: тесту нужны ДВЕ параллельные
    транзакции и одна для проверки состояния после гонки.
    """
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_card(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: str = "published",
    starts_at: datetime | None = None,
) -> tuple[int, uuid_lib.UUID]:
    """Пользователь, место и карточка в нужном статусе — с настоящим коммитом."""
    async with factory() as session:
        phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(session).create(
            provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
        )
        place_row = await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug, is_active) "
                "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": COMMUNITY},
        )
        place_id = int(place_row.scalar_one())
        starts = starts_at or (datetime.now(UTC) + timedelta(days=3))
        card_row = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
                "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
                "filled_count, verification_status, published_at) "
                "VALUES (:author, :place, :slug, 'Гонка', NULL, 'cafe', :starts, 3, 60, "
                ":expires, :status, 0, 'approved', :published_at) RETURNING uuid"
            ),
            {
                "author": user.id,
                "place": place_id,
                "slug": COMMUNITY,
                "starts": starts,
                "expires": starts - timedelta(hours=1),
                "status": status,
                "published_at": starts - timedelta(days=1),
            },
        )
        card_uuid = card_row.scalar_one()
        await session.commit()
        _CREATED_USERS.append(int(user.id))
        _CREATED_PLACES.append(place_id)
        return user.id, card_uuid


async def _status(factory: async_sessionmaker[AsyncSession], card_uuid: uuid_lib.UUID) -> str:
    async with factory() as session:
        row = await session.execute(
            text("SELECT status FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
        )
        return str(row.scalar_one())


async def test_concurrent_close_updates_exactly_one_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-10 (ADR-03): два одновременных `close` — ровно один переход.

    Краснеет, если из предиката CAS убрать `status='published'`: оба запроса
    прошли бы, и `closed_at` перезаписался бы дважды (потерянное обновление).
    Различитель — число успешных вызовов, а не коды ответов.
    """
    author_id, card_uuid = await _seed_card(factory)

    async def attempt() -> int:
        async with factory() as session:
            try:
                await close_card(session, author_id=author_id, card_uuid=card_uuid)
            except CardNotPublishedError:
                return 0
            return 1

    results = await asyncio.gather(attempt(), attempt())

    assert sum(results) == 1, f"успешных закрытий: {sum(results)} (ожидалось 1)"
    assert await _status(factory, card_uuid) == EventCardStatus.CLOSED


async def test_concurrent_cancel_updates_exactly_one_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-10: два одновременных `cancel` — ровно один переход.

    Краснеет, если убрать из предиката `status NOT IN ('finished','cancelled')`:
    второй запрос перезаписал бы уже отменённую карточку и ответил успехом.
    """
    author_id, card_uuid = await _seed_card(factory)

    async def attempt() -> int:
        async with factory() as session:
            try:
                await cancel_card(session, author_id=author_id, card_uuid=card_uuid)
            except CardNotCancellableError:
                return 0
            return 1

    results = await asyncio.gather(attempt(), attempt())

    assert sum(results) == 1, f"успешных отмен: {sum(results)} (ожидалось 1)"
    assert await _status(factory, card_uuid) == EventCardStatus.CANCELLED


async def test_cancel_versus_archive_has_single_winner(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-10: `cancel` и архив по времени — побеждает ровно один, без «потери».

    Сценарий реальный: встреча началась, автор нажимает «Отменить» в ту же
    секунду, когда воркер переводит карточку в `finished`. Оба предиката
    смотрят на `status`, поэтому второй получает 0 строк.

    Краснеет, если архив не проверяет `status IN ARCHIVABLE_STATUSES`: он
    перезаписал бы уже отменённую карточку в `finished`, и участники увидели бы
    «встреча прошла» вместо «встреча отменена автором».
    """
    author_id, card_uuid = await _seed_card(
        factory, starts_at=datetime.now(UTC) - timedelta(minutes=5)
    )

    async def cancel_attempt() -> str:
        async with factory() as session:
            try:
                await cancel_card(session, author_id=author_id, card_uuid=card_uuid)
            except CardNotCancellableError:
                return "cancel_lost"
            return "cancel_won"

    async def archive_attempt() -> str:
        async with factory() as session:
            finished = await finish_started_cards(session, now=datetime.now(UTC))
            return "archive_won" if finished else "archive_lost"

    results = await asyncio.gather(cancel_attempt(), archive_attempt())

    # Ровно один победитель: либо отмена, либо архив — не оба.
    assert sum(1 for item in results if item.endswith("won")) == 1, results
    final = await _status(factory, card_uuid)
    assert final in (EventCardStatus.CANCELLED, EventCardStatus.FINISHED)


async def test_worker_archive_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05 (ADR-03): повторный прогон воркера не переводит карточку дважды.

    Краснеет, если архив не CAS: второй прогон снова вернул бы 1, и метрика
    `worker.card.finished` показывала бы работу там, где её нет.
    """
    _, card_uuid = await _seed_card(factory, starts_at=datetime.now(UTC) - timedelta(minutes=5))
    moment = datetime.now(UTC)

    async with factory() as session:
        first = await finish_started_cards(session, now=moment)
    async with factory() as session:
        second = await finish_started_cards(session, now=moment)

    assert first == 1
    assert second == 0
    assert await _status(factory, card_uuid) == EventCardStatus.FINISHED
