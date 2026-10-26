"""Сервис ретенции фактов явки — 6 месяцев после встречи (стори 10.6; AC-01…AC-04).

**Что удаляется.** Только строки `attendance` (NFR43, D-10.6-2). Карточка
`event_cards` остаётся архивом — `architecture:74` требует именно этого
(«требование к retention-воркеру и к тому, что карточка-архив не
удаляется»). «Каскадом» в AC-01 значит способ «удалить, а не обезличить»
(ADR-07 «Отвергнуто (а)»: `SET NULL` невозможен — `participant_id` NOT NULL)
и производный каскад `reports` → `sanctions` (D-10.6-3).

**Срок считает PostgreSQL** (D-10.6-6). `timedelta(days=182)` короче шести
календарных месяцев и удалил бы данные раньше срока. Арифметику месяцев
обязан делать PostgreSQL, поэтому предикат — `interval '<N> months'` в SQL
репозитория, а не Python.

**`ATTENDANCE_RETENTION_MONTHS` объявлена в РЕПОЗИТОРИИ и ре-экспортируется
здесь** (P-2 CR 10.6). До патча константа жила в сервисе, а `6` дублировалось
литералом в SQL: мутация константы не красила тест границы срока. Теперь
значение одно — оно подставляется в текст запроса, а сервис только
переиздаёт имя для вызывающих. Обратный импорт (сервис → репозиторий)
был бы циклическим, поэтому источник истины в репозитории.

**`moment` — параметр, не `datetime.now()` внутри** (D-10.2-4, P-19): тест не
ждёт полгода и не подменяет конфиг. Заодно это делает границы срока
проверяемыми с обеих сторон (P-19).

**AC-04 — где он выполняется, а где нет.** Метрика NFR3 — фильтр
`created_at ∈ [period_from, period_to)` по `event_cards`
(`pilot_metrics.py:75-80`). Удаляемая карточка имеет
`created_at < starts_at < moment - 6 мес`, поэтому при
`period_from >= moment - 6 мес` её в окне нет и значение не меняется.
При более раннем начале окна значение МЕНЯЕТСЯ — это разрыв W-10.6-a (HIGH),
зафиксированный тестом-документом, а не замолчанный (D-10.6-1).

**Наблюдаемость (ADR-14).** Событие `attendance_retention_run` несёт ЧИСЛА
(`deleted`, `cards`, `duration_ms`); ни `card_id`, ни `participant_id` в лог
не идут (project-context §7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.attendance_retention import (
    ATTENDANCE_RETENTION_MONTHS,
    AttendanceRetentionRepository,
)

__all__ = [
    "ATTENDANCE_RETENTION_MONTHS",
    "RetentionResult",
    "purge_expired_attendance",
]


@dataclass(frozen=True)
class RetentionResult:
    """AC-01, AC-03: результат одного прогона ретенции.

    `deleted` — число ФАКТИЧЕСКИ удалённых строк `attendance`, не «сколько
    карточек найдено». `cards` — сколько карточек затронуто. Оба нужны логу:
    по `deleted == 0` прогон не логируется вовсе (D-10.6-8), иначе шум
    каждые 60 секунд.
    """

    deleted: int = 0
    cards: int = 0
    duration_ms: int = 0


async def purge_expired_attendance(
    session: AsyncSession, *, moment: datetime, limit: int
) -> RetentionResult:
    """AC-01, AC-03: удаление фактов явки карточек, чей срок хранения истёк.

    Порядок:
    1. выбрать карточки `starts_at < moment - interval '6 months'` с непустым
       `attendance` (`FOR UPDATE SKIP LOCKED`, D-10.6-5);
    2. одним `DELETE ... WHERE card_id = ANY(...)` убрать строки пачки;
    3. `commit` — воркер владеет транзакцией сам, роутера над ним нет.

    Пустой список кандидатов — ранний выход: `DELETE` с пустым массивом
    вернул бы 0, но прошёл бы до БД зря каждые 60 секунд.

    Идемпотентность (AC-03) держится `EXISTS attendance` в выборке: второй
    прогон на тех же данных не видит карточек без строк и возвращает 0.
    """
    started = time.monotonic()
    repository = AttendanceRetentionRepository(session)
    card_ids = await repository.select_expired_card_ids(moment=moment, limit=limit)
    if not card_ids:
        return RetentionResult(duration_ms=int((time.monotonic() - started) * 1000))

    deleted = await repository.delete_attendance_for_cards(card_ids)
    await session.commit()
    duration_ms = int((time.monotonic() - started) * 1000)

    # Лог прогона живёт в ВОРКЕРЕ (прецедент 10.2: `account_purge_run` в
    # `main()`), а не здесь: сервис возвращает числа, наблюдаемость — слой
    # воркера (project-context §6, ADR-14). Здесь остаётся только ошибка.
    return RetentionResult(
        deleted=deleted,
        cards=len(card_ids) if deleted else 0,
        duration_ms=duration_ms,
    )
