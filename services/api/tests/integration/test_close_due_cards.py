"""Автовыбор первого по истечении окна (стори 5б.3; AC-01…AC-10).

Реальный PostgreSQL, схема поднята Alembic (`0013`). Главные утверждения:

- **единое ядро** (AC-06): `GET /cards/{uuid}` с истёкшим окном и живыми
  `pending` даёт `full` с выбранным — тот же результат, что воркер. До 5б.3
  `GET` звал `close_if_due` и закрывал набор без выбора (FR80);
- **идемпотентность** (AC-03): повторный прогон не создаёт второго `selected`;
- **конкурентность** (AC-07): воркер ∥ авторский `select` и два воркера дают
  `count(selected)=1` — доказательство СЧЁТЧИКОМ, не кодами (урок CR 2.2);
- **детерминизм** (AC-05): равные `submitted_at` разрешаются `id ASC`;
- **частичный сбой** (AC-09): падение одной карточки не отменяет остальные.

§18.3: гонки — только на двух независимых соединениях: фикстура `session`
из `conftest.py` держит одну транзакцию, и второй запрос увидел бы
незакоммиченные изменения первого. Поэтому гоночные тесты готовят данные
собственным движком с настоящими коммитами.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.card_decision_service import DecisionOutcome, close_due_card
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session
from app.workers.close_due_cards import run_decisions

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# X-5a.6-3: гоночные тесты коммитят ПО-НАСТОЯЩЕМУ, и без уборки их строки
# остаются в БД до конца сессии. Глобальные `SELECT count(*)` и лента видят их,
# и посторонние тесты падают (воспроизведено: 41 ложное падение в полном прогоне).
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


# ——— фикстуры —————————————————————————————————————————————————————————————


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент на общей сессии: `commit()` сервиса освобождает SAVEPOINT."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные."""
    engine = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям.

    Паттерн 5а.6 (`test_card_leave_races`): без уборки коммиты гоночных
    тестов остаются в БД и ломают посторонние тесты глобальными `count(*)`.
    """
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


# ——— помощники ————————————————————————————————————————————————————————————


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    issued = await issue_session(session, user)
    return user, issued.access_token


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
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 1,
    filled_count: int = 0,
    status: str = "published",
    expires_in_minutes: int = -5,
) -> tuple[str, int]:
    """Карточка с управляемым окном и состоянием.

    `expires_in_minutes=-5` — окно УЖЕ истекло (штатный вход автовыбора).
    `capacity=1` — режим заявок (FR70). Окно задаётся смещением от `now`,
    чтобы тест не зависел от момента запуска.
    """
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    expires = now + timedelta(minutes=expires_in_minutes)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": expires,
            "status": status,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_request(
    session: AsyncSession,
    *,
    card_id: int,
    applicant_id: int,
    status: str = "pending",
    submitted_at: datetime | None = None,
) -> tuple[str, int]:
    """Заявка с управляемым `submitted_at` (AC-05: равные метки).

    `status='cancelled'` в БД минует CHECK: он разрешает отмену в любой момент.
    """
    moment = submitted_at if submitted_at is not None else datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, '', :status, :submitted, "
            "now() + interval '1 day') RETURNING uuid, id"
        ),
        {
            "card": card_id,
            "applicant": applicant_id,
            "status": status,
            "submitted": moment,
        },
    )
    request_uuid, request_id = row.one()
    return str(request_uuid), int(request_id)


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str, Any, Any]:
    row = await session.execute(
        text("SELECT filled_count, status, chat_uuid, expires_at FROM event_cards WHERE id = :id"),
        {"id": card_id},
    )
    filled, status, chat_uuid, expires_at = row.one()
    return int(filled), str(status), chat_uuid, expires_at


async def _request_statuses(session: AsyncSession, card_id: int) -> dict[int, str]:
    rows = await session.execute(
        text("SELECT id, status FROM join_requests WHERE card_id = :card"), {"card": card_id}
    )
    return {int(row[0]): str(row[1]) for row in rows.all()}


async def _count(session: AsyncSession, sql: str, **params: Any) -> int:
    row = await session.execute(text(sql), params)
    return int(row.scalar_one())


def _card_deadline() -> datetime:
    """Момент ПОСЛЕ истечения окна, созданного с `expires_in_minutes=-5`."""
    return datetime.now(UTC) + timedelta(minutes=1)


# ——— AC-01: автовыбор первого —————————————————————————————————————————————


async def test_auto_select_picks_first_by_submitted_at_and_rejects_rest(
    session: AsyncSession,
) -> None:
    """AC-01 (ADP-05, FR80): первый по `submitted_at, id` → `selected`, остальные → `not_selected`.

    Краснеет от:
    - снятия `reject_pending` (M3) — остались бы `pending`, `count(not_selected)=0`;
    - `ORDER BY submitted_at DESC` (M2) — выбрался бы последний;
    - разделения seat-CAS (M4) — `ck_event_cards_full_capacity` дал бы `500`.
    """
    author, _ = await _new_user(session)
    first, _ = await _new_user(session)
    second, _ = await _new_user(session)
    third, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)

    now = datetime.now(UTC)
    _, first_id = await _insert_request(
        session, card_id=card_id, applicant_id=first.id, submitted_at=now - timedelta(minutes=10)
    )
    _, second_id = await _insert_request(
        session, card_id=card_id, applicant_id=second.id, submitted_at=now - timedelta(minutes=5)
    )
    _, third_id = await _insert_request(
        session, card_id=card_id, applicant_id=third.id, submitted_at=now - timedelta(minutes=1)
    )

    result = await close_due_card(
        session, card_id=card_id, author_id=author.id, now=_card_deadline()
    )

    assert result.outcome is DecisionOutcome.SELECTED
    assert result.card_uuid == uuid_lib.UUID(card_uuid)
    assert result.chat_uuid is not None

    statuses = await _request_statuses(session, card_id)
    assert statuses[first_id] == "selected"
    assert statuses[second_id] == "not_selected"
    assert statuses[third_id] == "not_selected"

    filled, status, chat_uuid, _ = await _card_state(session, card_id)
    assert (filled, status) == (1, "full")  # AC-01
    assert str(chat_uuid) == str(result.chat_uuid)  # AC-01


async def test_auto_select_without_requests_closes_card(session: AsyncSession) -> None:
    """AC-02 (D-5b.3-1): нет `pending` → `closed`, окно НЕ продлевается.

    Краснеет от:
    - продления `expires_at` (M11) — окно изменилось бы;
    - выбора при пустой очереди — `chat_uuid` был бы непустым.
    """
    author, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    _, _, _, expires_before = await _card_state(session, card_id)

    result = await close_due_card(
        session, card_id=card_id, author_id=author.id, now=_card_deadline()
    )

    assert result.outcome is DecisionOutcome.CLOSED_NO_CHOICE
    assert result.chat_uuid is None

    filled, status, chat_uuid, expires_after = await _card_state(session, card_id)
    assert status == "closed"
    assert chat_uuid is None
    assert filled == 0
    # D-5b.3-1: окно не продлевается — второй раз заявок тоже не появится.
    assert expires_after == expires_before


async def test_auto_select_is_idempotent_on_full_card(session: AsyncSession) -> None:
    """AC-03: повторный прогон на `full` → `SKIPPED`, второго выбора нет.

    Краснеет от снятия проверки статуса: второй `chat_uuid` и второй
    `selected` (M1/M5 — предикат без `status`).
    """
    author, _ = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    _, request_id = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    deadline = _card_deadline()
    first = await close_due_card(session, card_id=card_id, author_id=author.id, now=deadline)
    assert first.outcome is DecisionOutcome.SELECTED
    _, _, chat_uuid_first, _ = await _card_state(session, card_id)

    second = await close_due_card(session, card_id=card_id, author_id=author.id, now=deadline)

    assert second.outcome is DecisionOutcome.SKIPPED
    assert second.chat_uuid is None
    assert await _count(session, "SELECT count(*) FROM join_requests WHERE status='selected'") == 1
    _, _, chat_uuid_second, _ = await _card_state(session, card_id)
    assert chat_uuid_second == chat_uuid_first
    assert (await _request_statuses(session, card_id))[request_id] == "selected"


async def test_auto_select_skips_open_window(session: AsyncSession) -> None:
    """AC-03: окно ещё открыто → `SKIPPED`, заявки остаются `pending`.

    Краснеет от снятия `expires_at <= now` из перечитывания: выбор сработал бы
    до срока, и автор потерял бы возможность решить сам.
    """
    author, _ = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, expires_in_minutes=60
    )
    _, request_id = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    result = await close_due_card(
        session, card_id=card_id, author_id=author.id, now=datetime.now(UTC)
    )

    assert result.outcome is DecisionOutcome.SKIPPED
    filled, status, chat_uuid, _ = await _card_state(session, card_id)
    assert (filled, status, chat_uuid) == (0, "published", None)
    assert (await _request_statuses(session, card_id))[request_id] == "pending"


@pytest.mark.parametrize("status", ["closed", "finished", "cancelled", "draft", "full"])
async def test_auto_select_skips_terminal_states(session: AsyncSession, status: str) -> None:
    """AC-03 (матрица строк 4–8): терминальные статусы → `SKIPPED`, 0 изменений.

    `full` создаётся с `filled_count=capacity`: иначе CHECK `0011` отверг бы
    строку ещё на вставке, и тест проверял бы ограничение, а не ядро.

    Краснеет от снятия `status != published` из проверки: ядро тронуло бы
    `draft`/`cancelled` и нарушило FR69/FK74.
    """
    author, _ = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    filled_count = 1 if status == "full" else 0
    _, card_id = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        status=status,
        filled_count=filled_count,
    )
    _, request_id = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    result = await close_due_card(
        session, card_id=card_id, author_id=author.id, now=_card_deadline()
    )

    assert result.outcome is DecisionOutcome.SKIPPED
    filled, actual_status, _, _ = await _card_state(session, card_id)
    assert actual_status == status
    assert filled == filled_count
    assert (await _request_statuses(session, card_id))[request_id] == "pending"


# ——— AC-06: ленивое чтение зовёт ТО ЖЕ ядро ————————————————————————————————


async def test_lazy_get_makes_auto_selection_like_worker(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06 (главный, D-5b.3-3): `GET` с истёкшим окном и живыми `pending` → `full`.

    До 5б.3 `GET` звал `close_if_due` — чистый CAS `published -> closed`.
    Наблюдатель, открывший карточку первым, закрыл бы набор, и все заявки
    остались бы `pending` (FR80, AC-04).

    Краснеет от M10 — возврата `close_if_due` (статус стал бы `closed`,
    `filled_count=0`, `count(selected)=0`).
    """
    author, _ = await _new_user(session)
    first, _ = await _new_user(session)
    second, viewer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    now = datetime.now(UTC)
    _, first_id = await _insert_request(
        session, card_id=card_id, applicant_id=first.id, submitted_at=now - timedelta(minutes=5)
    )
    _, second_id = await _insert_request(
        session, card_id=card_id, applicant_id=second.id, submitted_at=now - timedelta(minutes=1)
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "full"  # AC-06
    assert body["filled_count"] == 1  # AC-06

    statuses = await _request_statuses(session, card_id)
    assert statuses[first_id] == "selected"
    assert statuses[second_id] == "not_selected"
    assert await _count(session, "SELECT count(*) FROM join_requests WHERE status='selected'") == 1

    _, _, chat_uuid_first, _ = await _card_state(session, card_id)

    # Второй `GET` ничего не меняет: `chat_uuid` тот же, второго выбора нет.
    second_response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "full"
    _, _, chat_uuid_second, _ = await _card_state(session, card_id)
    assert chat_uuid_second == chat_uuid_first
    assert await _count(session, "SELECT count(*) FROM join_requests WHERE status='selected'") == 1


async def test_lazy_get_without_requests_closes_card(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06, AC-02: `GET` без заявок → `closed` (тот же исход, что у воркера).

    Краснеет от M10 вместе с предыдущим тестом: при `close_if_due` этот исход
    совпал бы, поэтому AC-06 доказывается ПАРОЙ тестов — «есть заявки» и
    «нет заявок» — и расхождение видно только в первом.
    """
    author, _ = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _ = await _insert_card(session, author_id=author.id, place_id=place_id)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"


async def test_lazy_get_keeps_full_card_unchanged(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-10: `full` с истёкшим окном остаётся `full`, `filled_count` не растёт.

    Наследие OQ-5b.1-1 вариант 3: карточка остаётся в ленте, `test_feed_expired_full.py`
    зелёный. Краснеет от повторного `select_seat` без проверки статуса: CHECK
    `ck_event_cards_full_capacity` дал бы `500` (X-39).
    """
    author, _ = await _new_user(session)
    member, _ = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, filled_count=1, status="full"
    )
    chat_uuid = uuid_lib.uuid4()
    await session.execute(
        text("UPDATE event_cards SET chat_uuid = :chat WHERE id = :id"),
        {"chat": chat_uuid, "id": card_id},
    )
    await _insert_request(session, card_id=card_id, applicant_id=member.id, status="selected")

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "full"
    assert body["filled_count"] == 1
    _, _, chat_uuid_after, _ = await _card_state(session, card_id)
    assert chat_uuid_after == chat_uuid


# ——— AC-03/AC-04: пачка воркера и идемпотентность —————————————————————————


async def test_worker_batch_handles_three_cards_and_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03, AC-09: пачка из 3 карточек, повторный прогон → те же счётчики.

    Состав: (а) есть `pending` → `selected`; (б) нет заявок → `closed_no_choice`;
    (в) уже `full` — кандидатом НЕ становится (предикат `status='published'`),
    поэтому в счётчики не входит. Повтор даёт все нули — второго выбора и
    второго `chat_uuid` нет.

    Краснеет от:
    - снятия проверки статуса (M1/M5) — повтор дал бы `selected:1`;
    - снятия `reject_pending` (M3) — `count(not_selected)` был бы 0.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author, _ = await _new_user(seed)
        applicant, _ = await _new_user(seed)
        member, _ = await _new_user(seed)
        place_id = await _place(seed)
        _, card_with_request = await _insert_card(seed, author_id=author.id, place_id=place_id)
        _, card_without = await _insert_card(seed, author_id=author.id, place_id=place_id)
        _, card_full = await _insert_card(
            seed, author_id=author.id, place_id=place_id, status="full", filled_count=1
        )
        await _insert_request(seed, card_id=card_with_request, applicant_id=applicant.id)
        await _insert_request(seed, card_id=card_full, applicant_id=member.id, status="selected")
        await seed.commit()

    first = await run_decisions(factory, now=moment + timedelta(minutes=1))

    # ОТКЛОНЕНИЕ от Task 3.4 стори (зафиксировано в Completion Notes):
    # `build_due_decision_ids_statement` (Task 1.1) отбирает ТОЛЬКО `published`,
    # поэтому карточка `full` кандидатом не становится и в `skipped` не входит.
    # Счётчик `skipped` наблюдаем только на гонке «кандидат прочитан, окно
    # закрыто ленивым чтением» — она доказана на уровне ядра (AC-03) и в
    # `test_worker_batch_is_idempotent_on_repeat` ниже.
    assert first == {"selected": 1, "closed_no_choice": 1, "skipped": 0, "failed": 0}

    second = await run_decisions(factory, now=moment + timedelta(minutes=2))

    # AC-03: повторный прогон не находит кандидатов — второго выбора нет.
    assert second == {"selected": 0, "closed_no_choice": 0, "skipped": 0, "failed": 0}

    async with factory() as check:
        assert (
            await _count(check, "SELECT count(*) FROM join_requests WHERE status='selected'") == 2
        )
        # Ни одной `published` не осталось: с заявками — `full`, без заявок — `closed`.
        assert (
            await _count(check, "SELECT count(*) FROM event_cards WHERE status = 'published'") == 0
        )
        # Ровно один `chat_uuid` записан — у карточки с автовыбором.
        assert (
            await _count(
                check,
                "SELECT count(*) FROM event_cards WHERE chat_uuid IS NOT NULL",
            )
            == 1
        )


# ——— AC-07: конкурентность —————————————————————————————————————————————————


async def test_worker_and_author_select_produce_one_selection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07(а) (ADR-09): воркер ∥ авторский `select` → РОВНО ОДИН выбор.

    Доказательство — СЧЁТЧИКИ, а не коды ответов (урок CR 2.2): `200` у автора
    и `selected:1` у воркера одновременно невозможны.

    Краснеет от M6 (снятый `lock_for_update`): два выбора дали бы
    `count(selected)=2`, а `ck_event_cards_full_capacity` — `500`.
    """
    from app.services.join_request_service import select_join_request

    moment = datetime.now(UTC)
    async with factory() as seed:
        author, _ = await _new_user(seed)
        first, _ = await _new_user(seed)
        second, _ = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(seed, author_id=author.id, place_id=place_id)
        request_uuid, _ = await _insert_request(seed, card_id=card_id, applicant_id=first.id)
        await _insert_request(seed, card_id=card_id, applicant_id=second.id)
        await seed.commit()

    class _Settings:
        default_city_slug = settings.default_city_slug

    async def _worker() -> dict[str, int]:
        return await run_decisions(factory, now=moment + timedelta(minutes=1))

    async def _author() -> str:
        async with factory() as session:
            try:
                await select_join_request(
                    session,
                    author_id=author.id,
                    request_uuid=uuid_lib.UUID(request_uuid),
                    settings=_Settings(),  # type: ignore[arg-type]
                    now=moment + timedelta(minutes=1),
                )
                await session.commit()
                return "selected"
            except Exception as error:  # noqa: BLE001 — исход гонки нормален
                await session.rollback()
                return type(error).__name__

    await asyncio.gather(_worker(), _author())

    async with factory() as check:
        # Главное утверждение: ровно один выбор на карточке.
        assert (
            await _count(
                check,
                "SELECT count(*) FROM join_requests WHERE card_id = :card AND status='selected'",
                card=card_id,
            )
            == 1
        )
        assert (
            await _count(
                check,
                "SELECT count(*) FROM event_cards WHERE id = :card AND chat_uuid IS NOT NULL",
                card=card_id,
            )
            == 1
        )


async def test_two_workers_on_same_card_produce_one_selection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07(б): два процесса воркера на одной карточке → один выбор, оба живы.

    Краснеет от M6: без строки автора оба процесса заняли бы место.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author, _ = await _new_user(seed)
        first, _ = await _new_user(seed)
        second, _ = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(seed, author_id=author.id, place_id=place_id)
        await _insert_request(seed, card_id=card_id, applicant_id=first.id)
        await _insert_request(seed, card_id=card_id, applicant_id=second.id)
        await seed.commit()

    results = await asyncio.gather(
        run_decisions(factory, now=moment + timedelta(minutes=1)),
        run_decisions(factory, now=moment + timedelta(minutes=1)),
    )

    total_selected = sum(result["selected"] for result in results)
    assert total_selected == 1  # AC-07(б)

    async with factory() as check:
        assert (
            await _count(
                check,
                "SELECT count(*) FROM join_requests WHERE card_id = :card AND status='selected'",
                card=card_id,
            )
            == 1
        )
        assert (
            await _count(
                check,
                "SELECT count(*) FROM event_cards WHERE chat_uuid IS NOT NULL AND id = :card",
                card=card_id,
            )
            == 1
        )


# ——— AC-05: детерминизм при равных `submitted_at` —————————————————————————


async def test_equal_submitted_at_resolved_by_id_asc(session: AsyncSession) -> None:
    """AC-05 (ADR-05): равные `submitted_at` → `id ASC` даёт одинаковый выбор.

    Краснеет от M1 (снятый `id` из `ORDER BY`): порядок зависел бы от
    физического размещения строк, и результат менялся бы между прогонами.
    """
    author, _ = await _new_user(session)
    first, _ = await _new_user(session)
    second, _ = await _new_user(session)
    third, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    same_moment = datetime.now(UTC) - timedelta(minutes=5)
    _, first_id = await _insert_request(
        session, card_id=card_id, applicant_id=first.id, submitted_at=same_moment
    )
    _, second_id = await _insert_request(
        session, card_id=card_id, applicant_id=second.id, submitted_at=same_moment
    )
    _, third_id = await _insert_request(
        session, card_id=card_id, applicant_id=third.id, submitted_at=same_moment
    )

    result = await close_due_card(
        session, card_id=card_id, author_id=author.id, now=_card_deadline()
    )

    assert result.outcome is DecisionOutcome.SELECTED
    statuses = await _request_statuses(session, card_id)
    # `id` минимальный — заявка, вставленная первой.
    assert min(first_id, second_id, third_id) == first_id
    assert statuses[first_id] == "selected"
    assert statuses[second_id] == "not_selected"
    assert statuses[third_id] == "not_selected"


# ——— AC-09: частичный сбой пачки ———————————————————————————————————————————


async def test_worker_partial_failure_does_not_stop_batch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-09: исключение на ОДНОЙ карточке не отменяет остальные в пачке.

    Подмена генератора `chat_uuid` на занятое значение даёт `IntegrityError`
    по `uq_event_cards_chat_uuid`. Краснеет от M8 (снятый `try/except`): пачка
    упала бы целиком, и `failed` не было бы.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author, _ = await _new_user(seed)
        first, _ = await _new_user(seed)
        second, _ = await _new_user(seed)
        third, _ = await _new_user(seed)
        place_id = await _place(seed)
        _, card_a = await _insert_card(seed, author_id=author.id, place_id=place_id)
        _, card_b = await _insert_card(seed, author_id=author.id, place_id=place_id)
        _, card_c = await _insert_card(seed, author_id=author.id, place_id=place_id)
        await _insert_request(seed, card_id=card_a, applicant_id=first.id)
        await _insert_request(seed, card_id=card_b, applicant_id=second.id)
        await _insert_request(seed, card_id=card_c, applicant_id=third.id)
        # Занятое значение `chat_uuid` — «занятая» строка.
        taken = uuid_lib.uuid4()
        _, other_card = await _insert_card(
            seed, author_id=author.id, place_id=place_id, status="closed"
        )
        await seed.execute(
            text("UPDATE event_cards SET chat_uuid = :chat WHERE id = :id"),
            {"chat": taken, "id": other_card},
        )
        await seed.commit()

    class _Factory:
        """Генератор: первое значение — занятое, дальше — настоящий `uuid4`."""

        def __init__(self) -> None:
            self._calls = 0

        def __call__(self) -> uuid_lib.UUID:
            self._calls += 1
            if self._calls == 1:
                return taken
            return uuid_lib.uuid4()

    counters = await run_decisions(
        factory, now=moment + timedelta(minutes=1), chat_uuid_factory=_Factory()
    )

    # Одна карточка упала, две обработаны — пачка продолжилась.
    assert counters["failed"] == 1
    assert counters["selected"] == 2

    async with factory() as check:
        # Упавшая осталась `published` с живым `pending` — попадёт в следующий прогон.
        # Проверка не привязана к конкретной карточке: порядок кандидатов —
        # `expires_at, id`, и «первая упавшая» определяется этим порядком,
        # а не порядком вставки в тесте.
        failed_cards = await _count(
            check,
            "SELECT count(*) FROM event_cards c WHERE c.status = 'published' "
            "AND EXISTS (SELECT 1 FROM join_requests r WHERE r.card_id = c.id "
            "AND r.status = 'pending')",
        )
        assert failed_cards == 1

    retry = await run_decisions(factory, now=moment + timedelta(minutes=2))
    assert retry["selected"] == 1  # AC-09: повтор берёт упавшую карточку


# ——— AC-08: безличность —————————————————————————————————————————————————


async def test_loser_does_not_see_rejection_in_my_requests(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (FR81): проигравший не видит заявку в `/my/requests` — `total` падает.

    Наследие анти-оракулов 5б.2: `not_selected` скрыт фильтром `VISIBLE`.
    Краснеет от снятия фильтра — уменьшившийся `total` выдал бы отказ.
    """
    author, _ = await _new_user(session)
    first, _ = await _new_user(session)
    second, loser_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    now = datetime.now(UTC)
    await _insert_request(
        session, card_id=card_id, applicant_id=first.id, submitted_at=now - timedelta(minutes=5)
    )
    await _insert_request(
        session, card_id=card_id, applicant_id=second.id, submitted_at=now - timedelta(minutes=1)
    )

    before = await client.get("/api/v1/my/requests", cookies={ACCESS_COOKIE_NAME: loser_token})
    assert before.status_code == 200
    assert before.json()["total"] == 1

    lazy = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: loser_token})
    assert lazy.status_code == 200

    after = await client.get("/api/v1/my/requests", cookies={ACCESS_COOKIE_NAME: loser_token})
    assert after.status_code == 200
    # AC-08: заявка проигравшего исчезла из видимых (не «стала not_selected»).
    assert after.json()["total"] == 0


async def test_card_screen_is_identical_for_loser_and_observer(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08: экран карточки одинаков для проигравшего и постороннего наблюдателя.

    Различие в теле выдало бы факт отказа — прямое нарушение безличности.
    Единственное допустимое расхождение — `server_time` (X-5b.2-2).

    Краснеет от: (а) добавления `not_selected`/`rejected` в ответ;
    (б) поля вроде `auto`/`my_request_status`.
    """
    author, _ = await _new_user(session)
    first, _ = await _new_user(session)
    second, loser_token = await _new_user(session)
    _, observer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    now = datetime.now(UTC)
    await _insert_request(
        session, card_id=card_id, applicant_id=first.id, submitted_at=now - timedelta(minutes=5)
    )
    await _insert_request(
        session, card_id=card_id, applicant_id=second.id, submitted_at=now - timedelta(minutes=1)
    )

    loser = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: loser_token}
    )
    observer = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: observer_token}
    )

    assert loser.status_code == 200, loser.text
    assert observer.status_code == 200, observer.text

    loser_body = loser.json()
    observer_body = observer.json()
    loser_body.pop("server_time", None)
    observer_body.pop("server_time", None)
    assert loser_body == observer_body  # AC-08

    raw = loser.text.lower()
    for marker in ("not_selected", "rejected", "отклон", "не выбран", "auto"):
        assert marker not in raw, f"утечка признака отказа: {marker}"


async def test_worker_logs_do_not_leak_applicant_identity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-08, D-5b.3-5: ни одно событие `worker.card.*` не содержит заявителя.

    Перехватываем structlog-события прогона: `card_uuid` есть, `applicant_id`
    и `user_id` — нет. Краснеет от M9 (добавление `applicant_id` в лог).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author, _ = await _new_user(seed)
        first, _ = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(seed, author_id=author.id, place_id=place_id)
        _, request_id = await _insert_request(seed, card_id=card_id, applicant_id=first.id)
        applicant_id = first.id
        await seed.commit()

    captured: list[Any] = []

    class _Capture:
        def __call__(self, logger: Any, method_name: str, event_dict: Any) -> Any:
            captured.append(dict(event_dict))
            return event_dict

    # CR 6.5: `cache_logger_on_first_use=False` обязателен.
    # Прод-конфиг (`configure_logging`, logging.py:159) кэширует логгер при
    # первом использовании, а `run_decisions` теперь вызывается РАНЬШЕ — в
    # тестах воркера стори 6.5 (`test_ws_worker_publish.py`). Без сброса кэша
    # логгер остаётся связанным с прод-процессорами, и `_Capture()` не видит
    # ни одного события: `assert []` в пачке с WS-тестами, одиночно — зелено
    # (проверено прогоном).
    structlog.configure(
        processors=[_Capture(), *structlog.get_config()["processors"]],
        cache_logger_on_first_use=False,
    )
    try:
        await run_decisions(factory, now=moment + timedelta(minutes=1))
    finally:
        structlog.reset_defaults()

    worker_events = [e for e in captured if str(e.get("event", "")).startswith("worker.card.")]
    assert worker_events, "прогон обязан оставить хотя бы одно событие"

    # Событие длительности агрегатное: `card_uuid` у него нет по построению.
    per_card = [e for e in worker_events if e.get("event") != "worker.card.decision.duration_ms"]
    assert per_card, "выбор/закрытие обязаны оставить per-card событие"
    for event_dict in per_card:
        assert "card_uuid" in event_dict

    for event_dict in worker_events:
        assert "applicant_id" not in event_dict
        assert "user_id" not in event_dict
        assert str(applicant_id) not in str(event_dict.get("detail", ""))


# ——— AC-04: инвариант «пропущенных окон 0» ——————————————————————————————


async def test_invariant_no_published_card_stays_past_window(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (NFR21, ADR-14): после прогона ни одной `published` с истёкшим окном.

    5 карточек с заявками, 5 прогонов с разными `now` и «рестарт» (пересоздание
    `session_factory`). Проверка — прямой запрос метрики из ADR-14.

    Краснеет от: (а) пропуска карточек в пачке (`LIMIT` без порядка);
    (б) отсутствия автовыбора — карточки остались бы `published`.
    """
    moment = datetime.now(UTC)
    own_cards: list[int] = []
    async with factory() as seed:
        author, _ = await _new_user(seed)
        place_id = await _place(seed)
        for _index in range(5):
            applicant, _ = await _new_user(seed)
            _, card_id = await _insert_card(seed, author_id=author.id, place_id=place_id)
            await _insert_request(seed, card_id=card_id, applicant_id=applicant.id)
            own_cards.append(card_id)
        await seed.commit()

    for attempt in range(5):
        await run_decisions(factory, now=moment + timedelta(minutes=attempt + 1))

    # «Рестарт»: новая фабрика и новый движок — состояние обязано сохраниться.
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    try:
        restarted = async_sessionmaker(bind=engine, expire_on_commit=False)
        await run_decisions(restarted, now=moment + timedelta(minutes=10))
        async with restarted() as check:
            # AC-04 / ADR-14: ни одной `published` с истёкшим окном среди СВОИХ карточек.
            # Ограничение по `card_id` обязательно: коммиты гоночных тестов
            # остаются в БД до конца сессии (TRUNCATE — на прогон, не на тест).
            assert (
                await _count(
                    check,
                    "SELECT count(*) FROM event_cards WHERE id = ANY(:cards) "
                    "AND status = 'published'",
                    cards=own_cards,
                )
                == 0
            )
            assert (
                await _count(
                    check,
                    "SELECT count(*) FROM join_requests WHERE card_id = ANY(:cards) "
                    "AND status='selected'",
                    cards=own_cards,
                )
                == 5
            )
            assert (
                await _count(
                    check,
                    "SELECT count(*) FROM event_cards WHERE id = ANY(:cards) "
                    "AND chat_uuid IS NOT NULL",
                    cards=own_cards,
                )
                == 5
            )
    finally:
        await engine.dispose()


# --- CR 5b.3: no auto-selection for open-set cards (AC-01, AC-02) ---


async def test_auto_selection_does_not_touch_open_set_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01 (FR80): `capacity>1` — открытый набор, выбора НЕТ (окно закрывается).

    Дефект, найденный CR 5б.3 исполнением: ядро `close_due_card` не проверяло
    режим карточки, хотя авторский `select_join_request` проверяет его
    (`join_request_service:570`). На карточке `capacity=2` с `pending` ядро
    возвращало `SELECTED`, писало `chat_uuid` и переводило заявку в `selected`,
    но карточка оставалась `published` с `filled_count=1`: seat-CAS
    `select_seat` требует `capacity=1` и молча не совпадал. Итог — «выбранный»
    на наборе, куда можно продолжать записываться, и `chat_uuid` на открытом
    наборе, которого не должно быть до эпика 6.

    Достижимость: `PATCH capacity 1 -> 2` при живых `pending` проходит, потому
    что гард «уже есть участник» смотрит на `filled_count` (0), а не на заявки.

    Ожидание: окно закрывается как обычно (AC-02), но БЕЗ выбора — заявки
    остаются `pending`, `chat_uuid` и `filled_count` не меняются.

    Краснеет от снятия проверки режима перед `first_pending`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author, _ = await _new_user(seed)
        applicant, _ = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(seed, author_id=author.id, place_id=place_id, capacity=2)
        await _insert_request(seed, card_id=card_id, applicant_id=applicant.id)
        await seed.commit()

    counters = await run_decisions(factory, now=moment)

    assert counters["selected"] == 0
    assert counters["closed_no_choice"] == 1
    async with factory() as check:
        status, filled, chat = (
            await check.execute(
                text("SELECT status, filled_count, chat_uuid FROM event_cards WHERE id=:i"),
                {"i": card_id},
            )
        ).one()
        assert status == "closed"
        assert filled == 0
        assert chat is None

        (pending,) = (
            await check.execute(
                text("SELECT count(*) FROM join_requests WHERE card_id=:i AND status='pending'"),
                {"i": card_id},
            )
        ).one()
        assert pending == 1
