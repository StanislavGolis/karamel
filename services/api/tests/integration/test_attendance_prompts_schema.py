"""Предикат воркера = предикат индекса (стори 7.3; AC-13).

**Почему НЕ `EXPLAIN` как основной оракул** (R1, TD 7.3 §7). На 20 строках
Postgres выбирает seq scan при ПРАВИЛЬНОМ коде (дешевле), а при
`enable_seqscan = off` покажет индекс и при НЕПРАВИЛЬНОМ (предикат не совпал,
но индекс всё равно пригоден). Более того: `= ANY(ARRAY[...])` эквивалентно
`IN (...)` для оптимизатора, поэтому `EXPLAIN` НЕ ловит подмену формы.

Основной оракул — сравнение ИСТОЧНИКОВ: текст `pg_indexes.indexdef` (что
лежит в БД) с кортежем `ATTENDANCE_PROMPTABLE_STATUSES` (что читает Python).
Расхождение = карточки молча выпадают из выборки → пропуск факта явки.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import ATTENDANCE_PROMPTABLE_STATUSES, EventCardStatus
from app.repositories.event_cards import EventCardRepository
from tests.integration.attendance_prompt_helpers import _cleanup_seed, factory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]


#: Хелпер: фактический кортеж значений предиката `status IN (...)`.
#:
#: Сравнивать ТЕКСТ запроса нельзя: SQLAlchemy рендерит `IN` как
#: postcompile-параметр (`IN (__[POSTCOMPILE_status_1])`), и литералов статусов
#: в `str(statement)` нет вообще — проверка «`'full' in sql`» падала бы даже на
#: правильном коде. Значения читаются из самого критерия, что строже текста:
#: это ровно тот кортеж, который уйдёт в БД.
def _status_values(statement: Any) -> tuple[str, ...]:
    from sqlalchemy.sql.elements import BinaryExpression

    for criterion in statement._where_criteria:
        if not isinstance(criterion, BinaryExpression):
            continue
        if str(criterion.left) != "event_cards.status":
            continue
        right = criterion.right
        value = getattr(right, "value", None)
        if isinstance(value, (tuple, list)):
            return tuple(str(item.value if hasattr(item, "value") else item) for item in value)
    raise AssertionError("предикат по status не найден")


# ——— AC-13: кортеж ——————————————————————————————————————————————


def test_promptable_statuses_tuple_is_exactly_three() -> None:
    """AC-13 (D-7.3-7): ровно `full`, `closed`, `finished` — и ничего больше.

    Кортеж — ЕДИНСТВЕННЫЙ источник списка: на него опираются `SELECT`, CAS и
    (дословно) предикат индекса. Проверка состава отдельно от БД, чтобы
    добавление статуса без миграции падало здесь с понятным сообщением.
    """
    assert ATTENDANCE_PROMPTABLE_STATUSES == (
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
        EventCardStatus.FINISHED,
    )


def test_cancelled_is_not_promptable() -> None:
    """AC-08 (D-7.3-5): `cancelled` НЕ входит — запрос по отменённой встрече ложен."""
    assert EventCardStatus.CANCELLED not in ATTENDANCE_PROMPTABLE_STATUSES


def test_draft_and_published_not_promptable() -> None:
    """A02: `draft`/`published` не входят — состава компании ещё нет."""
    assert EventCardStatus.DRAFT not in ATTENDANCE_PROMPTABLE_STATUSES
    assert EventCardStatus.PUBLISHED not in ATTENDANCE_PROMPTABLE_STATUSES


# ——— AC-13: предикат выборки ——————————————————————————————————


def test_select_contains_all_statuses_in_order() -> None:
    """AC-13 (S38): текст `SELECT` содержит все значения кортежа в порядке.

    Сравнение текста, а не результата: при `status <> 'cancelled'` выборка
    вернула бы `draft`/`published` с прошлым `starts_at` (M9), и тест на
    результате это поймал бы только при наличии таких карточек в фикстуре.
    """
    from datetime import UTC, datetime

    statement = EventCardRepository.build_due_prompt_ids_statement(now=datetime.now(UTC), limit=10)
    sql = " ".join(str(statement).split())

    assert "attendance_prompted_at IS NULL" in sql
    assert _status_values(statement) == tuple(
        status.value for status in ATTENDANCE_PROMPTABLE_STATUSES
    )


def test_select_has_no_negative_status_predicate() -> None:
    """AC-13 (S38, M9): в предикате НЕТ `<>` и `NOT IN` по статусу.

    «Более простое» `status <> 'cancelled'` включало бы `draft`/`published`:
    индекс от этого не защищает (он лишь оптимизация, а не гарант).
    """
    from datetime import UTC, datetime

    statement = EventCardRepository.build_due_prompt_ids_statement(now=datetime.now(UTC), limit=10)
    sql = " ".join(str(statement).split())
    assert "NOT IN" not in sql.upper()
    assert "<>" not in sql


def test_select_has_no_lower_time_bound() -> None:
    """AC-07 (D-7.3-4): в выборке НЕТ отсечки по времени — только верхняя граница.

    Отсечка `starts_at > :now - interval '15 min'` превратила бы лежачего
    воркера в НЕОБРАТИМЫЙ пропуск факта явки (AC-06 «пропусков 0»).
    Тест `now - 3 часа → prompted == 1` закрепляет это поведением; здесь —
    форма запроса, чтобы мутация падала сразу и с внятным сообщением.
    """
    from datetime import UTC, datetime

    statement = EventCardRepository.build_due_prompt_ids_statement(now=datetime.now(UTC), limit=10)
    sql = " ".join(str(statement).split())
    assert "interval" not in sql.lower()


def test_select_returns_starts_at() -> None:
    """D-TD-4: `starts_at` ВХОДИТ в выборку — иначе второй `SELECT` за payload.

    Payload уведомления требует `starts_at` (Task 3.3). Если его нет в
    выборке, реализация сделает второй запрос и заломает анти-оракул по
    числу SQL (урок D-3 TD 7.2). Контракт — ровно 4 запроса на карточку.
    """
    from datetime import UTC, datetime

    statement = EventCardRepository.build_due_prompt_ids_statement(now=datetime.now(UTC), limit=10)
    columns = [str(column) for column in statement.selected_columns]
    assert any("starts_at" in column for column in columns), columns


def test_cas_predicate_matches_select_predicate() -> None:
    """AC-05/AC-08/AC-13: CAS и выборка используют ОДИН кортеж статусов.

    Три копии списка разошлись бы при первом же добавлении статуса (класс
    дефекта 4.1/6.7, правило P-8). Здесь проверяется, что в `UPDATE` есть и
    `IS NULL`, и граница начала, и статусы.
    """
    from datetime import UTC, datetime

    statement = EventCardRepository.build_mark_attendance_prompted_statement(
        card_id=1, now=datetime.now(UTC)
    )
    sql = " ".join(str(statement).split())
    assert "attendance_prompted_at IS NULL" in sql
    assert "starts_at <=" in sql
    assert _status_values(statement) == tuple(
        status.value for status in ATTENDANCE_PROMPTABLE_STATUSES
    )


# ——— AC-13: индекс (S37) ——————————————————————————————————————


async def test_index_definition_contains_all_statuses(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-13 (S37): `indexdef` содержит все значения кортежа и `IS NULL`.

    Основной оракул AC-13: прямое сравнение источника истины (БД) с кортежем
    (Python). Краснеет при добавлении статуса в кортеж без миграции на
    перестройку частичного индекса — карточка с новым статусом стала бы
    невидимой для индексного пути.
    """
    async with factory() as session:
        result = await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_event_cards_prompt_due'")
        )
        definition = result.scalar_one()

    assert "attendance_prompted_at IS NULL" in definition
    for status in ATTENDANCE_PROMPTABLE_STATUSES:
        assert status.value in definition, status


async def test_index_exists_and_is_partial(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-13: индекс существует и он ЧАСТИЧНЫЙ (`WHERE` в определении).

    Неpartial-версия покрывала бы `cancelled`/`draft` и раздувала бы индекс;
    поведение от этого не меняется, но стоимость батча — да (R9).
    """
    async with factory() as session:
        result = await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_event_cards_prompt_due'")
        )
        definition = result.scalar_one()

    assert "CREATE INDEX" in definition
    assert "WHERE" in definition


# ——— S39: индекс ПРИГОДЕН для предиката (вспомогательный) ————————————


async def test_index_is_usable_for_predicate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-13 (S39): `EXPLAIN` при `enable_seqscan=off` использует индекс.

    **Вспомогательная проверка, не основной оракул.** Она доказывает только
    совместимость предиката и индекса (типы, литералы), но НЕ совпадение
    текстов: `= ANY(ARRAY[...])` тоже даёт index scan. Совпадение доказывают
    `S37`/`S38` выше.
    """
    async with factory() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        result = await session.execute(
            text(
                "EXPLAIN (FORMAT JSON) SELECT id FROM event_cards "
                "WHERE attendance_prompted_at IS NULL "
                "AND status IN ('full','closed','finished') "
                "AND starts_at <= now()"
            )
        )
        plan = str(result.scalar_one())

    assert "ix_event_cards_prompt_due" in plan
