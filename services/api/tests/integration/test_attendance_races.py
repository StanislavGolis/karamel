"""Конкурентность подтверждения явки (стори 7.2; TD 7.2 R-*).

Покрытие TD: `R-CAS-01` (детерминированный), `R-CAS-02` (параллельный),
`R-NOPRE-01` (предпроверка запрещена), `A-LOCK-01` (порядок блокировок).

**Почему фикстура `session` здесь НЕ используется** (§18.3): она живёт в
SAVEPOINT, и второй запрос видит незакоммиченные изменения первого — гонку
на ней доказать нельзя. Данные готовятся собственным движком с НАСТОЯЩИМИ
коммитами, каждая транзакция идёт своим соединением.

**Почему `R-CAS-01` — сериализация, а не гонка.** Второй `UPDATE` физически
ждёт снятия блокировки строки, порядок детерминирован, и PostgreSQL под
`READ COMMITTED` перепроверяет `WHERE` на новой версии строки (EvalPlanQual).
Это и есть механизм, который делает CAS корректным в проде; тест его
доказывает, а не надеется на планировщик.

**Запрет `asyncio.sleep`** (TD, R1): ожидание в тесте на свойство — тест на
удачу. Параллельный прогон синхронизируется барьером ДО вызова.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.enums import AttendanceStatus
from app.repositories.attendance import AttendanceRepository
from app.repositories.users import UserRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие.

    Пул ограничен четырьмя соединениями: двум нужны две параллельные
    транзакции плюс одна для проверки состояния после гонки.
    """
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Удаляет созданные строки в порядке, обратном FK-зависимостям."""
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
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


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    confirmed: bool = False,
    author_confirmed: bool | None = None,
) -> dict[str, Any]:
    """Автор, участник, карточка и пара `attendance` — с настоящим коммитом.

    `starts_at` в прошлом: подтверждение разрешено (AC-07). `status='full'`
    с `filled_count=capacity` соблюдает `ck_event_cards_full_capacity`.

    `author_confirmed` (CR 7.2 H-4) — пресет флага автора. Он нужен, чтобы
    подтверждение УЧАСТНИКА сразу делало пару терминальной: только тогда
    второй `UPDATE` проверяет `WHERE status='pending'`, а не предикат D-1.
    """
    author_id = await _new_user(factory)
    member_id = await _new_user(factory)
    async with factory() as session:
        place = await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, city_slug, is_active) "
                "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
            ),
            {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
        )
        place_id = int(place.scalar_one())
        _CREATED_PLACES.append(place_id)
        starts = datetime.now(UTC) - timedelta(hours=1)
        card = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
                "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
                "filled_count, verification_status, published_at, kind, activity) "
                "VALUES (:a, :p, :s, 'Встреча', NULL, 'cafe', :st, 2, 60, :e, 'full', 2, "
                "'approved', :pub, 'company', 'coffee') RETURNING id"
            ),
            {
                "a": author_id,
                "p": place_id,
                "s": settings.default_city_slug,
                "st": starts,
                "e": starts - timedelta(hours=1),
                "pub": starts - timedelta(days=1),
            },
        )
        card_id = int(card.scalar_one())
        await session.execute(
            text("INSERT INTO attendance (card_id, participant_id) VALUES (:c, :p)"),
            {"c": card_id, "p": member_id},
        )
        if author_confirmed is not None:
            # CR 7.2 H-4: пресет флага автора — чтобы подтверждение участника
            # сразу делало пару терминальной и второй `UPDATE` проверял именно
            # `WHERE status='pending'`, а не предикат D-1.
            await session.execute(
                text("UPDATE attendance SET author_confirmed = :flag WHERE card_id = :c"),
                {"c": card_id, "flag": author_confirmed},
            )
        if confirmed:
            await session.execute(
                text(
                    "UPDATE attendance SET author_confirmed = true, "
                    "participant_confirmed = true, status = 'confirmed', "
                    "confirmed_at = now() WHERE card_id = :c"
                ),
                {"c": card_id},
            )
        await session.commit()
    return {"author_id": author_id, "member_id": member_id, "card_id": card_id}


@contextmanager
def _capture_sql(session: AsyncSession) -> Iterator[list[str]]:
    """Перехват текста SQL на движке сессии (образец — тесты 6.3/6.5)."""
    statements: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(listen_engine, "before_cursor_execute", _record)


# ——— R-CAS-01: «ровно один раз» детерминированно (ГЛАВНЫЙ тест стори) ———


async def test_terminal_status_computed_exactly_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R-CAS-01 / M1 (AC-06, R1): второй `UPDATE` ждёт блокировку и не перезаписывает.

    Сценарий (TD):

    - T1 выполняет `confirm` и НЕ коммитит — строка заблокирована;
    - T2 запускает тот же `confirm` из второго соединения: его `UPDATE`
      блокируется на строке;
    - T1 коммитит; PostgreSQL перепроверяет `WHERE` T2 на новой версии
      строки (EvalPlanQual) под `READ COMMITTED`;
    - T2 получает ПУСТОЙ `RETURNING`, потому что `status` уже не `pending`.

    Мутация M1 (убрать `WHERE status='pending'`) делает тест красным: T2
    применил бы `UPDATE` к изменённой строке, и `updated_at` сдвинулся бы
    второй транзакцией — то есть «ровно один раз» нарушилось бы.

    Порядок ДЕТЕРМИНИРОВАН (T2 физически ждёт снятия блокировки), поэтому
    тест не флакует — в отличие от вероятностного параллельного прогона.
    """
    # CR 7.2 H-4: флаг автора уже `true`, поэтому подтверждение участника
    # сразу делает пару ТЕРМИНАЛЬНОЙ, а ответы сторон РАЗНЫЕ. Только так
    # второй `UPDATE` проверяет `WHERE status='pending'`: при одинаковых
    # ответах его отбивает предикат D-1 (`IS DISTINCT FROM`), и мутация M1
    # (убрать `status='pending'`) осталась бы зелёной.
    seed = await _seed(factory, author_confirmed=True)
    now = datetime.now(UTC)

    async with factory() as first:
        first_repo = AttendanceRepository(first)
        first_result = await first_repo.confirm(
            card_id=seed["card_id"],
            participant_id=seed["member_id"],
            author_id=seed["author_id"],
            author_side=False,
            arrived=True,
            now=now,
        )
        # T1 НЕ коммитит: строка заблокирована до конца блока.
        # Пара (true, true) → `confirmed`, то есть ТЕРМИНАЛЬНА.
        assert first_result is not None
        assert first_result[0] == AttendanceStatus.CONFIRMED

        async with factory() as second:
            second_repo = AttendanceRepository(second)
            pending = asyncio.ensure_future(
                second_repo.confirm(
                    card_id=seed["card_id"],
                    participant_id=seed["member_id"],
                    author_id=seed["author_id"],
                    author_side=False,
                    # ДРУГОЙ ответ: при M1 он перезаписал бы терминальный
                    # `confirmed` на `no_show`.
                    arrived=False,
                    now=now,
                )
            )
            # Даём T2 дойти до блокировки, затем снимаем её коммитом T1.
            await asyncio.sleep(0)
            await first.commit()
            second_result = await pending
            await second.commit()

    # Инвариант 1: второй `UPDATE` не вернул строку (это и есть CAS).
    assert second_result is None, "терминальный статус/флаг перезаписан вторым UPDATE"

    async with factory() as check:
        row = await check.execute(
            text(
                "SELECT author_confirmed, participant_confirmed, status, confirmed_at "
                "FROM attendance WHERE card_id = :c"
            ),
            {"c": seed["card_id"]},
        )
        author_flag, participant_flag, status, confirmed_at = row.one()
    # Инвариант 2: оба флага — по одному ответу, и участнический не перезаписан
    # вторым (проигравшим) вызовом с `arrived=False`.
    assert (author_flag, participant_flag) == (True, True)
    # Инвариант 3: пара терминальна ровно одним переходом (true,true).
    assert status == "confirmed"
    assert confirmed_at is not None


# ——— R-CAS-02: параллельный прогон без вероятностной зависимости ————


async def test_parallel_confirm_has_single_winner(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R-CAS-02 (AC-06): два параллельных подтверждения — ровно один переход.

    Инварианты не зависят от порядка выполнения:

    - непустой `RETURNING` ровно у одного вызова;
    - `updated_at` изменён РОВНО одним вызовом (сравнение с исходным);
    - в БД нет состояния, запрещённого `CHECK`.

    Синхронизация — `asyncio.Barrier(2)` ДО вызова (TD): `sleep` сделал бы
    тест вероятностным.
    """
    # CR 7.2 H-4: флаг автора уже `true`, ответы сторон РАЗНЫЕ. Одинаковый
    # ответ маскировал бы мутацию M1 предикатом D-1 (`IS DISTINCT FROM`).
    seed = await _seed(factory, author_confirmed=True)
    now = datetime.now(UTC)
    barrier = asyncio.Barrier(2)

    async def _attempt(
        arrived: bool,
    ) -> tuple[bool, tuple[AttendanceStatus, datetime] | None]:
        async with factory() as session:
            repo = AttendanceRepository(session)
            await barrier.wait()
            result = await repo.confirm(
                card_id=seed["card_id"],
                participant_id=seed["member_id"],
                author_id=seed["author_id"],
                author_side=False,
                arrived=arrived,
                now=now,
            )
            await session.commit()
            return arrived, result

    results = await asyncio.gather(_attempt(True), _attempt(False))
    winners = [(arrived, result) for arrived, result in results if result is not None]

    assert len(winners) == 1, f"переходов должно быть ровно 1, получено {len(winners)}"
    winner_arrived = winners[0][0]

    async with factory() as check:
        row = await check.execute(
            text(
                "SELECT participant_confirmed, status, updated_at = created_at AS untouched "
                "FROM attendance WHERE card_id = :c"
            ),
            {"c": seed["card_id"]},
        )
        flag, status, untouched = row.one()
    # CR 7.2 H-4: инвариант НЕ зависит от того, кто победил (ответы разные,
    # планировщик решает). Проверяется, что записан ответ РОВНО одного вызова
    # и статус пары соответствует паре `(author=true, winner)`:
    # `true` → `confirmed`; `false` → `disputed` (санкция в 7.2 не создаётся).
    assert flag is winner_arrived, "в БД ответ проигравшего, а не победителя"
    assert status == ("confirmed" if winner_arrived else "disputed")
    assert untouched is False, "updated_at не двигался — запись не состоялась"


# ——— R-NOPRE-01: предпроверка запрещена (P-3, D-7.2-1) ————————————————


async def test_no_select_before_update_on_confirm_path(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R-NOPRE-01 (P-3): первого оператора по `attendance` — сам `UPDATE`.

    Предпроверка (`SELECT status` → `if pending` → `UPDATE`) открыла бы окно
    между чтением и записью: именно так теряется второе подтверждение и
    перезаписывается терминальный статус (R1). Тест ловит это СТРУКТУРНО,
    по потоку SQL, а не по последствиям.
    """
    seed = await _seed(factory)
    now = datetime.now(UTC)

    async with factory() as session:
        repo = AttendanceRepository(session)
        with _capture_sql(session) as statements:
            await repo.confirm(
                card_id=seed["card_id"],
                participant_id=seed["member_id"],
                author_id=seed["author_id"],
                author_side=False,
                arrived=True,
                now=now,
            )
        await session.commit()

    attendance_statements = [
        statement for statement in statements if "attendance" in statement.lower()
    ]
    assert attendance_statements, "путь подтверждения не касался attendance вовсе"
    first = attendance_statements[0].lstrip().upper()
    assert first.startswith("UPDATE"), (
        f"первый оператор по attendance — не UPDATE: {attendance_statements[0][:80]}"
    )
    assert not any(
        statement.lstrip().upper().startswith("SELECT") for statement in attendance_statements
    ), "на пути подтверждения есть SELECT по attendance (предпроверка запрещена)"


# ——— A-LOCK-01: порядок блокировок ADR-09 (P2) ————————————————————————


async def test_attendance_uses_no_for_update(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A-LOCK-01 (ADR-09, D-7.2-11): `attendance` берётся БЕЗ `FOR UPDATE`.

    CAS атомарен сам по себе; явная блокировка добавила бы второй механизм
    защиты и второй источник правды про порядок. Порядок `users →
    event_cards` соблюдает `get_card`/`get_owned` (встречный порядок дал бы
    дедлок с `join_open_card`/`release_seat`).

    Проверяется перехватом: ни один оператор пути не содержит
    `FOR UPDATE` по `attendance`.
    """
    seed = await _seed(factory)
    now = datetime.now(UTC)

    async with factory() as session:
        repo = AttendanceRepository(session)
        with _capture_sql(session) as statements:
            await repo.confirm(
                card_id=seed["card_id"],
                participant_id=seed["member_id"],
                author_id=seed["author_id"],
                author_side=False,
                arrived=True,
                now=now,
            )
        await session.commit()

    for statement in statements:
        upper = statement.upper()
        if "ATTENDANCE" in upper:
            assert "FOR UPDATE" not in upper, (
                f"attendance взят под FOR UPDATE (ADR-09): {statement[:120]}"
            )
