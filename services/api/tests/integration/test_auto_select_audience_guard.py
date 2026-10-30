"""Гард-тест 14 и парный тест: автовыбор только среди прошедших рамки.

Стори 12.6, AC-5 (FR119, ADR-19). `capacity=1`, окно решения истекло.

**Гард-тест 14 (утверждение «не выбирает непрошедшего»).** Единственная
заявка — от 22 лет при `age_min=30`. Автовыбор ОБЯЗАН не состояться:
`status` остаётся `published`, `count(selected) = 0`, `chat_uuid IS NULL`,
`filled_count = 0`. Без этой проверки карточка молча уходила бы в `closed`
(`CLOSED_NO_CHOICE`) — то есть человек, подавший заявку, терял бы её без
всякого решения автора, а автор не узнал бы, что прошедших рамки не было.

**Парный тест (P-21, утверждение «выбирает прошедшего»).** Одна и та же
карточка с рамкой `age_min=30`: заявка от 22 (раньше) и от 35 (позже).
Должен быть выбран 35-летний, а 22-летний — `not_selected`.

Без пары один тест не отличает «фильтр работает» от «автовыбор сломан
вообще»: предикат → `false` (мутация U5) даёт `published` в гарде, но
ломает пару; предикат → `true` (U4) ломает гард, но проходит пару.

Тесты идут ЧЕРЕЗ ЯДРО (`run_decisions`), а не через прямой вызов: гард
обязан держаться на том пути, которым ходит воркер в продакшене.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.workers.close_due_cards import run_decisions

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

#: Строки, созданные тестом НАСТОЯЩИМИ коммитами. `run_decisions` берёт свои
#: сессии и коммитит сам, поэтому данные не откатываются фикстурой `session`,
#: и без уборки они ломали бы посторонние тесты глобальными `count(*)`
#: (X-5a.6-3, воспроизведено: 41 ложное падение в полном прогоне).
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


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
    """Удаляет созданные строки в порядке, обратном FK-зависимостям."""
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


async def _new_user(session: AsyncSession) -> int:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    row = await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
            "city_slug) VALUES ('phone', :pid, :phone, 'user', 'female', 'active', :slug) "
            "RETURNING id"
        ),
        {"pid": phone, "phone": phone, "slug": settings.default_city_slug},
    )
    user_id = int(row.scalar_one())
    _CREATED_USERS.append(user_id)
    return user_id


async def _profile(session: AsyncSession, *, user_id: int, age: int) -> None:
    """Анкета с legacy-числом: предикат обязан прочитать его как fallback."""
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, gender, bio, status) "
            "VALUES (:user, 'Имя', 'Томск', :age, 'female', 'о себе', 'complete')"
        ),
        {"user": user_id, "age": age},
    )


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    return int(row.scalar_one())


async def _expired_card(session: AsyncSession, *, author_id: int, place_id: int) -> tuple[str, int]:
    """Опубликованная карточка `capacity=1` с УЖЕ ИСТЁКШИМ окном решения."""
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity, age_min) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at, 'company', 'coffee', 30) "
            "RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "expires": now - timedelta(minutes=5),
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _request(
    session: AsyncSession, *, card_id: int, applicant_id: int, minutes_ago: int
) -> int:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, 'пойдём вместе', 'pending', "
            ":submitted, now() + interval '1 day') RETURNING id"
        ),
        {
            "card": card_id,
            "applicant": applicant_id,
            "submitted": datetime.now(UTC) - timedelta(minutes=minutes_ago),
        },
    )
    return int(row.scalar_one())


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str, Any, Any]:
    row = await session.execute(
        text("SELECT filled_count, status, chat_uuid, expires_at FROM event_cards WHERE id = :id"),
        {"id": card_id},
    )
    filled, status, chat_uuid, expires_at = row.one()
    return int(filled), str(status), chat_uuid, expires_at


async def _statuses(session: AsyncSession, card_id: int) -> dict[int, str]:
    rows = await session.execute(
        text("SELECT id, status FROM join_requests WHERE card_id = :card"), {"card": card_id}
    )
    return {int(row[0]): str(row[1]) for row in rows.all()}


async def test_guard_14_only_outsider_pending_keeps_card_published(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Гард-тест 14: единственная заявка вне рамок ⇒ выбора НЕТ.
    П6 (FR80, ADR-19): автовыбор идёт только среди прошедших рамки аудитории.

    Краснеет при: предикате → `true` (U4, выберет 22-летнего); снятии
    фильтра из `first_pending` (выберет кого угодно); закрытии карточки
    через `close_if_due`, когда прошедших нет — она ушла бы в `closed`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _expired_card(seed, author_id=author_id, place_id=place_id)

        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        young_request_id = await _request(
            seed, card_id=card_id, applicant_id=young_id, minutes_ago=60
        )
        await seed.commit()

    await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        filled, status, chat_uuid, _ = await _card_state(check, card_id)
        statuses = await _statuses(check, card_id)

    assert status == "published", (
        f"AC-5: при отсутствии прошедших рамки карточка обязана остаться "
        f"published (гард-тест 14), получено {status} (card {card_uuid})"
    )
    assert filled == 0, f"AC-5: `filled_count` обязан остаться 0, получено {filled}"
    assert chat_uuid is None, f"AC-5: чат не создаётся, получено {chat_uuid}"
    assert statuses[young_request_id] == "pending", (
        f"AC-5: заявка вне рамок не должна менять статус, получено {statuses[young_request_id]}"
    )
    selected = [req for req, value in statuses.items() if value == "selected"]
    assert selected == [], f"AC-5: `count(selected)` обязан быть 0, получено {selected}"


async def test_paired_insider_is_selected_over_earlier_outsider(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Парный тест (P-21): выбирается прошедший рамки, даже если он ПОЗЖЕ.

    22-летний подал раньше, 35-летний — позже. Порядок очереди ADR-05
    (`submitted_at, id`) применяется ПОСЛЕ фильтра рамок, поэтому выбран
    35-летний, а 22-летний получает `not_selected`.

    Краснеет при: предикате → `false` (U5, выбрал бы 22-летнего); снятии
    фильтра (тот же эффект); инверсии порядка «фильтр после LIMIT 1».
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _expired_card(seed, author_id=author_id, place_id=place_id)

        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        young_request_id = await _request(
            seed, card_id=card_id, applicant_id=young_id, minutes_ago=120
        )

        adult_id = await _new_user(seed)
        await _profile(seed, user_id=adult_id, age=35)
        adult_request_id = await _request(
            seed, card_id=card_id, applicant_id=adult_id, minutes_ago=10
        )
        await seed.commit()

    await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        filled, status, chat_uuid, _ = await _card_state(check, card_id)
        statuses = await _statuses(check, card_id)

    assert status == "full", (
        f"AC-5: прошедший рамки должен быть выбран, status={status} (card {card_uuid})"
    )
    assert filled == 1, f"AC-5: занято ровно одно место, получено {filled}"
    assert chat_uuid is not None, "AC-5: чат компании обязан быть создан"
    assert statuses[adult_request_id] == "selected", (
        f"AC-5: выбран должен быть 35-летний, получено {statuses[adult_request_id]}"
    )
    assert statuses[young_request_id] == "not_selected", (
        f"AC-5: 22-летний обязан получить not_selected, получено {statuses[young_request_id]}"
    )


async def test_no_requests_at_all_still_closes_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-5 + поведение 5б.3: заявок НЕТ вовсе — карточка закрывается.

    Отличие от гарда 14 принципиально и обязательно: «рамки отсекли всех»
    и «никто не пришёл» — разные факты. В первом случае решение ещё может
    состояться (сними автор рамку — заявка жива), во втором решать нечего,
    и карточка обязана закрыться (`closed_no_choice`), как до 12.6.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _expired_card(seed, author_id=author_id, place_id=place_id)
        await seed.commit()

    counters = await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        _, status, chat_uuid, _ = await _card_state(check, card_id)

    assert counters["closed_no_choice"] == 1, (
        f"AC-5: пустая очередь обязана закрыть карточку, счётчики {counters}"
    )
    assert status == "closed", (
        f"AC-5: карточка без заявок закрывается, получено {status} (card {card_uuid})"
    )
    assert chat_uuid is None
