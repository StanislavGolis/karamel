"""Доработка 12.6 по CR (H-1…H-5): замки на поведение воркера и рамок.

CR `code-review-12-6-2026-10-30.md` нашёл три ВЫСОКИХ дефекта, невидимых в
тестах стори: вечный повтор (H-1), регрессию открытого набора (H-2) и
голодание очереди воркера (H-3), плюс две дыры покрытия предиката (H-4 FR126,
H-5 граница равенства).

Тесты идут ЧЕРЕЗ ЯДРО (`run_decisions`) там, где проверяется воркер: гард
обязан держаться на том пути, которым воркер ходит в продакшене. Предикат
рамок (H-4, H-5) проверяется напрямую — он и есть единица утверждения
(AC-5a: один объект-выражение на автовыбор и на список автора).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.join_request import JoinRequest
from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository
from app.workers.close_due_cards import run_decisions

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class _CardSettings:
    """Минимум настроек `CardSettings` без чтения `.env` (Protocol не инстанцируется)."""

    default_city_slug = settings.default_city_slug
    city_slugs = (settings.default_city_slug,)
    card_default_decision_window_minutes = 60
    card_content_stop_words: tuple[str, ...] = ()
    card_active_limit = 5


class _JoinRequestSettings:
    """Минимум настроек `JoinRequestSettings` (Protocol не инстанцируется)."""

    default_city_slug = settings.default_city_slug
    join_request_active_limit = 10
    s3_signed_url_ttl = 900


_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты `run_decisions` настоящие."""
    engine = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Убирает созданные строки в порядке, обратном FK-зависимостям.

    `run_decisions` коммитит сам, поэтому данные не откатываются фикстурой
    `session`; без уборки они ломали бы посторонние тесты глобальными
    `count(*)` (X-5a.6-3, воспроизведено: 41 ложное падение в полном прогоне).
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


async def _profile(
    session: AsyncSession, *, user_id: int, age: int | None = None, birth_date: date | None = None
) -> None:
    """Анкета: legacy-число или дата рождения.

    CHECK `completeness` требует `name/city/bio` и возраст ЛИБО `birth_date`
    (миграция `0036`), поэтому пустая анкета здесь невозможна.
    """
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, birth_date, gender, bio, status) "
            "VALUES (:user, 'Имя', 'Томск', :age, :birth, 'female', 'о себе', 'complete')"
        ),
        {"user": user_id, "age": age, "birth": birth_date},
    )


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


async def _card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 1,
    age_min: int | None = None,
    gender_filter: str | None = None,
    expires_minutes_ago: int = 5,
) -> tuple[str, int]:
    """Опубликованная карточка с УЖЕ ИСТЁКШИМ окном решения."""
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity, "
            "gender_filter, age_min) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', 0, 'approved', :published_at, 'company', 'coffee', "
            ":gender, :age_min) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": now - timedelta(minutes=expires_minutes_ago),
            "published_at": starts - timedelta(days=1),
            "gender": gender_filter,
            "age_min": age_min,
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _request(
    session: AsyncSession,
    *,
    card_id: int,
    applicant_id: int,
    minutes_ago: int = 60,
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


async def _card_status(session: AsyncSession, card_id: int) -> tuple[str, Any]:
    row = await session.execute(
        text("SELECT status, audience_skipped_at FROM event_cards WHERE id = :id"),
        {"id": card_id},
    )
    status, skipped_at = row.one()
    return str(status), skipped_at


async def _predicate_flags(
    session: AsyncSession,
    *,
    card_id: int,
    age_min: int | None = None,
    age_max: int | None = None,
    gender_filter: str | None = None,
) -> dict[int, bool]:
    """Считает ТОТ ЖЕ предикат, что видит автор (AC-5a), по заявкам карточки.

    Прямое вычисление, а не HTTP: утверждение — про выражение, и лишний слой
    транспорта только размыл бы причину падения (P-25: проверяется эффект).
    """
    predicate = JoinRequestRepository.build_within_bounds_predicate(
        gender_filter=gender_filter, age_min=age_min, age_max=age_max
    )
    statement = select(JoinRequest.id, predicate.label("within")).where(
        JoinRequest.card_id == card_id
    )
    result = await session.execute(statement)
    return {int(row[0]): bool(row[1]) for row in result.all()}


# ——— H-1: маркер «рамки отсекли всех» останавливает вечный повтор ———


async def test_skipped_card_is_marked_and_worker_does_not_repeat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-1: второй прогон НЕ возвращает карточку, `count_overdue == 0`.

    До фикса карточка оставалась `published` (AC-5) и совпадала с предикатом
    воркера ВЕЧНО: замер CR давал `skipped: 1` на каждом прогоне, а
    `count_overdue_windows` = 1 — то есть ложный алерт «воркер лежит» каждый
    час на здоровом воркере (дедуп 10.7 замьютил бы канал).

    Краснеет при: снятии маркера; пропуске `mark_audience_skipped` в ветке;
    исключении маркера из `due_decision_ids` в обратную сторону (здоровая
    карточка перестала бы обрабатываться — проверяется парным тестом ниже).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _card(
            seed, author_id=author_id, place_id=place_id, capacity=1, age_min=30
        )
        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        request_id = await _request(seed, card_id=card_id, applicant_id=young_id)
        await seed.commit()

    run1 = await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)
    run2 = await run_decisions(factory, now=moment + timedelta(minutes=2), limit=200)

    async with factory() as check:
        status, skipped_at = await _card_status(check, card_id)
        request_status = (
            await check.execute(
                text("SELECT status FROM join_requests WHERE id = :id"), {"id": request_id}
            )
        ).scalar_one()
        overdue = await EventCardRepository(check).count_overdue_windows(
            now=moment + timedelta(minutes=3)
        )
        due = await EventCardRepository(check).due_decision_ids(
            now=moment + timedelta(minutes=3), limit=200
        )

    assert run1["skipped"] == 1, f"H-1: первый прогон обязан пометить карточку, {run1}"
    assert run2["skipped"] == 0, (
        f"H-1: второй прогон НЕ должен возвращать помеченную карточку, {run2}"
    )
    assert status == "published", f"AC-5: карточка обязана остаться published, получено {status}"
    assert skipped_at is not None, "H-1: маркер `audience_skipped_at` обязан быть поставлен"
    assert request_status == "pending", (
        f"AC-5: заявка вне рамок остаётся pending, получено {request_status}"
    )
    assert overdue == 0, (
        "H-1: помеченная карточка НЕ зависшая, иначе ложный алерт «воркер лежит» "
        f"(card {card_uuid})"
    )
    assert all(row[0] != card_id for row in due), (
        f"H-1: помеченная карточка не попадает в выборку воркера, получено {due}"
    )


async def test_unmarked_healthy_card_still_selected(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пара к H-1 (P-21): карточка БЕЗ пометки обрабатывается как раньше.

    Без этой пары исключение маркера из `due_decision_ids` не отличить от
    «воркер перестал обрабатывать всё».
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(seed, author_id=author_id, place_id=place_id, capacity=1)
        adult_id = await _new_user(seed)
        await _profile(seed, user_id=adult_id, age=35)
        await _request(seed, card_id=card_id, applicant_id=adult_id)
        await seed.commit()

    counters = await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        status, _ = await _card_status(check, card_id)

    assert counters["selected"] == 1, f"пара к H-1: карточка без рамок выбирается, {counters}"
    assert status == "full", f"пара к H-1: status должен стать full, получено {status}"


# ——— H-2: ветка ограничена режимом заявок ———


async def test_open_set_card_with_bounds_still_closes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-2: открытый набор (`capacity=3`) с рамками закрывается окном.

    Замер CR: до фикса такая карточка не закрывалась НИКОГДА (`cap=3` +
    рамки ⇒ ветка `SKIPPED` вечно), тогда как до 12.6 её закрывал
    `CLOSED_NO_CHOICE` (AC-02 5б.3). В открытом наборе заявок нет вовсе
    (`submit_join_request` отбивает `CAPACITY_ONE_USES_REQUESTS`), поэтому
    `pending` там — легаси-строка, и ждать решения по ней нечего.

    Краснеет при: снятии условия `capacity == CARD_REQUEST_MODE_CAPACITY`
    из ветки `SKIPPED`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(
            seed, author_id=author_id, place_id=place_id, capacity=3, age_min=30
        )
        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        await _request(seed, card_id=card_id, applicant_id=young_id)
        await seed.commit()

    counters = await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        status, skipped_at = await _card_status(check, card_id)

    assert counters["closed_no_choice"] == 1, (
        f"H-2: открытый набор обязан закрыться окном, счётчики {counters}"
    )
    assert status == "closed", f"H-2: status обязан стать closed, получено {status}"
    assert skipped_at is None, (
        "H-2: маркер «рамки отсекли всех» — свойство режима заявок, "
        "на открытом наборе он не ставится"
    )


async def test_request_mode_card_with_bounds_stays_published(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пара к H-2 (P-21): в режиме заявок та же карточка НЕ закрывается.

    Один тест не отличает «условие режима добавлено» от «ветка удалена».
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(
            seed, author_id=author_id, place_id=place_id, capacity=1, age_min=30
        )
        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        await _request(seed, card_id=card_id, applicant_id=young_id)
        await seed.commit()

    counters = await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        status, _ = await _card_status(check, card_id)

    assert counters["closed_no_choice"] == 0, (
        f"AC-5: в режиме заявок карточка не закрывается, счётчики {counters}"
    )
    assert status == "published", f"AC-5: status остаётся published, получено {status}"


# ——— H-3: зависшие карточки не вытесняют здоровые ———


async def test_stuck_cards_do_not_starve_healthy_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-3: при `limit=2` здоровая карточка обязана быть обработана.

    Замер CR: две зависшие карточки (самый старый `expires_at`, значит голова
    очереди) занимали весь батч, и здоровая карточка без рамок не была
    обработана НИ РАЗУ за три прогона. При `WORKER_BATCH_SIZE = 200`
    достаточно 200 таких карточек, чтобы воркер встал целиком.

    Краснеет при: снятии маркера из `due_decision_ids`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        # Две зависшие: окно истекло РАНЬШЕ, значит они в голове очереди.
        stuck_ids: list[int] = []
        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        for minutes_ago in (60, 50):
            _, stuck_id = await _card(
                seed,
                author_id=author_id,
                place_id=place_id,
                capacity=1,
                age_min=30,
                expires_minutes_ago=minutes_ago,
            )
            await _request(seed, card_id=stuck_id, applicant_id=young_id)
            stuck_ids.append(stuck_id)

        # Здоровая: окно истекло ПОЗЖЕ, значит она последняя в очереди.
        _, healthy_id = await _card(
            seed, author_id=author_id, place_id=place_id, capacity=1, expires_minutes_ago=10
        )
        adult_id = await _new_user(seed)
        await _profile(seed, user_id=adult_id, age=35)
        await _request(seed, card_id=healthy_id, applicant_id=adult_id)
        await seed.commit()

    # Два прогона при `limit=2`: первый занимает окно зависшими (они в голове
    # очереди), но ПОМЕЧАЕТ их — и второй прогон обязан дойти до здоровой.
    # До фикса здоровая не обрабатывалась ни на одном из трёх прогонов CR.
    run1 = await run_decisions(factory, now=moment + timedelta(minutes=1), limit=2)
    run2 = await run_decisions(factory, now=moment + timedelta(minutes=2), limit=2)
    counters = {
        key: run1[key] + run2[key] for key in ("selected", "closed_no_choice", "skipped", "failed")
    }

    async with factory() as check:
        healthy_status, _ = await _card_status(check, healthy_id)
        stuck_statuses = [await _card_status(check, card_id) for card_id in stuck_ids]

    assert run1["skipped"] == 2, f"H-3: первый прогон занимает окно зависшими, {run1}"
    assert run2["skipped"] == 0, (
        f"H-3: второй прогон не должен снова упираться в зависшие (маркер), {run2}"
    )
    assert counters["selected"] == 1, (
        f"H-3: здоровая карточка обязана быть обработана при limit=2, счётчики {counters}"
    )
    assert healthy_status == "full", (
        f"H-3: здоровую карточку должен был выбрать автовыбор, получено {healthy_status}"
    )
    assert all(status == "published" for status, _ in stuck_statuses), (
        f"AC-5: зависшие остаются published, получено {stuck_statuses}"
    )


# ——— H-4: возраст считается на `submitted_at` (FR126) ———


def _birth_date_turning_18_today() -> date:
    """Дата рождения человека, которому СЕГОДНЯ исполняется 18.

    `29.02` не встречается в датах рождения чаще одного раза в четыре года,
    поэтому вырожденный случай вынесен отдельно: `replace(year=...)` на
    високосной дате дал бы `ValueError`, и тест падал бы раз в четыре года.
    """
    today = date.today()
    try:
        return today.replace(year=today.year - 18)
    except ValueError:
        # 29 февраля: годом рождения берём 28-е — возраст на границе тот же.
        return today.replace(year=today.year - 18, day=28)


async def test_age_is_computed_on_submitted_at_not_now(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-4 (FR126): заявка подана за день до 18-летия → ВНЕ рамок.

    DS заявил в стори, что это покрыто тестом «заявка в день 18-летия»;
    такого теста не существовало, а официальный мутатор дал `SURVIVED`
    на подмене `submitted_at` → `func.now()`. Здесь подмена краснеет: на
    `now()` возраст был бы 18 и заявитель попал бы В рамки.

    Краснеет при: `JoinRequest.submitted_at` → `func.now()` в предикате.
    """
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(seed, author_id=author_id, place_id=place_id, capacity=1)

        # Подал вчера (17 лет), сегодня исполняется 18.
        birthday_id = await _new_user(seed)
        await _profile(seed, user_id=birthday_id, birth_date=_birth_date_turning_18_today())
        await _request(seed, card_id=card_id, applicant_id=birthday_id, minutes_ago=60 * 24)
        await seed.commit()

        flags = await _predicate_flags(seed, card_id=card_id, age_min=18)

    assert len(flags) == 1, f"H-4: ожидалась одна заявка, получено {flags}"
    assert all(value is False for value in flags.values()), (
        f"H-4 (FR126): на момент подачи заявителю было 17, значит вне рамок; "
        f"предикат на now() дал бы true — получено {flags}"
    )


async def test_age_is_in_bounds_when_submitted_on_birthday(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пара к H-4 (P-21): подача В ДЕНЬ 18-летия — в рамках.

    Без пары предыдущий тест не отличает «считает на `submitted_at`» от
    «заявитель не проходит рамки никогда».
    """
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(seed, author_id=author_id, place_id=place_id, capacity=1)

        birthday_id = await _new_user(seed)
        await _profile(seed, user_id=birthday_id, birth_date=_birth_date_turning_18_today())
        await _request(seed, card_id=card_id, applicant_id=birthday_id, minutes_ago=1)
        await seed.commit()

        flags = await _predicate_flags(seed, card_id=card_id, age_min=18)

    assert len(flags) == 1, f"пара к H-4: ожидалась одна заявка, получено {flags}"
    assert all(value is True for value in flags.values()), (
        f"пара к H-4: подал в день 18-летия — в рамках, получено {flags}"
    )


# ——— H-5: граница равенства возрастных рамок включительна ———


async def test_age_min_boundary_is_inclusive(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-5 (P-21): `age == age_min` проходит, `age == age_min - 1` — нет.

    Замер CR: 11 тестов файла использовали 22 и 35, то есть граница
    `age_min=30` была пуста, и мутация `>=` → `>` выживала. Здесь пара
    возрастов стоит РОВНО на границе и строкой ниже.

    Краснеет при: `>=` → `>` в предикате `age_min`.
    """
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(seed, author_id=author_id, place_id=place_id, capacity=1)

        on_boundary_id = await _new_user(seed)
        await _profile(seed, user_id=on_boundary_id, age=30)
        on_boundary_request = await _request(seed, card_id=card_id, applicant_id=on_boundary_id)

        below_id = await _new_user(seed)
        await _profile(seed, user_id=below_id, age=29)
        below_request = await _request(seed, card_id=card_id, applicant_id=below_id)
        await seed.commit()

        flags = await _predicate_flags(seed, card_id=card_id, age_min=30)

    assert flags[on_boundary_request] is True, (
        f"H-5: ровно 30 при `age_min=30` проходит, получено {flags}"
    )
    assert flags[below_request] is False, f"H-5: 29 при `age_min=30` не проходит, получено {flags}"


async def test_age_max_boundary_is_inclusive(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-5 (P-21): `age == age_max` проходит, `age == age_max + 1` — нет.

    Краснеет при: `<=` → `<` в предикате `age_max`.
    """
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(seed, author_id=author_id, place_id=place_id, capacity=1)

        on_boundary_id = await _new_user(seed)
        await _profile(seed, user_id=on_boundary_id, age=30)
        on_boundary_request = await _request(seed, card_id=card_id, applicant_id=on_boundary_id)

        above_id = await _new_user(seed)
        await _profile(seed, user_id=above_id, age=31)
        above_request = await _request(seed, card_id=card_id, applicant_id=above_id)
        await seed.commit()

        flags = await _predicate_flags(seed, card_id=card_id, age_max=30)

    assert flags[on_boundary_request] is True, (
        f"H-5: ровно 30 при `age_max=30` проходит, получено {flags}"
    )
    assert flags[above_request] is False, f"H-5: 31 при `age_max=30` не проходит, получено {flags}"


# ——— маркер самоисцеляется: снятие рамок и новая заявка ———


async def test_bounds_edit_clears_skip_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-1: автор снял/смягчил рамки — карточка возвращается воркеру.

    Без сброса маркера карточка осталась бы вне автовыбора НАВСЕГДА, и автор,
    расширивший рамки, не получил бы выбора вовсе — то есть фикс H-1 сам стал
    бы источником потери решения.

    Краснеет при: удалении сброса в `update_card`.
    """
    from app.schemas.event_card import EventCardUpdate
    from app.services.event_card_service import update_card

    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _card(
            seed, author_id=author_id, place_id=place_id, capacity=1, age_min=30
        )
        young_id = await _new_user(seed)
        await _profile(seed, user_id=young_id, age=22)
        await _request(seed, card_id=card_id, applicant_id=young_id)
        await seed.commit()

    await run_decisions(factory, now=moment + timedelta(minutes=1), limit=200)

    async with factory() as check:
        _, marked_at = await _card_status(check, card_id)
    assert marked_at is not None, "H-1: предусловие — карточка обязана быть помечена"

    # Автор снимает рамку.
    async with factory() as edit:
        await update_card(
            edit,
            author_id=author_id,
            card_uuid=uuid_lib.UUID(card_uuid),
            payload=EventCardUpdate(age_min=None),
            settings=_CardSettings(),
            now=moment + timedelta(minutes=2),
        )
        await edit.commit()

    async with factory() as check:
        _, after_edit = await _card_status(check, card_id)
        due = await EventCardRepository(check).due_decision_ids(
            now=moment + timedelta(minutes=3), limit=200
        )

    assert after_edit is None, "H-1: снятие рамок обязано снять маркер"
    assert any(row[0] == card_id for row in due), (
        "H-1: после снятия рамок карточка обязана вернуться в очередь воркера"
    )


async def test_new_request_clears_skip_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-1: новая заявка снимает маркер — заявитель может проходить рамки.

    **Сценарий — гонка, а не «после истечения».** Подача проверяет
    `expires_at <= now` и на истёкшем окне легально отвечает
    `CARD_NOT_ACCEPTING`, поэтому заявка физически не может прийти ПОСЛЕ
    метки. Реальный случай иной: заявка подана за миг до истечения, а
    воркер пометил карточку раньше, чем заявка закоммитилась. Такой
    заявитель может проходить рамки, и держать его вне очереди воркера до
    правки карточки значило бы терять выбор.

    Проверяется ровно проводка сброса: карточка помечена вручную (как это
    сделал бы воркер), затем подаётся заявка при ещё открытом окне.

    Краснеет при: удалении сброса в `submit_join_request`.
    """
    from app.services.join_request_service import submit_join_request

    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        # Окно ЕЩЁ ОТКРЫТО: подача разрешена (иначе `CARD_NOT_ACCEPTING`).
        card_uuid, card_id = await _card(
            seed,
            author_id=author_id,
            place_id=place_id,
            capacity=1,
            age_min=30,
            expires_minutes_ago=-10,
        )
        # Метку ставит воркер на предыдущем прогоне — моделируем это прямо.
        await seed.execute(
            text("UPDATE event_cards SET audience_skipped_at = :at WHERE id = :id"),
            {"at": moment - timedelta(minutes=1), "id": card_id},
        )
        adult_id = await _new_user(seed)
        await _profile(seed, user_id=adult_id, age=35)
        await seed.commit()

    async with factory() as check:
        _, marked_at = await _card_status(check, card_id)
    assert marked_at is not None, "H-1: предусловие — карточка обязана быть помечена"

    async with factory() as submit:
        await submit_join_request(
            submit,
            applicant_id=adult_id,
            card_uuid=uuid_lib.UUID(card_uuid),
            message="пойдём вместе",
            settings=_JoinRequestSettings(),
            now=moment,
        )

    async with factory() as check:
        _, after_submit = await _card_status(check, card_id)
        requests = (
            await check.execute(
                text("SELECT count(*) FROM join_requests WHERE card_id = :id"), {"id": card_id}
            )
        ).scalar_one()

    assert int(requests) == 1, f"H-1: предусловие — заявка обязана появиться, {requests}"
    assert after_submit is None, "H-1: новая заявка обязана снять маркер"


async def test_mark_audience_skipped_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR 12.6 H-1 (M11): маркер ставится CAS-ом, а не слепым `UPDATE`.

    Два прогона воркера могут взяться за одну карточку (два процесса, рестарт
    посреди пачки). Слепой `UPDATE` перезаписывал бы метку времени, и
    «сколько карточка ждёт» переставало бы быть фактом — а по этой метке
    решается, возвращать ли карточку в очередь. Поэтому маркер ставится только
    когда его нет (`WHERE audience_skipped_at IS NULL`), и второй вызов честно
    сообщает `False`.

    Краснеет при: снятии `audience_skipped_at IS NULL` из `WHERE`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author_id = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _card(seed, author_id=author_id, place_id=place_id, capacity=1)
        await seed.commit()

    async with factory() as first:
        put_first = await EventCardRepository(first).mark_audience_skipped(
            card_id=card_id, now=moment
        )
        await first.commit()

    async with factory() as second:
        marked_second = await EventCardRepository(second).mark_audience_skipped(
            card_id=card_id, now=moment + timedelta(minutes=5)
        )
        await second.commit()

    async with factory() as check:
        _, marked_at = await _card_status(check, card_id)

    assert put_first is True, "H-1: первый вызов обязан поставить маркер"
    assert marked_second is False, "H-1: второй вызов не должен считать себя постановщиком"
    assert marked_at == moment, (
        "H-1: метка не перезаписывается вторым прогоном (CAS), "
        f"ожидалось {moment}, получено {marked_at}"
    )
