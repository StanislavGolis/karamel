"""Одноразовые `state`-записи OAuth и PKCE (Task 2.2–2.4, AC-05).

D1: `state` живёт в Redis, а не в БД и не в подписанной cookie. `GETDEL` даёт
атомарную одноразовость одним вызовом: в БД понадобились бы `FOR UPDATE` плюс
джоб очистки, в cookie — подпись, но не одноразовость (replay в пределах TTL).

Хранилище спрятано за протоколом `StateStore`: unit-тесты используют in-memory
реализацию и не требуют `fakeredis` (лишняя зависимость ради двух методов).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis import get_redis

# AC-02: 32 байта энтропии -> 43 символа base64url, достаточно против перебора.
_STATE_BYTES = 32
# AC-02: verifier 32 байта -> challenge S256 длиной 43 символа.
_VERIFIER_BYTES = 32


class RedisUnavailableError(Exception):
    """Хранилище `state` недоступно.

    Без этого исключения обрыв Redis давал 500, а в access-логе uvicorn
    оставался полный URL с `code` и `state` — одноразовые креды попадали в лог.
    """


@dataclass(frozen=True, slots=True)
class StateRecord:
    """Восстановленная запись `state`: провайдер и PKCE-verifier."""

    provider: str
    code_verifier: str
    created_at: str


@dataclass(frozen=True, slots=True)
class IssuedState:
    """Выданный `state` и вычисленный из него challenge.

    Оба значения возвращаются вместе: если считать challenge отдельно, verifier
    и challenge разойдутся, и VK отклонит обмен на сверке PKCE.
    """

    state: str
    code_challenge: str


class StateStore(Protocol):
    """Контракт хранилища: записать один раз, погасить один раз."""

    async def set_once(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def pop(self, key: str) -> str | None: ...


class RedisStateStore:
    """Боевая реализация: `SET … EX … NX` и `GETDEL`.

    `NX` отсекает коллизию ключей, `GETDEL` — единственный способ погасить
    запись ровно один раз без гонки.
    """

    async def set_once(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            stored = await get_redis().set(key, value, ex=ttl_seconds, nx=True)
        except RedisError as exc:
            raise RedisUnavailableError("Redis недоступен") from exc
        # `SET … NX` возвращает None, если ключ уже существует. Молча вернуть
        # IssuedState значило бы отправить пользователя к провайдеру с `state`,
        # которого нет в хранилище: на callback он получил бы `invalid_state`.
        if not stored:
            raise RuntimeError(f"state уже существует: {key}")

    async def pop(self, key: str) -> str | None:
        try:
            raw = await get_redis().getdel(key)
        except RedisError as exc:
            raise RedisUnavailableError("Redis недоступен") from exc
        return raw if isinstance(raw, str) else None


class InMemoryStateStore:
    """Тестовая реализация. Живёт на объекте, поэтому тесты не делят состояние."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}

    async def set_once(self, key: str, value: str, ttl_seconds: int) -> None:
        """`NX`-семантика: существующий ключ не перезаписывается."""
        if key in self._data:
            raise RuntimeError("ключ уже существует")
        # Храним дедлайн, а не TTL: иначе проверить истечение нечем,
        # и тест на TTL проверял бы только повторное погашение.
        self._data[key] = (value, time.monotonic() + ttl_seconds)

    async def pop(self, key: str) -> str | None:
        """Погашение: запись удаляется при первом чтении, повторов нет."""
        item = self._data.pop(key, None)
        if item is None:
            return None
        value, deadline = item
        if time.monotonic() > deadline:
            return None  # просроченная запись считается отсутствующей
        return value


_store: StateStore = RedisStateStore()


def set_state_store(store: StateStore) -> None:
    """Подмена хранилища в тестах (in-memory вместо Redis)."""
    global _store
    _store = store


def get_state_store() -> StateStore:
    return _store


def _key(state: str) -> str:
    return f"oauth:state:{state}"


def code_challenge(verifier: str) -> str:
    """AC-02: PKCE S256 — base64url(SHA-256(verifier)) без паддинга."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    """AC-02: verifier 32 байта -> 43 символа base64url."""
    return secrets.token_urlsafe(_VERIFIER_BYTES)


async def issue_state(provider: str) -> IssuedState:
    """Task 2.2: выпускает `state` и сохраняет запись с TTL из настроек.

    `code_verifier` сохраняется всегда (Task 2.4): в authorize-URL он уходит только
    для VK, но единый формат записи избавляет от ветвлений при чтении.
    """
    state = secrets.token_urlsafe(_STATE_BYTES)
    verifier = new_code_verifier()
    record = StateRecord(
        provider=provider,
        code_verifier=verifier,
        created_at=secrets.token_hex(8),  # метка корреляции, не ПД
    )
    payload = json.dumps(
        {
            "provider": record.provider,
            "code_verifier": record.code_verifier,
            "created_at": record.created_at,
        }
    )
    await get_state_store().set_once(_key(state), payload, settings.oauth_state_ttl_seconds)
    return IssuedState(state=state, code_challenge=code_challenge(verifier))


async def consume_state(state: str, provider: str) -> StateRecord | None:
    """Task 2.2 (AC-05): погашает запись и возвращает её, если провайдер совпал.

    Запись гасится **до** сверки провайдера: чужой provider означает, что
    `state` не наш или утёк, и давать ему второй шанс нельзя — иначе перебором
    провайдеров можно повторно использовать одну запись.
    """
    raw = await get_state_store().pop(_key(state))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("provider") != provider:
        return None
    verifier = data.get("code_verifier")
    if not isinstance(verifier, str):
        return None
    return StateRecord(
        provider=str(data.get("provider")),
        code_verifier=verifier,
        created_at=str(data.get("created_at", "")),
    )
