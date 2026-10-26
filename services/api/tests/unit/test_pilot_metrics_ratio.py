"""Пороги, доля и критерий отказа метрик пилота (стори 10.5, Task 3; AC-01, AC-03, AC-04, AC-05).

Без БД и без HTTP: проверяются чистые функции сервиса. Каждый тест
сформулирован от AC (P-14), а не от кода: границы взяты из формулировок
«≥ 30%», «строго ниже 10%», «две метрики», «6 недель».

**Границы проверяются в ОБОИХ значениях** (§18.11): порог NFR1 проверяется
на 0.30 (выполнено) и на 0.2999 (провал), окно отказа — на 42 днях (можно
судить) и на 41 (нельзя). Тест только на «прошло» прошёл бы и при константе.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.pilot_metrics_service import (
    FAILURE_WINDOW_DAYS,
    NFR1_THRESHOLD,
    NFR2_THRESHOLD,
    NFR3_THRESHOLD,
    NFR3_WARNING_THRESHOLD,
    NFR6_DAY,
    WARNING_NFR1_BELOW_10_PERCENT,
    Metric,
    _failure_criteria,
    _passed,
    _pilot_day,
    _ratio,
    _warnings,
)


def _metric(
    code: str = "nfr1",
    *,
    value: float | None = 0.5,
    threshold: float = NFR1_THRESHOLD,
    passed: bool | None = None,
) -> Metric:
    """Метрика с явными значениями — числитель/знаменатель для этих тестов не важны."""
    if passed is None and value is not None:
        passed = value >= threshold
    return Metric(
        code=code,
        value=value,
        numerator=1,
        denominator=2,
        threshold=threshold,
        passed=passed,
    )


# --- AC-05: null вместо деления на ноль ------------------------------------


def test_ratio_zero_denominator_is_none_not_zero() -> None:
    """AC-05.1: нулевой знаменатель даёт `None`, а не `0.0`.

    `0.0` означал бы «посчитали и получили ноль» — настоящий провал пилота.
    Слить эти состояния значит объявить пилот провалившимся на пустой базе.
    """
    assert _ratio(0, 0) is None


def test_ratio_zero_numerator_non_zero_denominator_is_zero() -> None:
    """AC-05.1, обратная сторона: ноль ЧИСЛИТЕЛЯ при данных — настоящий `0.0`.

    Без этого теста `_ratio` мог бы возвращать `None` при любом нуле, и
    «собрано 0 из 10» выглядело бы как «нет данных».
    """
    assert _ratio(0, 10) == 0.0


def test_ratio_computes_share() -> None:
    """AC-01: доля — числитель / знаменатель."""
    assert _ratio(3, 4) == 0.75


# --- AC-01.5: порог включительный -----------------------------------------


def test_passed_at_threshold_is_true() -> None:
    """AC-01.5: ровно на пороге — выполнение (`>=`, не `>`).

    Мутация M2 (`>=` → `>`) обязана покраснеть именно здесь.
    """
    assert _passed(0.30, NFR1_THRESHOLD) is True


def test_passed_just_below_threshold_is_false() -> None:
    """AC-01.5: чуть ниже порога — провал. Вторая сторона границы (§18.11)."""
    assert _passed(0.2999, NFR1_THRESHOLD) is False


def test_passed_none_value_is_none_not_false() -> None:
    """AC-05.3: при `value: null` — `null`, НЕ `false`.

    Мутация M5 (`passed = False` при `null`) обязана покраснеть здесь:
    иначе кабинет показал бы «провалено» вместо «нет данных».
    """
    assert _passed(None, NFR1_THRESHOLD) is None


def test_thresholds_match_product() -> None:
    """AC-01: пороги NFR1–NFR4 — из требований, не подогнаны под данные."""
    assert NFR1_THRESHOLD == 0.30
    assert NFR2_THRESHOLD == 1.5
    assert NFR3_THRESHOLD == 0.60
    assert NFR3_WARNING_THRESHOLD == 0.10
    assert NFR6_DAY == 14
    assert FAILURE_WINDOW_DAYS == 42


# --- AC-03.1: день пилота считается от `to`, не от «сейчас» ---------------


def test_pilot_day_is_counted_from_period_to() -> None:
    """AC-03.1: день пилота — разница `to - started_at`, а не «сколько прошло с старта».

    Администратор смотрит метрики на произвольную дату: `datetime.now()`
    дал бы «14-й день» независимо от выбранного окна.
    """
    started = datetime(2026, 11, 1, tzinfo=UTC)
    period_to = started + timedelta(days=14)
    assert _pilot_day(period_to, started) == 14


def test_pilot_day_none_when_start_unknown() -> None:
    """AC-03.4: дата старта не задана → `pilot_day: null`, НЕ `0`.

    `0` означал бы «стартовал сегодня» и был бы выдумкой конфигурации.
    """
    assert _pilot_day(datetime(2026, 12, 1, tzinfo=UTC), None) is None


def test_pilot_day_negative_when_start_in_future() -> None:
    """TD-10.5-2: старт в будущем — отрицательный день КАК ЕСТЬ, не клампится в `0`.

    Кламп в `0` сделал бы ошибку конфигурации невидимой: `0` неотличим от
    «стартовал сегодня».
    """
    started = datetime(2026, 12, 1, tzinfo=UTC)
    period_to = datetime(2026, 11, 1, tzinfo=UTC)
    assert _pilot_day(period_to, started) == -30


# --- AC-03.2: предупреждение NFR6 ------------------------------------------


def test_warning_on_day_14_below_ten_percent() -> None:
    """AC-03.2: ровно 14-й день и 5% явки → предупреждение."""
    warnings = _warnings(NFR6_DAY, _metric(value=0.05))
    assert warnings == (WARNING_NFR1_BELOW_10_PERCENT,)


def test_no_warning_on_day_13() -> None:
    """AC-03.2: 13-й день — рано, даже при 5% (граница снизу)."""
    assert _warnings(NFR6_DAY - 1, _metric(value=0.05)) == ()


def test_no_warning_at_exactly_ten_percent() -> None:
    """AC-03.2: порог предупреждения — СТРОГО ниже 10%.

    Мутация M8 (`<` → `<=`) обязана покраснеть здесь: ровно 10% — уже норма.
    """
    assert _warnings(NFR6_DAY, _metric(value=0.10)) == ()


def test_no_warning_when_value_is_null() -> None:
    """AC-03.2: нет данных — не повод для предупреждения.

    Иначе пустая база на 14-й день кричала бы «явка ниже 10%», хотя явки
    просто не измеряли.
    """
    assert _warnings(NFR6_DAY, _metric(value=None)) == ()


def test_no_warning_when_start_unknown() -> None:
    """AC-03.4: `pilot_day: null` → предупреждения нет."""
    assert _warnings(None, _metric(value=0.01)) == ()


# --- AC-04: критерий отказа ------------------------------------------------


def test_failure_triggered_on_two_failures_in_42_days() -> None:
    """AC-04.1: два провала и окно ровно 42 дня → `triggered: true`.

    Граница включительная: 42 дня — уже «6 недель» (мутация M6 на
    `>= 42` → `> 42` обязана покраснеть здесь).
    """
    metrics = (
        _metric("nfr1", value=0.1, passed=False),
        _metric("nfr2", value=1.0, passed=False),
        _metric("nfr3", value=0.9, passed=True),
        _metric("nfr4", value=0.9, passed=True),
    )
    criteria = _failure_criteria(metrics, FAILURE_WINDOW_DAYS)
    assert criteria.triggered is True
    assert criteria.insufficient_window is False
    assert criteria.failed_count == 2
    assert criteria.failed_metrics == ("nfr1", "nfr2")


def test_failure_not_triggered_on_41_days() -> None:
    """AC-04.2: те же два провала на 41 дне → `false` и `insufficient_window: true`.

    «Ещё нельзя судить» — отдельное утверждение, а не «всё хорошо».
    """
    metrics = (
        _metric("nfr1", value=0.1, passed=False),
        _metric("nfr2", value=1.0, passed=False),
    )
    criteria = _failure_criteria(metrics, FAILURE_WINDOW_DAYS - 1)
    assert criteria.triggered is False
    assert criteria.insufficient_window is True
    assert criteria.failed_count == 2


def test_failure_not_triggered_on_single_failure() -> None:
    """AC-04.1: одного провала недостаточно (порог — «две из четырёх»)."""
    metrics = (
        _metric("nfr1", value=0.1, passed=False),
        _metric("nfr2", value=1.9, passed=True),
        _metric("nfr3", value=0.9, passed=True),
        _metric("nfr4", value=0.9, passed=True),
    )
    assert _failure_criteria(metrics, FAILURE_WINDOW_DAYS).triggered is False


def test_failure_ignores_null_metrics() -> None:
    """AC-04.3: `passed: null` — НЕ провал.

    Один провал + два `null` на 42 днях → критерий не срабатывает. Мутация M7
    (провал = `passed !== true`) обязана покраснеть: она посчитала бы три
    «провала» и объявила отказ по отсутствию данных.
    """
    metrics = (
        _metric("nfr1", value=0.1, passed=False),
        _metric("nfr2", value=None, passed=None),
        _metric("nfr3", value=None, passed=None),
        _metric("nfr4", value=0.9, passed=True),
    )
    criteria = _failure_criteria(metrics, FAILURE_WINDOW_DAYS)
    assert criteria.triggered is False
    assert criteria.failed_count == 1
    assert criteria.failed_metrics == ("nfr1",)
