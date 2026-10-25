"""Модель `join_requests` (стори 5а.1, Task 5.2–5.3; AC-01…AC-05).

§18.2: проверяется наблюдаемое свойство объекта (имена в контракте,
состав индексов, текст предиката), а не факт объявления класса.
Имена ограничений и индексов — часть контракта: `alembic check` и
`downgrade` опираются на них (§18.7), и расхождение модели с миграцией
валит откат на несуществующем имени (дефекты 3.1/4.1).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Index, Table

from app.models.join_request import JoinRequest, _trimmed

# AC-01, §18.2: `__table__` у модели типизирован как `FromClause`, но здесь
# нужен именно `Table` (коллекции `constraints`/`indexes`). Приведение
# типов не меняет поведение — mypy-gate проекта (`mypy --strict app`) чист.
_TABLE = cast(Table, JoinRequest.__table__)


def _index(name: str) -> Index:
    """Возвращает объявление индекса по имени из `__table__`."""
    for index in _TABLE.indexes:
        if index.name == name:
            return index
    raise AssertionError(f"индекс {name} не объявлен")


def test_constraint_names_follow_naming_convention() -> None:
    """AC-01…AC-03, §18.7: имена ограничений — часть контракта.

    Краснеет, если в модели написать короткое `name="uuid"` у
    `UniqueConstraint` (дефект 3.1): у конвенции `uq` нет токена
    `%(constraint_name)s`, имя стало бы `uuid` без префикса, миграция
    с `op.f("uq_join_requests_uuid")` разошлась бы с моделью, и `downgrade`
    упал бы на несуществующем ограничении.
    """
    names = {constraint.name for constraint in _TABLE.constraints}

    assert {
        "pk_join_requests",
        "uq_join_requests_uuid",
        "ck_join_requests_status",
        "ck_join_requests_message",
        "fk_join_requests_card_id_event_cards",
        "fk_join_requests_applicant_id_users",
    } <= names


def test_index_names_are_full_strings() -> None:
    """AC-04, AC-05: ровно три индекса с полными именами.

    Краснеет, если индекс переименовать только в модели или только
    в миграции: `alembic check` покажет расхождение, а частичный предикат
    перестанет совпадать с запросом лимита (FR77) и автовыбора (ADR-05).
    """
    names = {index.name for index in _TABLE.indexes}

    assert names == {
        "uq_join_requests_card_applicant_active",
        "ix_join_requests_applicant_pending",
        "ix_join_requests_card_queue",
        # Стори 6.1 (AC-06, долг X-5a.3-1): страница «мои заявки» — четыре
        # ВИДИМЫХ статуса и порядок `(submitted_at, uuid)`. Частичный
        # `WHERE status='pending'` под `IN (...)` неприменим, поэтому индекс
        # отдельный. Имя обязано совпадать с миграцией `0014`.
        "ix_join_requests_applicant_visible",
    }


def test_active_unique_index_is_partial_over_two_statuses() -> None:
    """AC-04, FR82: `uq` — уникальный и ЧАСТИЧНЫЙ по двум статусам.

    Краснеет, если `unique=False`: две активные заявки на пару прошли бы.
    Краснеет, если предикат сузить до `pending`: два `selected` на паре
    сосуществовали бы. Краснеет и при безусловном `UNIQUE` (предикат `None`):
    повторная подача после отмены (FR82) стала бы невозможной.
    """
    index = _index("uq_join_requests_card_applicant_active")
    predicate = str(index.dialect_options["postgresql"]["where"])

    assert index.unique is True
    assert "pending" in predicate
    assert "selected" in predicate


def test_card_queue_index_matches_sort_key() -> None:
    """AC-05, ADR-05: ключ очереди — `(card_id, submitted_at, id)`.

    Краснеет, если убрать `id`: при равных `submitted_at` порядок не
    определён, и курсор `WHERE (submitted_at, id) >` терял бы или дублировал
    строки между страницами автовыбора.
    """
    index = _index("ix_join_requests_card_queue")

    assert [column.name for column in index.columns] == [
        "card_id",
        "submitted_at",
        "id",
    ]


def test_pending_predicates_are_exact_strings() -> None:
    """AC-05: предикаты `pending`-индексов совпадают с задуманными посимвольно.

    Краснеет, если предикат `ix_join_requests_card_queue` расширить до
    `status IN (...)` или снять вовсе: индекс начнёт индексировать историю,
    и подсчёт лимита (FR77) потянет в план все терминальные заявки.
    """
    for name in ("ix_join_requests_applicant_pending", "ix_join_requests_card_queue"):
        predicate = str(_index(name).dialect_options["postgresql"]["where"])

        assert predicate == "status = 'pending'", name


def test_message_check_uses_full_whitespace_set() -> None:
    """AC-03: `btrim` получает набор с NBSP и узким NBSP.

    Краснеет, если `_trimmed` вызовет `btrim(x)` без второго аргумента:
    режется только U+0020, и `message = '\u00a0'` прошёл бы CHECK — заявка
    попадала бы в очередь автора визуально пустой (CR 3.1, патч 8).
    """
    expression = _trimmed("message")

    assert " " in expression
    assert " " in expression


def test_message_check_is_equality_not_empty_only() -> None:
    """AC-03: CHECK обрезки — РАВЕНСТВО, а не `<> ''`.

    Краснеет, если заменить на `message <> ''`: `' привет'` и `'привет'`
    сосуществовали бы, и сервис, нормализующий вход, считал бы их одним
    сообщением (класс дефекта `places.name`).
    """
    checks = [
        str(constraint.sqltext)
        for constraint in _TABLE.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == "ck_join_requests_message"
    ]
    assert len(checks) == 1, "ck_join_requests_message объявлен один раз"
    assert checks[0].startswith("message = btrim(message,")
    assert "<>" not in checks[0]


def test_status_check_includes_every_enum_value() -> None:
    """AC-02: CHECK статуса собирается из `JoinRequestStatus`, все пять значений.

    Краснеет, если список вписать строкой и потерять `expired`: воркер 5б
    не смог бы перевести просроченную заявку, и она навсегда заняла бы слот
    лимита (NFR29).
    """
    checks = [
        str(constraint.sqltext)
        for constraint in _TABLE.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == "ck_join_requests_status"
    ]

    assert len(checks) == 1
    for value in ("pending", "selected", "not_selected", "expired", "cancelled"):
        assert f"'{value}'" in checks[0]


def test_decided_at_has_no_check_constraint() -> None:
    """OQ-5a.1-1: CHECK на `decided_at` НЕ ставится.

    Краснеет, если добавить `CHECK (decided_at IS NULL OR status <> 'pending')`:
    легальный переход при забытом поле дал бы `500` — класс дефекта X-39.
    Заполнение `decided_at` — обязанность сервиса (5а.3/5б).
    """
    check_texts = [
        str(constraint.sqltext).lower()
        for constraint in _TABLE.constraints
        if isinstance(constraint, CheckConstraint)
    ]

    assert not any("decided_at" in value for value in check_texts)
