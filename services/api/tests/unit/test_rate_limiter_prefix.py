"""`key_prefix` у `JoinRequestRateLimiter` (стори 13.1, Task 2.3; AC-6).

Лимитер переиспользуется вторым потребителем — лимитом адресного поиска.
Без параметра оба лимита писали бы в один ключ `join_request:submit:<id>`,
и `INCR` поиска расходовал бы суточный лимит подач: участник, набравший
адрес в форме карточки, потерял бы возможность подать заявку.

§18.2: фейк Redis повторяет логику окна минимально (`SET NX`, `INCR`, `TTL`).
Проверяется НЕПЕРЕСЕЧЕНИЕ ключей, а не форма строки: имя префикса — деталь,
а раздельность счётчиков — свойство.
"""

from __future__ import annotations

import pytest

import app.services.join_request_rate as rate_module
from app.services.join_request_rate import JoinRequestRateLimiter

ACCOUNT_ID = 7


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


@pytest.fixture
async def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_module, "get_redis", lambda: fake)
    return fake


async def test_default_prefix_keeps_join_request_behaviour(fake_redis: _FakeRedis) -> None:
    """AC-6: без аргумента лимитер работает как раньше — ключ `join_request:submit:`.

    Краснеет, если дефолт заменить на другой: старые вызовы (`join_requests.py:178`
    и пять тестов) начали бы писать в другой ключ, а развёрнутый стенд сохранил
    бы исчерпанные счётчики по прежнему имени — участники получили бы лимит
    заново, то есть анти-абьюз молча ослаб бы на деплое.
    """
    limiter = JoinRequestRateLimiter(limit=1, window_seconds=86400)
    assert await limiter.hit(ACCOUNT_ID) is None
    assert list(fake_redis.data) == [f"join_request:submit:{ACCOUNT_ID}"]


async def test_two_limiters_with_different_prefixes_do_not_intersect(
    fake_redis: _FakeRedis,
) -> None:
    """AC-6: два лимитера с разными префиксами считают РАЗНОЕ.

    Краснеет, если `key_prefix` принимается, но не попадает в ключ:
    исчерпание одного лимита блокировало бы второй, и поиск адреса съедал
    бы суточный лимит заявок (и наоборот).

    Пара проверок, а не одна: `search` исчерпан до отказа, `submit` при этом
    обязан остаться доступным — иначе тест зеленел бы и при полном отсутствии
    изоляции, если бы оба лимитера «всегда отказывали».
    """
    search = JoinRequestRateLimiter(limit=1, window_seconds=60, key_prefix="address:search:")
    submit = JoinRequestRateLimiter(limit=1, window_seconds=86400)

    assert await search.hit(ACCOUNT_ID) is None
    assert await search.hit(ACCOUNT_ID) is not None, "второй поиск обязан быть отклонён"

    # Изоляция: лимит заявок не тронут.
    assert await submit.hit(ACCOUNT_ID) is None
    assert await submit.hit(ACCOUNT_ID) is not None, "вторая подача обязана быть отклонена"

    assert set(fake_redis.data) == {
        f"address:search:{ACCOUNT_ID}",
        f"join_request:submit:{ACCOUNT_ID}",
    }


async def test_empty_prefix_is_rejected() -> None:
    """AC-6: пустой префикс — ошибка программиста, а не «ключ без префикса».

    Краснеет, если убрать проверку: `key_prefix=""` дал бы ключ `7`,
    который пересекается с любым другим лимитом по тому же числу и делает
    расследование инцидента по имени ключа невозможным.
    """
    with pytest.raises(ValueError):
        JoinRequestRateLimiter(limit=1, window_seconds=60, key_prefix="")



async def test_address_place_does_not_consume_join_request_quota(
    fake_redis: _FakeRedis,
) -> None:
    """M-3 CR 2026-10-31: лимит создания места НЕ пересекается с подачей заявок.

    **Зачем.** Три действия живут на одном Redis, и общий счётчик означал бы:
    автор, сохранивший места к трём карточкам, теряет право подать заявку —
    отказ на действии, к которому он не притрагивался. Класс `D-10.4-1`.

    Краснеет, если `address:place:` совпадёт с `join_request:submit:` или
    с `address:search:`: `INCR` одного ключа считал бы чужие действия.
    """
    from app.api.v1.address import _ADDRESS_PLACE_KEY_PREFIX, _ADDRESS_SEARCH_KEY_PREFIX

    join = JoinRequestRateLimiter(limit=2, window_seconds=60)
    place = JoinRequestRateLimiter(
        limit=2, window_seconds=60, key_prefix=_ADDRESS_PLACE_KEY_PREFIX
    )
    search = JoinRequestRateLimiter(
        limit=2, window_seconds=60, key_prefix=_ADDRESS_SEARCH_KEY_PREFIX
    )

    # Исчерпываем лимит СОЗДАНИЯ места.
    assert await place.hit(ACCOUNT_ID) is None
    assert await place.hit(ACCOUNT_ID) is None
    assert await place.hit(ACCOUNT_ID) is not None

    # Подача заявок и поиск НЕ тронуты: ключи разные.
    assert await join.hit(ACCOUNT_ID) is None
    assert await search.hit(ACCOUNT_ID) is None

    # И обратно: подача заявки не расходует квоту создания.
    assert await join.hit(ACCOUNT_ID) is None
    assert await join.hit(ACCOUNT_ID) is not None
    assert len([key for key in fake_redis.data if key.startswith(_ADDRESS_PLACE_KEY_PREFIX)]) == 1


async def test_place_limiter_uses_its_own_setting() -> None:
    """M20 CR r2: порог создания места берётся из СВОЕЙ настройки.

    Краснеет, если фабрика читает `address_search_rate_limit` (10) вместо
    `address_place_rate_limit` (5): лимиты перепутаны местами, и правка
    частоты поиска молча меняет скорость наполнения справочника. Прежний
    тест строил лимитеры ВРУЧНУЮ с `limit=2` и настройку не читал вовсе.
    """
    from app.api.v1.address import get_address_place_rate_limiter
    from app.core.config import settings

    limiter = get_address_place_rate_limiter()

    assert limiter._limit == settings.address_place_rate_limit  # noqa: SLF001
    assert limiter._limit != settings.address_search_rate_limit, (
        "порог создания места совпал с поисковым — настройки перепутаны"
    )
    assert limiter._window == settings.address_place_rate_window_seconds  # noqa: SLF001
