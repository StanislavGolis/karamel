"""Воркер health-probe: порог «2 подряд», сброс и алерты (Task 5.3/5.4; AC-01).

**Почему подменяется и HTTP, и Redis.** HTTP — `httpx.MockTransport`, а не
живой api: проверяется решение воркера («считать неудачей или нет»), а не
сеть. Redis — фейк с минимальной семантикой `INCR`/`EXPIRE`/`DEL`/`SET NX`,
потому что проверяемые свойства (серия, сброс, дедуп) — логика поверх
команды, а не сама команда; живой Redis дал бы то же, но оставил бы ключи
между тестами.

**Что именно проверяется по AC-01.** «При 2 подряд неудачах уходит алерт»:
одна неудача — тишина, две — одно сообщение. «Подряд» — значит успех между
неудачами обнуляет серию. И «не на каждой следующей»: три неудачи подряд
вызывают отправку ДВА раза (порог достигнут на второй и на третьей), но
дедуп часа пропускает только одно сообщение — иначе восстановление после
инцидента не отличить от продолжения.

**Чего эти тесты не проверяют.** Реального Telegram (Task 4.3), реального
`/health/deep` (Task 2.3) и настоящей недоступности PostgreSQL. Каждый из
этих слоёв проверен своим тестом; здесь — решение воркера.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from redis.exceptions import RedisError

import app.services.alert_service as alert_module
import app.workers.health_probe as probe_module
from app.services.health_state import FAILURES_KEY


class _FakeRedis:
    """Минимум Redis: `INCR`, `EXPIRE`, `DEL`, `SET NX EX`."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True


class _BrokenRedis(_FakeRedis):
    """Redis недоступен: `INCR` бросает `RedisError` (D-10.7-3)."""

    async def incr(self, key: str) -> int:
        raise RedisError("нет соединения")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заданные токен и чат: иначе алерты уходят в ветку `disabled`."""
    monkeypatch.setattr(alert_module.settings, "alert_telegram_bot_token", "123:TESTTOKEN")
    monkeypatch.setattr(alert_module.settings, "alert_telegram_chat_id", "-1001234567890")


class _Posts:
    """Счётчик реальных POST'ов в Telegram через подменённый транспорт."""

    def __init__(self) -> None:
        self.count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        return httpx.Response(200, json={"ok": True})


def _failure_client() -> httpx.AsyncClient:
    """Транспорт, который всегда отвечает 503: `health/deep` говорит «плохо»."""

    def _degraded(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "degraded"})

    return httpx.AsyncClient(transport=httpx.MockTransport(_degraded))


def _ok_client() -> httpx.AsyncClient:
    """Транспорт, который всегда отвечает 200."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    )


async def test_single_failure_does_not_alert(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: одна неудача — тишина. Иначе любой флейк поднимал бы бота."""
    posts = _Posts()
    redis = _FakeRedis()

    async with (
        _failure_client() as probe_client,
        httpx.AsyncClient(transport=httpx.MockTransport(posts.handler)) as alert_client,
    ):
        monkeypatch.setattr(probe_module, "send_alert", _alert_with(alert_client))
        result = await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]

    assert result is False
    assert posts.count == 0
    # Счётчик всё равно вырос: о неудаче знает диагностика, просто молчит канал.
    assert redis.data[FAILURES_KEY] == "1"


async def test_second_consecutive_failure_sends_one_alert(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: вторая подряд неудача → ровно одно сообщение."""
    posts = _Posts()
    redis = _FakeRedis()

    async with (
        _failure_client() as probe_client,
        httpx.AsyncClient(transport=httpx.MockTransport(posts.handler)) as alert_client,
    ):
        monkeypatch.setattr(probe_module, "send_alert", _alert_with(alert_client))
        await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]
        await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]

    assert posts.count == 1


async def test_success_between_failures_resets_the_series(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: «подряд» — успех обнуляет серию, следующий отказ снова первый.

    Без сброса два отказа, разнесённых на часы, считались бы серией, и алерт
    приходил бы за инцидент, которого нет.
    """
    posts = _Posts()
    redis = _FakeRedis()

    async with (
        _failure_client() as failing,
        _ok_client() as healthy,
        httpx.AsyncClient(transport=httpx.MockTransport(posts.handler)) as alert_client,
    ):
        monkeypatch.setattr(probe_module, "send_alert", _alert_with(alert_client))
        await probe_module.run_once(redis, client=failing)  # type: ignore[arg-type]
        await probe_module.run_once(redis, client=healthy)  # type: ignore[arg-type]
        await probe_module.run_once(redis, client=failing)  # type: ignore[arg-type]

    assert posts.count == 0
    assert redis.data.get(FAILURES_KEY) == "1"


async def test_third_failure_calls_alert_twice_but_posts_once(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 5.4: три неудачи подряд → решений об алерте два, сообщение одно.

    Порог достигнут и на второй, и на третьей неудаче (два решения об алерте),
    но часовой дедуп пропускает только первое сообщение. Без дедупа зависший
    инцидент слал бы сообщение каждый прогон probe, и бот был бы замьючен —
    то есть канал исчез бы ровно тогда, когда нужен.
    """
    posts = _Posts()
    redis = _FakeRedis()
    decisions: list[str] = []

    async with (
        _failure_client() as probe_client,
        httpx.AsyncClient(transport=httpx.MockTransport(posts.handler)) as alert_client,
    ):

        async def _counting_alert(text: str, *, key: str, redis: Any, client: Any = None) -> bool:
            # Клиент подменён: настоящий `send_alert` получил бы MockTransport,
            # а не сеть. Счётчик решений отдельный от счётчика POST'ов —
            # именно их разницу проверяет этот тест.
            decisions.append(key)
            return await alert_module.send_alert(text, key=key, redis=redis, client=alert_client)

        monkeypatch.setattr(probe_module, "send_alert", _counting_alert)
        for _ in range(3):
            await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]

    assert len(decisions) == 2
    assert posts.count == 1


async def test_redis_failure_alerts_immediately(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-10.7-3: Redis лёг → алерт НЕМЕДЛЕННО, а не «2 подряд».

    Считать серию нечем: счётчик живёт в Redis. Асимметрия намеренна —
    «2 подряд» защищает от сетевого флейка, а отказ Redis флейком не бывает;
    без этой ветки отказ Redis не был бы замечен НИКОГДА.
    """
    posts = _Posts()
    redis = _BrokenRedis()

    async def _boom(key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        # Дедуп-ключ записать некуда: Redis лежит. `send_alert` обязан
        # справиться с этим, а не упасть.
        raise RedisError("нет соединения")

    redis.set = _boom  # type: ignore[method-assign]

    async with (
        _failure_client() as probe_client,
        httpx.AsyncClient(transport=httpx.MockTransport(posts.handler)) as alert_client,
    ):
        monkeypatch.setattr(probe_module, "send_alert", _alert_with(alert_client))
        result = await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]

    assert result is False
    # D-10.7-3: алерт ушёл НЕМЕДЛЕННО, несмотря на лежачий Redis. Дедуп
    # записать некуда, поэтому отправка происходит без него — иначе отказ
    # Redis не был бы замечен никогда (это и есть смысл асимметрии).
    assert posts.count == 1


async def test_empty_token_does_not_crash_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-10.7-4: пустой токен = алертинг выключен, probe работает.

    Старт не падает и прогон завершается: probe обязан подниматься в dev и CI,
    где бота нет. Иначе проверка эксплуатационной готовности сама стала бы
    единой точкой отказа.
    """
    monkeypatch.setattr(alert_module.settings, "alert_telegram_bot_token", "")
    monkeypatch.setattr(alert_module.settings, "alert_telegram_chat_id", "")
    redis = _FakeRedis()

    async with _failure_client() as probe_client:
        await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]
        # Две неудачи: порог достигнут, но канал выключен — исключения нет.
        result = await probe_module.run_once(redis, client=probe_client)  # type: ignore[arg-type]

    assert result is False
    assert redis.data[FAILURES_KEY] == "2"


def _alert_with(client: httpx.AsyncClient) -> Any:
    """`send_alert` с подставленным клиентом: HTTP идёт через MockTransport."""

    async def _send(text: str, *, key: str, redis: Any, client: Any = None) -> bool:
        return await alert_module.send_alert(text, key=key, redis=redis, client=_client_arg(client))

    def _client_arg(ignored: Any) -> httpx.AsyncClient:
        return client

    return _send
