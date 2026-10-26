"""Счётчик «подряд идущих неудач» (стори 10.7, Task 3.2/3.3; AC-01, D-10.7-2).

**Почему тест идёт в живой Redis.** Проверяемое свойство — ПЕРЕЖИВАНИЕ
пересоздания процесса. Счётчик в памяти процесса этому свойству
удовлетворяет ровно до первого рестарта, и подделка клиента Redis (мок,
фейк-словарь) воспроизвела бы только вызовы, но не саму гарантию. Поэтому
тест создаёт ДВА независимых клиента к одному URL: второй не знает о
первом ничего, кроме содержимого Redis. На счётчике в памяти он увидит `0`
(мутация M2 ниже) — тест обязан покраснеть.

**Без `REDIS_URL` тест скипается, а не зеленеет тихо.** Пропуск объявлен с
причиной (как в `tests/integration/`): «зелёный без Redis» — тот класс
дефекта, из-за которого в эпике 9 десять находок были заявлениями без
доказательств.

**Чистка ключа.** `health:` входит в namespace'ы `tests/conftest.py`
(P-12), поэтому между прогонами ключ удаляется. Внутри теста ключ удаляется
явно в setup: тест обязан начинать с нуля независимо от того, был ли
предыдущий прогон прерван.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.services.health_state import (
    FAILURES_KEY,
    FAILURES_TTL_SECONDS,
    register_failure,
    register_success,
)

REDIS_URL = os.environ.get("REDIS_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="нужен реальный Redis: проверяется переживание пересоздания процесса",
)


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Первый клиент. Ключ счётчика удаляется до и после теста."""
    assert REDIS_URL is not None
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.delete(FAILURES_KEY)
    try:
        yield client
    finally:
        await client.delete(FAILURES_KEY)
        await client.aclose()


async def test_failure_counter_survives_client_recreation(redis_client: Redis) -> None:
    """D-10.7-2: новый клиент видит ТОТ ЖЕ счётчик — он в Redis, не в памяти.

    Мутация M2 (Task 11.4) заменяет Redis-счётчик атрибутом модуля — этот
    тест обязан покраснеть на `expected 2, got 1`:
    новый клиент к тому же URL не увидел бы атрибут процесса.
    """
    assert await register_failure(redis_client) == 1

    assert REDIS_URL is not None
    second = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        assert await register_failure(second) == 2
        # И второй клиент читает то же значение, что записал первый.
        assert await redis_client.get(FAILURES_KEY) == "2"
    finally:
        await second.aclose()


async def test_failure_counter_has_ttl(redis_client: Redis) -> None:
    """Предохранитель: ключ обязан истечь, а не жить вечно.

    Без `EXPIRE` остаток «1 неудача» от упавшего процесса жил бы месяцами и
    следующая настоящая неудача дала бы алерт на ПЕРВОМ отказе — защита от
    флейка исчезла бы молча.
    """
    await register_failure(redis_client)

    ttl = await redis_client.ttl(FAILURES_KEY)

    assert 0 < ttl <= FAILURES_TTL_SECONDS


async def test_success_resets_counter_so_next_failure_is_one(redis_client: Redis) -> None:
    """Task 3.3: после успеха следующая неудача даёт `1`, а не `3`.

    Это и есть смысл «ПОДРЯД»: два разнесённых во времени отказа не инцидент.
    """
    await register_failure(redis_client)
    await register_failure(redis_client)

    await register_success(redis_client)

    assert await redis_client.get(FAILURES_KEY) is None
    assert await register_failure(redis_client) == 1


async def test_register_success_on_empty_state_is_not_an_error(redis_client: Redis) -> None:
    """Сброс на пустом счётчике — норма: Redis отвечает `DEL` → 0.

    Probe вызывает сброс после КАЖДОГО успешного прогона, а не только после
    серии неудач; исключение здесь остановило бы воркер на первом же
    штатном прогоне.
    """
    await register_success(redis_client)

    assert await redis_client.get(FAILURES_KEY) is None
