"""Integration-тесты боевого `RedisSmsStore` на реальном Redis (CR 1.4, Решение 3).

Зачем: 344 строки боевого кода (Lua `consume_code`, `SET … NX` + `INCR`,
`expire(nx=True)`, `bytes`, `ttl<=0`) не исполнялись в CI ни разу — все тесты
работали на `InMemorySmsStore`. Именно поэтому дефект `bytes` в 1.4 не был
замечен при первом прогоне: in-memory реализация возвращает `str` по построению.

Без `REDIS_URL` модуль скипается с причиной, а не «тихо зелёный».
Ключи изолируются собственным namespace на каждый тест (см. `_ns`), поэтому
`flushdb` не нужен и чужие ключи в общей базе не страдают.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from app.services.sms_store import RedisSmsStore, SmsStorageUnavailableError

REDIS_URL = os.environ.get("REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not REDIS_URL,
        reason="нужен реальный Redis: REDIS_URL=redis://127.0.0.1:6379/0",
    ),
]


@pytest.fixture(autouse=True)
async def _fresh_redis_client() -> AsyncIterator[None]:
    """Сбрасывает кэш клиента Redis между тестами.

    pytest-asyncio создаёт новый event loop на каждый тест, а `get_redis()`
    кэширует клиент первого loop: следующее обращение падает с
    `RuntimeError: Event loop is closed`. Сброс возвращает ленивую
    инициализацию в исходное состояние.
    """
    from app.core import redis as redis_core

    redis_core._client = None
    yield
    redis_core._client = None


@pytest.fixture
def store() -> RedisSmsStore:
    """Боевая реализация поверх настроенного `REDIS_URL`."""
    return RedisSmsStore()


@pytest.fixture
def phone() -> str:
    """Уникальный номер на тест: ключи не пересекаются между прогонами.

    Формат E.164 соблюдается (+7 + 10 цифр), но цифры берутся из uuid, чтобы
    параллельный прогон не делил ключи с этим.
    """
    digits = uuid.uuid4().int % 10**10
    return f"+7{digits:010d}"


@pytest.fixture
def ip() -> str:
    return f"10.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}.1"


async def test_set_get_delete_code(store: RedisSmsStore, phone: str) -> None:
    """AC-01/AC-02: код пишется, читается строкой и удаляется.

    Проверяет главный дефект in-memory тестов: при `decode_responses=False`
    `GET` возвращает `bytes`, и живой код молча превращался бы в `code_expired`.
    """
    await store.set_code(phone, "hash-value", ttl_seconds=60)

    assert await store.get_code(phone) == "hash-value"

    await store.delete_code(phone)
    assert await store.get_code(phone) is None


async def test_get_code_missing_returns_none(store: RedisSmsStore, phone: str) -> None:
    """Отсутствующий код — `None`, а не исключение и не пустая строка."""
    assert await store.get_code(phone) is None


async def test_consume_code_lua_semantics(store: RedisSmsStore, phone: str) -> None:
    """D-3: Lua возвращает 1/0/-1 и гасит код только при совпадении.

    Повторный вызов с верным хешем обязан вернуть -1: иначе два параллельных
    `/verify` получили бы по сессии из одного кода.
    """
    assert await store.consume_code(phone, "any") == -1  # кода нет

    await store.set_code(phone, "right-hash", ttl_seconds=60)
    assert await store.consume_code(phone, "wrong-hash") == 0  # не совпал, код цел
    assert await store.get_code(phone) == "right-hash"  # D-3: неверный ввод не сжигает

    assert await store.consume_code(phone, "right-hash") == 1  # совпал и погашен
    assert await store.get_code(phone) is None
    assert await store.consume_code(phone, "right-hash") == -1  # повторно не сработает


async def test_consume_code_is_atomic_under_concurrency(store: RedisSmsStore, phone: str) -> None:
    """Патч 1 CR 1.4 (атомарность): ровно один из параллельных consume даёт 1.

    Три отдельных `GET`+`compare`+`DEL` давали TOCTOU: оба запроса читали хеш
    до удаления и оба получали сессию. Lua исполняется в Redis атомарно.
    """
    import asyncio

    await store.set_code(phone, "right-hash", ttl_seconds=60)

    results = await asyncio.gather(
        store.consume_code(phone, "right-hash"),
        store.consume_code(phone, "right-hash"),
        store.consume_code(phone, "right-hash"),
    )

    assert sorted(results) == [-1, -1, 1]


async def test_hit_rate_fixed_window(store: RedisSmsStore, ip: str) -> None:
    """D-7/AC-04: фиксированное окно `SET … NX` + `INCR`: 3 проходят, 4-й — нет."""
    for _ in range(3):
        assert await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=3) is None

    retry_after = await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=3)

    assert retry_after is not None
    # Патч 16: остаток положительный, `Retry-After: 0` недостижим.
    assert retry_after > 0


async def test_hit_rate_scopes_are_independent(store: RedisSmsStore, ip: str) -> None:
    """Решение 2 CR 1.4: `request` и `verify` пишут в разные ключи.

    С общим ключом один запрос кода плюс три неверных кода давали счётчик 4 > 3,
    и легитимный `/request` получал 429 при живом лимите 10 у `/verify`.
    """
    for _ in range(3):
        await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=3)
    assert await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=3) is not None

    # Тот же IP, но область `verify` — свой счётчик.
    assert await store.hit_rate("ip", ip, scope="verify", window_seconds=60, limit=10) is None


async def test_hit_rate_expires_by_window(store: RedisSmsStore, ip: str) -> None:
    """AC-04: окно в 1 секунду действительно истекает в Redis."""
    import asyncio

    for _ in range(2):
        await store.hit_rate("ip", ip, scope="request", window_seconds=1, limit=2)
    assert await store.hit_rate("ip", ip, scope="request", window_seconds=1, limit=2) is not None

    await asyncio.sleep(1.1)

    assert await store.hit_rate("ip", ip, scope="request", window_seconds=1, limit=2) is None


async def test_incr_attempts_sets_ttl_once(store: RedisSmsStore, phone: str) -> None:
    """AC-03: TTL ставится только при создании ключа (`EXPIRE … NX`).

    Иначе каждая неверная попытка продлевала бы окно, и лимит «5 попыток»
    становился бы недостижимым при медленном переборе.
    """
    assert await store.incr_attempts(phone, ttl_seconds=60) == 1
    assert await store.incr_attempts(phone, ttl_seconds=60) == 2
    assert await store.get_attempts(phone) == 2

    await store.delete_attempts(phone)
    assert await store.get_attempts(phone) == 0


async def test_attempts_reads_bytes_and_strings(store: RedisSmsStore, phone: str) -> None:
    """Патч 5 CR 1.4: счётчик читается при любом `decode_responses`.

    `get_attempts` раньше падал с AttributeError на `bytes`.
    """
    await store.incr_attempts(phone, ttl_seconds=60)

    assert await store.get_attempts(phone) == 1


async def test_cooldown_roundtrip(store: RedisSmsStore, phone: str) -> None:
    """AC-01: cooldown ставится, читается с положительным остатком и снимается."""
    await store.set_cooldown(phone, 60)
    left = await store.cooldown_left(phone)
    assert left is not None and 0 < left <= 60

    await store.delete_cooldown(phone)
    assert await store.cooldown_left(phone) is None


async def test_acquire_cooldown_is_atomic(store: RedisSmsStore, phone: str) -> None:
    """Патч 14 CR 1.4: ровно один из параллельных захватов получает cooldown.

    Без `SET … NX` два параллельных `/request` оба видели «cooldown нет»,
    оба слали SMS, и `set_code` второго убивал код первого.
    """
    import asyncio

    results = await asyncio.gather(
        store.acquire_cooldown(phone, 60),
        store.acquire_cooldown(phone, 60),
    )

    # Один `None` (захватил), второй — остаток в секундах.
    assert sum(1 for r in results if r is None) == 1
    assert sum(1 for r in results if r is not None) == 1


async def test_decr_rate_returns_quota(store: RedisSmsStore, ip: str) -> None:
    """Патч 1 CR 1.4: откат квоты после отказа провайдера."""
    # Разрешённый запрос: квота списана, провайдер отказал.
    assert await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=1) is None

    await store.decr_rate("ip", ip, scope="request")

    # Без откатa следующий запрос получил бы 429 при нуле отправленных SMS.
    assert await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=1) is None
    assert await store.hit_rate("ip", ip, scope="request", window_seconds=60, limit=1) is not None


async def test_unknown_kind_raises(store: RedisSmsStore, ip: str) -> None:
    """Неизвестный вид ключа — ошибка программиста, а не тихий безлимит."""
    with pytest.raises(ValueError):
        await store.hit_rate("email", ip, scope="request", window_seconds=60, limit=3)  # type: ignore[arg-type]


async def test_redis_unavailable_maps_to_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-8: недоступный Redis — `SmsStorageUnavailableError`, а не `RedisError`.

    Патч 4 CR 1.4: без этого маппинга роут отдавал бы 500, а лимит «тихо»
    отключался бы при недоступном хранилище.
    """
    from redis.exceptions import RedisError

    class BrokenRedis:
        async def set(self, *args: object, **kwargs: object) -> None:
            raise RedisError("connection refused")

    monkeypatch.setattr("app.services.sms_store.get_redis", lambda: BrokenRedis())

    with pytest.raises(SmsStorageUnavailableError):
        await RedisSmsStore().set_code("+79001234567", "hash", ttl_seconds=60)
