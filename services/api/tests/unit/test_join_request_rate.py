"""Технический rate limit подач (стори 5а.2, Task 7; AC-10, FR61).

§18.2: фейк Redis повторяет ЛОГИКУ ОКНА минимально (fixed-window: `SET NX`,
`INCR`, `TTL`), чтобы проверялось поведение лимитера, а не сам фейк. Сбой
Redis моделируется исключением — это единственная ветка, которую нельзя
наблюдать на живом Redis без его остановки.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import RedisError

import app.services.join_request_rate as rate_module
from app.services.join_request_rate import JoinRequestRateLimiter

APPLICANT_ID = 7


class _FakeRedis:
    """Минимум Redis: `SET NX EX`, `INCR`, `TTL` в одном словаре."""

    def __init__(self) -> None:
        self.data: dict[str, int] = {}
        self.ttl_seconds = 3600

    async def set(self, key: str, value: int, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = int(value)
        if ex is not None:
            self.ttl_seconds = ex
        return True

    async def incr(self, key: str) -> int:
        self.data[key] = self.data.get(key, 0) + 1
        return self.data[key]

    async def ttl(self, key: str) -> int:
        return self.ttl_seconds


class _BrokenRedis(_FakeRedis):
    """Redis, который недоступен: первая же команда бросает `RedisError`."""

    async def set(self, key: str, value: int, *, ex: int | None = None, nx: bool = False) -> bool:
        raise RedisError("нет соединения")


@pytest.fixture
async def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Подменяет `get_redis` в модуле лимитера — глобальный клиент не создаётся."""
    fake = _FakeRedis()
    monkeypatch.setattr(rate_module, "get_redis", lambda: fake)
    return fake


async def test_requests_under_limit_are_allowed(fake_redis: _FakeRedis) -> None:
    """AC-10: 10 подач проходят, 11-я получает остаток окна.

    Краснеет, если заменить `<= limit` на `< limit` (10-я подача получила бы
    отказ) или убрать инкремент.
    """
    limiter = JoinRequestRateLimiter(limit=10, window_seconds=86400)
    results = [await limiter.hit(APPLICANT_ID) for _ in range(11)]
    assert results[:10] == [None] * 10
    assert results[10] == 86400


async def test_key_is_scoped_per_account(fake_redis: _FakeRedis) -> None:
    """AC-10, NFR17: лимит по АККАУНТУ, а не по карточке или IP.

    Краснеет, если убрать `applicant_id` из ключа: исчерпание лимита одного
    участника блокировало бы подачи всех — или, наоборот, обход через вторую
    карточку стал бы возможен.
    """
    limiter = JoinRequestRateLimiter(limit=1, window_seconds=86400)
    assert await limiter.hit(APPLICANT_ID) is None
    assert await limiter.hit(APPLICANT_ID) is not None
    assert await limiter.hit(APPLICANT_ID + 1) is None


async def test_window_comes_from_settings(fake_redis: _FakeRedis) -> None:
    """AC-10: окно — из настроек (`join_request_rate_window_seconds`), не литерал.

    Краснеет, если захардкодить окно: FR61 требует менять поведение без правок
    кода, а `Retry-After` показывал бы чужое значение.
    """
    limiter = JoinRequestRateLimiter(limit=1, window_seconds=60)
    assert await limiter.hit(APPLICANT_ID) is None
    assert await limiter.hit(APPLICANT_ID) == 60


async def test_redis_failure_does_not_block_legal_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-10 (ADR-11): сбой Redis пропускает подачу, а не отдаёт `503`/`429`.

    Краснеет, если пробросить `RedisError` наружу: недоступный Redis
    заблокировал бы легальное действие, а NFR17 — анти-абьюз, не инвариант
    домена.
    """
    broken = _BrokenRedis()
    monkeypatch.setattr(rate_module, "get_redis", lambda: broken)
    limiter = JoinRequestRateLimiter(limit=1, window_seconds=86400)
    assert await limiter.hit(APPLICANT_ID) is None


async def test_technical_limit_is_above_domain_limit() -> None:
    """AC-10 (OQ-5a.2-1): технический потолок строго выше доменного (2).

    Краснеет, если выставить `join_request_rate_limit = 2`: отменив заявку,
    участник исчерпал бы суточный счётчик и не смог бы подать вторую — FR82
    стал бы невыполним.
    """
    from app.core.config import settings

    assert settings.join_request_rate_limit > settings.join_request_active_limit


def test_rate_limit_validator_uses_actual_domain_limit() -> None:
    """AC-10 (OQ-5a.2-1): потолок сравнивается с ФАКТИЧЕСКИМ доменным лимитом.

    CR 5а.2, патч 4: прежний валидатор сравнивал с константой
    `JOIN_REQUEST_ACTIVE_LIMIT_MIN` (=1), поэтому `ACTIVE_LIMIT=5, RATE_LIMIT=3`
    проходил, и технический потолок оказывался НИЖЕ доменного — участник получал
    `rate_limited` раньше, чем доменный лимит начинал считаться (FR82 невыполним).
    Краснеет, если вернуть сравнение с константой.
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(join_request_active_limit=5, join_request_rate_limit=3)
    # Ровно на границе: домен 1, потолок 2 — допустимо.
    assert Settings(join_request_active_limit=1, join_request_rate_limit=2)


def test_settings_validators_reject_bad_values() -> None:
    """AC-10: валидаторы настроек роняют старт, а не ломают запрос.

    Краснеет, если снять валидаторы: `JOIN_REQUEST_RATE_LIMIT=1` сделал бы
    анти-абьюз строже продукта, а `WINDOW=0` создал бы ключ без TTL.
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(join_request_rate_limit=1)
    with pytest.raises(ValidationError):
        Settings(join_request_rate_window_seconds=0)
    with pytest.raises(ValidationError):
        Settings(join_request_active_limit=0)


async def test_retry_after_is_never_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-10: `ttl <= 0` означает «срок неизвестен» — отдаём окно целиком.

    Краснеет, если вернуть `ttl` как есть: `Retry-After: 0` заставил бы клиента
    повторить немедленно и получить второй `429` (CR 1.4, патч 16).
    """

    class _NoTtlRedis(_FakeRedis):
        async def ttl(self, key: str) -> int:
            return -1

    fake: Any = _NoTtlRedis()
    monkeypatch.setattr(rate_module, "get_redis", lambda: fake)
    limiter = JoinRequestRateLimiter(limit=1, window_seconds=86400)
    assert await limiter.hit(APPLICANT_ID) is None
    assert await limiter.hit(APPLICANT_ID) == 86400
