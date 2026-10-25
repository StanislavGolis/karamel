"""Текст запросов выдачи мест (Task 7.2; AC-01, AC-02, AC-04).

§18.2: проверяется текст запроса, собранного ПРОИЗВОДСТВЕННЫМ билдером, а не
свой `select` в тесте. Иначе удаление `.order_by(Place.uuid)` или замена `>`
на `>=` оставляло бы набор зелёным (урок CR 2.6, патч 3).

§18.1: каждый тест краснеет от конкретной правки — в докстринге указано,
какой именно.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.places import PlaceRepository


def _sql(statement: Any) -> str:
    """Текст запроса в диалекте PostgreSQL — с `LIMIT` и row-value как в бою."""
    return str(statement.compile(dialect=postgresql.dialect()))


def _where(statement: Any) -> str:
    """Только `WHERE`-часть: `SELECT` тянет все колонки строки, включая
    `is_active` и `category`, и проверка по всему тексту ловила бы их вместо
    предиката. Ограничение осознанное: наружу уходят `PlaceRead`, а не SQL.
    """
    sql = _sql(statement)
    return sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]


def _after() -> tuple[datetime, uuid_lib.UUID]:
    return datetime(2026, 10, 23, 11, 20, 31, 482913, tzinfo=UTC), uuid_lib.uuid4()


def test_active_only_adds_is_active_condition() -> None:
    """AC-01: без `include_inactive` в предикате есть `is_active`.

    Краснеет, если снять `Place.is_active.is_(True)`: скрытое место (FR68)
    попало бы в публичную ленту, и участник выбрал бы то, что администратор
    убрал из справочника.
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=False,
        after=None,
        limit=50,
    )
    where = _where(statement)

    assert "places.community_slug = " in where
    assert "places.is_active IS true" in where


def test_include_inactive_removes_is_active_condition() -> None:
    """AC-03: модератор с `include_inactive=True` получает срез без фильтра активности.

    Краснеет, если условие `is_active` добавлять безусловно: админ не увидел бы
    скрытое место и не смог бы его восстановить через `PATCH` — единственный
    маршрут, который 3.2 предоставляет для возврата в справочник.
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=True,
        after=None,
        limit=50,
    )
    where = _where(statement)

    assert "places.community_slug = " in where
    assert "is_active" not in where


def test_category_condition_appears_only_when_set() -> None:
    """AC-02: фильтр категории добавляется только при наличии значения.

    Краснеет, если передавать `None` в `Place.category == None` (даёт `IS NULL`,
    то есть пустую выдачу) или ставить условие безусловно.
    """
    without = _where(
        PlaceRepository.build_list_statement(
            community_slug="tomsk", category=None, include_inactive=False, after=None, limit=50
        )
    )
    with_category = _where(
        PlaceRepository.build_list_statement(
            community_slug="tomsk",
            category="cinema",
            include_inactive=False,
            after=None,
            limit=50,
        )
    )

    assert "places.category" not in without
    assert "places.category = " in with_category


def test_cursor_uses_strict_row_value_comparison() -> None:
    """AC-04: переход по курсору — `>`, а не `>=`, и парой колонок.

    Краснеет от двух правок: `>=` вернул бы последнюю строку предыдущей
    страницы на каждой границе (дубли), а сравнение только по `created_at`
    потеряло бы строки с одинаковым `created_at` (одна транзакция).
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=False,
        after=_after(),
        limit=50,
    )
    sql = _sql(statement)

    assert "(places.created_at, places.uuid) > (" in sql
    assert ">= " not in sql


def test_no_cursor_keeps_statement_without_row_value() -> None:
    """AC-01: без курсора условия перехода нет — первая страница.

    Краснеет, если подставлять «сейчас» вместо отсутствующего курсора:
    первая страница начиналась бы с будущего времени и была бы пустой.
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=False,
        after=None,
        limit=50,
    )
    sql = _sql(statement)

    assert "places.uuid) >" not in sql


def test_order_by_contains_created_at_and_uuid() -> None:
    """AC-04: порядок — `created_at, uuid`, то есть ключ курсора.

    Краснеет, если убрать `Place.uuid` из `ORDER BY`: две строки одной
    транзакции имеют одинаковый `created_at`, порядок между ними становится
    произвольным, и `LIMIT` может отдать их в разном порядке на разных
    страницах — дубль или потеря.
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=False,
        after=None,
        limit=50,
    )
    sql = _sql(statement)
    order_by = sql.split("ORDER BY", 1)[1]

    assert "places.created_at ASC" in order_by
    assert "places.uuid ASC" in order_by


def test_limit_has_one_extra_row() -> None:
    """AC-04: `LIMIT limit + 1` — лишняя строка как признак «есть дальше».

    Краснеет, если убрать `+1`: сервис не узнает о следующей странице,
    `cursor` всегда будет `null`, и клиент остановится на первой странице.
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=False,
        after=None,
        limit=50,
    )

    assert "LIMIT" in _sql(statement)
    # Значение параметра — единственный надёжный признак: имя параметра
    # в диалекте PostgreSQL рендерится как `%(param_N)s::INTEGER`.
    assert 51 in statement.compile().params.values()


def test_count_statement_has_no_order_limit_or_cursor() -> None:
    """AC-02, AC-04, D-4: `total` — по предикату, без порядка, предела и курсора.

    Краснеет от трёх правок: `ORDER BY` в `COUNT` (работа без результата),
    `LIMIT` (счётчик стал бы «сколько на странице») и курсор (листание меняло
    бы `total`, а контракт §7.1 требует «сколько всего под фильтром»).
    """
    statement = PlaceRepository.build_count_statement(
        community_slug="tomsk", category=None, include_inactive=False
    )
    sql = _sql(statement)

    assert "count(" in sql
    assert "ORDER BY" not in sql
    assert "LIMIT" not in sql
    assert "places.uuid) >" not in sql


def test_count_statement_carries_category_filter() -> None:
    """AC-02: `total` считается по тому же фильтру, что страница.

    Краснеет, если `total` собирать без категории: `total` расходится
    с содержимым страницы, и пагинация «2 страницы из 1 строки».
    """
    statement = PlaceRepository.build_count_statement(
        community_slug="tomsk", category="cinema", include_inactive=False
    )
    where = _where(statement)

    assert "places.category = " in where
    assert "places.is_active IS true" in where


def test_count_statement_honours_include_inactive() -> None:
    """AC-03: админский счётчик считается с флагом (RISK-2).

    Краснеет, если `count` всегда фильтрует по `is_active`: у модератора
    `total` не совпал бы с числом элементов в выдаче.
    """
    statement = PlaceRepository.build_count_statement(
        community_slug="tomsk", category=None, include_inactive=True
    )

    assert "is_active" not in _where(statement)


@pytest.mark.parametrize("truthy_non_bool", ["false", "0", "no", 1])
def test_truthy_non_bool_include_inactive_stays_fail_closed(truthy_non_bool: object) -> None:
    """AC-01, AC-03 (CR 3.3, патч 5): небулево значение не снимает фильтр.

    §18.1: краснеет, если вернуть инверсию `if not include_inactive`.
    Прежняя форма была fail-open: строка `"false"` истинна, условие
    `is_active` не добавлялось, и СКРЫТЫЕ места попадали в публичную ленту
    (доказано исполнением: `'false' -> is_active filter present: False`).
    Через HTTP недостижимо (FastAPI коэрсит в `bool`), но воркер/импорт
    идут мимо схем, а отказ обязан быть закрытым: сомнительное значение =
    публичная выдача, а не админская.
    """
    statement = PlaceRepository.build_list_statement(
        community_slug="tomsk",
        category=None,
        include_inactive=truthy_non_bool,  # type: ignore[arg-type]
        after=None,
        limit=50,
    )
    where = _where(statement)

    assert "places.is_active IS true" in where
