"""Напоминание автору о приближении окна (стори 5б.4; AC-01…AC-08).

Реальный PostgreSQL, схема поднята Alembic (`0013`). Главные утверждения:

- **идемпотентность** (AC-03, ADR-15): маркер `decision_reminded_at` — CAS
  по `NULL`; повторный прогон и рестарт воркера не создают второго события;
- **границы** (AC-01, AC-05, D-5b.4-2): окно `(now, now+порог]` — строго
  слева, включительно справа; ровно `expires_at == now` напоминания НЕ даёт;
- **статус** (AC-04): `full`/`closed`/`cancelled` напоминания не получают;
- **частичный сбой** (AC-06): падение одной карточки не отменяет остальные;
- **порядок шагов** (AC-07): напоминание раньше автовыбора, иначе FR103
  недостижим ровно в сценарии, ради которого существует;
- **безличность** (AC-08, FR81): в событиях нет `author_id`/`applicant_id`.

§18.3: `now` — параметр везде; `sleep` и реальное время в тестах не участвуют.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.card_reminder_service import ReminderOutcome, remind_due_decision
from app.workers.card_windows import run_once
from app.workers.decision_reminders import run_reminders

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
THRESHOLD = 15

# X-5a.6-3: тесты коммитят ПО-НАСТОЯЩЕМУ (собственный движок), и без уборки
# их строки остаются в БД до конца сессии, ломая посторонние `count(*)`.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


# ——— фикстуры —————————————————————————————————————————————————————————————


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
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям."""
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
    expires_at: datetime | None = None,
    reminded_at: datetime | None = None,
) -> tuple[str, int]:
    """Карточка с ЯВНЫМ `expires_at` — границы окна задаются точно."""
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    expires = expires_at if expires_at is not None else now + timedelta(minutes=-5)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, decision_reminded_at,"
            "kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at, :reminded,"
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
            "reminded": reminded_at,
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, submitted_at: datetime | None = None
) -> tuple[str, int]:
    moment = submitted_at if submitted_at is not None else datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, '', 'pending', :submitted, "
            "now() + interval '1 day') RETURNING uuid, id"
        ),
        {"card": card_id, "applicant": applicant_id, "submitted": moment},
    )
    request_uuid, request_id = row.one()
    return str(request_uuid), int(request_id)


async def _reminded_at(session: AsyncSession, card_id: int) -> Any:
    row = await session.execute(
        text("SELECT decision_reminded_at FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    return row.scalar_one()


# ——— AC-01: напоминание уходит в окне (now, now+порог] ————————————————


async def test_reminder_sent_inside_window(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01 (FR103): за 10 минут до `expires_at` при пороге 15 — напоминаем.

    Краснеет, если предикат окна сузить или порог проигнорировать.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=10),
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    # 9.4 (Task 1.5): исход напоминания 5б.4 не изменился; доставка FR110
    # считается ОТДЕЛЬНО (`notified`/`notify_failed`), поэтому проверяются
    # обе группы — смысл ассерта сохранён дословно.
    assert counters["reminded"] == 1
    assert counters["skipped"] == 0
    assert counters["failed"] == 0
    assert counters["notified"] == 1
    assert counters["notify_failed"] == 0
    async with factory() as check:
        assert await _reminded_at(check, card_id) is not None
        del card_uuid


async def test_reminder_not_sent_before_window(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01: за 2 часа — рано, порог 15 минут.

    Краснеет, если снять верхнюю границу `expires_at <= now + порог`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(hours=2),
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert counters["reminded"] == 0
    async with factory() as check:
        assert await _reminded_at(check, card_id) is None


async def test_reminder_right_bound_is_inclusive(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01, D-5b.4-2 (M4): ровно за порог минут — УЖЕ напоминаем.

    Краснеет при `<=` → `<` в выборке кандидатов И в ядре. Точное равенство
    достигается передачей одного и того же `moment` в данные и в прогон:
    окно строится как `moment + 15 минут`, а не «примерно за 15».
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=THRESHOLD),
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert counters["reminded"] == 1
    async with factory() as check:
        assert await _reminded_at(check, card_id) is not None


async def test_reminder_left_bound_is_strict(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05 (M3): `expires_at == now` — окно истекло, напоминания НЕТ.

    Краснеет при `>` → `>=` в выборке кандидатов И в ядре: карточка получила
    бы «успей выбрать» одновременно с автовыбором 5б.3.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed, author_id=author.id, place_id=place_id, expires_at=moment
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert counters["reminded"] == 0
    async with factory() as check:
        assert await _reminded_at(check, card_id) is None


# ——— AC-03: идемпотентность —————————————————————————————————————————————


async def test_reminder_is_idempotent_across_runs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03 (M1, M2): второй прогон не напоминает повторно.

    Краснеет при снятии `decision_reminded_at IS NULL` из выборки (M2) или
    из CAS (M1): счётчик `reminded` рос бы каждый цикл воркера.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.commit()

    first = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)
    second = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert first["reminded"] == 1
    assert second["reminded"] == 0
    async with factory() as check:
        marker = await _reminded_at(check, card_id)
        assert marker is not None


async def test_reminder_cas_returns_skipped_on_marker_race(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03, ADR-15 (M1): маркер поставлен МЕЖДУ чтением состояния и CAS.

    Детерминированно воспроизводит гонку двух прогонов: `reminder_state`
    подменён так, что возвращает `decision_reminded_at = NULL` ПРИ РЕАЛЬНО
    СТОЯЩЕМ маркере в БД. Значит проверки шагов 2–5 проходят, и решение
    принимает только CAS.

    Краснеет, если из `build_mark_reminded_statement` убрать
    `WHERE decision_reminded_at IS NULL`: безусловный `UPDATE` вернул бы
    строку, сервис отрапортовал бы `REMINDED`, и второе напоминание ушло бы.
    Без такой подмены гонку не воспроизвести: штатная проверка шага 3
    отсекает карточку раньше CAS, и мутация осталась бы зелёной (класс
    эквивалентности, а не покрытие).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
            reminded_at=moment - timedelta(seconds=30),
        )
        await seed.commit()

    from app.repositories.event_cards import EventCardRepository

    # Оригинал захватывается ДО подмены: после `setattr` атрибут класса уже
    # указывает на `_stale_state`, и вызов через класс дал бы рекурсию
    # (воспроизведено первым прогоном: `RecursionError`).
    original_state = EventCardRepository.reminder_state

    async def _stale_state(self: EventCardRepository, *, card_id: int) -> Any:
        real = await original_state(self, card_id=card_id)
        assert real is not None
        # «Прочитали до того, как другой прогон поставил маркер».
        # 9.4 (R2): `reminder_state` отдаёт шесть полей; подмена сохраняет
        # смысл 5б.4 дословно — маркер «не стоит», адрес автора не меняется.
        return (real[0], real[1], real[2], None, real[4], real[5])

    monkeypatch.setattr(EventCardRepository, "reminder_state", _stale_state)

    async with factory() as session:
        result = await remind_due_decision(
            session, card_id=card_id, now=moment, threshold_minutes=THRESHOLD
        )
        await session.commit()

    assert result.outcome is ReminderOutcome.SKIPPED


# ——— AC-04: статус карточки —————————————————————————————————————————————


@pytest.mark.parametrize("status", ["full", "closed", "cancelled", "finished", "draft"])
async def test_reminder_skipped_for_non_published(
    factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    """AC-04 (M5): автор уже выбрал / окно закрыто / карточка отменена.

    Краснеет при снятии `status='published'` из предиката: автор получил бы
    «успей выбрать» по карточке, где решение уже принято.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        filled = 1 if status == "full" else 0
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            status=status,
            filled_count=filled,
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert counters["reminded"] == 0
    async with factory() as check:
        assert await _reminded_at(check, card_id) is None


# ——— AC-06: частичный сбой ———————————————————————————————————————————————


async def test_partial_failure_does_not_stop_batch(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-06 (M6): падение одной карточки не отменяет остальные.

    Краснеет при снятии `try/except` вокруг карточки: исключение на первой
    убило бы весь прогон, и вторая карточка не получила бы напоминания.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, broken_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=3),
        )
        _, ok_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=4),
        )
        await seed.commit()

    from app.services import card_reminder_service as svc

    original = svc.remind_due_decision

    async def _flaky(session: AsyncSession, **kwargs: Any) -> Any:
        if kwargs["card_id"] == broken_id:
            raise RuntimeError("сбой на одной карточке")
        return await original(session, **kwargs)

    monkeypatch.setattr("app.workers.decision_reminders.remind_due_decision", _flaky)

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert counters["failed"] == 1
    assert counters["reminded"] == 1
    async with factory() as check:
        assert await _reminded_at(check, broken_id) is None
        assert await _reminded_at(check, ok_id) is not None


# ——— AC-07: порядок шагов ————————————————————————————————————————————————


async def test_reminder_runs_before_autoselect_in_run_once(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-07 (M7): в `card_windows.run_once` напоминание вызвано ДО решений.

    Окна не пересекаются (`> now` против `<= now`), поэтому перестановка шагов
    НЕ видна по статусам карточек: тест, сравнивающий только `reminded`
    и маркеры, остался бы зелёным при мутации M7 (проверено прогоном —
    первая версия теста именно так и повела себя).

    Поэтому контракт доказывается ПЕРЕХВАТОМ ПОСЛЕДОВАТЕЛЬНОСТИ вызовов —
    тот же приём, что `before_cursor_execute` для порядка блокировок (ADR-09):
    наблюдаемо только фактическое выполнение, а не результат.

    Краснеет от M7 (перенос `run_reminders` после `run_decisions`): порядок
    в списке станет `[decisions, reminders]`. Почему порядок важен по сути:
    решения — первый шаг, который может изменить статус карточки, и любой
    будущий триггер напоминания (эпик 6) обязан видеть карточку до закрытия
    окна, иначе FR103 теряется на границе.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, in_window_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        _, expired_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment - timedelta(minutes=1),
        )
        await seed.commit()

    order: list[str] = []

    from app.workers import card_windows as windows_module

    original_reminders = windows_module.run_reminders
    original_decisions = windows_module.run_decisions
    # Стори 7.3 (Task 4.6, D-TD-6): `run_once` получил четвёртый шаг —
    # запрос подтверждения явки. Замокан, чтобы тест 5б.4 не лез в БД чужой
    # стори: его контракт — порядок напоминания и решений, а порядок
    # `prompts` после `finish_started` доказывает отдельный тест S41.
    original_prompts = windows_module.run_attendance_prompts

    async def _reminders(*args: Any, **kwargs: Any) -> Any:
        order.append("reminders")
        return await original_reminders(*args, **kwargs)

    async def _decisions(*args: Any, **kwargs: Any) -> Any:
        order.append("decisions")
        return await original_decisions(*args, **kwargs)

    async def _prompts(*args: Any, **kwargs: Any) -> Any:
        order.append("prompts")
        return {"prompted": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(windows_module, "run_reminders", _reminders)
    monkeypatch.setattr(windows_module, "run_decisions", _decisions)
    monkeypatch.setattr(windows_module, "run_attendance_prompts", _prompts)
    del original_prompts

    counters = await run_once(factory, now=moment, reminder_threshold_minutes=THRESHOLD)

    # Стори 7.3 (Task 4.6): новый шаг входит в последовательность последним.
    assert order == ["reminders", "decisions", "prompts"]
    assert counters["reminded"] == 1
    async with factory() as check:
        assert await _reminded_at(check, in_window_id) is not None
        assert await _reminded_at(check, expired_id) is None


# ——— CR 5б.4: CAS не ставит маркер на закрытую карточку ————————————————


async def test_cas_does_not_mark_card_closed_after_state_read(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04, AC-05 (CR 5б.4, патч 1): карточку закрыли МЕЖДУ чтением и CAS.

    Дефект, доказанный исполнением до патча: `build_mark_reminded_statement`
    содержал только `decision_reminded_at IS NULL`, без статуса. Гонка
    «прочитали `published` → автор выбрал/автовыбор закрыл → CAS» ставила
    маркер на `closed` и выдавала `REMINDED`, то есть событие «окно
    истекает» уходило ПОСЛЕ истечения (AC-05) и по карточке с уже принятым
    решением (AC-04). Краснеет при снятии `status='published'` из CAS.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            status="closed",
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.commit()

    from app.repositories.event_cards import EventCardRepository

    original = EventCardRepository.reminder_state

    async def _stale_published(self: EventCardRepository, *, card_id: int) -> Any:
        real = await original(self, card_id=card_id)
        assert real is not None
        # «Прочитали до того, как карточку закрыли».
        # 9.4 (R2): шесть полей; «прочитали до закрытия» — статус published.
        return (real[0], "published", real[2], None, real[4], real[5])

    monkeypatch.setattr(EventCardRepository, "reminder_state", _stale_published)

    async with factory() as session:
        result = await remind_due_decision(
            session, card_id=card_id, now=moment, threshold_minutes=THRESHOLD
        )
        await session.commit()

    assert result.outcome is ReminderOutcome.SKIPPED
    async with factory() as check:
        assert await _reminded_at(check, card_id) is None


# ——— CR 5б.4: наблюдаемые поля лога и самодостаточность ядра ————————


async def test_reminder_reports_remaining_seconds(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01 (CR 5б.4, патч 4): `remaining_seconds` — факт, а не decoration.

    До патча ни один тест не читал это поле: значение можно было заменить
    на `0` (мутация Ma) и всё оставалось зелёным, хотя эпик 6 соберёт по
    нему текст «осталось N минут». Событие лога обязано нести ровно
    `expires_at - now`.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=7),
        )
        await seed.commit()

    captured: list[Any] = []

    class _Capture:
        def __call__(self, logger: Any, method_name: str, event_dict: Any) -> Any:
            captured.append(dict(event_dict))
            return event_dict

    structlog.configure(processors=[_Capture(), *structlog.get_config()["processors"]])
    try:
        async with factory() as session:
            result = await remind_due_decision(
                session, card_id=card_id, now=moment, threshold_minutes=THRESHOLD
            )
            await session.commit()
    finally:
        structlog.reset_defaults()

    assert result.outcome is ReminderOutcome.REMINDED
    assert result.remaining_seconds == 7 * 60
    events = [e for e in captured if e.get("event") == "worker.card.decision.reminder"]
    assert len(events) == 1
    assert events[0]["remaining_seconds"] == 7 * 60


async def test_service_is_self_sufficient_without_candidate_selection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01, AC-05 (CR 5б.4, патч 5): ядро проверяет окно САМО.

    Докстринг сервиса обещает «ядро самодостаточно — его можно звать без
    предварительной выборки». До патча это было неправдой: снятие проверки
    `expires_at <= now` (мутация Me) оставляло все тесты зелёными, потому
    что единственным гардом истёкшего окна была выборка кандидатов.
    Здесь сервис зовут НАПРЯМУЮ по просроченной карточке — без `run_reminders`.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        _, card_id = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment - timedelta(seconds=1),
        )
        await seed.commit()

    async with factory() as session:
        result = await remind_due_decision(
            session, card_id=card_id, now=moment, threshold_minutes=THRESHOLD
        )
        await session.commit()

    assert result.outcome is ReminderOutcome.SKIPPED
    async with factory() as check:
        assert await _reminded_at(check, card_id) is None


async def test_reminder_respects_candidate_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """NFR20 (CR 5б.4, патч 6): `limit` ограничивает прогон, а не игнорируется.

    До патча параметр не проверялся ни одним тестом: мутация «передать
    `limit=10000`» (Md) выживала. Без ограничения воркер на тысячах
    карточек не вернул бы управление и не записал длительность.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        for offset in (1, 2, 3):
            await _insert_card(
                seed,
                author_id=author.id,
                place_id=place_id,
                expires_at=moment + timedelta(minutes=offset),
            )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD, limit=2)

    assert counters["reminded"] == 2


# ——— AC-08: безличность —————————————————————————————————————————————————


async def test_reminder_logs_do_not_leak_user_identity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-08 (FR81): событие содержит `card_uuid`, но не `author_id`.

    Краснеет при добавлении `author_id` или `applicant_id` в событие:
    безличность 5б.2 запрещает выводить идентификаторы пользователей
    в логи воркеров.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, _ = await _insert_card(
            seed,
            author_id=author.id,
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        author_id = int(author.id)
        await seed.commit()

    captured: list[Any] = []

    class _Capture:
        def __call__(self, logger: Any, method_name: str, event_dict: Any) -> Any:
            captured.append(dict(event_dict))
            return event_dict

    structlog.configure(processors=[_Capture(), *structlog.get_config()["processors"]])
    try:
        await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)
    finally:
        structlog.reset_defaults()

    events = [e for e in captured if e.get("event") == "worker.card.decision.reminder"]
    assert len(events) == 1
    assert events[0]["card_uuid"] == card_uuid
    for event_dict in captured:
        assert "author_id" not in event_dict
        assert "applicant_id" not in event_dict
        assert "user_id" not in event_dict
        # `detail` — единственное свободное поле события (путь ошибки):
        # идентификатор автора не должен утекать и туда. Подстроку в `card_uuid`
        # не проверяем: короткое число вроде `14` встречается в hex-представлении
        # любого uuid, и ассерт был бы ложно-красным (дефект первого прогона).
        assert str(author_id) not in str(event_dict.get("detail", ""))
