"""Проводка контроля зависших окон в `run_once` (стори 10.7, P-3 из CR; AC-03).

**Что было не так.** Функция `check_overdue_windows` покрыта отлично
(`tests/integration/test_overdue_windows.py`), но САМ ФАКТ её вызова из
`run_once` не проверял ни один тест. Замерено ревьюером: мутация M10
(вызов закомментирован) дала `6 passed`. То есть контроль можно было
удалить из воркера целиком, CI остался бы зелёным, а на стенде событие
`worker.window.overdue` не появилось бы никогда — и никто бы не заметил
до инцидента.

**Почему unit, а не integration.** Проверяется ПРОВОДКА (одна строка вызова
и её аргументы), а не SQL-предикат. Предикат проверен на живой PostgreSQL
отдельно; здесь шаги воркера подменены, и прогон занимает миллисекунды
вместо секунд на переходах карточек.

**Почему проверяется ещё и `now`.** Контроль обязан считать по ТОМУ ЖЕ
моменту, что и переходы прогона: с собственным `datetime.now(UTC)` он
сравнивал бы окна с другим временем, и на границе пяти минут давал бы
то ложный алерт, то пропуск.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

import app.workers.card_windows as worker_module

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _patch_steps(monkeypatch: pytest.MonkeyPatch, calls: dict[str, Any]) -> None:
    """Подменяет все шаги `run_once`, кроме проверяемого.

    Каждая подмена фиксируется в `calls`: если шаг перестанет вызываться,
    тест покраснеет на своей проверке, а не на побочном эффекте чужого.
    """

    async def _reminders(
        session_factory: Any, *, now: datetime, threshold_minutes: int, limit: int
    ) -> dict[str, int]:
        calls["reminders"] = now
        return {"reminded": 0, "failed": 0}

    async def _decisions(session_factory: Any, *, now: datetime, limit: int) -> dict[str, int]:
        calls["decisions"] = now
        return {"selected": 0, "closed_no_choice": 0}

    async def _finish(session: Any, *, now: datetime, limit: int) -> int:
        calls["finish"] = now
        return 0

    async def _prompts(session_factory: Any, *, now: datetime, limit: int) -> dict[str, int]:
        calls["prompts"] = now
        return {"prompted": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(worker_module, "run_reminders", _reminders)
    monkeypatch.setattr(worker_module, "run_decisions", _decisions)
    monkeypatch.setattr(worker_module, "finish_started_cards", _finish)
    monkeypatch.setattr(worker_module, "run_attendance_prompts", _prompts)


def _session_factory() -> Any:
    """Минимальная фабрика: `run_once` открывает сессию для архивации."""

    @asynccontextmanager
    async def _manager() -> AsyncIterator[object]:
        yield object()

    return _manager


async def test_run_once_calls_overdue_windows_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03, P-3: `run_once` ОБЯЗАН вызвать контроль зависших окон.

    Краснеет, если вызов закомментировать (мутация M10 ревьюера) — ровно
    та дыра, из-за которой AC-03 не был замкнут.
    """
    calls: dict[str, Any] = {}
    overdue_calls: list[dict[str, Any]] = []

    async def _overdue(session_factory: Any, *, now: datetime, duration_ms: int) -> int:
        overdue_calls.append({"now": now, "duration_ms": duration_ms})
        return 0

    _patch_steps(monkeypatch, calls)
    monkeypatch.setattr(worker_module, "check_overdue_windows", _overdue)

    await worker_module.run_once(_session_factory(), now=NOW)

    assert len(overdue_calls) == 1, "run_once не вызвал контроль зависших окон (AC-03)"
    # Момент времени обязан быть тем же, что у переходов прогона.
    assert overdue_calls[0]["now"] == NOW
    assert overdue_calls[0]["duration_ms"] >= 0
    # Остальные шаги тоже отработали: иначе тест доказывал бы вызов контроля
    # в прогоне, который вообще ничего не делает.
    assert set(calls) == {"reminders", "decisions", "finish", "prompts"}


async def test_run_once_calls_overdue_after_all_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-10.7-6: контроль считается ПОСЛЕ всех переходов.

    Иначе карточка, закрытая этим же прогоном, попала бы в контроль — и
    воркер алертил бы на собственную штатную работу. Порядок проверяется
    журналом вызовов, а не глазами по коду: перестановка строки обязана
    красить тест.
    """
    calls: dict[str, Any] = {}
    order: list[str] = []

    async def _overdue(session_factory: Any, *, now: datetime, duration_ms: int) -> int:
        order.append("overdue")
        return 0

    _patch_steps(monkeypatch, calls)
    monkeypatch.setattr(worker_module, "check_overdue_windows", _overdue)

    async def _tracking_prompts(
        session_factory: Any, *, now: datetime, limit: int
    ) -> dict[str, int]:
        order.append("prompts")
        return {"prompted": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(worker_module, "run_attendance_prompts", _tracking_prompts)

    await worker_module.run_once(_session_factory(), now=NOW)

    assert order == ["prompts", "overdue"], f"порядок шагов нарушен: {order}"
