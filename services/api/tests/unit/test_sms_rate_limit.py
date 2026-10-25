"""Unit-тесты лимитов и cooldown поверх `InMemorySmsStore` (Task 9.3; AC-04).

Redis в unit-тестах не нужен: проверяется семантика ключей (фиксированное окно,
независимость счётчиков IP и телефона, разделение областей `/request` и `/verify`,
cooldown), а не клиент Redis. Сам клиент покрыт
`tests/integration/test_sms_store_redis.py` (CR 1.4, Решение 3).
"""

import time

import pytest
from redis.exceptions import RedisError

from app.services.sms_store import (
    InMemorySmsStore,
    RedisSmsStore,
    SmsStorageUnavailableError,
)

PHONE = "+79001234567"
IP = "127.0.0.1"


@pytest.fixture
async def store() -> InMemorySmsStore:
    """Чистое хранилище: счётчики не протекают между тестами."""
    return InMemorySmsStore()


async def test_ip_limit_allows_three_then_blocks(store: InMemorySmsStore) -> None:
    """AC-04: 3 запроса проходят, 4-й за окно упирается в лимит."""
    for _ in range(3):
        assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is None

    retry_after = await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3)

    assert retry_after is not None
    assert retry_after > 0


async def test_ip_limit_resets_after_window(
    store: InMemorySmsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04: окно фиксированное — после его истечения лимит снова доступен.

    CR 1.4, патч 5: время сдвигается явно через `time.monotonic`. Прежний тест
    передавал `window_seconds=0`, а стор принудительно сбрасывал счётчик при
    `<= 0` — тест проходил при полностью нерабочем сбросе по времени.
    """
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)

    for _ in range(3):
        await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3)
    assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is not None

    # Окно ещё не истекло — лимит держится.
    monkeypatch.setattr(time, "monotonic", lambda: now + 599)
    assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is not None

    # Окно истекло — счётчик начинается заново.
    monkeypatch.setattr(time, "monotonic", lambda: now + 601)
    assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is None


async def test_ip_and_phone_counters_are_independent(store: InMemorySmsStore) -> None:
    """AC-04: лимиты по IP и по телефону считаются раздельно.

    Общий счётчик позволил бы обойти лимит, меняя один из двух параметров.
    """
    for _ in range(3):
        await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3)

    # IP исчерпан, но телефон — нет: другой IP с тем же номером ещё не пробовал.
    assert (
        await store.hit_rate("phone", PHONE, scope="request", window_seconds=600, limit=3) is None
    )


async def test_request_and_verify_counters_are_independent(store: InMemorySmsStore) -> None:
    """Решение 2 CR 1.4: `/request` и `/verify` не вычитаются из одного ключа.

    Общий ключ `sms:rate:ip:` смешивал лимиты 3 и 10 с разными окнами: один
    запрос кода плюс три неверных кода давали счётчик 4 > 3, и легитимный
    `/request` получал 429, а лимит 10 у `/verify` не достигался никогда.
    """
    for _ in range(3):
        await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3)

    # `/verify` со своим ключом: три проверки кода не исчерпывают его лимит 10.
    for _ in range(10):
        assert await store.hit_rate("ip", IP, scope="verify", window_seconds=600, limit=10) is None
    assert await store.hit_rate("ip", IP, scope="verify", window_seconds=600, limit=10) is not None


@pytest.mark.parametrize("kind", ["email", "", "ip:"])
async def test_unknown_kind_raises(store: InMemorySmsStore, kind: str) -> None:
    """Неподдерживаемый вид ключа — ошибка, а не тихий безлимит.

    Виды `ip` и `phone` валидны; здесь именно неизвестные значения, иначе
    опечатка в имени вида (`emial`) молча отключала бы защиту.
    """
    with pytest.raises(ValueError):
        await store.hit_rate(
            kind,
            "x",
            scope="request",
            window_seconds=600,
            limit=3,  # type: ignore[arg-type]
        )


async def test_decr_rate_returns_quota(store: InMemorySmsStore) -> None:
    """Патч 1 CR 1.4: откат квоты после неудачной отправки SMS.

    Без откатa три отказа провайдера выжигали 3/3 запросов за 10 минут при
    нуле отправленных SMS, и пользователь получал 429 на любой повтор.
    """
    # Неудачный запрос: `hit_rate` уже списал единицу, провайдер отказал.
    assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is None
    await store.decr_rate("ip", IP, scope="request")

    # Квота восстановлена полностью: доступны все три запроса.
    for _ in range(3):
        assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is None
    assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is not None


async def test_decr_rate_does_not_go_negative(store: InMemorySmsStore) -> None:
    """Откат на пустом счётчике не создаёт отрицательное значение.

    Отрицательный счётчик дал бы лишние бесплатные запросы: `count <= limit`
    выполнялось бы ещё несколько раз.
    """
    await store.decr_rate("ip", IP, scope="request")

    for _ in range(3):
        assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is None
    assert await store.hit_rate("ip", IP, scope="request", window_seconds=600, limit=3) is not None


async def test_acquire_cooldown_is_atomic(store: InMemorySmsStore) -> None:
    """Патч 14 CR 1.4: cooldown захватывается, а не проверяется отдельно.

    Первый захват отдаёт `None` (наш), второй — остаток: без `NX` два
    параллельных `/request` оба проходили проверку и слали два платных SMS.
    """
    assert await store.acquire_cooldown(PHONE, 60) is None

    left = await store.acquire_cooldown(PHONE, 60)

    assert left is not None
    assert 0 < left <= 60


async def test_cooldown_blocks_immediate_resend(store: InMemorySmsStore) -> None:
    """AC-01: повторная отправка раньше 60 с отклоняется, остаток сообщается."""
    await store.set_cooldown(PHONE, seconds=60)

    left = await store.cooldown_left(PHONE)

    assert left is not None
    assert 0 < left <= 60


async def test_cooldown_zero_means_allowed(store: InMemorySmsStore) -> None:
    """AC-01: без активного cooldown проверка возвращает None."""
    assert await store.cooldown_left(PHONE) is None


async def test_cooldown_expires(store: InMemorySmsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01: после истечения cooldown повторная отправка разрешена."""
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    await store.set_cooldown(PHONE, seconds=60)
    assert await store.cooldown_left(PHONE) is not None

    monkeypatch.setattr(time, "monotonic", lambda: now + 61)

    assert await store.cooldown_left(PHONE) is None


async def test_code_can_be_stored_and_read(store: InMemorySmsStore) -> None:
    """AC-02: код сохраняется с TTL и читается до истечения."""
    await store.set_code(PHONE, "hash-value", ttl_seconds=300)

    assert await store.get_code(PHONE) == "hash-value"


async def test_code_expires_by_ttl(
    store: InMemorySmsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03: истёкший код читается как отсутствующий."""
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    await store.set_code(PHONE, "hash-value", ttl_seconds=300)
    assert await store.get_code(PHONE) == "hash-value"

    monkeypatch.setattr(time, "monotonic", lambda: now + 301)

    assert await store.get_code(PHONE) is None


async def test_delete_code_removes_it(store: InMemorySmsStore) -> None:
    """AC-02: успешная проверка удаляет код — второй раз он не сработает."""
    await store.set_code(PHONE, "hash-value", ttl_seconds=300)

    await store.delete_code(PHONE)

    assert await store.get_code(PHONE) is None


async def test_attempts_counter_increments(store: InMemorySmsStore) -> None:
    """AC-03: попытки считаются, чтобы ограничить перебор кода."""
    assert await store.get_attempts(PHONE) == 0

    assert await store.incr_attempts(PHONE, ttl_seconds=300) == 1
    assert await store.incr_attempts(PHONE, ttl_seconds=300) == 2
    assert await store.get_attempts(PHONE) == 2


async def test_redis_error_maps_to_storage_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-04/D-8: недоступность Redis — отдельное исключение, а не 500.

    CR 1.4, патч 4: прежний тест проверял `issubclass(SmsStorageUnavailableError,
    Exception)` — истину для любого класса, включая тот, который нигде не
    возбуждается. Здесь `RedisError` реально бросается клиентом, и проверяется
    маппинг в доменное исключение.
    """

    class BrokenRedis:
        async def set(self, *args: object, **kwargs: object) -> None:
            raise RedisError("connection refused")

    monkeypatch.setattr("app.services.sms_store.get_redis", lambda: BrokenRedis())

    with pytest.raises(SmsStorageUnavailableError):
        await RedisSmsStore().hit_rate("ip", IP, scope="request", window_seconds=600, limit=3)
