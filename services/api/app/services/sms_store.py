"""Хранилище SMS-кодов, попыток, cooldown и лимитов (Task 3; AC-02, AC-03, AC-04).

D-7: окно лимита **фиксированное**. `SET key 0 EX window NX` + `INCR` — TTL
ставится только при создании ключа. Вариант `INCR` + `EXPIRE` на каждом запросе
даёт скользящее окно: при равномерном потоке ключ продлевается вечно, и лимит
«3 в 10 минут» не наступает никогда.

Хранилище спрятано за протоколом `SmsStore` по образцу `oauth_state.StateStore`:
unit-тесты работают на in-memory реализации и не тянут `fakeredis`.
"""

from __future__ import annotations

import time
from typing import Literal, Protocol

from redis.exceptions import RedisError

from app.core.redis import get_redis

RateKind = Literal["ip", "phone"]
# CR 1.4, Решение 2: `scope` разделяет счётчики `/request` и `/verify`.
# Общий ключ `sms:rate:ip:` смешивал лимиты 3 и 10 с разными окнами: один запрос
# кода + три неверных кода давали счётчик 4 > 3, и легитимный `/request`
# получал 429, а лимит 10 у `/verify` не достигался никогда.
RateScope = Literal["request", "verify"]

# Префиксы ключей. Один namespace (`sms:`) на всё, что относится к телефонному
# входу: разбор инцидента начинается с `KEYS sms:*`, а не с поиска по всему Redis.
_CODE_PREFIX = "sms:code:"
_ATTEMPTS_PREFIX = "sms:attempts:"
_COOLDOWN_PREFIX = "sms:cooldown:"
# Ключ — пара (вид, область): `sms:rate:ip:request:`, `sms:rate:ip:verify:`,
# `sms:rate:phone:request:`, `sms:rate:phone:verify:`.
_RATE_PREFIXES: dict[tuple[str, str], str] = {
    ("ip", "request"): "sms:rate:ip:request:",
    ("phone", "request"): "sms:rate:phone:request:",
    ("ip", "verify"): "sms:rate:ip:verify:",
    ("phone", "verify"): "sms:rate:phone:verify:",
}

# Атомарное гашение кода: сравнение и удаление в одном вызове.
# Три отдельных await (GET → compare → DEL) дают TOCTOU: два параллельных verify
# читают один хеш до удаления и оба получают сессию. Lua исполняется в Redis
# атомарно, поэтому ровно один вызов получит 1, остальные — 0.
# Возвращает: 1 — код совпал и погашен, 0 — не совпал, -1 — кода нет.
_CONSUME_LUA = """
local stored = redis.call('GET', KEYS[1])
if not stored then
  return -1
end
if stored == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


class SmsStorageUnavailableError(Exception):
    """Redis недоступен (D-8).

    Без отдельного исключения обрыв Redis давал 500, а вариант «отправить SMS
    без лимита» превращал эндпоинт в бесплатный генератор сообщений за деньги
    владельца. Поэтому — `503 storage_unavailable` и ни одного вызова провайдера.
    """


class SmsStore(Protocol):
    """Контракт хранилища. Все методы — про один телефон или один IP."""

    async def set_code(self, phone: str, value: str, ttl_seconds: int) -> None: ...

    async def get_code(self, phone: str) -> str | None: ...

    async def delete_code(self, phone: str) -> None: ...

    async def incr_attempts(self, phone: str, ttl_seconds: int) -> int: ...

    async def get_attempts(self, phone: str) -> int: ...

    async def delete_attempts(self, phone: str) -> None: ...

    async def hit_rate(
        self, kind: RateKind, value: str, *, scope: RateScope, window_seconds: int, limit: int
    ) -> int | None: ...

    async def decr_rate(self, kind: RateKind, value: str, *, scope: RateScope) -> None: ...

    async def acquire_cooldown(self, phone: str, seconds: int) -> int | None: ...

    async def set_cooldown(self, phone: str, seconds: int) -> None: ...

    async def delete_cooldown(self, phone: str) -> None: ...

    async def cooldown_left(self, phone: str) -> int | None: ...

    async def consume_code(self, phone: str, code_hash: str) -> int: ...


class RedisSmsStore:
    """Боевая реализация: коды с TTL, счётчики с фиксированным окном."""

    async def set_code(self, phone: str, value: str, ttl_seconds: int) -> None:
        """AC-01: код живёт ровно `ttl_seconds`; в Redis лежит хеш, не код (D-2)."""
        try:
            await get_redis().set(_CODE_PREFIX + phone, value, ex=ttl_seconds)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc

    async def get_code(self, phone: str) -> str | None:
        """D-3: чтение **без** удаления — неверный ввод не должен сжигать код."""
        try:
            raw = await get_redis().get(_CODE_PREFIX + phone)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        # decode_responses=True выставлен в core/redis, но защищаемся явно:
        # при bytes `raw` молча превращался бы в None, и живой код отвечал бы
        # `code_expired` без единой ошибки в логах.
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return raw if isinstance(raw, str) else None

    async def delete_code(self, phone: str) -> None:
        """AC-02: одноразовость. `DEL` после успешной сверки (D-3)."""
        try:
            await get_redis().delete(_CODE_PREFIX + phone)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc

    async def consume_code(self, phone: str, code_hash: str) -> int:
        """AC-02 (D-3): атомарная сверка и гашение одним Lua-вызовом.

        Возвращает: 1 — код совпал и погашен, 0 — не совпал, -1 — кода нет.
        Атомарность обязательна: при трёх отдельных командах два параллельных
        `/verify` с верным кодом оба проходят сверку и оба получают сессию.
        """
        try:
            result = await get_redis().eval(_CONSUME_LUA, 1, _CODE_PREFIX + phone, code_hash)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        return int(result)

    async def incr_attempts(self, phone: str, ttl_seconds: int) -> int:
        """AC-03: счётчик попыток проверки.

        TTL ставится только при создании ключа (`EXPIRE ... NX`): иначе каждая
        неверная попытка продлевала бы окно, и лимит «5 попыток» становился бы
        недостижимым при медленном переборе.
        """
        key = _ATTEMPTS_PREFIX + phone
        try:
            client = get_redis()
            value = await client.incr(key)
            # EXPIRE ... NX идемпотентен: ставит TTL только если его нет.
            # Вызов при каждом INCR закрывает случай «ключ унаследован без TTL»:
            # иначе телефон навсегда получал бы `code_attempts_exceeded`.
            await client.expire(key, ttl_seconds, nx=True)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        return int(value)

    async def get_attempts(self, phone: str) -> int:
        try:
            raw = await get_redis().get(_ATTEMPTS_PREFIX + phone)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="replace")
        return int(raw) if isinstance(raw, str) and raw.isdigit() else 0

    async def delete_attempts(self, phone: str) -> None:
        """AC-02, AC-03: счётчик сбрасывается при успехе и при выдаче нового кода.

        Без сброса исчерпанные попытки переезжали бы на следующий код: пользователь
        запросил новый код, но проверить его уже не смог бы ни разу.
        """
        try:
            await get_redis().delete(_ATTEMPTS_PREFIX + phone)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc

    async def hit_rate(
        self, kind: RateKind, value: str, *, scope: RateScope, window_seconds: int, limit: int
    ) -> int | None:
        """D-7 (AC-04): фиксированное окно через `SET … NX` + `INCR`.

        Возвращает `None`, если запрос разрешён, и остаток окна в секундах —
        если лимит исчерпан. Остаток нужен клиенту (`Retry-After`), а не только
        факт отказа: без него фронт показывает «попробуйте позже» без срока.

        CR 1.4, Решение 2: ключ включает `scope`, поэтому лимиты `/request`
        и `/verify` не вычитаются из одного счётчика.
        """
        prefix = _RATE_PREFIXES.get((kind, scope))
        if prefix is None:
            # Неизвестный вид ключа — ошибка программиста. Тихий безлимит
            # сделал бы защиту необязательной именно там, где её забыли.
            raise ValueError(f"неизвестный вид лимита: {kind}/{scope}")
        key = prefix + value
        try:
            client = get_redis()
            # NX: TTL ставится только на создании. Ключ с истёкшим окном
            # создастся заново и начнёт новый отсчёт.
            await client.set(key, 0, ex=window_seconds, nx=True)
            count = await client.incr(key)
            if int(count) <= limit:
                return None
            ttl = await client.ttl(key)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        # CR 1.4, патч 16: `ttl` = -1/-2/0 значит «срок неизвестен». Отдавать
        # `Retry-After: 0` нельзя — клиент повторит сразу и получит второй 429.
        return int(ttl) if ttl and ttl > 0 else window_seconds

    async def decr_rate(self, kind: RateKind, value: str, *, scope: RateScope) -> None:
        """CR 1.4, патч 1: возвращает квоту после неудачной отправки SMS.

        `hit_rate` инкрементирует счётчик до вызова провайдера. Без откатa три
        отказа провайдера выжигали квоту 3/10 мин при нуле отправленных SMS:
        пользователь получал 503, затем 429 и не мог войти вовсе.
        Проверка `> 0` держит счётчик неотрицательным; гонка двух откатов на
        одном ключе безопасна — худший исход `0`, а не минус.
        """
        prefix = _RATE_PREFIXES.get((kind, scope))
        if prefix is None:
            raise ValueError(f"неизвестный вид лимита: {kind}/{scope}")
        key = prefix + value
        try:
            client = get_redis()
            current = await client.get(key)
            if current is not None and int(current) > 0:
                await client.decr(key)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc

    async def acquire_cooldown(self, phone: str, seconds: int) -> int | None:
        """AC-01 (CR 1.4, патч 14): атомарный захват cooldown через `SET … NX`.

        Проверка `ttl` и последующая запись — два round-trip: два параллельных
        `/request` оба видели «cooldown нет», оба слали SMS, и `set_code`
        второго убивал код первого. `NX` делает проверку и захват одной
        операцией. Возвращает `None`, если cooldown захвачен нами, иначе
        остаток в секундах.
        """
        key = _COOLDOWN_PREFIX + phone
        try:
            client = get_redis()
            acquired = await client.set(key, 1, ex=seconds, nx=True)
            if acquired:
                return None
            ttl = await client.ttl(key)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        return int(ttl) if ttl and ttl > 0 else seconds

    async def set_cooldown(self, phone: str, seconds: int) -> None:
        """AC-01: 60 с между отправками на один номер."""
        try:
            await get_redis().set(_COOLDOWN_PREFIX + phone, 1, ex=seconds)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc

    async def delete_cooldown(self, phone: str) -> None:
        """Снимает cooldown: нужен при отказе провайдера.

        Без него неудачная отправка запирала пользователя на 60 с, хотя SMS
        не ушла и повторять было нечего.
        """
        try:
            await get_redis().delete(_COOLDOWN_PREFIX + phone)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc

    async def cooldown_left(self, phone: str) -> int | None:
        """AC-01: остаток cooldown в секундах или `None`, если отправка разрешена."""
        try:
            ttl = await get_redis().ttl(_COOLDOWN_PREFIX + phone)
        except RedisError as exc:
            raise SmsStorageUnavailableError("Redis недоступен") from exc
        return int(ttl) if ttl and ttl > 0 else None


class InMemorySmsStore:
    """Тестовая реализация: те же ключи и семантика, дедлайны по `time.monotonic()`.

    Дедлайн, а не TTL: иначе проверить истечение было бы нечем (см. `oauth_state`).
    """

    def __init__(self) -> None:
        self._codes: dict[str, tuple[str, float]] = {}
        self._attempts: dict[str, tuple[int, float]] = {}
        self._cooldowns: dict[str, float] = {}
        self._rates: dict[str, tuple[int, float]] = {}

    async def set_code(self, phone: str, value: str, ttl_seconds: int) -> None:
        self._codes[phone] = (value, time.monotonic() + ttl_seconds)

    async def get_code(self, phone: str) -> str | None:
        item = self._codes.get(phone)
        if item is None:
            return None
        value, deadline = item
        if time.monotonic() > deadline:
            # Истёкший код = отсутствующий. Удаляем сразу: иначе ключ жил бы
            # в памяти до конца теста и влиял бы на счётчики.
            del self._codes[phone]
            return None
        return value

    async def delete_code(self, phone: str) -> None:
        self._codes.pop(phone, None)

    async def consume_code(self, phone: str, code_hash: str) -> int:
        """Та же семантика, что Lua в Redis: -1 нет кода, 0 не совпал, 1 погашен."""
        item = self._codes.get(phone)
        if item is None:
            return -1
        value, deadline = item
        if time.monotonic() > deadline:
            del self._codes[phone]
            return -1
        if value != code_hash:
            return 0
        del self._codes[phone]
        return 1

    async def incr_attempts(self, phone: str, ttl_seconds: int) -> int:
        item = self._attempts.get(phone)
        now = time.monotonic()
        if item is None or now > item[1]:
            value, deadline = 1, now + ttl_seconds
        else:
            value, deadline = item[0] + 1, item[1]
        self._attempts[phone] = (value, deadline)
        return value

    async def get_attempts(self, phone: str) -> int:
        item = self._attempts.get(phone)
        if item is None:
            return 0
        value, deadline = item
        if time.monotonic() > deadline:
            del self._attempts[phone]
            return 0
        return value

    async def delete_attempts(self, phone: str) -> None:
        self._attempts.pop(phone, None)

    async def hit_rate(
        self, kind: RateKind, value: str, *, scope: RateScope, window_seconds: int, limit: int
    ) -> int | None:
        prefix = _RATE_PREFIXES.get((kind, scope))
        if prefix is None:
            raise ValueError(f"неизвестный вид лимита: {kind}/{scope}")
        key = prefix + value
        now = time.monotonic()
        item = self._rates.get(key)
        # Истёкшее окно начинает новый отсчёт — тот же эффект, что `SET … NX`
        # на удалённом по TTL ключе. `window_seconds <= 0` НЕ сбрасывает
        # принудительно (CR 1.4, патч 5): иначе тест истечения окна проходил бы
        # при полностью нерабочем сбросе по времени.
        if item is None or now > item[1]:
            count, deadline = 0, now + window_seconds
        else:
            count, deadline = item[0], item[1]
        count += 1
        self._rates[key] = (count, deadline)
        if count <= limit:
            return None
        return max(0, int(round(deadline - now)))

    async def decr_rate(self, kind: RateKind, value: str, *, scope: RateScope) -> None:
        """Симметрично Redis: откат квоты, счётчик не уходит в минус."""
        prefix = _RATE_PREFIXES.get((kind, scope))
        if prefix is None:
            raise ValueError(f"неизвестный вид лимита: {kind}/{scope}")
        key = prefix + value
        item = self._rates.get(key)
        if item is None or item[0] <= 0:
            return
        self._rates[key] = (item[0] - 1, item[1])

    async def acquire_cooldown(self, phone: str, seconds: int) -> int | None:
        """Симметрично Redis: `None` — захвачен нами, иначе остаток."""
        now = time.monotonic()
        deadline = self._cooldowns.get(phone)
        if deadline is not None and now < deadline:
            return max(1, int(round(deadline - now)))
        self._cooldowns[phone] = now + seconds
        return None

    async def set_cooldown(self, phone: str, seconds: int) -> None:
        self._cooldowns[phone] = time.monotonic() + seconds

    async def delete_cooldown(self, phone: str) -> None:
        self._cooldowns.pop(phone, None)

    async def cooldown_left(self, phone: str) -> int | None:
        deadline = self._cooldowns.get(phone)
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            del self._cooldowns[phone]
            return None
        return max(1, int(round(left)))


_store: SmsStore = RedisSmsStore()


def set_sms_store(store: SmsStore) -> None:
    """Подмена хранилища в тестах (in-memory вместо Redis), как `set_state_store`."""
    global _store
    _store = store


def get_sms_store() -> SmsStore:
    return _store
