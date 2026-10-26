"""Порядок блокировок автовыбора (стори 5б.3; AC-07, ADR-09).

§18.3, урок ER 5а №3: порядок `users -> event_cards -> join_requests`
доказывается ТОЛЬКО перехватом фактически выполненных операторов. Коды
ответов и финальные статусы от перестановки шагов не меняются — меняется
лишь вероятность дедлока под нагрузкой, которую локальный прогон не
воспроизводит.

Оба триггера (воркер и ленивое чтение) обязаны делить ОДИН порядок:
расхождение дало бы встречную очередь с `join_open_card` (ADR-09).

M7 (перестановка `join_requests` раньше `event_cards`) краснеет именно здесь.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.card_decision_service import close_due_card
from app.services.event_card_service import get_card
from app.services.session_service import issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# X-5a.6-3: перехват требует НАСТОЯЩИХ коммитов, поэтому строки остаются в БД
# до уборки. Без неё посторонние тесты падают глобальными `count(*)`
# (воспроизведено: 41 ложное падение в полном прогоне).
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


class _CardSettings:
    """Минимум настроек сервиса карточек (протокол `CardSettings`)."""

    default_community_slug = settings.default_community_slug
    community_slugs = [settings.default_community_slug]


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


async def _seed(
    session: AsyncSession, *, with_request: bool = True
) -> tuple[int, int, uuid_lib.UUID, int]:
    now = datetime.now(UTC)
    author = await _new_user(session)
    place_id = await _place(session)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at) RETURNING uuid, id"
        ),
        {
            "author": author.id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": now + timedelta(days=3),
            "expires": now - timedelta(minutes=5),
            "published_at": now - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()

    applicant_id = 0
    if with_request:
        applicant = await _new_user(session)
        applicant_id = applicant.id
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
                "VALUES (:card, :applicant, '', 'pending', now() + interval '1 day')"
            ),
            {"card": card_id, "applicant": applicant.id},
        )
    await session.commit()
    return author.id, int(card_id), uuid_lib.UUID(str(card_uuid)), applicant_id


def _index_of(statements: list[str], *needles: str) -> int:
    for position, statement in enumerate(statements):
        upper = statement.upper()
        if all(needle.upper() in upper for needle in needles):
            return position
    raise AssertionError(f"оператор не найден: {needles}")


@pytest.fixture
async def capture() -> object:
    """Движок с перехватом операторов; `event.remove` в `finally` (паттерн 5а.6)."""
    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    statements: list[str] = []
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    def _capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        yield factory, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed() -> AsyncIterator[None]:
    """Удаляет созданные строки в порядке, обратном FK-зависимостям.

    Паттерн 5а.6: перехват порядка блокировок требует настоящих коммитов,
    поэтому уборка обязательна — иначе посторонние тесты видят чужие строки.
    """
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_USERS and not _CREATED_PLACES:
        return
    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
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
    finally:
        await engine.dispose()


async def test_worker_core_takes_locks_in_adr_order(capture: object) -> None:
    """AC-07 (ADR-09, M7): ядро берёт `users -> event_cards -> join_requests`.

    Краснеет от:
    - перестановки шагов (M7) — `UPDATE join_requests` оказался бы раньше
      `UPDATE event_cards`;
    - снятия `lock_for_update` (M6) — оператора `FOR UPDATE ... users` не было бы;
    - блокировки карточки ДО автора (D-5b.3-2) — `FOR UPDATE` на `event_cards`
      встал бы первым.
    """
    factory, statements = capture  # type: ignore[misc]

    async with factory() as seed:
        author_id, card_id, _card_uuid, _applicant_id = await _seed(seed)

    async with factory() as session:
        await close_due_card(
            session,
            card_id=card_id,
            author_id=author_id,
            now=datetime.now(UTC) + timedelta(minutes=1),
        )
        await session.commit()

    users_lock = _index_of(statements, "FOR UPDATE", "FROM USERS")
    cards_update = _index_of(statements, "UPDATE EVENT_CARDS")
    requests_update = _index_of(statements, "UPDATE JOIN_REQUESTS")

    assert users_lock < cards_update, "блокировка `users` обязана быть ПЕРВОЙ (ADR-09)"
    assert cards_update < requests_update, "`event_cards` раньше `join_requests`"
    # D-5b.3-2: карточка НЕ блокируется `FOR UPDATE` — это дало бы дедлок.
    assert not [
        statement
        for statement in statements
        if "FOR UPDATE" in statement.upper() and "EVENT_CARDS" in statement.upper()
    ]


async def test_lazy_read_takes_locks_in_same_order(capture: object) -> None:
    """AC-06, AC-07: ленивое чтение даёт ТОТ ЖЕ порядок блокировок, что воркер.

    Расхождение триггеров дало бы встречную очередь с `join_open_card`:
    `GET` брал бы карточку раньше автора, воркер — наоборот.

    Краснеет от M10 (возврат к `close_if_due` в `GET`): операторов
    `FOR UPDATE ... users` и `UPDATE EVENT_CARDS` не было бы вовсе.
    """
    factory, statements = capture  # type: ignore[misc]

    async with factory() as seed:
        _author_id, _card_id, card_uuid, _applicant_id = await _seed(seed)
        viewer = await _new_user(seed)
        viewer_token = await issue_session(seed, viewer)
        await seed.commit()

    async with factory() as session:
        await get_card(
            session,
            card_uuid=card_uuid,
            viewer_id=viewer.id if viewer_token else 0,
            viewer_community_slug="tomsk",
            settings=_CardSettings(),
            now=datetime.now(UTC) + timedelta(minutes=1),
        )

    users_lock = _index_of(statements, "FOR UPDATE", "FROM USERS")
    cards_update = _index_of(statements, "UPDATE EVENT_CARDS")
    requests_update = _index_of(statements, "UPDATE JOIN_REQUESTS")

    assert users_lock < cards_update, "ленивое чтение: `users` ПЕРВЫМ (ADR-09)"
    assert cards_update < requests_update, "ленивое чтение: карточка раньше заявок"
