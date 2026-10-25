"""Открытие чата триггером присоединения (стори 6.2; AC-01…AC-08).

TD 6.2 покрывает этот файл сценариями I1–I12 и спаями S1–S3. Реальный
PostgreSQL, схема поднята Alembic (`0014` — таблица `chats` уже создана).

Главные утверждения:

- **три пути сборки** (I1, I4, I7): авторский `select`, мгновенное
  присоединение к последнему месту и автовыбор воркера — все три создают
  строку `chats` с `uuid == event_cards.chat_uuid` (контракт 5б.2);
- **идемпотентность** (I3, I3b): повторный вызов не создаёт второй чат и не
  отдаёт ошибку (`ON CONFLICT (event_card_id) DO NOTHING`);
- **FR30** (I5, I6): до сборки чата нет и `chat_uuid IS NULL` — оба значения
  признака проверяются (§18.11);
- **порядок блокировок** (S1–S3): только ПЕРЕХВАТОМ — итоговое состояние
  одинаково при любом порядке (§18.11);
- **X-39** (I8): `chat_uuid` пишется ТЕМ ЖЕ `UPDATE`, что инкремент и `full`.

§18.3: тесты, которым нужны настоящие коммиты (спаи, гонки), работают через
собственный движок `factory` — фикстура `session` держит одну транзакцию.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.enums import JoinRequestStatus
from app.models.user import User
from app.repositories import event_cards as event_cards_module
from app.repositories import users as users_module
from app.repositories.chats import ChatRepository
from app.repositories.event_cards import EventCardRepository
from app.repositories.users import UserRepository
from app.services.card_decision_service import DecisionOutcome, close_due_card
from app.services.card_join_service import (
    CardIsFullError,
    CardJoinResult,
    CardNotAcceptingError,
    join_open_card,
)
from app.services.event_card_service import get_card
from app.services.join_request_service import (
    RequestNotPendingError,
    SelectJoinRequestResult,
    select_join_request,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# X-5a.6-3: гоночные тесты коммитят по-настоящему; без уборки их строки ломают
# глобальные `count(*)` в посторонних тестах.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


# ——— фикстуры —————————————————————————————————————————————————————————————


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Собственный движок: нужен и фабрике сессий, и перехвату SQL (I8).

    `event.listen` ставится на `engine.sync_engine`: слушатели работают на
    синхронном уровне, а `AsyncEngine` их не принимает.
    """
    created = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield created
    await created.dispose()


@pytest.fixture
async def factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие."""
    yield async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _cleanup_seed(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Удаляет созданные строки в порядке, обратном FK-зависимостям.

    `chats` уходит ПЕРВЫМ: на него ссылается `messages`, а на карточку —
    сам чат; `TRUNCATE` не используется, чтобы не задеть строки других тестов.
    """
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


# ——— помощники ————————————————————————————————————————————————————————————


async def _new_user(session: AsyncSession) -> User:
    phone = f"+7904{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
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
    capacity: int = 1,
    filled_count: int = 0,
    status: str = "published",
    expires_in_minutes: int = 60,
    chat_uuid: uuid_lib.UUID | None = None,
) -> tuple[uuid_lib.UUID, int]:
    """Карточка с управляемыми местами, статусом и (опционально) чатом."""
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    expires = now + timedelta(minutes=expires_in_minutes)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at, :chat_uuid) "
            "RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": expires,
            "status": status,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1),
            "chat_uuid": chat_uuid,
        },
    )
    card_uuid, card_id = row.one()
    return uuid_lib.UUID(str(card_uuid)), int(card_id)


async def _insert_request(
    session: AsyncSession,
    *,
    card_id: int,
    applicant_id: int,
    status: str = JoinRequestStatus.PENDING.value,
) -> uuid_lib.UUID:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, '', :status, now(), "
            "now() + interval '1 day') RETURNING uuid"
        ),
        {"card": card_id, "applicant": applicant_id, "status": status},
    )
    return uuid_lib.UUID(str(row.scalar_one()))


async def _insert_participation(session: AsyncSession, *, card_id: int, applicant_id: int) -> None:
    """Участие в открытом наборе: `selected`, пустое сообщение (5а.5)."""
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "decided_at, expires_at) VALUES (:card, :applicant, '', 'selected', now(), now(), "
            "now() + interval '1 day')"
        ),
        {"card": card_id, "applicant": applicant_id},
    )


async def _chat_rows(session: AsyncSession, *, card_id: int) -> list[tuple[Any, ...]]:
    result = await session.execute(
        text("SELECT uuid FROM chats WHERE event_card_id = :id"), {"id": card_id}
    )
    return [tuple(row) for row in result]


async def _card_chat_uuid(session: AsyncSession, *, card_id: int) -> uuid_lib.UUID | None:
    result = await session.execute(
        text("SELECT chat_uuid FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    value = result.scalar_one()
    return None if value is None else uuid_lib.UUID(str(value))


class _JoinSettings:
    """Минимум настроек для `join_open_card` (Protocol `CardJoinSettings`)."""

    default_community_slug = settings.default_community_slug


class _CardFeedSettings:
    """Минимум настроек для `get_card` (`CardSettings`), CR-2."""

    default_community_slug = settings.default_community_slug
    community_slugs = (settings.default_community_slug,)
    card_default_decision_window_minutes = 60
    card_content_stop_words: tuple[str, ...] = ()


class _RequestSettings:
    """Минимум настроек для `select_join_request` (`JoinRequestSettings`)."""

    default_community_slug = settings.default_community_slug
    join_request_active_limit = 2
    s3_signed_url_ttl = 900


# ——— I1–I3b: путь capacity=1 (авторский select) —————————————————————————


async def test_select_creates_chat_with_same_uuid(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I1, AC-01: выбор автора создаёт чат с `uuid == event_cards.chat_uuid`.

    Краснеет от мутации M4 (убрать `ensure_for_card` из `select`): ответ `200`
    содержал бы `chat_uuid`, а строки в `chats` не было — «идентификатор
    указывает в пустоту».
    """
    async with factory() as session:
        author = await _new_user(session)
        applicant = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=1
        )
        request_uuid = await _insert_request(
            session, card_id=card_id, applicant_id=int(applicant.id)
        )
        await session.commit()

    async with factory() as session:
        result = await select_join_request(
            session,
            author_id=int(author.id),
            request_uuid=request_uuid,
            settings=_RequestSettings(),
        )
        assert isinstance(result, SelectJoinRequestResult)

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)

    assert len(rows) == 1, rows
    assert uuid_lib.UUID(str(rows[0][0])) == card_chat == result.chat_uuid


async def test_repeat_select_keeps_single_chat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I3, AC-04: повторный `select` не создаёт второй чат (контракт 5б.2 — 409).

    Краснеет, если вставка чата окажется вне транзакции выбора: второй вызов
    получил бы `REQUEST_NOT_PENDING`, но успел бы вставить вторую строку.
    """
    async with factory() as session:
        author = await _new_user(session)
        applicant = await _new_user(session)
        place = await _place(session)
        _, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=1
        )
        request_uuid = await _insert_request(
            session, card_id=card_id, applicant_id=int(applicant.id)
        )
        await session.commit()

    async with factory() as session:
        first = await select_join_request(
            session,
            author_id=int(author.id),
            request_uuid=request_uuid,
            settings=_RequestSettings(),
        )

    async with factory() as session:
        with pytest.raises(RequestNotPendingError):
            await select_join_request(
                session,
                author_id=int(author.id),
                request_uuid=request_uuid,
                settings=_RequestSettings(),
            )

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)

    assert len(rows) == 1
    assert uuid_lib.UUID(str(rows[0][0])) == first.chat_uuid


async def test_ensure_for_card_twice_returns_none(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I3b, AC-04, TD п.1: второй вызов → `None`, а не исключение.

    Краснеет от мутации M11 (убрать `on_conflict_do_nothing`): повторная
    сборка давала бы `IntegrityError` по `uq_chats_event_card_id` → `500`.
    §18.11: проверяются ОБА исхода — `(uuid, id)` и `None`.
    """
    async with factory() as session:
        author = await _new_user(session)
        place = await _place(session)
        _, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=2
        )
        await session.commit()

    chat_uuid = uuid_lib.uuid4()
    async with factory() as session:
        first = await ChatRepository(session).ensure_for_card(
            chat_uuid=chat_uuid, event_card_id=card_id
        )
        await session.commit()

    async with factory() as session:
        second = await ChatRepository(session).ensure_for_card(
            chat_uuid=uuid_lib.uuid4(), event_card_id=card_id
        )
        await session.commit()

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)

    assert first is not None and first[0] == chat_uuid
    assert second is None
    assert len(rows) == 1
    assert uuid_lib.UUID(str(rows[0][0])) == chat_uuid


# ——— I4–I6, I12: путь открытого набора ————————————————————————————————


async def test_join_last_seat_creates_chat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I4, AC-02: последнее место (`capacity=2`, занято 1) открывает чат.

    Краснеет от мутации M5 (убрать `ensure_for_card` из `join_open_card`):
    `chat_uuid` записан, а строки `chats` нет. Краснеет и от M3 (`>=` → `>`).
    """
    async with factory() as session:
        author = await _new_user(session)
        first_member = await _new_user(session)
        second_member = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=2, filled_count=1
        )
        await _insert_participation(session, card_id=card_id, applicant_id=int(first_member.id))
        await session.commit()

    async with factory() as session:
        result = await join_open_card(
            session,
            actor_id=int(second_member.id),
            card_uuid=card_uuid,
            settings=_JoinSettings(),
        )
        assert isinstance(result, CardJoinResult)

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)
        status = (
            await session.execute(
                text("SELECT status FROM event_cards WHERE id = :id"), {"id": card_id}
            )
        ).scalar_one()

    assert len(rows) == 1, rows
    assert uuid_lib.UUID(str(rows[0][0])) == card_chat
    assert status == "full"


async def test_not_full_no_chat(factory: async_sessionmaker[AsyncSession]) -> None:
    """I5, AC-05, FR30: занято 2 из 4 — чата НЕТ, `chat_uuid IS NULL`.

    Краснеет от мутации M2 (безусловная запись `chat_uuid`): чат появился бы
    до сборки компании.
    """
    async with factory() as session:
        author = await _new_user(session)
        first_member = await _new_user(session)
        second_member = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=4, filled_count=1
        )
        await _insert_participation(session, card_id=card_id, applicant_id=int(first_member.id))
        await session.commit()

    async with factory() as session:
        await join_open_card(
            session,
            actor_id=int(second_member.id),
            card_uuid=card_uuid,
            settings=_JoinSettings(),
        )

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)
        filled = (
            await session.execute(
                text("SELECT filled_count FROM event_cards WHERE id = :id"), {"id": card_id}
            )
        ).scalar_one()

    assert filled == 2
    assert rows == []
    assert card_chat is None


async def test_third_of_four_no_chat_until_last(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I6, AC-05, §18.11: занято 3 из 4 — чата всё ещё нет (второе значение).

    Краснеет от мутации M3 (`>=` → `>` даёт молчание на последнем месте) в
    паре с I4: здесь проверяется «до», там — «после».
    """
    async with factory() as session:
        author = await _new_user(session)
        members = [await _new_user(session) for _ in range(3)]
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=4, filled_count=2
        )
        for member in members[:2]:
            await _insert_participation(session, card_id=card_id, applicant_id=int(member.id))
        await session.commit()

    async with factory() as session:
        await join_open_card(
            session,
            actor_id=int(members[2].id),
            card_uuid=card_uuid,
            settings=_JoinSettings(),
        )

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)

    assert rows == []
    assert card_chat is None


async def test_existing_chat_not_overwritten(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I12, риск R10: ранее записанный `chat_uuid` не затирается.

    Краснеет от мутации M8 (`else_=EventCard.chat_uuid` → `else_=None`):
    присоединение на незаполненной карточке обнулило бы чат прошлой сборки
    (достижимо при отмене участия и повторной сборке, 5а.6).
    """
    existing = uuid_lib.uuid4()
    async with factory() as session:
        author = await _new_user(session)
        member = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session,
            author_id=int(author.id),
            place_id=place,
            capacity=4,
            filled_count=1,
            chat_uuid=existing,
        )
        await session.commit()

    async with factory() as session:
        await join_open_card(
            session,
            actor_id=int(member.id),
            card_uuid=card_uuid,
            settings=_JoinSettings(),
        )

    async with factory() as session:
        card_chat = await _card_chat_uuid(session, card_id=card_id)

    assert card_chat == existing


# ——— I7, I10: третий путь (автовыбор) и контракт uuid ————————————————


async def test_close_due_card_creates_chat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I7, AC-01, D-6.2-4, риск R3: автовыбор создаёт чат.

    Реальный дефект до 6.2: `close_due_card` писал `chat_uuid` в карточку,
    но строки `chats` не создавал. Краснеет от мутации M7.
    """
    async with factory() as session:
        author = await _new_user(session)
        applicant = await _new_user(session)
        place = await _place(session)
        _, card_id = await _insert_card(
            session,
            author_id=int(author.id),
            place_id=place,
            capacity=1,
            expires_in_minutes=-5,
        )
        await _insert_request(session, card_id=card_id, applicant_id=int(applicant.id))
        await session.commit()

    async with factory() as session:
        result = await close_due_card(
            session,
            card_id=card_id,
            author_id=int(author.id),
            now=datetime.now(UTC),
        )
        await session.commit()

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)

    assert result.outcome == DecisionOutcome.SELECTED
    assert result.chat_uuid is not None
    assert len(rows) == 1, rows
    assert uuid_lib.UUID(str(rows[0][0])) == card_chat == result.chat_uuid


async def test_chat_uuid_matches_in_all_three_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I10: `chats.uuid == event_cards.chat_uuid` во ВСЕХ трёх путях сборки.

    Параметризация по путям не нужна: каждый путь создаёт свою карточку в
    одном тесте, и проверка одна — расхождение означает «чат недоступен по
    известному `chat_uuid`» (инвариант 5б.2).
    """
    async with factory() as session:
        author = await _new_user(session)
        applicant = await _new_user(session)
        member = await _new_user(session)
        auto_applicant = await _new_user(session)
        place = await _place(session)

        # 1. Авторский select (`capacity=1`).
        _, select_card = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=1
        )
        select_request_uuid = await _insert_request(
            session, card_id=select_card, applicant_id=int(applicant.id)
        )

        # 2. Открытый набор, последнее место.
        join_card_uuid, join_card = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=2, filled_count=1
        )
        await _insert_participation(session, card_id=join_card, applicant_id=int(member.id))

        # 3. Автовыбор (`capacity=1`, окно истекло).
        _, auto_card = await _insert_card(
            session,
            author_id=int(author.id),
            place_id=place,
            capacity=1,
            expires_in_minutes=-5,
        )
        await _insert_request(session, card_id=auto_card, applicant_id=int(auto_applicant.id))
        await session.commit()

    async with factory() as session:
        await select_join_request(
            session,
            author_id=int(author.id),
            request_uuid=select_request_uuid,
            settings=_RequestSettings(),
        )

    async with factory() as session:
        third = await _new_user(session)
        await session.commit()
        third_id = int(third.id)

    async with factory() as session:
        await join_open_card(
            session, actor_id=third_id, card_uuid=join_card_uuid, settings=_JoinSettings()
        )

    async with factory() as session:
        await close_due_card(
            session, card_id=auto_card, author_id=int(author.id), now=datetime.now(UTC)
        )
        await session.commit()

    async with factory() as session:
        for card_id in (select_card, join_card, auto_card):
            rows = await _chat_rows(session, card_id=card_id)
            card_chat = await _card_chat_uuid(session, card_id=card_id)
            assert len(rows) == 1, (card_id, rows)
            assert uuid_lib.UUID(str(rows[0][0])) == card_chat


# ——— I8: один UPDATE —————————————————————————————————————————————————


async def test_one_update_writes_chat_uuid_and_full(
    factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    """I8, AC-07, X-39: РОВНО один `UPDATE event_cards` за сборку.

    Перехват `before_cursor_execute`: два `UPDATE` дали бы промежуточную
    строку `published` + `filled_count = capacity`, запрещённую CHECK
    (`500` на легальном действии). Краснеет от вынесения чата во второй запрос.
    """
    async with factory() as session:
        author = await _new_user(session)
        member = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=2, filled_count=1
        )
        await session.commit()

    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        # CR 6.2 (auditor #10): проверяется ФАКТ обновления таблицы
        # `event_cards` в любом написании (`UPDATE "event_cards"`,
        # `UPDATE public.event_cards`), а не строковый префикс: иначе второй
        # `UPDATE` в другой форме прошёл бы мимо счётчика.
        normalized = " ".join(statement.lower().replace('"', "").split())
        if normalized.startswith("update event_cards"):
            statements.append(statement)

    async with factory() as session:
        # `event.listen` — на `sync_engine`: слушатели SQLAlchemy работают на
        # синхронном уровне, `AsyncEngine` их не принимает.
        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            await join_open_card(
                session,
                actor_id=int(member.id),
                card_uuid=card_uuid,
                settings=_JoinSettings(),
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert len(statements) == 1, statements
    statement = statements[0].lower()
    assert "chat_uuid" in statement
    assert "filled_count" in statement


# ——— I11: 0 строк seat-CAS — чата нет ————————————————————————————————


async def test_no_chat_when_seat_cas_zero_rows(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I11, риск R11, TD п.4 (мутация M12): 0 строк seat-CAS не создаёт чат.

    Ситуация: карточку закрыли/место заняли МЕЖДУ чтением и `UPDATE` —
    `join_seat` вернул `None`. Без guard'а `if seat.chat_uuid is not None`
    строка `chats` всё равно появилась бы у несобранной карточки (FR30).

    `join_seat` подменяется на «0 строк»: настоящую гонку воспроизвести
    детерминированно нельзя (окно между SELECT и UPDATE), а поведение
    сервиса при `None` — ровно то, что проверяет AC-02.
    """

    async def _no_seat(self: EventCardRepository, *, card_id: int, chat_uuid: Any) -> Any:
        return None

    monkeypatch.setattr(EventCardRepository, "join_seat", _no_seat)

    async with factory() as session:
        author = await _new_user(session)
        member = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=2, filled_count=1
        )
        await session.commit()

    async with factory() as session:
        # Отказ доменный: `join_seat` вернул 0 строк → сервис перечитывает
        # карточку и отвечает `CARD_IS_FULL` либо `CARD_NOT_ACCEPTING`.
        with pytest.raises((CardIsFullError, CardNotAcceptingError)):
            await join_open_card(
                session,
                actor_id=int(member.id),
                card_uuid=card_uuid,
                settings=_JoinSettings(),
            )

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)

    assert rows == []
    assert card_chat is None


# ——— I13–I14: стабильность uuid и диагностика рассинхрона ———————————


async def test_second_assembly_keeps_existing_chat_uuid(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I13, AC-02, CR blind #1 (HIGH): повторная сборка НЕ меняет `chat_uuid`.

    Сценарий: компания была собрана (чат `X`), участник вышел (5а.6: `full →
    published`, `chat_uuid` в карточке остаётся, строка `chats` тоже),
    карточку добрали заново. Без `coalesce` в карточку лёг бы НОВЫЙ `uuid4`,
    а `chats.uuid` остался бы `X` — контракт 6.1 нарушен навсегда, клиент
    получил бы идентификатор несуществующего чата.

    Краснеет от мутации M13 (`coalesce` → голый `chat_uuid`).
    """
    existing = uuid_lib.uuid4()
    async with factory() as session:
        author = await _new_user(session)
        first_member = await _new_user(session)
        rival = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session,
            author_id=int(author.id),
            place_id=place,
            capacity=2,
            filled_count=1,
            chat_uuid=existing,
        )
        await ChatRepository(session).ensure_for_card(chat_uuid=existing, event_card_id=card_id)
        await _insert_participation(session, card_id=card_id, applicant_id=int(first_member.id))
        await session.commit()

    async with factory() as session:
        await join_open_card(
            session, actor_id=int(rival.id), card_uuid=card_uuid, settings=_JoinSettings()
        )

    async with factory() as session:
        rows = await _chat_rows(session, card_id=card_id)
        card_chat = await _card_chat_uuid(session, card_id=card_id)

    assert len(rows) == 1
    assert uuid_lib.UUID(str(rows[0][0])) == existing
    assert card_chat == existing


async def test_ensure_for_card_mismatch_is_logged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I14, AC-04, CR blind #10: рассинхрон uuid виден в журнале, но не ломает API.

    `ON CONFLICT DO NOTHING` возвращает `None` и при СОВПАДАЮЩЕМ uuid
    (штатный повтор), и при РАСХОДЯЩЕМСЯ (нарушение контракта 6.1). Молчание
    делало бы второй случай ненаблюдаемым. Краснеет, если сверку и
    `logger.error` убрать.
    """
    events: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if event_dict.get("event") == "chat.ensure.mismatch":
            events.append(dict(event_dict))
        return event_dict

    structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
    try:
        async with factory() as session:
            author = await _new_user(session)
            place = await _place(session)
            _, card_id = await _insert_card(
                session, author_id=int(author.id), place_id=place, capacity=2
            )
            stored = uuid_lib.uuid4()
            await ChatRepository(session).ensure_for_card(chat_uuid=stored, event_card_id=card_id)
            await session.commit()

        async with factory() as session:
            result = await ChatRepository(session).ensure_for_card(
                chat_uuid=uuid_lib.uuid4(), event_card_id=card_id
            )
            await session.commit()
    finally:
        structlog.reset_defaults()

    assert result is None
    assert events, "расхождение uuid не зафиксировано в журнале"
    assert events[0]["card_id"] == card_id
    assert "stored_uuid" in events[0]


# ——— I9: безличность лога ————————————————————————————————————————————


async def test_no_applicant_id_in_log(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """I9, AC-08, ADR-13: событие сборки несёт `card_uuid`/`chat_uuid`, не `applicant_id`.

    Краснеет от мутации M10 (добавить `applicant_id` в лог): заявитель
    становится виден в журналах, что нарушает безличность (FR81).
    """
    events: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if event_dict.get("event") == "chat.opened":
            events.append(dict(event_dict))
        return event_dict

    structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
    try:
        async with factory() as session:
            author = await _new_user(session)
            applicant = await _new_user(session)
            place = await _place(session)
            _, card_id = await _insert_card(
                session, author_id=int(author.id), place_id=place, capacity=1
            )
            request_uuid = await _insert_request(
                session, card_id=card_id, applicant_id=int(applicant.id)
            )
            await session.commit()

        async with factory() as session:
            await select_join_request(
                session,
                author_id=int(author.id),
                request_uuid=request_uuid,
                settings=_RequestSettings(),
            )
    finally:
        structlog.reset_defaults()

    assert events, "событие chat.opened не записано"
    payload = events[0]
    assert "card_uuid" in payload
    assert "chat_uuid" in payload
    assert all("applicant" not in key for key in payload)


async def test_no_chat_opened_log_on_repeat_assembly(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-08, CR-2 (blind #7): повторное присоединение к СОБРАННОЙ карточке
    НЕ пишет `chat.opened` — второго чата не открывалось.

    Сценарий (5а.6): компания собрана (чат есть), участник вышел
    (`full → published`), карточку добрали заново. `seat.chat_uuid` непуст,
    `ensure_for_card` вернёт `None`. Краснеет, если лог писать по признаку
    `seat.chat_uuid is not None`: журнал сообщал бы об открытии чата на
    каждом присоединении к давно собранной карточке.
    """
    existing = uuid_lib.uuid4()
    events: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if event_dict.get("event") == "chat.opened":
            events.append(dict(event_dict))
        return event_dict

    async with factory() as session:
        author = await _new_user(session)
        member = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session,
            author_id=int(author.id),
            place_id=place,
            capacity=2,
            filled_count=1,
            chat_uuid=existing,
        )
        await ChatRepository(session).ensure_for_card(chat_uuid=existing, event_card_id=card_id)
        await session.commit()

    structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
    try:
        async with factory() as session:
            await join_open_card(
                session,
                actor_id=int(member.id),
                card_uuid=card_uuid,
                settings=_JoinSettings(),
            )
    finally:
        structlog.reset_defaults()

    # Чат открыт ДО этого вызова — новое событие было бы ложным аудитом.
    assert events == []


async def test_lazy_read_writes_chat_opened_after_commit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-08, CR-2 (blind #7): ленивое чтение пишет `chat.opened` ПОСЛЕ commit.

    `close_due_card` сам не логирует: владелец транзакции — вызывающий.
    Краснеет, если лог убрать из `get_card` целиком: третий путь сборки
    (ленивый автовыбор) остаётся без события сборки, и аудит теряет путь.
    """
    events: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if event_dict.get("event") == "chat.opened":
            events.append(dict(event_dict))
        return event_dict

    async with factory() as session:
        author = await _new_user(session)
        applicant = await _new_user(session)
        viewer = await _new_user(session)
        place = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=int(author.id), place_id=place, capacity=1, expires_in_minutes=-5
        )
        await _insert_request(session, card_id=card_id, applicant_id=int(applicant.id))
        await session.commit()

    structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
    try:
        async with factory() as session:
            await get_card(
                session,
                card_uuid=card_uuid,
                viewer_id=int(viewer.id),
                settings=_CardFeedSettings(),
            )
    finally:
        structlog.reset_defaults()

    assert len(events) == 1, events
    assert events[0]["card_uuid"] == str(card_uuid)
    assert "chat_uuid" in events[0]
    assert all("applicant" not in key for key in events[0])


# ——— S1–S3: порядок блокировок перехватом ————————————————————————————


async def test_lock_order_join_open_card(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1, AC-06: `lock_for_update` → `join_seat` → `ensure_for_card`.

    §18.11: порядок доказывается ПЕРЕХВАТОМ, а не итоговым состоянием —
    обратный порядок даёт тот же результат на одном соединении.
    Мутация M6 (перенести `ensure_for_card` до `join_seat`) краснит ТОЛЬКО
    этот тест; все интеграционные остаются зелёными.
    """
    calls: list[str] = []
    await _spy_order(factory, monkeypatch, calls=calls, path="join")

    assert calls == ["lock_for_update", "join_seat", "ensure_for_card"]


async def test_lock_order_select_join_request(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2, AC-06: `lock_for_update` → `select_seat` → `ensure_for_card`."""
    calls: list[str] = []
    await _spy_order(factory, monkeypatch, calls=calls, path="select")

    assert calls == ["lock_for_update", "select_seat", "ensure_for_card"]


async def test_lock_order_close_due_card(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3, AC-06, D-6.2-4: третий путь соблюдает тот же порядок."""
    calls: list[str] = []
    await _spy_order(factory, monkeypatch, calls=calls, path="auto")

    assert calls == ["lock_for_update", "select_seat", "ensure_for_card"]


async def _spy_order(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: list[str],
    path: str,
) -> None:
    """Готовит карточку под выбранный путь и пишет фактическую последовательность.

    Спаи ставятся на ТРИ класса (`UserRepository`, `EventCardRepository`,
    `ChatRepository`) и пишут ИМЯ операции при каждом вызове. Сравнение —
    точное (`==`), не `in` (TD п.7): иначе лишний или переставленный вызов
    остался бы незамеченным.
    """
    original_lock = UserRepository.lock_for_update
    original_join = EventCardRepository.join_seat
    original_select = EventCardRepository.select_seat
    original_ensure = ChatRepository.ensure_for_card

    async def lock(self: Any, user_id: int) -> None:
        calls.append("lock_for_update")
        return await original_lock(self, user_id)

    async def join_seat(self: Any, *, card_id: int, chat_uuid: uuid_lib.UUID) -> Any:
        calls.append("join_seat")
        return await original_join(self, card_id=card_id, chat_uuid=chat_uuid)

    async def select_seat(self: Any, *, card_id: int, chat_uuid: uuid_lib.UUID) -> Any:
        calls.append("select_seat")
        return await original_select(self, card_id=card_id, chat_uuid=chat_uuid)

    async def ensure_for_card(
        self: ChatRepository, *, chat_uuid: uuid_lib.UUID, event_card_id: int
    ) -> tuple[uuid_lib.UUID, int] | None:
        calls.append("ensure_for_card")
        return await original_ensure(self, chat_uuid=chat_uuid, event_card_id=event_card_id)

    monkeypatch.setattr(users_module.UserRepository, "lock_for_update", lock)
    monkeypatch.setattr(event_cards_module.EventCardRepository, "join_seat", join_seat)
    monkeypatch.setattr(event_cards_module.EventCardRepository, "select_seat", select_seat)
    monkeypatch.setattr(ChatRepository, "ensure_for_card", ensure_for_card)

    async with factory() as session:
        author = await _new_user(session)
        actor = await _new_user(session)
        place = await _place(session)
        if path == "join":
            card_uuid, card_id = await _insert_card(
                session, author_id=int(author.id), place_id=place, capacity=2, filled_count=1
            )
        elif path == "select":
            card_uuid, card_id = await _insert_card(
                session, author_id=int(author.id), place_id=place, capacity=1
            )
            await _insert_request(session, card_id=card_id, applicant_id=int(actor.id))
        else:
            card_uuid, card_id = await _insert_card(
                session,
                author_id=int(author.id),
                place_id=place,
                capacity=1,
                expires_in_minutes=-5,
            )
            await _insert_request(session, card_id=card_id, applicant_id=int(actor.id))
        await session.commit()

    async with factory() as session:
        if path == "join":
            await join_open_card(
                session,
                actor_id=int(actor.id),
                card_uuid=card_uuid,
                settings=_JoinSettings(),
            )
        elif path == "select":
            request_row = (
                await session.execute(
                    text(
                        "SELECT uuid FROM join_requests WHERE card_id = :id AND status = 'pending'"
                    ),
                    {"id": card_id},
                )
            ).scalar_one()
            await select_join_request(
                session,
                author_id=int(author.id),
                request_uuid=uuid_lib.UUID(str(request_row)),
                settings=_RequestSettings(),
            )
        else:
            await close_due_card(
                session,
                card_id=card_id,
                author_id=int(author.id),
                now=datetime.now(UTC),
            )
            await session.commit()
