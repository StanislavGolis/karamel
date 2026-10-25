"""Модель `attendance` (стори 7.1; AC-01, AC-02, AC-08).

§18.2: проверяется наблюдаемое свойство (точный состав колонок, nullable,
имена ограничений после `NAMING_CONVENTION`, порядок индекса), а не факт
объявления класса. Имена — часть контракта: `alembic check` и `downgrade`
опираются на них (§18.7).

§18.11: булев признак проверяется в ОБОИХ значениях. Здесь это «три
состояния флага»: `NULL` допустим (не подтвердил), и одновременно
`NOT NULL` на `status` обязателен (статус у записи есть всегда).
"""

from __future__ import annotations

from typing import cast

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Table,
    UniqueConstraint,
)

from app.models.attendance import Attendance
from app.models.enums import ATTENDANCE_TERMINAL_STATUSES, AttendanceStatus

_TABLE = cast(Table, Attendance.__table__)


def test_attendance_columns_are_exactly_expected() -> None:
    """AC-01: состав колонок фиксирован — равенство, не подмножество.

    Краснеет и при лишней колонке (`uuid`, `attendance_prompted_at`), и при
    недостающей. `uuid` запрещён project-context §7 (таблица неадресуемая),
    `attendance_prompted_at` — дубль маркера карточки (D-7.1-3).
    """
    assert list(_TABLE.columns.keys()) == [
        "id",
        "card_id",
        "participant_id",
        "author_confirmed",
        "participant_confirmed",
        "status",
        "confirmed_at",
        "created_at",
        "updated_at",
    ]


def test_attendance_has_no_uuid_and_no_prompt_marker() -> None:
    """AC-01, D-7.1-3/4: запрещённые имена колонок отсутствуют.

    Краснеет, если вернут `uuid` (таблица неадресуемая) или
    `attendance_prompted_at` (CAS воркера живёт на карточке).
    """
    forbidden = ("uuid", "prompted")

    for column in _TABLE.columns:
        assert not any(token in column.name for token in forbidden), column.name


def test_confirmation_flags_are_nullable() -> None:
    """AC-01: оба флага `NULL`-able — третье состояние «не ответил».

    Краснеет, если флаги станут `NOT NULL DEFAULT false`: тогда `pending`
    стал бы неотличим от «обе стороны ответили: не пришли» (`no_show`).
    """
    assert _TABLE.columns["author_confirmed"].nullable is True
    assert _TABLE.columns["participant_confirmed"].nullable is True
    assert isinstance(_TABLE.columns["author_confirmed"].type, Boolean)
    assert isinstance(_TABLE.columns["participant_confirmed"].type, Boolean)


def test_status_is_not_null_with_pending_server_default() -> None:
    """AC-02: статус есть всегда; по умолчанию — `pending` (воркер 7.3).

    Краснеет, если убрать `server_default`: сырой `INSERT` воркера упал бы на
    `NOT NULL`, либо получил бы значение, отличное от `pending`.
    """
    status = _TABLE.columns["status"]

    assert status.nullable is False
    default_sql = str(status.server_default.arg) if status.server_default else ""
    assert "pending" in default_sql


def test_confirmed_at_is_nullable_timestamptz() -> None:
    """AC-02: метка фиксации — `timestamptz` и допускает `NULL` (pending)."""
    confirmed_at = _TABLE.columns["confirmed_at"]

    assert confirmed_at.nullable is True
    assert isinstance(confirmed_at.type, sa.DateTime)
    assert confirmed_at.type.timezone is True


def test_unique_pair_constraint_present() -> None:
    """AC-02: одна запись на пару «карточка + участник».

    Краснеет, если `UNIQUE` убрать: на пару появилось бы две строки, и
    `POST /attendance` 7.2 писал бы в случайную из них.
    """
    unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in _TABLE.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("card_id", "participant_id") in unique_sets


def test_check_constraint_names_match_naming_convention() -> None:
    """AC-02, §18.7: имена CHECK — bare + префикс конвенции.

    Краснеет при двойном префиксе (`ck_attendance_ck_attendance_...`) —
    класс дефекта 3.1/4.1/6.7-D3, из-за которого `downgrade` не находит
    ограничение по имени.
    """
    checks = {
        constraint.name
        for constraint in _TABLE.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert checks == {
        "ck_attendance_status",
        "ck_attendance_status_consistency",
        "ck_attendance_confirmed_at_consistency",
        # CR 7.1, D2: тот же `id > 0`, что и на `users`.
        "ck_attendance_id_positive",
    }


def test_status_check_uses_all_enum_values() -> None:
    """AC-02: `CHECK status` собран из `ATTENDANCE_STATUSES`, а не строкой.

    Краснеет, если набор значений в модели разойдётся с `AttendanceStatus`:
    статус, прошедший Pydantic, отвергался бы PostgreSQL.
    """
    status_check = next(
        constraint
        for constraint in _TABLE.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == "ck_attendance_status"
    )
    sql = str(status_check.sqltext)

    for status in AttendanceStatus:
        assert f"'{status.value}'" in sql, status.value


def test_foreign_keys_cascade() -> None:
    """AC-01, P-11: оба FK с `ON DELETE CASCADE`.

    Проверяются оба значения признака: «каскад есть у каждого FK» и «другого
    поведения нет» — иначе `RESTRICT` у одного из двух прошёл бы незамеченным.
    """
    fks = {
        constraint.column_keys[0]: constraint
        for constraint in _TABLE.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert set(fks) == {"card_id", "participant_id"}
    for name, fk in fks.items():
        assert fk.ondelete == "CASCADE", name


def test_participant_fk_points_to_users_not_event_cards() -> None:
    """AC-01: `participant_id` → `users.id` (участник, не автор).

    Краснеет, если FK уйдёт на `event_cards` или на `profiles`: явка
    фиксируется для УЧЁТКИ (санкция эпика 8 применяется к аккаунту).
    """
    participant_fk = next(
        constraint
        for constraint in _TABLE.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.column_keys == ["participant_id"]
    )

    assert participant_fk.elements[0].target_fullname == "users.id"


def test_history_index_columns_and_order() -> None:
    """AC-02, FR108: индекс истории — ровно `(participant_id, status)`.

    Краснеет при смене порядка колонок: предикат истории 7.5 фильтрует по
    `participant_id` и сортирует по `status`, и обратный порядок сделал бы
    индекс бесполезным для фильтра.
    """
    indexes = {index.name: index for index in _TABLE.indexes if isinstance(index, Index)}

    assert "ix_attendance_participant_status" in indexes
    assert list(indexes["ix_attendance_participant_status"].columns.keys()) == [
        "participant_id",
        "status",
    ]


def test_no_index_on_card_id_alone() -> None:
    """AC-02: отдельного индекса по `card_id` нет — его покрывает `UNIQUE`.

    Краснеет, если добавят дублирующий индекс: `UNIQUE (card_id,
    participant_id)` уже даёт префикс `card_id`, а лишний индекс платит
    записью на каждый факт явки.
    """
    for index in _TABLE.indexes:
        columns = list(index.columns.keys())
        assert columns != ["card_id"], index.name


def test_terminal_statuses_exclude_pending() -> None:
    """AC-02: `pending` — не терминальный статус.

    Краснеет, если `pending` попадёт в терминальные: CAS 7.2 перестал бы
    различать «факт зафиксирован» и «ждём вторую сторону».
    """
    assert AttendanceStatus.PENDING not in ATTENDANCE_TERMINAL_STATUSES
    assert set(ATTENDANCE_TERMINAL_STATUSES) == {
        AttendanceStatus.CONFIRMED,
        AttendanceStatus.NO_SHOW,
        AttendanceStatus.DISPUTED,
    }
