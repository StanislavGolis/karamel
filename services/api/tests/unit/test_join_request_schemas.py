"""Схемы заявки на участие (стори 5а.2, Task 7.1; AC-01).

§18.1: каждый тест краснеет от конкретной правки — она названа в докстринге.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.join_request import MESSAGE_MAX_LENGTH, JoinRequestCreate, JoinRequestRead


@pytest.mark.parametrize(
    "blank",
    ["", "   ", "\t", "\u00a0", "\u202f", " \t\u00a0 "],
)
def test_blank_message_is_rejected(blank: str) -> None:
    """AC-01 (D-5a.1-8): пустое после нормализации сообщение — `422`.

    Краснеет, если: снять валидатор `_strip_message` (тогда `'   '` пройдёт
    `min_length=1` как 3 символа), либо поставить `min_length=0`.
    Пустая строка прошла бы CHECK в БД (`message = btrim(message)` истинно),
    поэтому схема — единственный рубеж.
    """
    with pytest.raises(ValidationError):
        JoinRequestCreate(message=blank)


def test_message_300_is_allowed_and_301_is_rejected() -> None:
    """AC-01: предел 300 — граница колонки `String(300)`.

    Краснеет, если `max_length` убрать (301 дойдёт до БД → `DBAPIError`/`500`)
    или поставить `>=` вместо `>`.
    """
    assert len(JoinRequestCreate(message="a" * MESSAGE_MAX_LENGTH).message) == 300
    with pytest.raises(ValidationError):
        JoinRequestCreate(message="a" * (MESSAGE_MAX_LENGTH + 1))


def test_normalization_runs_before_length_check() -> None:
    """AC-01: нормализация ДО проверки длины, а не после.

    Краснеет, если перенести нормализацию в `mode="after"` и оставить
    `max_length`: 300 символов + пробелы по краям дали бы `422`, хотя после
    нормализации строка ровно 300.
    """
    payload = JoinRequestCreate(message="  " + "a" * 300 + "  ")
    assert payload.message == "a" * 300


def test_message_is_normalized() -> None:
    """AC-01: NFC, NBSP и внутренние пробелы — тот же `normalize_text`, что у анкеты.

    Краснеет, если заменить нормализатор на `str.strip()`: NBSP и `\\t`
    остались бы внутри строки и CHECK `message = btrim(...)` дал бы `500`.
    """
    assert JoinRequestCreate(message="  привет\u00a0 \tмир  ").message == "привет мир"


def test_extra_field_is_forbidden() -> None:
    """AC-01: `extra="forbid"` — серверные поля не принимаются.

    Краснеет, если сменить на `extra="ignore"`: клиент считал бы, что назначил
    заявке `status` или `expires_at`, а они молча отбрасывались бы.
    """
    with pytest.raises(ValidationError):
        JoinRequestCreate.model_validate({"message": "привет", "status": "selected"})


def test_non_string_message_is_rejected_by_pydantic() -> None:
    """AC-01: нормализатор не подменяет тип — «ожидалась строка» сообщает Pydantic.

    Краснеет, если `_normalize` начнёт приводить всё к `str`: число 42
    превратилось бы в «42» и прошло бы валидацию.
    """
    with pytest.raises(ValidationError):
        JoinRequestCreate.model_validate({"message": 42})


def test_read_schema_has_no_message_field() -> None:
    """AC-02 (OQ-5a.2-2): `message` не отдаётся в ответе `201`.

    Краснеет, если добавить поле `message` в `JoinRequestRead`: эхо
    расширило бы контракт, который 5а.3 всё равно расширяет.
    """
    fields = set(JoinRequestRead.model_fields)
    assert fields == {"uuid", "status", "submitted_at"}
    assert "message" not in fields


def test_read_schema_accepts_orm_like_object() -> None:
    """AC-02: `from_attributes=True` — ответ собирается из ORM-объекта.

    Краснеет, если убрать `from_attributes`: роутеру пришлось бы переписывать
    поля вручную, и расхождение с моделью стало бы возможным.
    """

    class _Fake:
        uuid = "11111111-1111-1111-1111-111111111111"
        status = "pending"
        submitted_at = "2026-10-24T12:00:00Z"
        extra: Any = None

    read = JoinRequestRead.model_validate(_Fake())
    assert read.status.value == "pending"
