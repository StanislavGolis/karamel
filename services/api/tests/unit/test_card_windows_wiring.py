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
import structlog.testing

import app.workers.card_windows as worker_module
from app.repositories.event_cards import EventCardRepository

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


# ——— Стори 11.8: гард на ЭФФЕКТ контроля, а не на форму вызова (P-25) ———


def _patch_overdue_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
    alert_ok: bool = True,
    calls: dict[str, Any] | None = None,
) -> list[datetime]:
    """Готовит прогон, в котором контроль РАБОТАЕТ, а не подменяется целиком.

    Ключевое отличие от `_patch_steps` выше: `check_overdue_windows` НЕ
    патчится. Патчится то, от чего зависит его эффект — метод репозитория
    `count_overdue_windows` (AC-3) и внешние зависимости (`send_alert`,
    `get_redis`), которые без подмены полезли бы в Redis/HTTP.

    Возвращает журнал моментов `now`, с которыми вызывался счётчик: тест
    обязан доказать, что контроль считает по ТОМУ ЖЕ моменту, что и
    переходы прогона.
    """
    counted_at: list[datetime] = []
    steps: dict[str, Any] = calls if calls is not None else {}
    _patch_steps(monkeypatch, steps)

    async def _count(self: Any, *, now: datetime, grace_minutes: int = 5) -> int:
        counted_at.append(now)
        return count

    monkeypatch.setattr(EventCardRepository, "count_overdue_windows", _count)

    alerts: list[str] = []

    async def _alert(text: str, *, key: str, redis: Any, client: Any = None) -> bool:
        alerts.append(text)
        return alert_ok

    monkeypatch.setattr(worker_module, "send_alert", _alert)
    monkeypatch.setattr(worker_module, "get_redis", lambda: object())
    steps["alerts"] = alerts
    return counted_at


async def test_run_once_overdue_control_has_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3, AC-4, P-25: контроль обязан ДАТЬ ЭФФЕКТ, а не просто вызваться.

    Краснеет на мутации «тело контроля обезврежено» (`overdue = 0` вместо
    запроса к репозиторию) — ровно та мутация, которую существующий
    wiring-тест пропускал (2 passed). Ловится потому, что патчится не
    функция контроля, а МЕТОД РЕПОЗИТОРИЯ: обезвреженное тело перестаёт
    его звать, счётчик остаётся невостребованным, лог не появляется.
    """
    # CR 11.8: число 7, а НЕ 3 — на литерале `3` мутация «залогировать
    # константу 3 вместо счётчика» выживала (M-11.8-g). Ноль тоже
    # не годится: он неотличим от ветки «здоровый воркер».
    calls: dict[str, Any] = {}
    counted_at = _patch_overdue_dependencies(monkeypatch, count=7, calls=calls)

    with structlog.testing.capture_logs() as captured:
        await worker_module.run_once(_session_factory(), now=NOW)

    events = [entry["event"] for entry in captured]
    assert "worker.window.overdue" in events, (
        f"контроль не дал эффекта при count=7, журнал: {events}"
    )
    overdue_entries = [e for e in captured if e["event"] == "worker.window.overdue"]
    # CR 11.8, M-11.8-g: именно ЗНАЧЕНИЕ счётчика, а не факт события.
    assert overdue_entries[0]["count"] == 7, (
        f"в лог ушло не число зависших окон: {overdue_entries[0]}"
    )
    # CR 11.8, M-11.8-k: уровень `error`, а не любой. `capture_logs()` без
    # проверки уровня одинаково зеленеет на `logger.warning` — а по D-10.7-6
    # это тревожный канал, и понижение уровня до `warning` утопило бы его
    # в штатном потоке.
    assert overdue_entries[0]["log_level"] == "error", (
        f"событие зависшего окна залогировано не как error: {overdue_entries[0]}"
    )
    # CR 11.8, M-11.8-l: алерт обязан уйти. Без этого ассерта `send_alert`
    # можно удалить целиком — лог останется, CI зелёный, человек не узнает.
    alerts = calls["alerts"]
    assert alerts, "контроль не отправил алерт при count=7"
    assert "count=7" in alerts[0], f"в алерте нет числа окон: {alerts[0]}"
    # Момент тот же, что у переходов прогона: иначе на границе пяти минут
    # контроль давал бы то ложный алерт, то пропуск.
    assert counted_at == [NOW], f"счётчик вызван не по now прогона: {counted_at}"


async def test_run_once_overdue_control_silent_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3: при `count == 0` события быть НЕ ДОЛЖНО.

    Без этого ассерта мутация «логировать всегда» выживала бы: тест на
    count=3 зеленел бы и на безусловном `logger.error`. Контроль обязан
    молчать на здоровом воркере — иначе алерт-канал замьючен в первый день.
    """
    _patch_overdue_dependencies(monkeypatch, count=0)

    with structlog.testing.capture_logs() as captured:
        await worker_module.run_once(_session_factory(), now=NOW)

    events = [entry["event"] for entry in captured]
    assert "worker.window.overdue" not in events, (
        f"контроль залогировал зависшее окно при count=0: {events}"
    )


async def test_run_once_overdue_alert_failure_does_not_block_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3, D-10.7-3: отказ алерта НЕ отменяет работу воркера.

    Контроль — диагностика: `send_alert` возвращает `False` на любом отказе
    (Redis недоступен, бот заблокирован). Если бы отказ пробрасывался,
    падение алерта отменяло бы УЖЕ ВЫПОЛНЕННЫЕ переходы прогона — то есть
    диагностика ломала бы продукт ровно тогда, когда он и так нездоров.
    """
    calls: dict[str, Any] = {}
    _patch_overdue_dependencies(monkeypatch, count=2, alert_ok=False, calls=calls)

    with structlog.testing.capture_logs() as captured:
        result = await worker_module.run_once(_session_factory(), now=NOW)

    assert set(calls) >= {"reminders", "decisions", "finish", "prompts"}
    events = [entry["event"] for entry in captured]
    assert "worker.window.overdue" in events
    assert isinstance(result, dict)
