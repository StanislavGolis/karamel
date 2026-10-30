"""Гонка `PATCH capacity` с присоединением (§18.3, X-6.1-1, chore P-7).

Долг CR 6.1: `update_card` читала карточку БЕЗ `FOR UPDATE`, и гард AC-08
(`filled_count > 0` и живой `pending` запрещают вход/выход из режима заявок)
проверялся по снимку. Окно гонки — между чтением снимка и `commit`.

**Где именно окно.** `update_card` блокирует автора (`lock_author`), а
`join_open_card` — ЗАЯВИТЕЛЯ (`lock_for_update(actor_id)`). Это разные строки
`users`, поэтому транзакции не сериализуются, и без блокировки карточки
возможен исход: `PATCH capacity 3 → 1` коммитится после того, как
присоединение уже заняло место. Тогда в БД оказывается карточка режима
заявок с участником — состояние, которое AC-08 запрещает, а
`ck_event_cards_full_capacity` превращает в `500` на легальном действии
(класс дефекта X-39).

§18.3: гонку в одной сессии доказать нельзя. Поэтому фикстура `session` из
`conftest.py` здесь НЕ используется: данные готовятся собственным движком и
коммитятся по-настоящему, а действия идут через реальные сервисы, каждое —
в своей сессии со своим соединением.

**Доказательство — итоговое состояние строки карточки**, а не коды ответов:
`409` не различает ветки, а пара `(capacity, filled_count, status)`
показывает, осталась ли карточка консистентной.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repositories.event_cards import EventCardRepository
from app.repositories.users import UserRepository
from app.schemas.event_card import EventCardUpdate
from app.services.card_join_service import join_open_card
from app.services.event_card_service import update_card

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"


class _Settings:
    """Минимум настроек обоих сервисов: протоколы без чтения `.env`."""

    default_city_slug = COMMUNITY
    city_slugs = (COMMUNITY,)
    card_default_decision_window_minutes = 2880
    card_content_stop_words = ("казино", "займ")
    card_active_limit = 2


# Коммиты здесь НАСТОЯЩИЕ (§18.3): строки видны остальным тестам и обязаны
# исчезнуть до их старта, иначе лента и счётчики получат лишние карточки.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные."""
    engine = create_async_engine(DATABASE_URL, pool_size=6, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям."""
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


async def _new_user(factory: async_sessionmaker[AsyncSession]) -> int:
    """Пользователь с настоящим коммитом (нужен для FK и для блокировки)."""
    async with factory() as session:
        phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(session).create(
            provider="phone",
            provider_user_id=phone,
            phone=phone,
            role="user",
            gender="female",
            status="active",
        )
        await session.commit()
        _CREATED_USERS.append(int(user.id))
        return int(user.id)


async def _seed_card(
    factory: async_sessionmaker[AsyncSession], *, author_id: int, capacity: int
) -> tuple[int, uuid_lib.UUID]:
    """Место и опубликованная карточка с настоящим коммитом."""
    async with factory() as session:
        place_row = await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, city_slug, is_active) "
                "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": COMMUNITY},
        )
        place_id = int(place_row.scalar_one())
        starts = datetime.now(UTC) + timedelta(days=3)
        card_row = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
                "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
                "filled_count, verification_status, published_at, kind, activity) "
                "VALUES (:author, :place, :slug, 'Гонка вместимости', NULL, 'cafe', :starts, "
                ":capacity, 60, :expires, 'published', 0, 'approved', :published_at,"
                "'company', 'coffee') "
                "RETURNING id, uuid"
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
        card_id, card_uuid = card_row.one()
        await session.commit()
        _CREATED_PLACES.append(place_id)
        return int(card_id), uuid_lib.UUID(str(card_uuid))


async def _state(factory: async_sessionmaker[AsyncSession], card_id: int) -> tuple[int, int, str]:
    async with factory() as session:
        row = await session.execute(
            text("SELECT capacity, filled_count, status FROM event_cards WHERE id = :id"),
            {"id": card_id},
        )
        capacity, filled, status = row.one()
        return int(capacity), int(filled), str(status)


async def _patch_to_request_mode(
    factory: async_sessionmaker[AsyncSession], *, author_id: int, card_uuid: uuid_lib.UUID
) -> str:
    """Одна попытка `PATCH capacity 3 → 1` в собственной сессии."""
    async with factory() as session:
        try:
            await update_card(
                session,
                author_id=author_id,
                card_uuid=card_uuid,
                payload=EventCardUpdate(capacity=1, decision_window_minutes=60),
                settings=_Settings(),
            )
        except Exception as error:  # noqa: BLE001 — важен исход, не класс
            return type(error).__name__
        return "ok"


async def _join(
    factory: async_sessionmaker[AsyncSession], *, actor_id: int, card_uuid: uuid_lib.UUID
) -> str:
    """Одна попытка мгновенного присоединения в собственной сессии."""
    async with factory() as session:
        try:
            await join_open_card(
                session, actor_id=actor_id, card_uuid=card_uuid, settings=_Settings()
            )
        except Exception as error:  # noqa: BLE001 — важен исход, не класс
            return type(error).__name__
        return "ok"


async def test_join_after_patch_to_request_mode_takes_no_seat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """X-6.1-1 (AC-08, FR70, P-3): место в режиме заявок не занимается.

    Воспроизводится РОВНО окно TOCTOU, детерминированно: `join_open_card`
    уже прочитал карточку (она была открытым набором), и в этом окне
    `PATCH capacity 3 → 1` коммитится ДРУГОЙ сессией. Дальше идёт настоящий
    `join_seat`, и вопрос один: увидит ли его `UPDATE` новую вместимость.

    Параллельный `asyncio.gather` для доказательства не годится: исход
    зависит от планировщика, и при порядке «join первым» некорректная
    реализация даёт консистентное состояние по случайности. Перехват
    `join_seat` задаёт порядок жёстко, а мутация остаётся наблюдаемой.

    Краснеет от снятия предиката `EventCard.capacity > CARD_REQUEST_MODE_CAPACITY`
    из `build_join_seat_statement`: `UPDATE` пройдёт по карточке режима
    заявок, `filled_count` станет 1, `status` — `full`, то есть появится
    «компания из одного заявкой» (FR70 запрещает).
    """
    author_id = await _new_user(factory)
    actor_id = await _new_user(factory)
    card_id, card_uuid = await _seed_card(factory, author_id=author_id, capacity=3)

    original = EventCardRepository.join_seat
    patched: list[str] = []

    async def patch_then_join_seat(
        self: EventCardRepository, *, card_id: int, chat_uuid: uuid_lib.UUID
    ) -> Any:
        """Имитирует `PATCH` автора в окне между чтением карточки и `UPDATE` мест.

        Сигнатура повторяет `join_seat` дословно (урок 6.2: иначе `TypeError`
        вместо проверяемой гонки).
        """
        patched.append(
            await _patch_to_request_mode(factory, author_id=author_id, card_uuid=card_uuid)
        )
        return await original(self, card_id=card_id, chat_uuid=chat_uuid)

    EventCardRepository.join_seat = patch_then_join_seat  # type: ignore[method-assign]
    try:
        async with factory() as session:
            try:
                await join_open_card(
                    session,
                    actor_id=actor_id,
                    card_uuid=card_uuid,
                    settings=_Settings(),
                )
            except Exception as error:  # noqa: BLE001 — важен исход, не класс
                outcome = type(error).__name__
            else:
                outcome = "ok"
    finally:
        EventCardRepository.join_seat = original  # type: ignore[method-assign]

    # `PATCH` обязан пройти: участников не было, режим менялся легально.
    assert patched == ["ok"], patched
    assert outcome != "ok", outcome
    capacity, filled, status = await _state(factory, card_id)
    assert (capacity, filled) == (1, 0), (outcome, capacity, filled, status)
    assert status == "published", (outcome, capacity, filled, status)


async def test_patch_locks_author_then_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """X-6.1-1 (ADR-09, §18.11): порядок блокировок `users → event_cards`.

    Порядок — часть контракта и проверяется по фактическим операторам, а не
    по порядку вызовов в коде: перехват `before_cursor_execute` фиксирует
    последовательность `SQL` внутри транзакции `PATCH`.

    Краснеет от переноса `get_owned(for_update=True)` ПЕРЕД `lock_author`
    (порядок `event_cards → users` дал бы встречную очередь с
    `join_open_card`) либо от снятия `for_update` (тогда `FOR UPDATE` по
    `event_cards` не будет вовсе, и окно TOCTOU открывается снова).
    """
    author_id = await _new_user(factory)
    _, card_uuid = await _seed_card(factory, author_id=author_id, capacity=3)

    statements: list[str] = []

    async with factory() as session:
        connection = await session.connection()

        def _capture(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
        ) -> None:
            statements.append(" ".join(statement.split()))

        event.listen(connection.sync_connection, "before_cursor_execute", _capture)
        try:
            await update_card(
                session,
                author_id=author_id,
                card_uuid=card_uuid,
                payload=EventCardUpdate(capacity=1, decision_window_minutes=60),
                settings=_Settings(),
            )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", _capture)

    locking = [sql for sql in statements if "for update" in sql.lower()]
    assert len(locking) >= 2, f"ожидались две блокировки: {statements}"
    first = locking[0].lower()
    second = locking[1].lower()
    # `UserRepository.build_lock_statement` блокирует `users` ПЕРВОЙ.
    assert "from users" in first, first
    # Карточка — ВТОРОЙ, и именно `FOR UPDATE` (X-6.1-1).
    assert "from event_cards" in second, second
    # Ни один запрос к `join_requests` не идёт до блокировки карточки:
    # иначе порядок ADR-09 (`users → event_cards → join_requests`) нарушен.
    card_lock_index = statements.index(locking[1])
    pending_index = next(
        (
            index
            for index, sql in enumerate(statements)
            if sql.lower().startswith("select") and "from join_requests" in sql.lower()
        ),
        None,
    )
    if pending_index is not None:
        assert card_lock_index < pending_index, statements
