"""Профиль: `birth_date` вместо `age` (стори 12.2; AC-2, AC-4, AC-5).

Проверяется доменный слой (`_validate_and_normalize_*`) и `_compute_status`,
а не HTTP: те же проверки обязаны работать и из `PATCH`, и из воркера,
и именно поэтому лимиты живут в сервисе, а не в схеме (CR 2.3, Решение 1).

Краснеет при:
- возврате `age` в `ProfileCreate` (M6) — `create(age=32)` прошёл бы;
- удалении проверки `underage` (M7) — несовершеннолетний сохранился бы;
- использовании `date.today()` вместо параметра (M8) — граница не тестируема;
- удалении legacy-fallback в `_compute_status` (M9) — старые учётки стали бы
  `draft`, то есть `status='complete'` сломался бы (AC-5).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas.profile import ProfileCreate, ProfileUpdate
from app.services.profile_service import (
    ProfileFieldError,
    _compute_status,
    _validate_and_normalize_create,
    _validate_and_normalize_update,
)

PILOT_CITY = "Томск"
TODAY = date(2026, 10, 29)


class TestCreateAcceptsBirthDate:
    """AC-2: `ProfileWrite` принимает `birth_date` (тип `date`), а не `age`."""

    def test_birth_date_is_stored(self) -> None:
        payload = ProfileCreate(
            name="Аня", city="Томск", birth_date=date(1994, 3, 14), bio="люблю горы"
        )
        values = _validate_and_normalize_create(payload, PILOT_CITY, today=TODAY)
        assert values["birth_date"] == date(1994, 3, 14)

    def test_age_is_not_a_field_of_write_schema(self) -> None:
        """Поле `age` ушло из запроса: два источника правды не допускаются.

        `extra=\"forbid\"` обязан отклонить старое поле, иначе клиент
        продолжил бы присылать число, и оно молча игнорировалось бы.
        """
        with pytest.raises(Exception) as excinfo:
            ProfileCreate(name="Аня", city="Томск", age=32, bio="люблю горы")
        assert "age" in str(excinfo.value)

    def test_underage_rejected(self) -> None:
        """AC-4: менее 18 полных лет — отказ с кодом `underage`."""
        payload = ProfileCreate(name="Аня", city="Томск", birth_date=date(2010, 1, 1), bio="текст")
        with pytest.raises(ProfileFieldError) as excinfo:
            _validate_and_normalize_create(payload, PILOT_CITY, today=TODAY)
        assert excinfo.value.code == "underage"

    def test_birthday_today_is_accepted(self) -> None:
        """Граница AC-6: ровно 18 сегодня — сохраняется."""
        payload = ProfileCreate(
            name="Аня", city="Томск", birth_date=date(2008, 10, 29), bio="текст"
        )
        values = _validate_and_normalize_create(payload, PILOT_CITY, today=TODAY)
        assert values["birth_date"] == date(2008, 10, 29)

    def test_future_date_rejected(self) -> None:
        payload = ProfileCreate(name="Аня", city="Томск", birth_date=date(2027, 1, 1), bio="текст")
        with pytest.raises(ProfileFieldError) as excinfo:
            _validate_and_normalize_create(payload, PILOT_CITY, today=TODAY)
        assert excinfo.value.code == "invalid_birth_date"


class TestUpdateAcceptsBirthDate:
    """AC-2: частичная правка тоже принимает дату, а не число."""

    def test_birth_date_patch(self) -> None:
        payload = ProfileUpdate(birth_date=date(1990, 5, 12))
        values = _validate_and_normalize_update(payload, PILOT_CITY, today=TODAY)
        assert values == {"birth_date": date(1990, 5, 12)}

    def test_explicit_null_rejected(self) -> None:
        """D-3: стереть поле нельзя — `422 field_required`."""
        payload = ProfileUpdate(birth_date=None)
        with pytest.raises(ProfileFieldError) as excinfo:
            _validate_and_normalize_update(payload, PILOT_CITY, today=TODAY)
        assert excinfo.value.code == "field_required"

    def test_absent_field_untouched(self) -> None:
        """Отсутствующий ключ — поле не трогаем (D-11)."""
        payload = ProfileUpdate(name="Аня")
        values = _validate_and_normalize_update(payload, PILOT_CITY, today=TODAY)
        assert values == {"name": "Аня"}

    def test_patch_does_not_write_legacy_age(self) -> None:
        """VS-2: `PATCH` с `birth_date` не пишет `age` вовсе.

        У старой учётки `age` остаётся как был, у новой — `NULL`. Записать
        вычисленное число значило бы вернуть второй источник правды.
        """
        payload = ProfileUpdate(birth_date=date(1990, 5, 12))
        values = _validate_and_normalize_update(payload, PILOT_CITY, today=TODAY)
        assert "age" not in values


class TestComputeStatus:
    """AC-5: grandfathering — `complete` держится на любом источнике возраста."""

    def test_complete_with_birth_date_only(self) -> None:
        assert (
            _compute_status(
                name="Аня", city="Томск", birth_date=date(1990, 5, 12), age=None, bio="текст"
            )
            == "complete"
        )

    def test_complete_with_legacy_age_only(self) -> None:
        """40 008 старых учёток: есть `age`, нет `birth_date` — анкета полная."""
        assert (
            _compute_status(name="Аня", city="Томск", birth_date=None, age=32, bio="текст")
            == "complete"
        )

    def test_draft_without_any_age(self) -> None:
        assert (
            _compute_status(name="Аня", city="Томск", birth_date=None, age=None, bio="текст")
            == "draft"
        )
