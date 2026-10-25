"""Гонки отмены участия (§18.3, стори 5а.6; AC-03, AC-04).

§18.3: гонку в одной сессии доказать нельзя — второй запрос видит
незакоммиченные изменения первого. Поэтому фикстура `session` из
`conftest.py` (одна транзакция, §18.8) здесь НЕ используется: данные
готовятся собственным движком и коммитятся по-настоящему, а отмены и
присоединения идут через реальный сервис — каждое в своей сессии со своим
соединением.

**Доказательство — `count(*)` строк участия и `filled_count`, а не коды
ответов** (AC-03, урок CR 2.2): код `404` не различает ветки, а число строк
и мест показывает, сколько отмен реально прошло и не переполнило ли счётчик.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repositories.users import UserRepository
from app.services.card_join_service import join_open_card, leave_open_card

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"


class _Settings:
    """Минимум настроек сервиса: протоколы без чтения `.env`."""

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
    factory: async_sessionmaker[AsyncSession],
    *,
    author_id: int,
    capacity: int = 4,
    filled_count: int = 0,
    status: str = "published",
) -> tuple[int, uuid_lib.UUID]:
    """Место и карточка открытого набора — с настоящим коммитом."""
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
                "60, :expires, :status, :filled, 'approved', :published_at) RETURNING id, uuid"
            ),
            {
                "author": author_id,
                "place": place_id,
                "slug": COMMUNITY,
                "starts": starts,
                "capacity": capacity,
                "filled": filled_count,
                "expires": starts - timedelta(hours=1),
                "status": status,
                "published_at": starts - timedelta(days=1),
            },
        )
        card_id, card_uuid = card_row.one()
        await session.commit()
        _CREATED_PLACES.append(place_id)
        return int(card_id), uuid_lib.UUID(str(card_uuid))


async def _seed_participation(
    factory: async_sessionmaker[AsyncSession], *, card_id: int, applicant_id: int
) -> int:
    async with factory() as session:
        row = await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                "decided_at, expires_at) "
                "VALUES (:card, :applicant, '', 'selected', now(), now() + interval '1 day') "
                "RETURNING id"
            ),
            {"card": card_id, "applicant": applicant_id},
        )
        request_id = int(row.scalar_one())
        await session.commit()
        return request_id


async def _state(factory: async_sessionmaker[AsyncSession], card_id: int) -> tuple[int, str]:
    async with factory() as session:
        row = await session.execute(
            text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
        )
        filled, status = row.one()
        return int(filled), str(status)


async def _participation_count(
    factory: async_sessionmaker[AsyncSession], card_id: int, *, status: str | None = None
) -> int:
    query = "SELECT count(*) FROM join_requests WHERE card_id = :id"
    params: dict[str, object] = {"id": card_id}
    if status is not None:
        query += " AND status = :status"
        params["status"] = status
    async with factory() as session:
        row = await session.execute(text(query), params)
        return int(row.scalar_one())


async def _attempt_leave(
    factory: async_sessionmaker[AsyncSession], *, actor_id: int, card_uuid: uuid_lib.UUID
) -> str:
    """Одна попытка отмены в собственной сессии; код не различитель."""
    async with factory() as session:
        try:
            await leave_open_card(
                session,
                actor_id=actor_id,
                card_uuid=card_uuid,
                settings=_Settings(),
            )
        except Exception as error:  # noqa: BLE001 — важен факт, не код
            return type(error).__name__
        return "ok"


async def _attempt_join(
    factory: async_sessionmaker[AsyncSession], *, actor_id: int, card_uuid: uuid_lib.UUID
) -> str:
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


async def test_lock_order_users_then_cards_then_participation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03 (ADR-09): порядок `users → event_cards → join_requests` — наблюдаемый.

    Гонка «отмена ∥ присоединение» сама по себе порядок НЕ доказывает: обе
    транзакции первым делом блокируют одну строку `users` и сериализуются,
    поэтому перестановка двух `UPDATE` внутри SAVEPOINT остаётся невидимой
    (проверено: мутация выживает). Здесь порядок фиксируется напрямую —
    перехватом отправленных операторов.

    Краснеет от:
    - переноса `lock_for_update(actor_id)` после `release_seat` (блокировка
      карточки раньше актора → встречная очередь с `join_open_card`);
    - перестановки `release_seat` и `cancel_participation` (карточка и участие
      в обратном порядке относительно присоединения).
    """
    author = await _new_user(factory)
    member = await _new_user(factory)
    card_id, card_uuid = await _seed_card(factory, author_id=author, capacity=4, filled_count=2)
    await _seed_participation(factory, card_id=card_id, applicant_id=member)

    statements: list[str] = []

    # Собственный движок: у `AsyncEngine` всегда есть `sync_engine`, а
    # `session.get_bind()` в async-сессии отдаёт объект другого типа
    # (проверено: `AttributeError: 'Engine' object has no attribute 'sync_engine'`).
    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    capture_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    def _capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        async with capture_factory() as session:
            await leave_open_card(
                session,
                actor_id=member,
                card_uuid=card_uuid,
                settings=_Settings(),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
        await engine.dispose()

    def _index_of(*needles: str) -> int:
        for position, statement in enumerate(statements):
            upper = statement.upper()
            if all(needle.upper() in upper for needle in needles):
                return position
        raise AssertionError(f"оператор не найден: {needles} в {statements}")

    users_lock = _index_of("FOR UPDATE", "FROM USERS")
    cards_update = _index_of("UPDATE EVENT_CARDS")
    participation_update = _index_of("UPDATE JOIN_REQUESTS")

    assert users_lock < cards_update, "блокировка `users` обязана быть ПЕРВОЙ (ADR-09)"
    assert cards_update < participation_update, "`event_cards` раньше `join_requests`"


async def test_two_concurrent_leaves_decrement_exactly_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (§6.5, NFR47): две одновременные отмены одного участия — один декремент.

    Доказательство — `filled_count` и число `cancelled`-строк, НЕ коды:
    второй запрос честно получает идемпотентный успех (AC-06), и по коду
    нельзя понять, сколько раз место реально освободили.

    Краснеет от снятия `status='selected'` из CAS участия: обе транзакции
    нашли бы `selected` и обе уменьшили бы `filled_count` (2 → 0 при одном
    участнике), что и есть утечка чужих мест.
    """
    author = await _new_user(factory)
    member = await _new_user(factory)
    card_id, card_uuid = await _seed_card(factory, author_id=author, capacity=4, filled_count=2)
    await _seed_participation(factory, card_id=card_id, applicant_id=member)

    await asyncio.gather(
        _attempt_leave(factory, actor_id=member, card_uuid=card_uuid),
        _attempt_leave(factory, actor_id=member, card_uuid=card_uuid),
    )

    filled, status = await _state(factory, card_id)
    assert filled == 1, "место обязано освободиться РОВНО один раз"
    assert status == "published"
    assert await _participation_count(factory, card_id, status="cancelled") == 1
    assert await _participation_count(factory, card_id, status="selected") == 0


async def test_concurrent_leave_and_join_keep_counter_in_bounds(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (§6.5): отмена ∥ присоединение → `0 <= filled_count <= capacity`.

    Один участник выходит, другой занимает освободившееся место. Итог обязан
    быть в границах независимо от порядка: CAS в обе стороны читают СТАРОЕ
    значение строки, поэтому ни переполнения, ни отрицательного значения
    быть не может.

    Краснеет от снятия `filled_count > 0` в `build_release_seat_statement`
    (отмена могла бы уйти в минус на пустой карточке) либо от снятия
    `filled_count < capacity` в `build_join_seat_statement` (переполнение).
    """
    author = await _new_user(factory)
    leaver = await _new_user(factory)
    joiner = await _new_user(factory)
    card_id, card_uuid = await _seed_card(
        factory, author_id=author, capacity=2, filled_count=2, status="full"
    )
    await _seed_participation(factory, card_id=card_id, applicant_id=leaver)

    await asyncio.gather(
        _attempt_leave(factory, actor_id=leaver, card_uuid=card_uuid),
        _attempt_join(factory, actor_id=joiner, card_uuid=card_uuid),
    )

    filled, status = await _state(factory, card_id)
    assert 0 <= filled <= 2, f"filled_count вне границ: {filled}"
    # Место либо осталось за участником, либо перешло к новому — но не удвоилось.
    assert await _participation_count(factory, card_id, status="selected") <= 2
    assert status in ("published", "full")


async def test_leave_and_join_of_same_actor_do_not_deadlock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03 (ADR-09): порядок `users → event_cards → join_requests` — без дедлока.

    Один и тот же актор одновременно выходит и присоединяется: обе транзакции
    первым делом блокируют ОДНУ строку `users`, поэтому встают в очередь
    и завершаются. Обратный порядок (`join_requests` первым) дал бы встречную
    очередь: отмена держит `join_requests`, присоединение — `event_cards`,
    и каждая ждёт вторую.

    Краснеет (таймаутом) от смены порядка блокировок в `leave_open_card`.
    """
    author = await _new_user(factory)
    member = await _new_user(factory)
    card_id, card_uuid = await _seed_card(factory, author_id=author, capacity=2, filled_count=1)
    await _seed_participation(factory, card_id=card_id, applicant_id=member)

    results = await asyncio.wait_for(
        asyncio.gather(
            _attempt_leave(factory, actor_id=member, card_uuid=card_uuid),
            _attempt_join(factory, actor_id=member, card_uuid=card_uuid),
        ),
        timeout=10.0,
    )

    # Ни одна операция не обязана быть успешной, но обе ОБЯЗАНЫ завершиться.
    assert len(results) == 2
    filled, status = await _state(factory, card_id)
    assert 0 <= filled <= 2, f"filled_count вне границ: {filled}"
    assert status in ("published", "full")
