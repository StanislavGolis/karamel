"""Прогон воркера ретенции явки (стори 10.6; Task 3.1, 3.2).

**Task 3.2 — тест регрессии 10.2 БЕЗ правки его файла.** `main()` получил
второй прогон (D-10.6-8), а `test_worker_logs_orphaned_objects` стори 10.2
утверждает `len(captured) == 1`. Лог ретенции эмитится только при
`deleted > 0`, поэтому на фикстуре 10.2 (без просроченных карточек)
второго события нет и чужой тест остаётся зелёным. Здесь фиксируется
ИМЕННО это свойство: при `deleted == 0` в журнал ничего не идёт.

**Почему `monkeypatch` фабрики сессий.** Воркер владеет транзакцией сам
(`factory() as session`), а `main()` крутится вечно — прямой вызов с
настоящей фабрикой ждал бы БД и не давал проверить один проход.
Прецедент — `test_account_deletion_purge.py:520`.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.attendance_retention_service import RetentionResult
from tests.integration.attendance_history_helpers import add_attendance, make_card, make_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _boundary(session: AsyncSession, moment: datetime) -> datetime:
    """Граница «6 месяцев назад» по часам PostgreSQL (D-10.6-6)."""
    row = await session.execute(
        text("SELECT CAST(:m AS timestamptz) - interval '6 months'"), {"m": moment}
    )
    return row.scalar_one()


async def test_run_attendance_retention_returns_result(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.1, AC-01: `run_attendance_retention(moment=…)` → `RetentionResult`.

    Проверяется не только число, но и тип: воркер обязан отдать результат
    наружу (по прецеденту P-10 CR 10.2 — счётчик, который никто не читает,
    это заявление без доказательства).
    """
    from app.workers import retention

    moment = datetime.now(UTC)
    boundary = await _boundary(session, moment)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    _, card_id = await make_card(
        session, author_id=author.id, starts_at=boundary - timedelta(days=5)
    )
    await add_attendance(session, card_id=card_id, participant_id=participant.id, status="pending")
    await session.flush()

    @asynccontextmanager
    async def _factory() -> Any:
        yield session

    monkeypatch.setattr(retention, "get_session_factory", lambda: _factory)

    result = await retention.run_attendance_retention(moment=moment, limit=10)

    assert isinstance(result, RetentionResult)
    assert result.deleted == 1
    assert result.cards == 1
    assert result.duration_ms >= 0


async def test_main_does_not_log_when_nothing_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.2, D-10.6-8: при `deleted == 0` события `attendance_retention_run` НЕТ.

    Это и есть причина, по которой `test_worker_logs_orphaned_objects`
    стори 10.2 остаётся зелёным без правки: его фикстура не содержит
    просроченных карточек, значит второй прогон не пишет в журнал.
    Безусловный лог уронил бы `len(captured) == 1` шумом, а не дефектом.
    """
    from app.workers import retention

    @asynccontextmanager
    async def _factory() -> Any:
        yield session

    captured: list[dict[str, Any]] = []

    class _StopWorker(Exception):
        pass

    async def _stop(_seconds: float) -> None:
        raise _StopWorker

    original_info = retention.logger.info

    def _capture(event: str, **kwargs: Any) -> None:
        if event == "attendance_retention_run":
            captured.append({"event": event, **kwargs})
        original_info(event, **kwargs)

    monkeypatch.setattr(retention, "get_session_factory", lambda: _factory)
    monkeypatch.setattr(retention.asyncio, "sleep", _stop)
    monkeypatch.setattr(retention.logger, "info", _capture)

    with pytest.raises(_StopWorker):
        await retention.main()

    assert captured == [], "при deleted = 0 лог обязан молчать (D-10.6-8)"


async def test_main_logs_attendance_run_when_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-10.6-8, ADR-14: при `deleted > 0` событие несёт числа и `duration_ms`.

    Обратная сторона предыдущего теста: лог обязан БЫТЬ, когда есть что
    логировать, и обязан нести `deleted`/`cards`/`duration_ms` — без
    `card_id` и `participant_id` (project-context §7).
    """
    from app.workers import retention

    moment_now = datetime.now(UTC)
    boundary = await _boundary(session, moment_now)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    _, card_id = await make_card(
        session, author_id=author.id, starts_at=boundary - timedelta(days=5)
    )
    await add_attendance(session, card_id=card_id, participant_id=participant.id, status="pending")
    await session.flush()

    @asynccontextmanager
    async def _factory() -> Any:
        yield session

    captured: list[dict[str, Any]] = []

    class _StopWorker(Exception):
        pass

    async def _stop(_seconds: float) -> None:
        raise _StopWorker

    original_info = retention.logger.info

    def _capture(event: str, **kwargs: Any) -> None:
        if event == "attendance_retention_run":
            captured.append({"event": event, **kwargs})
        original_info(event, **kwargs)

    monkeypatch.setattr(retention, "get_session_factory", lambda: _factory)
    monkeypatch.setattr(retention.asyncio, "sleep", _stop)
    monkeypatch.setattr(retention.logger, "info", _capture)

    with pytest.raises(_StopWorker):
        await retention.main()

    assert len(captured) == 1
    assert captured[0]["deleted"] == 1
    assert captured[0]["cards"] == 1
    assert "duration_ms" in captured[0]
    # ADR-14, §7: идентификаторы в лог не идут.
    assert "card_id" not in captured[0]
    assert "participant_id" not in captured[0]
