"""Гонки выбора автора (стори 5б.2; AC-05, AC-07).

§18.3: гонку в одной сессии доказать нельзя — второй запрос видит
незакоммиченные изменения первого. Поэтому фикстура `session` из
`conftest.py` здесь НЕ используется: данные готовятся собственным движком и
коммитятся по-настоящему, а выборы идут через реальный сервис — каждый в
своей сессии со своим соединением.

**Доказательство — `count(*)` строк `selected` и `filled_count`, а не коды
ответов** (урок CR 2.2): код `409` не различает ветки, а число выбранных
показывает, сколько решений реально прошло.

Порядок блокировок (`users -> event_cards -> join_requests`, ADR-09)
доказывается отдельным тестом по `before_cursor_execute`: он краснеет от
перестановки шагов, а не от изменения кодов.
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
from app.services.join_request_service import select_join_request

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"


class _Settings:
    """Минимум настроек сервиса: протокол без чтения `.env`."""

    default_community_slug = COMMUNITY


_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные."""
    engine = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
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
    factory: async_sessionmaker[AsyncSession], *, author_id: int
) -> tuple[int, str]:
    """Карточка режима «заявки» с открытым окном — с настоящим коммитом."""
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
                "VALUES (:author, :place, :slug, 'Гонка', NULL, 'cafe', :starts, 1, 60, "
                ":expires, 'published', 0, 'approved', :published_at) RETURNING id, uuid"
            ),
            {
                "author": author_id,
                "place": place_id,
                "slug": COMMUNITY,
                "starts": starts,
                "expires": datetime.now(UTC) + timedelta(minutes=60),
                "published_at": starts - timedelta(days=1),
            },
        )
        card_id, card_uuid = card_row.one()
        await session.commit()
        _CREATED_PLACES.append(place_id)
        return int(card_id), str(card_uuid)


async def _seed_request(
    factory: async_sessionmaker[AsyncSession], *, card_id: int, applicant_id: int
) -> uuid_lib.UUID:
    async with factory() as session:
        row = await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
                "VALUES (:card, :applicant, '', 'pending', now() + interval '1 day') "
                "RETURNING uuid"
            ),
            {"card": card_id, "applicant": applicant_id},
        )
        request_uuid = uuid_lib.UUID(str(row.scalar_one()))
        await session.commit()
        return request_uuid


async def _card_state(factory: async_sessionmaker[AsyncSession], card_id: int) -> tuple[int, str]:
    async with factory() as session:
        row = await session.execute(
            text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
        )
        filled, status = row.one()
        return int(filled), str(status)


async def _selected_count(factory: async_sessionmaker[AsyncSession], card_id: int) -> int:
    async with factory() as session:
        row = await session.execute(
            text("SELECT count(*) FROM join_requests WHERE card_id = :id AND status = 'selected'"),
            {"id": card_id},
        )
        return int(row.scalar_one())


async def _chat_count(factory: async_sessionmaker[AsyncSession], card_id: int) -> int:
    async with factory() as session:
        row = await session.execute(
            text("SELECT count(DISTINCT chat_uuid) FROM event_cards WHERE id = :id"),
            {"id": card_id},
        )
        return int(row.scalar_one())


async def _attempt(
    factory: async_sessionmaker[AsyncSession], *, author_id: int, request_uuid: uuid_lib.UUID
) -> str:
    """Одна попытка выбора в собственной сессии; код не различитель."""
    async with factory() as session:
        try:
            await select_join_request(
                session,
                author_id=author_id,
                request_uuid=request_uuid,
                settings=_Settings(),  # type: ignore[arg-type]
            )
        except Exception as error:  # noqa: BLE001 — важен факт, не код
            return type(error).__name__
        return "ok"


# --- AC-05: два одновременных выбора ------------------------------------------


async def test_two_concurrent_selects_produce_exactly_one_winner(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05: ровно один `200`, ровно одна заявка `selected`, один `chat_uuid`.

    Ключевое утверждение — СЧЁТЧИКИ, а не коды (урок CR 2.2). Краснеет от
    снятия `status='published'`/`filled_count < capacity` из seat-CAS
    (мутация M5): тогда оба выбора прошли бы, и `selected` стало бы 2 при
    `capacity=1` — а `ck_event_cards_full_capacity` дал бы `500`.

    Второй исход легален в двух видах и оба приемлемы для клиента:
    `CardAlreadyFilledError` (карточка уже собрана) или
    `RequestNotPendingError` (его заявку уже перевели в `not_selected`
    reject-шагом первого выбора). Требование AC «ровно один `200`»
    выполняется в любом случае.
    """
    author_id = await _new_user(factory)
    first_id = await _new_user(factory)
    second_id = await _new_user(factory)
    card_id, _ = await _seed_card(factory, author_id=author_id)
    first_request = await _seed_request(factory, card_id=card_id, applicant_id=first_id)
    second_request = await _seed_request(factory, card_id=card_id, applicant_id=second_id)

    results = await asyncio.gather(
        _attempt(factory, author_id=author_id, request_uuid=first_request),
        _attempt(factory, author_id=author_id, request_uuid=second_request),
    )

    assert results.count("ok") == 1, results
    loser_error = next(result for result in results if result != "ok")
    assert loser_error in {"CardAlreadyFilledError", "RequestNotPendingError"}, results

    assert await _selected_count(factory, card_id) == 1
    assert await _card_state(factory, card_id) == (1, "full")
    assert await _chat_count(factory, card_id) == 1


async def test_repeat_select_after_success_does_not_create_second_chat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05 (D-5b.2-9): повтор после успеха → `REQUEST_NOT_PENDING`, чат тот же.

    Краснеет от идемпотентности (`200` на повтор): сервис пересоздал бы
    `chat_uuid`, и компания в эпике 6 получила бы ДВА чата на одно событие.
    """
    author_id = await _new_user(factory)
    applicant_id = await _new_user(factory)
    card_id, _ = await _seed_card(factory, author_id=author_id)
    request_uuid = await _seed_request(factory, card_id=card_id, applicant_id=applicant_id)

    assert await _attempt(factory, author_id=author_id, request_uuid=request_uuid) == "ok"
    second = await _attempt(factory, author_id=author_id, request_uuid=request_uuid)

    assert second == "RequestNotPendingError"
    assert await _selected_count(factory, card_id) == 1
    assert await _chat_count(factory, card_id) == 1


# --- AC-07: порядок блокировок ------------------------------------------------


async def test_author_row_is_locked_before_card_and_requests(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07 (ADR-09, §18.2): `users -> event_cards -> join_requests`.

    Порядок — часть контракта, и проверяется по ФАКТИЧЕСКИМ операторам, а не
    по порядку вызовов в коде: перехватывается `before_cursor_execute` и
    фиксируется последовательность `SQL`.

    Краснеет от переноса блокировки автора после seat-CAS (мутация M7:
    порядок стал бы `event_cards -> users`), либо от её снятия вовсе — тогда
    первого `FOR UPDATE` по `users` не будет совсем.

    Ручная очистка `event.remove` в `finally` обязательна: слушатель живёт на
    соединении движка, и без снятия он протекает в чужие тесты (долг
    `X-5a.6-3`, 45 ложных падений в CR 5а.6).
    """
    author_id = await _new_user(factory)
    applicant_id = await _new_user(factory)
    card_id, _ = await _seed_card(factory, author_id=author_id)
    request_uuid = await _seed_request(factory, card_id=card_id, applicant_id=applicant_id)

    statements: list[str] = []

    async with factory() as session:
        connection = await session.connection()

        def _capture(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            statements.append(" ".join(statement.split()))

        event.listen(connection.sync_connection, "before_cursor_execute", _capture)
        try:
            await select_join_request(
                session,
                author_id=author_id,
                request_uuid=request_uuid,
                settings=_Settings(),  # type: ignore[arg-type]
            )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", _capture)

    locking = [sql for sql in statements if "FOR UPDATE" in sql.upper()]
    assert locking, f"ни одной блокировки: {statements}"
    # ПЕРВАЯ блокировка — строка автора (`users`), а не карточка и не заявка.
    assert "users" in locking[0].lower(), locking[0]

    # seat-CAS (`UPDATE event_cards`) обязан идти ПОСЛЕ блокировки автора.
    lock_index = statements.index(locking[0])
    seat_index = next(
        index
        for index, sql in enumerate(statements)
        if sql.lower().startswith("update event_cards")
    )
    assert lock_index < seat_index, statements

    # request-CAS (`UPDATE join_requests`) — ПОСЛЕ seat-CAS: обратный порядок
    # дал бы встречную очередь с `join_open_card` (ADR-09).
    request_index = next(
        index
        for index, sql in enumerate(statements)
        if sql.lower().startswith("update join_requests")
    )
    assert seat_index < request_index, statements
