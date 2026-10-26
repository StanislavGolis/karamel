"""Форма SQL и результат сервиса ретенции явки (стори 10.6; Task 2.1, 2.2).

**Зачем проверять исходник, а не поведение** (Task 2.1, P-16). Мутация
`interval '6 months'` → `interval '182 days'` НЕ меняет ответ на данных
фикстуры: карточка «на 10 дней старше границы» удаляется и так. Поведением
различие 6 календарных месяцев и 182 дней ловится только на конкретных датах
(тест границы в `test_attendance_retention_queries.py`), а требование
D-10.6-6 — про ФОРМУ предиката. Тот же приём, что `NULLIF` 4/4 в 10.5.

**Почему текст берётся из репозитория, а не из литерала теста.** Копия
запроса в тесте не увидела бы правку репозитория: мутация осталась бы
SURVIVED (урок 10.2, `build_due_candidates_statement`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.repositories.attendance_retention import candidate_statement_sql
from app.services.attendance_retention_service import (
    ATTENDANCE_RETENTION_MONTHS,
    RetentionResult,
    purge_expired_attendance,
)


def test_sql_uses_six_calendar_months() -> None:
    """D-10.6-6: предикат содержит `interval '6 months'`, не `182 days`.

    Шесть календарных месяцев = 181–184 дня. `timedelta(days=182)` удалил бы
    данные на 1–3 дня РАНЬШЕ срока в месяцах с 31 днём, нарушив AC-01 и NFR43.
    Арифметику месяцев обязан делать PostgreSQL, а не Python.
    """
    sql = candidate_statement_sql()

    assert "interval '6 months'" in sql
    assert "182" not in sql
    assert "timedelta" not in sql
    assert ATTENDANCE_RETENTION_MONTHS == 6


def test_candidate_statement_has_exists_and_skip_locked() -> None:
    """D-10.6-5: `EXISTS attendance` и `SKIP LOCKED` присутствуют в выборке.

    Без `EXISTS` очередь заклинивает на пустых карточках; без `SKIP LOCKED`
    второй контейнер воркера ждёт первый (NFR46–NFR49). Поведением оба
    проверяются интеграционно; здесь — перечень требований к тексту, чтобы
    правка запроса не потеряла ни одного молча.
    """
    sql = candidate_statement_sql()

    assert "EXISTS (SELECT 1 FROM attendance" in sql
    assert "SKIP LOCKED" in sql


def test_candidate_statement_filters_by_starts_at_not_created_at() -> None:
    """AC-01: срок считается от `starts_at` (момент встречи), не от `created_at`.

    `created_at` карточки раньше встречи, поэтому предикат по нему удалял бы
    данные раньше шести месяцев после встречи.
    """
    sql = candidate_statement_sql()

    assert "ec.starts_at <" in sql
    assert "created_at" not in sql


def test_retention_result_carries_numbers() -> None:
    """Task 2.2, ADR-14: `RetentionResult` несёт `deleted`, `cards`, `duration_ms`.

    Все три нужны логу: `deleted` решает, писать ли событие; `cards` отличает
    «одна карточка, сто строк» от «сто карточек, сто строк»; `duration_ms`
    обязывает ADR-14. Счётчик, который никто не читает, — заявление без
    доказательства (класс P-10 CR 10.2).
    """
    result = RetentionResult(deleted=5, cards=2, duration_ms=17)

    assert result.deleted == 5
    assert result.cards == 2
    assert result.duration_ms == 17
    assert RetentionResult().deleted == 0


def test_negative_limit_is_rejected_before_sql() -> None:
    """P-15 (CR 10.2): `limit < 1` — `ValueError`, а не `LIMIT must not be negative`.

    Молчаливый `0` скрыл бы ошибку вызывающего кода навсегда, а пропуск
    отрицательного значения до PostgreSQL дал бы техническое исключение
    каждый цикл воркера.
    """
    from app.repositories.attendance_retention import AttendanceRetentionRepository

    with pytest.raises(ValueError, match="limit must be positive"):
        AttendanceRetentionRepository._validate_limit(0)


@pytest.mark.parametrize("moment", [datetime(2026, 10, 27, tzinfo=UTC)])
async def test_service_signature_accepts_injected_moment(moment: datetime) -> None:
    """D-10.2-4, P-19: `moment` — параметр, не `datetime.now()` внутри.

    Сервис обязан принимать момент снаружи: иначе тест границы ждал бы
    полгода или подменял бы системные часы (`freezegun` запрещён правилами
    тестов). Проверяется сигнатура — вызов с собственным `moment`
    удовлетворяет протоколу.
    """
    import inspect

    parameters = inspect.signature(purge_expired_attendance).parameters

    assert "moment" in parameters
    assert "limit" in parameters
    assert parameters["moment"].kind is inspect.Parameter.KEYWORD_ONLY
