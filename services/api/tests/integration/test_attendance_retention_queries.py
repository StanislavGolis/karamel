"""Запросы ретенции фактов явки (стори 10.6; Task 1.1–1.4, Task 5.1–5.2).

**Почему интеграционные.** Предмет проверки — поведение PostgreSQL:
`interval '6 months'`, `EXISTS`, `FOR UPDATE SKIP LOCKED`. Мок SQL не
проверяет ничего из этого: он вернул бы то, что в него положили.

**Почему граница срока считается запросом к БД.** Шесть КАЛЕНДАРНЫХ месяцев —
181–184 дня, а не константа (D-10.6-6). Граница берётся у PostgreSQL
(`CAST(:m AS timestamptz) - interval '6 months'`), а `±1 день` прибавляется
в Python: так проверяется СРАВНЕНИЕ кода, а не повторение его формулы.

**Почему `limit=1` в тесте 5.2.** С `limit=200` заклинивание (D-10.6-5) не
воспроизводится: обе карточки попадают в одну пачку, и «второй прогон берёт
B» выполняется даже без `EXISTS`. Мутация «убрать `EXISTS`» обязана покраснеть
именно на `limit=1`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repositories.attendance_retention import AttendanceRetentionRepository
from app.services.attendance_retention_service import purge_expired_attendance
from tests.integration.attendance_history_helpers import add_attendance, make_card, make_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")


async def _retention_boundary(session: AsyncSession, moment: datetime) -> datetime:
    """Граница «6 календарных месяцев назад» по часам PostgreSQL.

    Считается БД, а не Python: `timedelta(days=182)` — это ДРУГАЯ дата
    (181–184 дня в зависимости от месяцев), и тест на Python-арифметике
    подтвердил бы дефект, который призван поймать (D-10.6-6).
    """
    row = await session.execute(
        text("SELECT CAST(:m AS timestamptz) - interval '6 months'"), {"m": moment}
    )
    return row.scalar_one()


async def _card_at(
    session: AsyncSession, *, author_id: int, starts_at: datetime, participant_id: int
) -> int:
    """Карточка с заданным `starts_at` и одной строкой `attendance`."""
    _, card_id = await make_card(session, author_id=author_id, starts_at=starts_at)
    await add_attendance(session, card_id=card_id, participant_id=participant_id, status="pending")
    return card_id


# --- Task 1.1: выборка по сроку ---


async def test_expired_card_is_selected_and_fresh_is_not(session: AsyncSession) -> None:
    """AC-01, P-19: границы срока по ОБЕ стороны.

    `starts_at = момент - 6 мес - 1 день` → в выборке;
    `starts_at = момент - 6 мес + 1 день` → НЕ в выборке.

    Односторонняя проверка («старое удаляется») прошла бы и на предикате
    `starts_at < now() - interval '1 month'`: удалялось бы больше, чем
    разрешено NFR43, и тест молчал бы.
    """
    moment = datetime.now(UTC)
    boundary = await _retention_boundary(session, moment)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)

    expired_id = await _card_at(
        session,
        author_id=author.id,
        starts_at=boundary - timedelta(days=1),
        participant_id=participant.id,
    )
    fresh_id = await _card_at(
        session,
        author_id=author.id,
        starts_at=boundary + timedelta(days=1),
        participant_id=participant.id,
    )

    repo = AttendanceRetentionRepository(session)
    selected = await repo.select_expired_card_ids(moment=moment, limit=200)

    assert expired_id in selected
    assert fresh_id not in selected


# --- Task 1.2: EXISTS attendance ---


async def test_expired_card_without_attendance_is_not_selected(session: AsyncSession) -> None:
    """AC-03, D-10.6-5: карточка без строк `attendance` в выборку НЕ попадает.

    Без `EXISTS` очередь заклинивает: каждые 60 секунд воркер берёт те же
    пустые карточки, и до следующих дело не доходит. Это не идемпотентность,
    а остановка (тест 5.2 проверяет то же с другой стороны — через `limit`).
    """
    moment = datetime.now(UTC)
    boundary = await _retention_boundary(session, moment)
    author, _ = await make_user(session)
    _, empty_card_id = await make_card(
        session, author_id=author.id, starts_at=boundary - timedelta(days=10)
    )

    repo = AttendanceRetentionRepository(session)
    selected = await repo.select_expired_card_ids(moment=moment, limit=200)

    assert empty_card_id not in selected


# --- Task 1.3: limit ---


async def test_limit_is_applied(session: AsyncSession) -> None:
    """D-10.6-5: `limit` соблюдается — 3 просроченные карточки, `limit=2` → 2.

    Воркер обязан вернуть управление даже при тысячах кандидатов
    (`WORKER_BATCH_SIZE = 200`).
    """
    moment = datetime.now(UTC)
    boundary = await _retention_boundary(session, moment)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    for days_ago in (30, 20, 10):
        await _card_at(
            session,
            author_id=author.id,
            starts_at=boundary - timedelta(days=days_ago),
            participant_id=participant.id,
        )

    repo = AttendanceRetentionRepository(session)
    selected = await repo.select_expired_card_ids(moment=moment, limit=2)

    assert len(selected) == 2


# --- Task 1.4: FOR UPDATE SKIP LOCKED ---


async def test_skip_locked_does_not_wait_for_locked_card() -> None:
    """D-10.6-5, NFR46–NFR49: второй воркер берёт ДРУГУЮ карточку, а не ждёт.

    Гонку в одной сессии доказать нельзя (§18.3): второй запрос не блокируется
    блокировкой, которую держит та же транзакция. Поэтому здесь собственный
    движок и НАСТОЯЩИЕ коммиты, а не фикстура `session` (§18.8).

    Мутация «убрать `SKIP LOCKED`» обязана покраснеть: второй запрос встанет
    в ожидание блокировки первой сессии и не вернёт B. Ожидание ограничено
    таймаутом, а `TimeoutError` превращается в `None` — чтобы текст падения
    был утверждением, а не техническим исключением (P-21).
    """
    moment = datetime.now(UTC)
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    created: list[int] = []
    try:
        async with factory() as seed:
            boundary = await _retention_boundary(seed, moment)
            author, _ = await make_user(seed)
            participant, _ = await make_user(seed)
            card_a = await _card_at(
                seed,
                author_id=author.id,
                starts_at=boundary - timedelta(days=20),
                participant_id=participant.id,
            )
            card_b = await _card_at(
                seed,
                author_id=author.id,
                starts_at=boundary - timedelta(days=10),
                participant_id=participant.id,
            )
            created = [card_a, card_b]
            await seed.commit()

        async with factory() as first, factory() as second:
            repo_first = AttendanceRetentionRepository(first)
            repo_second = AttendanceRetentionRepository(second)
            held = await repo_first.select_expired_card_ids(moment=moment, limit=1)
            assert held == [card_a], "первая сессия обязана взять самую старую карточку"
            try:
                other = await asyncio.wait_for(
                    repo_second.select_expired_card_ids(moment=moment, limit=1), timeout=3
                )
            except TimeoutError:
                other = None
            await first.rollback()
            await second.rollback()

        assert other == [card_b], (
            "вторая сессия должна взять B, не дожидаясь освобождения A — "
            "иначе FOR UPDATE SKIP LOCKED потерян"
        )
    finally:
        if created:
            async with factory() as cleanup:
                await cleanup.execute(
                    text("DELETE FROM attendance WHERE card_id = ANY(:ids)"), {"ids": created}
                )
                await cleanup.execute(
                    text("DELETE FROM event_cards WHERE id = ANY(:ids)"), {"ids": created}
                )
                await cleanup.commit()
        await engine.dispose()


# --- Task 5.1, 5.2: идемпотентность и отсутствие заклинивания ---


async def test_second_run_deletes_nothing(session: AsyncSession) -> None:
    """AC-03: второй прогон на тех же данных → `deleted = 0`, без исключений.

    Идемпотентность держится `EXISTS attendance`: после первого прогона
    карточек с непустым `attendance` не остаётся, и выборка пуста.
    """
    moment = datetime.now(UTC)
    boundary = await _retention_boundary(session, moment)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    await _card_at(
        session,
        author_id=author.id,
        starts_at=boundary - timedelta(days=10),
        participant_id=participant.id,
    )

    first = await purge_expired_attendance(session, moment=moment, limit=200)
    second = await purge_expired_attendance(session, moment=moment, limit=200)

    assert first.deleted == 1
    assert second.deleted == 0


async def test_second_run_moves_to_next_card_with_limit_one(session: AsyncSession) -> None:
    """D-10.6-5: `limit=1` → первый прогон A, второй B, а НЕ A снова.

    Это тест заклинивания, а не идемпотентности: без `EXISTS` второй прогон
    снова выбрал бы A (её `starts_at` всё ещё старый), удалил бы 0 строк и
    ВЕЧНО ходил бы по той же карточке.
    """
    moment = datetime.now(UTC)
    boundary = await _retention_boundary(session, moment)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    card_a = await _card_at(
        session,
        author_id=author.id,
        starts_at=boundary - timedelta(days=20),
        participant_id=participant.id,
    )
    card_b = await _card_at(
        session,
        author_id=author.id,
        starts_at=boundary - timedelta(days=10),
        participant_id=participant.id,
    )

    repo = AttendanceRetentionRepository(session)
    first_batch = await repo.select_expired_card_ids(moment=moment, limit=1)
    assert first_batch == [card_a]

    await purge_expired_attendance(session, moment=moment, limit=1)

    second_batch = await repo.select_expired_card_ids(moment=moment, limit=1)
    assert second_batch == [card_b], (
        "второй прогон обязан продвинуться к B — иначе карточка без attendance "
        "заклинивает очередь (EXISTS потерян)"
    )
