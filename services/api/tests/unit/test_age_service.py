"""Домен возраста (стори 12.2, AC-2, AC-4, AC-5, AC-6).

Граница 18 лет — единственное место, где ошибка стоит реальных последствий:
несовершеннолетний получает доступ к UGC или совершеннолетний его теряет
в день рождения. Поэтому `today` — параметр, а не `date.today()` внутри:
иначе граница не тестируется детерминированно и «работает» только в тот
день, когда её написали.

Тесты чистые (без БД): домен не знает ни про SQLAlchemy, ни про FastAPI.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.age_service import (
    AGE_MAX,
    AGE_MIN,
    compute_age,
    is_adult,
    validate_birth_date,
)

TODAY = date(2026, 10, 29)


class TestComputeAge:
    """AC-2: возраст вычисляется из даты рождения на сервере."""

    def test_birthday_today_counts_full_year(self) -> None:
        """День рождения СЕГОДНЯ — полные годы уже наступили (граница AC-6).

        Мутация `<=` вместо `<` в сравнении месяца-дня даёт здесь 17 вместо 18
        и отказывает совершеннолетнему в его же день рождения.
        """
        assert compute_age(date(2008, 10, 29), None, TODAY) == 18

    def test_birthday_tomorrow_is_still_seventeen(self) -> None:
        """День рождения ЗАВТРА — полных лет ещё нет (обратная граница AC-6).

        Мутация `<` в `<=` даёт здесь 18 и пускает несовершеннолетнего.
        """
        assert compute_age(date(2008, 10, 30), None, TODAY) == 17

    def test_birthday_yesterday_is_eighteen(self) -> None:
        """День рождения ВЧЕРА — год уже полный."""
        assert compute_age(date(2008, 10, 28), None, TODAY) == 18

    def test_day_before_eighteenth_birthday(self) -> None:
        """17 лет 364 дня: за день до 18-летия — 17."""
        assert compute_age(date(2008, 10, 30), None, TODAY) == 17

    def test_leap_day_birthday(self) -> None:
        """29 февраля: в невисокосный год день рождения «наступает» 1 марта.

        Сравнение кортежем `(month, day)` даёт 28 февраля → 17 лет,
        1 марта → 18. Это ожидаемое поведение: `date` не умеет иначе.
        """
        assert compute_age(date(2008, 2, 29), None, date(2026, 2, 28)) == 17
        assert compute_age(date(2008, 2, 29), None, date(2026, 3, 1)) == 18

    def test_falls_back_to_legacy_age(self) -> None:
        """AC-5: без даты рождения источник — legacy-`age` (grandfathering)."""
        assert compute_age(None, 34, TODAY) == 34

    def test_birth_date_wins_over_legacy_age(self) -> None:
        """AC-5: при наличии обоих источник истины — `birth_date`.

        Расхождение возможно у учётки, которая заполнила дату поверх
        старого числа: отдавать устаревшее число значило бы врать.
        """
        assert compute_age(date(2000, 1, 1), 34, TODAY) == 26

    def test_both_missing_returns_none(self) -> None:
        """Ни даты, ни числа — `None`, а не 0: 0 означал бы «младенец»."""
        assert compute_age(None, None, TODAY) is None


class TestIsAdult:
    """AC-4: 18+ проверяется по вычисленному возрасту."""

    def test_eighteen_is_adult(self) -> None:
        assert is_adult(18) is True

    def test_seventeen_is_not_adult(self) -> None:
        assert is_adult(17) is False

    def test_none_is_not_adult(self) -> None:
        """Неизвестный возраст — не пропуск: `None` не совершеннолетний."""
        assert is_adult(None) is False


class TestValidateBirthDate:
    """AC-2, AC-4: отказ `422` до записи, а не `IntegrityError` → `500`."""

    def test_valid_adult_date_accepted(self) -> None:
        assert validate_birth_date(date(1990, 5, 12), TODAY) == date(1990, 5, 12)

    def test_underage_rejected_with_code(self) -> None:
        """Менее 18 полных лет — код `underage` (не общий `invalid_request`).

        Клиент по коду показывает сообщение у поля, а не общий баннер.
        """
        with pytest.raises(ValueError) as excinfo:
            validate_birth_date(date(2010, 1, 1), TODAY)
        assert "underage" in str(excinfo.value)

    def test_birthday_today_accepted(self) -> None:
        """Ровно 18 сегодня — принимается (граница, AC-6)."""
        assert validate_birth_date(date(2008, 10, 29), TODAY) == date(2008, 10, 29)

    def test_future_date_rejected(self) -> None:
        """Дата в будущем — отказ: возраст отрицательный, `age_service` не гадает."""
        with pytest.raises(ValueError) as excinfo:
            validate_birth_date(date(2027, 1, 1), TODAY)
        assert "invalid_birth_date" in str(excinfo.value)

    def test_too_old_rejected(self) -> None:
        """Верхняя граница `AGE_MAX` повторяет CHECK БД (`age <= 99`)."""
        with pytest.raises(ValueError) as excinfo:
            validate_birth_date(date(1900, 1, 1), TODAY)
        assert "invalid_birth_date" in str(excinfo.value)

    def test_bounds_match_database_check(self) -> None:
        """Границы сервиса совпадают с CHECK `profiles.age` в БД.

        Расхождение дало бы `IntegrityError` → `500` вместо `422`:
        сервис пропустил бы значение, которое БД не примет.
        """
        assert (AGE_MIN, AGE_MAX) == (18, 99)