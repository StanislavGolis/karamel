"""Гонки мгновенного присоединения (§18.3, стори 5а.5; AC-03, AC-04, AC-05).

§18.3: гонку в одной сессии доказать нельзя — второй запрос видит
незакоммиченные изменения первого. Поэтому фикстура `session` из
`conftest.py` (одна транзакция, §18.8) здесь НЕ используется: данные
готовятся собственным движком и коммитятся по-настоящему, а присоединения
идут через реальный сервис, каждое — в своей сессии со своим соединением.

**Доказательство — `count(*)` строк участия и `filled_count`, а не коды
ответов** (AC-03, урок CR 2.2): код `409` не различает ветки, а число строк
и мест показывает, сколько присоединений реально прошло и не переполнило ли
счётчик.
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

from app.repositories.event_cards import EventCardRepository
from app.repositories.users import UserRepository
from app.services.card_join_service import join_open_card

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"


class _Settings:
    """Минимум настроек сервиса: протокол `CardJoinSettings` без чтения `.env`."""

    default_city_slug = COMMUNITY


# Коммиты здесь НАСТОЯЩИЕ (§18.3): строки видны остальным тестам и обязаны
# исчезнуть до их старта, иначе прогон пачки даёт лишние карточки в ленте.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные.

    Пул ограничен восемью соединениями: нужны до ШЕСТИ параллельных
    транзакций (AC-04) плюс одна для проверки состояния после гонки.
    """
    engine = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
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


async def _new_user(factory: async_sessionmaker[AsyncSession]) -> int:
    """Пользователь с настоящим коммитом (нужен для FK и для блокировки)."""
    async with factory() as session:
        phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
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
    factory: async_sessionmaker[AsyncSession],
    *,
    author_id: int,
    capacity: int = 4,
    filled_count: int = 0,
) -> int:
    """Место и опубликованная карточка открытого набора — с настоящим коммитом."""
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
                "VALUES (:author, :place, :slug, 'Гонка', NULL, 'cafe', :starts, :capacity, "
                "60, :expires, 'published', :filled, 'approved', :published_at,"
                "'company', 'coffee') RETURNING id, uuid"
            ),
            {
                "author": author_id,
                "place": place_id,
                "slug": COMMUNITY,
                "starts": starts,
                "capacity": capacity,
                "filled": filled_count,
                "expires": starts - timedelta(hours=1),
                "published_at": starts - timedelta(days=1),
            },
        )
        card_id, card_uuid = card_row.one()
        await session.commit()
        _CREATED_PLACES.append(place_id)
        # `uuid` карточки нужен сервису: адресация только по нему.
        return int(card_id), uuid_lib.UUID(str(card_uuid))


async def _state(factory: async_sessionmaker[AsyncSession], card_id: int) -> tuple[int, str]:
    async with factory() as session:
        row = await session.execute(
            text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
        )
        filled, status = row.one()
        return int(filled), str(status)


async def _participation_count(factory: async_sessionmaker[AsyncSession], card_id: int) -> int:
    async with factory() as session:
        row = await session.execute(
            text("SELECT count(*) FROM join_requests WHERE card_id = :id AND status = 'selected'"),
            {"id": card_id},
        )
        return int(row.scalar_one())


async def _attempt(
    factory: async_sessionmaker[AsyncSession], *, actor_id: int, card_uuid: uuid_lib.UUID
) -> str:
    """Одна попытка присоединения в собственной сессии; код не различитель."""
    async with factory() as session:
        try:
            await join_open_card(
                session,
                actor_id=actor_id,
                card_uuid=card_uuid,
                settings=_Settings(),
            )
        except Exception as error:  # noqa: BLE001 — важен факт, не код
            return type(error).__name__
        return "ok"


async def test_two_on_last_seat_yields_exactly_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03 (FR84, ADR-10): двое на последнее место → ровно один успех.

    Доказательство — `count(*)` участий и `filled_count`, НЕ коды ответов:
    `409` пришёл бы и от `CARD_NOT_ACCEPTING`, и от `CARD_IS_FULL`, и тест
    на кодах не различил бы «место занято конкурентом» от «набор закрыт».

    Краснеет от снятия `filled_count < capacity` в предикате: обе транзакции
    увидели бы свободное место, `filled_count` стал бы 3 при `capacity=2`
    (`ck_event_cards_full_capacity` поймал бы это `500`), а строк участия
    оказалось бы две. Краснеет от снятия блокировки актора: то же самое,
    но без `500` — две строки при одном месте.
    """
    author_id = await _new_user(factory)
    first_id = await _new_user(factory)
    second_id = await _new_user(factory)
    card_id, card_uuid = await _seed_card(factory, author_id=author_id, capacity=2, filled_count=1)

    results = await asyncio.gather(
        _attempt(factory, actor_id=first_id, card_uuid=card_uuid),
        _attempt(factory, actor_id=second_id, card_uuid=card_uuid),
    )

    assert results.count("ok") == 1, results
    assert await _participation_count(factory, card_id) == 1
    assert await _state(factory, card_id) == (2, "full")


async def test_six_joiners_for_one_free_seat_yield_exactly_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (NFR47): `capacity=6`, свободно одно место, 6 параллельных → 1 успех.

    NFR47: `filled_count` не превышает `capacity` и не теряет мест. Краснеет
    от снятия предиката `filled_count < capacity` (несколько успехов при
    одном месте) или от `CASE` без `>=` (последнее место занято, но статус
    остался `published` — место потеряно для индикатора).
    """
    author_id = await _new_user(factory)
    joiners = [await _new_user(factory) for _ in range(6)]
    card_id, card_uuid = await _seed_card(factory, author_id=author_id, capacity=6, filled_count=5)

    results = await asyncio.gather(
        *[_attempt(factory, actor_id=joiner, card_uuid=card_uuid) for joiner in joiners]
    )

    assert results.count("ok") == 1, results
    assert await _participation_count(factory, card_id) == 1
    assert await _state(factory, card_id) == (6, "full")


async def test_same_actor_two_cards_does_not_deadlock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05 (ADR-09): два присоединения одного актора к РАЗНЫМ карточкам.

    Инвариант `users → cards`: транзакции встают в очередь по строке актора,
    а не по `event_cards`, поэтому встречной пары блокировок не возникает.
    Обе карточки имеют свободные места, поэтому ожидаемый исход — ДВА успеха;
    `DeadlockDetected` не должен появиться ни при каком плане.

    **Честная граница:** этот тест НЕ различает порядок блокировок — при
    обратном (`cards → users`) одна транзакция ждала бы другую, но дедлока
    на двух разных карточках не было бы. Порядок доказывает отдельный тест
    `test_actor_row_is_locked_before_card_write` (`before_cursor_execute`):
    он краснеет от перестановки шагов. Здесь проверяется наблюдаемое
    следствие — оба легальных действия проходят и мест не теряют.
    """
    author_id = await _new_user(factory)
    actor_id = await _new_user(factory)
    first_card_id, first_uuid = await _seed_card(
        factory, author_id=author_id, capacity=4, filled_count=1
    )
    second_card_id, second_uuid = await _seed_card(
        factory, author_id=author_id, capacity=4, filled_count=1
    )

    results = await asyncio.gather(
        _attempt(factory, actor_id=actor_id, card_uuid=first_uuid),
        _attempt(factory, actor_id=actor_id, card_uuid=second_uuid),
    )

    assert results == ["ok", "ok"], results
    assert await _state(factory, first_card_id) == (2, "published")
    assert await _state(factory, second_card_id) == (2, "published")


async def test_actor_row_is_locked_before_card_write(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05 (ADR-09, §18.2): ПЕРВЫЙ `FOR UPDATE` в транзакции — строка `users`.

    Порядок блокировок — часть контракта, и проверяется по фактическим
    операторам, а не по порядку вызовов в коде: `_attempt` внутри одной
    транзакции фиксирует последовательность `SQL`-выражений.

    Краснеет от переноса `UPDATE` мест перед `lock_for_update` (порядок
    `cards → users`) либо от снятия блокировки актора вовсе — тогда первого
    `FOR UPDATE` по `users` не будет совсем.
    """
    author_id = await _new_user(factory)
    actor_id = await _new_user(factory)
    _, card_uuid = await _seed_card(factory, author_id=author_id, capacity=4)

    statements: list[str] = []

    async with factory() as session:
        connection = await session.connection()

        def _capture(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
        ) -> None:
            statements.append(" ".join(statement.split()))

        event.listen(connection.sync_connection, "before_cursor_execute", _capture)
        try:
            await join_open_card(
                session,
                actor_id=actor_id,
                card_uuid=card_uuid,
                settings=_Settings(),
            )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", _capture)

    locking = [sql for sql in statements if "FOR UPDATE" in sql.upper()]
    assert locking, f"ни одной блокировки: {statements}"
    first = locking[0].lower()
    # `UserRepository.build_lock_statement` блокирует `users` до чтения карточки.
    assert "users" in first, first
    # `UPDATE` мест обязан идти ПОСЛЕ блокировки актора, а не до неё.
    seat_index = next(
        index
        for index, sql in enumerate(statements)
        if sql.lower().startswith("update event_cards")
    )
    lock_index = statements.index(locking[0])
    assert lock_index < seat_index, statements


async def test_card_closed_between_read_and_update_takes_no_seat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07 (FR74): карточку закрыли МЕЖДУ чтением и `UPDATE` — место не занято.

    Единственный сценарий, где предикат `status = 'published'` в самом
    `UPDATE` реально работает: сервисная проверка статуса (шаг 5) читает
    строку ДО записи, и закрытие в этом окне она пропустит.

    Краснеет от снятия `EventCard.status == PUBLISHED` из предиката
    `build_join_seat_statement`: `UPDATE` прошёл бы по закрытой карточке,
    `filled_count` вырос бы, и участие появилось бы на отменённой встрече
    (`500` не будет — гонка легальна по кодам, дефект тихий).

    Порядок блокировок (`users → cards`) сохранён: закрытие идёт ДРУГОЙ
    сессией и коммитится до `UPDATE` мест.
    """
    author_id = await _new_user(factory)
    actor_id = await _new_user(factory)
    card_id, card_uuid = await _seed_card(factory, author_id=author_id, capacity=4)

    original = EventCardRepository.join_seat
    closed: list[str] = []

    async def close_then_join_seat(
        self: EventCardRepository, *, card_id: int, chat_uuid: uuid_lib.UUID
    ) -> Any:
        """Имитирует закрытие карточки автором в окне между чтением и записью.

        Стори 6.2: сигнатура `join_seat` получила `chat_uuid` — спай обязан
        повторять её дословно, иначе `TypeError` вместо проверяемой гонки.
        """
        async with factory() as other:
            await other.execute(
                text("UPDATE event_cards SET status = 'closed', closed_at = now() WHERE id = :id"),
                {"id": card_id},
            )
            await other.commit()
        closed.append("closed")
        return await original(self, card_id=card_id, chat_uuid=chat_uuid)

    async with factory() as session:
        monkeypatch_target = EventCardRepository
        monkeypatch_target.join_seat = close_then_join_seat  # type: ignore[method-assign]
        try:
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
            monkeypatch_target.join_seat = original  # type: ignore[method-assign]

    assert closed == ["closed"]
    assert outcome == "CardNotAcceptingError", outcome
    assert await _state(factory, card_id) == (0, "closed")
    assert await _participation_count(factory, card_id) == 0
