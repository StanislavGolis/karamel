"""Нижняя граница окна метрик: `_months_ago` и кламп дня (стори 11.7; AC-1).

**Зачем чистая функция, а не `dateutil`.** `dateutil` в зависимостях нет
(замер CS: `grep -n dateutil pyproject.toml` → пусто), а `timedelta(days=182)`
неверен: февраль короче, и предел разъехался бы с ретенцией, которая считает
`interval '6 months'` по календарю (`attendance_retention.py:59`).

**Почему кламп 31-го числа проверяется отдельно.** `2026-08-31 - 6 мес` в
календаре не существует (`февраль 31`); PostgreSQL берёт последний день
месяца (`2026-02-28`). Без клампа функция либо бросила бы `ValueError` (500
на валидном запросе), либо уехала бы в март и расширила окно на сутки.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.api.v1.metrics import _months_ago


class TestMonthsAgoClamp:
    """Кламп дня до последнего дня целевого месяца (как `interval '6 months'`)."""

    def test_last_day_of_august_minus_six_months_is_february_28(self) -> None:
        """AC-1, Task 1.3: `2026-08-31 - 6 мес → 2026-02-28` (не 03-03)."""
        assert _months_ago(datetime(2026, 8, 31, tzinfo=UTC), 6) == datetime(
            2026, 2, 28, tzinfo=UTC
        )

    def test_last_day_of_march_minus_one_month_is_february_28(self) -> None:
        """Кламп на коротком месяце: `2026-03-31 - 1 мес → 2026-02-28`."""
        assert _months_ago(datetime(2026, 3, 31, tzinfo=UTC), 1) == datetime(
            2026, 2, 28, tzinfo=UTC
        )

    def test_leap_year_february_29(self) -> None:
        """Високосный год: `2024-08-31 - 6 мес → 2024-02-29`."""
        assert _months_ago(datetime(2024, 8, 31, tzinfo=UTC), 6) == datetime(
            2024, 2, 29, tzinfo=UTC
        )

    def test_day_that_exists_is_not_clamped(self) -> None:
        """Обратная сторона: `2026-05-30 - 1 мес → 2026-04-30`, клампа нет."""
        assert _months_ago(datetime(2026, 5, 30, tzinfo=UTC), 1) == datetime(
            2026, 4, 30, tzinfo=UTC
        )


class TestMonthsAgoArithmetic:
    """Арифметика месяцев: переход через год, сохранение времени суток."""

    def test_crosses_year_boundary(self) -> None:
        """`2026-03-15 - 6 мес → 2025-09-15`."""
        assert _months_ago(datetime(2026, 3, 15, tzinfo=UTC), 6) == datetime(
            2025, 9, 15, tzinfo=UTC
        )

    def test_january_minus_one_month_is_december_of_previous_year(self) -> None:
        """`2026-01-31 - 1 мес → 2025-12-31` — и год, и день."""
        assert _months_ago(datetime(2026, 1, 31, tzinfo=UTC), 1) == datetime(
            2025, 12, 31, tzinfo=UTC
        )

    def test_time_of_day_is_preserved(self) -> None:
        """Время суток и микросекунды не теряются: граница — тот же момент."""
        moment = datetime(2026, 10, 27, 19, 44, 26, 123456, tzinfo=UTC)
        assert _months_ago(moment, 6) == datetime(2026, 4, 27, 19, 44, 26, 123456, tzinfo=UTC)

    def test_zero_months_returns_same_moment(self) -> None:
        """`0` месяцев — тождество: функция не округляет и не сдвигает."""
        moment = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        assert _months_ago(moment, 0) == moment

    def test_six_months_ago_from_today_is_april_27(self) -> None:
        """Сверка с замером CS: от `2026-10-27` шесть месяцев назад = `2026-04-27`."""
        assert _months_ago(datetime(2026, 10, 27, tzinfo=UTC), 6) == datetime(
            2026, 4, 27, tzinfo=UTC
        )
