"""Порядок операций и приватность лога при решении по жалобе (8.4; D-47, NFR32).

**Почему порядок доказывается перехватом, а не итоговым состоянием** (§18.11).
Ассерт по состоянию БД порядок не доказывает: обратный порядок даёт тот же
результат на одном соединении. Здесь записывается фактическая
последовательность вызовов `session.execute`, и проверяется, что
`SELECT ... FOR UPDATE` идёт ПЕРВЫМ — до чтения пары, до вставки санкции и
до CAS-обновления.

**Что даёт тест.** Он краснеет ровно при перестановке шагов (например, если
проверку `status` вынести до блокировки — класс H-1 из CR 8.2). Мутации
«снять `AND status='open'`» он не заменяет: ту ловит race-тест.

**Почему фейковая сессия, а не мок `AsyncSession`.** Нужна только
`execute`, а результат каждого запроса задаётся по тексту SQL. Полноценный
мок тянул бы за собой диалект и скрыл бы порядок за побочными эффектами.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services import moderation_resolve_service as service


class _FakeResult:
    """Минимальный результат: `.first()`/`.scalar_one_or_none()`/`.all()`."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0][0] if self._rows else None


class _FakeSession:
    """Пишет порядок SQL и отдаёт заранее заданные строки."""

    def __init__(self, *, report_row: tuple[Any, ...], accused_id: int) -> None:
        self.calls: list[str] = []
        self._report_row = report_row
        self._accused_id = accused_id
        self._sanction = uuid_lib.uuid4()
        self._resolved_at = datetime.now(UTC)

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        self.calls.append(sql)
        if "FOR UPDATE OF r" in sql:
            return _FakeResult([self._report_row])
        if sql.startswith("SELECT accused.user_id"):
            return _FakeResult([(self._accused_id,)])
        if sql.startswith("INSERT INTO sanctions"):
            return _FakeResult([(self._sanction,)])
        if sql.startswith("UPDATE reports"):
            return _FakeResult([(self._resolved_at,)])
        if sql.startswith("UPDATE users"):
            return _FakeResult([])
        if sql.startswith("INSERT INTO notifications"):
            return _FakeResult([])
        raise AssertionError(f"Неожиданный SQL: {sql[:80]}")


@pytest.mark.asyncio
async def test_lock_is_the_first_statement_with_status_read_under_it() -> None:
    """D-47: `FOR UPDATE` первым, и `status` читается В ТОМ ЖЕ запросе.

    Второй ассерт — не формальность: если статус вынести отдельным `SELECT`
    до блокировки, между ним и блокировкой пройдёт конкурент, и решение будет
    принято по устаревшим данным (класс H-1 из CR 8.2).
    """
    report_uuid = uuid_lib.uuid4()
    report_row = (1, "no_show", None, 7, 42, "open", None)
    fake = _FakeSession(report_row=report_row, accused_id=99)

    async def execute(statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        fake.calls.append(sql)
        if "FOR UPDATE OF r" in sql:
            return _FakeResult([report_row])
        if sql.startswith("SELECT a.status"):
            return _FakeResult([("no_show",)])
        if sql.startswith("SELECT accused.user_id"):
            return _FakeResult([(99,)])
        if sql.startswith("INSERT INTO sanctions"):
            return _FakeResult([(uuid_lib.uuid4(),)])
        if sql.startswith("UPDATE reports"):
            return _FakeResult([(datetime.now(UTC),)])
        if sql.startswith("UPDATE users"):
            return _FakeResult([])
        if sql.startswith("INSERT INTO notifications"):
            return _FakeResult([])
        raise AssertionError(f"Неожиданный SQL: {sql[:80]}")

    fake.execute = execute  # type: ignore[method-assign]

    await service.resolve_report(
        fake,  # type: ignore[arg-type]
        report_uuid=report_uuid,
        moderator_id=5,
        resolution="warning",
        reason="Причина",
    )

    lock_index = next(i for i, sql in enumerate(fake.calls) if "FOR UPDATE OF r" in sql)
    # Первый вызов — блокировка. Если статус вынести отдельным `SELECT` ДО неё,
    # этот вызов станет нулевым и ассерт покраснеет — отдельная проверка
    # «нет standalone-чтения статуса» не нужна и ловила бы `_ACCUSED_SQL`.
    assert lock_index == 0, fake.calls
    # `status` входит в проекцию блокирующего запроса — читается под ним.
    assert "r.status" in fake.calls[0], fake.calls[0]

    # Порядок записи: санкция → жалоба → пользователь → уведомления.
    insert_sanction = next(
        i for i, sql in enumerate(fake.calls) if sql.startswith("INSERT INTO sanctions")
    )
    update_report = next(i for i, sql in enumerate(fake.calls) if sql.startswith("UPDATE reports"))
    update_user = next(i for i, sql in enumerate(fake.calls) if sql.startswith("UPDATE users"))
    first_notification = next(
        i for i, sql in enumerate(fake.calls) if sql.startswith("INSERT INTO notifications")
    )
    assert insert_sanction < update_report < update_user < first_notification, fake.calls


@pytest.mark.asyncio
async def test_dismissed_skips_sanction_and_accused_notification() -> None:
    """D-46, M11: путь `dismissed` не вставляет санкцию и не пишет `users`.

    Проверяется СОСТАВ вызовов, а не результат: именно отсутствие запросов
    отличает закрытие без санкции от санкции с пустым результатом.
    """
    report_uuid = uuid_lib.uuid4()
    report_row = (1, "message", 3, None, 42, "open", uuid_lib.uuid4())
    fake = _FakeSession(report_row=report_row, accused_id=99)

    async def execute(statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        fake.calls.append(sql)
        if "FOR UPDATE OF r" in sql:
            return _FakeResult([report_row])
        if sql.startswith("SELECT accused.user_id"):
            return _FakeResult([(99,)])
        if sql.startswith("UPDATE reports"):
            return _FakeResult([(datetime.now(UTC),)])
        if sql.startswith("INSERT INTO notifications"):
            return _FakeResult([])
        raise AssertionError(f"Неожиданный SQL на пути dismissed: {sql[:80]}")

    fake.execute = execute  # type: ignore[method-assign]

    await service.resolve_report(
        fake,  # type: ignore[arg-type]
        report_uuid=report_uuid,
        moderator_id=5,
        resolution="dismissed",
        reason="Нарушения нет",
    )

    assert not any(sql.startswith("INSERT INTO sanctions") for sql in fake.calls), fake.calls
    assert not any(sql.startswith("UPDATE users") for sql in fake.calls), fake.calls
    # Ровно одно уведомление — автору жалобы.
    notifications = [sql for sql in fake.calls if sql.startswith("INSERT INTO notifications")]
    assert len(notifications) == 1, fake.calls


def test_cas_predicate_is_present_in_update_sql() -> None:
    """D-47: CAS-предикат `status = 'open'` в тексте `UPDATE reports` (M2).

    **Почему проверяется ТЕКСТ, а не поведение.** Race-тест 6.4 до CAS не
    доходит: проигравший упирается в `SELECT ... FOR UPDATE`, после коммита
    победителя перечитывает строку и получает `409` на шаге (2) — до
    `UPDATE reports` он не добирается. Проверено мутацией: снятие
    `AND status = 'open'` оставляет race-тест ЗЕЛЁНЫМ.

    Следствие, которое нельзя замалчивать: CAS — **вторая линия, а не
    первая**. Она защищает от рассинхрона, если статус всё же прочитан
    устаревшим (снятие `FOR UPDATE`, другая изоляция, будущий вызов из
    воркера). §18.3 разрешает для таких случаев unit на текст запроса.
    """
    sql = " ".join(service._UPDATE_REPORT_SQL.split())
    assert "UPDATE reports" in sql, sql
    assert "AND status = 'open'" in sql, sql
    assert "RETURNING resolved_at" in sql, sql


def test_lock_predicate_is_present_in_lock_sql() -> None:
    """D-47: `FOR UPDATE` в тексте блокирующего запроса.

    Поведенчески снятие блокировки race-тест не краснит (см. выше), но
    блокировка — обязательный элемент порядка: она сериализует двух
    модераторов ДО проверки статуса. Тест на текст фиксирует её наличие.
    """
    sql = " ".join(service._LOCK_REPORT_SQL.split())
    assert "FOR UPDATE OF r" in sql, sql
    # Статус читается В ТОМ ЖЕ запросе — иначе класс H-1 из CR 8.2.
    assert "r.status" in sql, sql


@pytest.mark.asyncio
async def test_cas_failure_raises_and_writes_nothing_after_sanction() -> None:
    """D-47, AC-05: провал CAS после вставки санкции → `409`, дальше ничего.

    Ловушка: если `None` из `UPDATE reports` проигнорировать, санкция останется
    без решения, а `users.status` сменится — «санкция-сирота» (AC-05). Тест
    проверяет, что после провала CAS сервис бросает ДО `UPDATE users` и ДО
    уведомлений; откат транзакции делает `get_session`.
    """
    report_uuid = uuid_lib.uuid4()
    report_row = (1, "message", 3, None, 42, "open", uuid_lib.uuid4())
    fake = _FakeSession(report_row=report_row, accused_id=99)
    calls: list[str] = []

    async def execute(statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        calls.append(sql)
        if "FOR UPDATE OF r" in sql:
            return _FakeResult([report_row])
        if sql.startswith("SELECT accused.user_id"):
            return _FakeResult([(99,)])
        if sql.startswith("INSERT INTO sanctions"):
            return _FakeResult([(uuid_lib.uuid4(),)])
        if sql.startswith("UPDATE reports"):
            # CAS не обновил ни одной строки: жалобу закрыл кто-то другой.
            return _FakeResult([])
        raise AssertionError(f"После провала CAS запрос недопустим: {sql[:80]}")

    fake.execute = execute  # type: ignore[method-assign]

    with pytest.raises(service.ReportAlreadyResolvedError):
        await service.resolve_report(
            fake,  # type: ignore[arg-type]
            report_uuid=report_uuid,
            moderator_id=5,
            resolution="warning",
            reason="Причина",
        )

    # `UPDATE users` и уведомления не выполнялись: raise стоит между ними.
    assert not any(sql.startswith("UPDATE users") for sql in calls), calls
    assert not any(sql.startswith("INSERT INTO notifications") for sql in calls), calls
