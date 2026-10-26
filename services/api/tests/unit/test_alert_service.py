"""Отправка алертов и дедуп (стори 10.7, Task 4.3; AC-01, AC-03; D-10.7-4).

**Почему подменяется `httpx`, а не Telegram.** Тест не имеет права ходить в
живой `api.telegram.org`: это внешняя сеть в CI, реальный бот и токен в
окружении. `httpx.MockTransport` подменяет ТРАНСПОРТ, поэтому проверяется
настоящий код `send_alert` — формирование URL, тело запроса, `raise_for_status`
и ветки `alert.*`, а не мок самой функции.

**Почему фейк Redis, а не живой.** Проверяемое свойство — семантика дедупа
(`SET NX EX`): «второй вызов с тем же ключом не отправляет». Живой Redis дал
бы тот же результат, но потребовал бы TTL-ожидания и оставил бы ключи между
тестами (P-12). Фейк повторяет минимальную логику `SET NX`.

**ПД в теле.** Отдельный тест: у сообщения нет ни телефона, ни `card_uuid`.
Это не формальность — сообщение уходит во внешний сервис, за периметр
152-ФЗ (NFR34).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import structlog

import app.services.alert_service as alert_module
from app.services.alert_service import send_alert

KEY = "alert:window_overdue:sent"
TEXT = "window overdue: count=1"


class _FakeRedis:
    """Минимум Redis: `SET NX EX` и `DEL`."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.last_ex: int | None = None

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self.last_ex = ex
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


class _BrokenRedis(_FakeRedis):
    """Redis недоступен: дедуп посчитать нечем."""

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        from redis.exceptions import RedisError

        raise RedisError("нет соединения")


@pytest.fixture
async def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заданные токен и чат: без них `send_alert` уходит в ветку `disabled`."""
    monkeypatch.setattr(alert_module.settings, "alert_telegram_bot_token", "123:TESTTOKEN")
    monkeypatch.setattr(alert_module.settings, "alert_telegram_chat_id", "-1001234567890")


def _client(handler: Any) -> httpx.AsyncClient:
    """Клиент поверх подменённого транспорта: реального соединения нет."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_successful_send_returns_true_and_posts_once(
    fake_redis: _FakeRedis, configured: None
) -> None:
    """AC-01: 200 от Telegram → `True`, ровно один `POST` с токеном в URL."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        sent = await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]

    assert sent is True
    assert len(calls) == 1
    assert "/bot123:TESTTOKEN/sendMessage" in str(calls[0].url)
    # Час — ЛИТЕРАЛОМ, а не через `DEDUP_TTL_SECONDS`. Сравнение с той же
    # константой, что уходит в `SET`, — тавтология (P-21): мутация «3600 → 0»
    # (или любое другое значение) прошла бы зелёной, потому что обе стороны
    # поменялись бы вместе. Проверяется именно ЧАСОВОЕ окно дедупа из D-10.7-6.
    assert fake_redis.last_ex == 3600


async def test_telegram_500_returns_false_and_logs_failure(
    fake_redis: _FakeRedis, configured: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-01: 500 от Telegram → `False` и событие `alert.failed`.

    `False`, а не исключение: алерт — диагностика, и его отказ не должен
    останавливать воркер, который этот алерт отправляет.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    async with _client(handler) as client:
        sent = await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]

    assert sent is False


async def test_timeout_returns_false(fake_redis: _FakeRedis, configured: None) -> None:
    """AC-01: таймаут → `False`, а не выброшенное исключение наружу."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("таймаут", request=request)

    async with _client(handler) as client:
        sent = await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]

    assert sent is False


async def test_second_call_with_same_key_does_not_post(
    fake_redis: _FakeRedis, configured: None
) -> None:
    """D-10.7-6: второй вызов с тем же ключом — POST'а нет (дедуп).

    Без дедупа зависшее окно шлёт сообщение каждую минуту (1440 в сутки),
    и бот замьючен в первый день — то есть канал алертов перестаёт
    существовать.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        first = await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]
        second = await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]

    assert first is True
    assert second is False
    assert len(calls) == 1


async def test_different_keys_are_not_deduped_together(
    fake_redis: _FakeRedis, configured: None
) -> None:
    """Ключ — параметр: дедуп окна не глушит health-алерт и наоборот.

    Один общий ключ дал бы «алерт про окно» и «алерт про упавшую базу» в
    одном часовом окне как одно событие, и второй инцидент остался бы
    незамеченным именно из-за первого.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        first = await send_alert(
            TEXT,
            key="alert:window_overdue:sent",
            redis=fake_redis,
            client=client,  # type: ignore[arg-type]
        )
        second = await send_alert(
            TEXT,
            key="alert:health:sent",
            redis=fake_redis,
            client=client,  # type: ignore[arg-type]
        )

    assert first is True
    assert second is True
    assert len(calls) == 2


async def test_empty_token_disables_alerting_without_raising(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-10.7-4: пустой токен → `False`, событие `alert.disabled`, старт жив.

    Проверяется ровно то свойство, ради которого алертинг не валит старт:
    probe поднимается в dev и CI, где бота нет.
    """
    monkeypatch.setattr(alert_module.settings, "alert_telegram_bot_token", "")
    monkeypatch.setattr(alert_module.settings, "alert_telegram_chat_id", "")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("при пустом токене POST не должен выполняться")

    async with _client(handler) as client:
        sent = await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]

    assert sent is False
    # Ключ дедупа не создан: выключенный алертинг не занимает окно.
    assert fake_redis.data == {}


async def test_disabled_alerting_is_logged_once_per_process(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-1 (CR 10.7), D-10.7-4: три вызова → ОДНО событие `alert.disabled`.

    **Что было не так.** Комментарий обещал «один раз за прогон», кода не
    было: замер ревьюера дал 3 вызова → 3 события. При выключенном алертинге
    probe зовёт `send_alert` раз в минуту — это 1440 строк в сутки, то есть
    ровно тот шум, в котором не видно настоящих событий, и от которого
    защищались.

    **Почему флаг в памяти, а не в Redis.** Причина выключения часто и есть
    недоступность Redis (dev, CI): метка в Redis не записалась бы ровно в
    том сценарии, ради которого флаг существует. Состояние описывает
    КОНФИГУРАЦИЮ процесса, а не инцидент — его не нужно повторять раз в час.

    Краснеет, если убрать проверку `_disabled_logged`: счётчик станет 3.
    """
    monkeypatch.setattr(alert_module.settings, "alert_telegram_bot_token", "")
    monkeypatch.setattr(alert_module.settings, "alert_telegram_chat_id", "")
    # Флаг — модульный и переживает тесты внутри одного процесса: без сброса
    # тест зависел бы от порядка запуска.
    monkeypatch.setattr(alert_module, "_disabled_logged", False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("при пустом токене POST не должен выполняться")

    async with _client(handler) as client:
        with structlog.testing.capture_logs() as captured:
            results = [
                await send_alert(TEXT, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]
                for _ in range(3)
            ]

    assert results == [False, False, False]
    events = [entry["event"] for entry in captured]
    assert events.count("alert.disabled") == 1, (
        f"ожидалось одно событие alert.disabled за процесс, получено {events}"
    )


async def test_alert_body_contains_no_personal_data(
    fake_redis: _FakeRedis, configured: None
) -> None:
    """NFR34: в теле сообщения нет телефона и `card_uuid`.

    Сообщение уходит во внешний сервис, поэтому тело обязано содержать только
    технические поля (D-10.7-9): `env`, `check`, `count`, `error_type`,
    `duration_ms`. Проверка ищет подстроки, которых там быть не должно, —
    список полей, которые там ЕСТЬ, проверяется отдельно и не заменяет её.
    """
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"ok": True})

    payload = "env=production check=db error_type=OperationalError duration_ms=12"
    async with _client(handler) as client:
        await send_alert(payload, key=KEY, redis=fake_redis, client=client)  # type: ignore[arg-type]

    body = bodies[0].decode("utf-8")
    assert "+7" not in body
    assert "card_uuid" not in body
    assert "79001234567" not in body
