"""Метрики пилота: пороги, доля, предупреждение и критерий отказа (стори 10.5, Task 3).

AC-01…AC-05, NFR1–NFR6. Слой без HTTP: здесь считаются `value`, `passed`,
`pilot_day`, `warnings` и `failure_criteria`. Роутер (`api/v1/metrics.py`)
только валидирует окно и отдаёт готовое.

**Пороги — единственный источник (TD-10.5-4).** `NFR1_THRESHOLD`…
`NFR4_THRESHOLD`, `NFR6_DAY` и `NFR3_WARNING_THRESHOLD` объявлены ТОЛЬКО
здесь. UI получает их из ответа API и не дублирует: два представления одного
порога разошлись бы при первой правке, и кабинет показывал бы «провал» там,
где сервер считает «норму» (дефект D-TD-6).

**`None` рождается в SQL, не в Python (AC-05.1).** Репозиторий отдаёт
`(numerator, denominator)`, где знаменатель уже прошёл `NULLIF(..., 0)`.
`_ratio` просто возвращает `None` при нулевом знаменателе — `ZeroDivisionError`
здесь не ловится намеренно: `except ZeroDivisionError` замаскировал бы
отсутствие `NULLIF` и сделал бы мутацию M1 неприменимой (класс P-20).

**Провал — только `False`, не «не `True`» (AC-04.3).** `passed: null`
(данных нет) НЕ считается провалом: иначе пустая база на 42-й день
объявляла бы критерий отказа, то есть «пилот провалился, потому что мы ничего
не измеряли». Мутация M7 (`passed !== true`) ловится тестом с двумя `null`
и одним провалом.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.pilot_metrics import PilotMetricsRepository

__all__ = [
    "FAILURE_WINDOW_DAYS",
    "FailureCriteria",
    "Metric",
    "NFR1_THRESHOLD",
    "NFR2_THRESHOLD",
    "NFR3_THRESHOLD",
    "NFR3_WARNING_THRESHOLD",
    "NFR4_THRESHOLD",
    "NFR6_DAY",
    "PilotMetrics",
    "WARNING_NFR1_BELOW_10_PERCENT",
    "build_metrics",
]

#: AC-01, NFR1–NFR4: пороги продукта. Доли — безразмерные, NFR2 — «на карточку».
NFR1_THRESHOLD = 0.30
NFR2_THRESHOLD = 1.5
NFR3_THRESHOLD = 0.60
NFR4_THRESHOLD = 0.25

#: AC-03.2, NFR6: день пилота, на котором включается предупреждение.
NFR6_DAY = 14
#: AC-03.2, NFR6: порог предупреждения — СТРОГО ниже 10%.
#: `<=` превратил бы «ровно 10%» в предупреждение (мутация M8).
NFR3_WARNING_THRESHOLD = 0.10

#: AC-04.2, D-10.5-9: «6 недель» — свойство ОКНА, а не календаря.
FAILURE_WINDOW_DAYS = 42

#: AC-03.2: машинный код предупреждения NFR6.
WARNING_NFR1_BELOW_10_PERCENT = "nfr1_below_10_percent"


@dataclass(frozen=True)
class Metric:
    """Одна метрика в ответе (AC-01.2).

    `numerator`/`denominator` присутствуют всегда, даже при `value: null`:
    они ОБЪЯСНЯЮТ `null` (нулевой знаменатель) — AC-05.2.
    """

    code: str
    value: float | None
    numerator: int
    denominator: int
    threshold: float
    passed: bool | None


@dataclass(frozen=True)
class FailureCriteria:
    """AC-04: критерий отказа за 6 недель.

    `insufficient_window` — не «всё хорошо», а «ещё нельзя судить»
    (D-10.5-9). Короткое окно не делает `triggered: true` и не делает
    `failed_count: 0` молчаливой ложью.
    """

    triggered: bool
    failed_count: int
    window_days: int
    insufficient_window: bool
    failed_metrics: tuple[str, ...]


@dataclass(frozen=True)
class PilotMetrics:
    """Полный ответ кабинета: четыре метрики + предупреждения + критерий."""

    metrics: tuple[Metric, ...]
    failure_criteria: FailureCriteria
    warnings: tuple[str, ...]
    pilot_day: int | None
    window_days: int


def _ratio(numerator: int, denominator: int) -> float | None:
    """AC-05: доля или `None`, если знаменатель нулевой.

    `None`, а не `0.0`: `0.0` означал бы «посчитали и получили ноль», то есть
    настоящий провал. Слить эти два состояния — значит объявить пилот
    провалившимся на пустой базе (AC-05.3, AC-05.4).
    """
    if denominator == 0:
        return None
    return numerator / denominator


def _passed(value: float | None, threshold: float) -> bool | None:
    """AC-01.5: `passed = value >= threshold`; при `null` — `null`, не `False`.

    Граница включительная: ровно 0.30 — уже выполнение (мутация M2 снимает
    равенство и обязана покраснеть). `False` при `null` нарушил бы AC-05.3:
    кабинет показал бы «провалено» вместо «нет данных».
    """
    if value is None:
        return None
    return value >= threshold


def _pilot_day(period_to: datetime, started_at: datetime | None) -> int | None:
    """AC-03.1: номер дня пилота на дату `to`, а НЕ «сегодня» (ADR-16).

    День считается от `to`, потому что администратор смотрит метрики на
    произвольную дату: `datetime.now()` дал бы «14-й день» независимо от
    выбранного окна, и предупреждение NFR6 включалось бы не тогда, когда
    данные действительно относятся к 14-му дню.

    `started_at is None` → `None`: дата старта неизвестна (AC-03.4).

    Отрицательное значение (старт в будущем) отдаётся КАК ЕСТЬ, не
    клампится в `0` (TD-10.5-2): `0` неотличим от «стартовал сегодня», и
    ошибка в конфиге стала бы невидимой. `pilot_day < 14` ложно —
    предупреждение не выводится.
    """
    if started_at is None:
        return None
    return (period_to - started_at).days


def _warnings(pilot_day: int | None, nfr1: Metric) -> tuple[str, ...]:
    """AC-03.2: предупреждение NFR6 на 14-й день при явке ниже 10%.

    Три условия обязательны все: день ≥ 14, значение ЕСТЬ (не `null`),
    значение строго ниже порога. Пустой список — норма, а не «нет проверки».

    Предупреждение не блокирует пилот (AC-03.3): это поле в ответе, ни один
    роут из-за него не отдаёт `403`/`409`.
    """
    if pilot_day is None or pilot_day < NFR6_DAY:
        return ()
    if nfr1.value is None or nfr1.value >= NFR3_WARNING_THRESHOLD:
        return ()
    return (WARNING_NFR1_BELOW_10_PERCENT,)


def _failure_criteria(metrics: tuple[Metric, ...], window_days: int) -> FailureCriteria:
    """AC-04: критерий отказа — ДВА провала и окно не короче 6 недель.

    `passed is False` — именно `is False`, не `not passed`: при `null`
    `not None` истинно, и пустая база объявляла бы отказ (мутация M7).

    Короткое окно (`< 42`) → `triggered: false` и `insufficient_window: true`:
    «ещё нельзя судить» отделено от «всё хорошо» (AC-04.2, мутация M6 на
    `>= 42` → `> 42`).
    """
    failed = tuple(metric.code for metric in metrics if metric.passed is False)
    insufficient = window_days < FAILURE_WINDOW_DAYS
    triggered = not insufficient and len(failed) >= 2
    return FailureCriteria(
        triggered=triggered,
        failed_count=len(failed),
        window_days=window_days,
        insufficient_window=insufficient,
        failed_metrics=failed,
    )


async def build_metrics(
    session: AsyncSession, *, period_from: datetime, period_to: datetime
) -> PilotMetrics:
    """Собирает ответ кабинета: ровно ЧЕТЫРЕ запроса к БД (Task 3.7).

    Срез — `settings.default_city_slug` (AC-01.6, ADR-01): в пилоте одно
    сообщество, и «метрики по всем сообществам сразу» смешали бы два рынка.

    Запросов ровно четыре — по одному на метрику. Пятый запрос (например,
    «сколько авторов всего») сделал бы счётчик `≤ 4` ложью и разошёлся бы с
    числом методов репозитория.
    """
    repository = PilotMetricsRepository(session)
    # Аргументы передаются ЯВНО, а не через `**common`: `dict[str, object]`
    # несовместим с типизированными параметрами (`mypy --strict`), и ошибка
    # в имени ключа стала бы `TypeError` в рантайме вместо ошибки типа.
    city_slug = settings.default_city_slug

    assembled = await repository.count_assembled(
        city_slug=city_slug, period_from=period_from, period_to=period_to
    )
    joined = await repository.count_joined_avg(
        city_slug=city_slug, period_from=period_from, period_to=period_to
    )
    attendance = await repository.count_attendance_confirmed(
        city_slug=city_slug, period_from=period_from, period_to=period_to
    )
    repeat = await repository.count_repeat_authors(
        city_slug=city_slug, period_from=period_from, period_to=period_to
    )

    raw: tuple[tuple[str, tuple[int, int], float], ...] = (
        ("nfr1", assembled, NFR1_THRESHOLD),
        ("nfr2", joined, NFR2_THRESHOLD),
        ("nfr3", attendance, NFR3_THRESHOLD),
        ("nfr4", repeat, NFR4_THRESHOLD),
    )
    metrics = tuple(
        Metric(
            code=code,
            value=(value := _ratio(numerator, denominator)),
            numerator=numerator,
            denominator=denominator,
            threshold=threshold,
            passed=_passed(value, threshold),
        )
        for code, (numerator, denominator), threshold in raw
    )

    window_days = (period_to - period_from).days
    day = _pilot_day(period_to, settings.pilot_started_at)

    return PilotMetrics(
        metrics=metrics,
        failure_criteria=_failure_criteria(metrics, window_days),
        warnings=_warnings(day, metrics[0]),
        pilot_day=day,
        window_days=window_days,
    )
