"""Порядок шагов `card_windows.run_once` (стори 7.3; S41, S49, D-TD-6).

**Почему новый тест, а не только правка `test_decision_reminders.py`**
(D-TD-6, score 3). Существующий тест 5б.4 не перехватывает
`finish_started_cards` вообще, поэтому порядок «prompts ПОСЛЕ finish» им
недоказуем: он видел бы `[reminders, decisions, prompts]` и при вставке
шага ДО архивации. Доказательство живёт здесь, где перехвачены все четыре.

Почему порядок важен по сути (Task 4.4): предикат воркера —
`status IN (full, closed, finished)`, а `published`-карточка с наступившим
`starts_at` становится `finished` именно шагом архивации. Поставь запрос
раньше — карточка не попадёт в выборку этого прогона, и поведение станет
недетерминированным по фазе цикла (NFR22 в запас, но не случайность).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.workers.card_windows import run_once
from tests.integration.attendance_prompt_helpers import _cleanup_seed, factory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]


async def test_run_once_calls_prompts_after_finish_started(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S41 (D-TD-6): последовательность ровно `reminders → decisions →
    finish_started → prompts`.

    Краснеет от двух правок: шаг `prompts` перед `finish_started_cards` (R14)
    и шаг, забытый в `run_once` вовсе. Перехват последовательности —
    единственный способ это увидеть: статусы карточек при перестановке
    совпадают, результат прогона тоже.
    """
    moment = datetime.now(UTC)
    order: list[str] = []

    from app.workers import card_windows as windows_module

    real_reminders = windows_module.run_reminders
    real_decisions = windows_module.run_decisions
    real_finish = windows_module.finish_started_cards
    real_prompts = windows_module.run_attendance_prompts

    async def _reminders(*args: Any, **kwargs: Any) -> Any:
        order.append("reminders")
        return await real_reminders(*args, **kwargs)

    async def _decisions(*args: Any, **kwargs: Any) -> Any:
        order.append("decisions")
        return await real_decisions(*args, **kwargs)

    async def _finish(*args: Any, **kwargs: Any) -> Any:
        order.append("finish_started")
        return await real_finish(*args, **kwargs)

    async def _prompts(*args: Any, **kwargs: Any) -> Any:
        order.append("prompts")
        return await real_prompts(*args, **kwargs)

    monkeypatch.setattr(windows_module, "run_reminders", _reminders)
    monkeypatch.setattr(windows_module, "run_decisions", _decisions)
    monkeypatch.setattr(windows_module, "finish_started_cards", _finish)
    monkeypatch.setattr(windows_module, "run_attendance_prompts", _prompts)

    await run_once(factory, now=moment, reminder_threshold_minutes=15)

    assert order == ["reminders", "decisions", "finish_started", "prompts"]


async def test_run_once_returns_old_and_new_counters(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """S49: словарь `run_once` содержит старые ключи И новые ключи 7.3.

    Проверено перед реализацией: ни один существующий тест не сравнивает
    словарь целиком (единственное полное сравнение — `run_reminders`), поэтому
    расширение безопасно. Тест фиксирует это решение: удаление старого ключа
    сломает наблюдаемость 5б.3/5б.4, а исчезновение нового — отчёт 7.3.
    """
    moment = datetime.now(UTC)

    counters = await run_once(factory, now=moment, reminder_threshold_minutes=15)

    assert {
        "closed",
        "finished",
        "reminded",
        "reminder_failed",
        "prompted",
        "prompt_skipped",
        "prompt_failed",
    } <= set(counters)
    assert all(isinstance(value, int) for value in counters.values())
