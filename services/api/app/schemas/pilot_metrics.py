"""Схемы метрик пилота (стори 10.5, Task 4.1; AC-01, AC-04, AC-05).

**`value` и `passed` — `float | None` и `bool | None`.** `null` здесь не
«пустое значение по умолчанию», а осмысленный ответ: знаменатель нулевой,
считать нечего (AC-05). Именно поэтому `numerator`/`denominator` объявлены
`int` БЕЗ `| None`: они обязаны дойти до клиента даже при `null`, иначе
`null` становится необъяснимым (AC-05.2).

**Порог приходит из ответа (TD-10.5-4).** `threshold` лежит в каждой метрике,
а не в UI: два представления одного порога разошлись бы при первой правке.

**`code` — машинный идентификатор** (`nfr1`…`nfr4`), не подпись: подпись
русским текстом живёт в UI, иначе API стал бы владельцем формулировок.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FailureCriteriaRead",
    "MetricRead",
    "PilotMetricsRead",
]


class MetricRead(BaseModel):
    """AC-01.2: одна метрика.

    `passed: null` при `value: null` — НЕ `false` (AC-05.3): кабинет обязан
    показать «нет данных», а не «провалено».
    """

    model_config = ConfigDict(from_attributes=True)

    # AC-01.2: `nfr1`…`nfr4`. Машинный код — по нему клиент ветвится.
    code: str
    # AC-05.1: `null`, когда знаменатель нулевой. Не `0.0`.
    value: float | None
    # AC-05.2: целые ВСЕГДА — они объясняют `value: null`.
    numerator: int
    denominator: int
    # AC-01.2, TD-10.5-4: порог из сервиса, UI не дублирует.
    threshold: float
    # AC-01.5: `value >= threshold`; при `value: null` — `null`.
    passed: bool | None


class FailureCriteriaRead(BaseModel):
    """AC-04: критерий отказа за 6 недель.

    `insufficient_window: true` означает «ещё нельзя судить», и это отдельное
    поле, а не `failed_count: 0` (D-10.5-9): «мало данных» и «всё хорошо» —
    разные утверждения, и слить их значит соврать в отчёте.
    """

    model_config = ConfigDict(from_attributes=True)

    # AC-04.1: `true` только при ≥ 2 провалах И окне ≥ 42 дня.
    triggered: bool
    # AC-04.4: «провалено N из 4».
    failed_count: int
    window_days: int
    # AC-04.2: окно короче 6 недель.
    insufficient_window: bool
    # AC-04.4: перечень проваленных — для текста «что именно ниже порога».
    failed_metrics: list[str] = Field(default_factory=list)


class PilotMetricsRead(BaseModel):
    """AC-01: ответ кабинета — четыре метрики на выбранную дату.

    `pilot_day: null` — дата старта пилота не задана (AC-03.4), НЕ «день 0».
    """

    model_config = ConfigDict(from_attributes=True)

    # AC-01.1: все четыре метрики присутствуют всегда.
    metrics: list[MetricRead]
    failure_criteria: FailureCriteriaRead
    # AC-03.2: коды предупреждений; пустой список — норма.
    warnings: list[str] = Field(default_factory=list)
    # AC-03.1, AC-03.4: день пилота на дату `to`; `null` — старт неизвестен.
    pilot_day: int | None
    window_days: int
