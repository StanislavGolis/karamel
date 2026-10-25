"""Ленивый клиент Redis (Task 2.1, AC-05).

Клиент создаётся при первом обращении, а не на импорте: иначе любой тест,
поднимающий приложение без Redis, падал бы на этапе сборки импортов.
Redis здесь нужен для одноразовых `state`-записей OAuth, SMS-кодов,
rate limiting (1.4/1.5) и pub/sub WS-событий (6.5).

**Почему клиент пересоздаётся при смене event loop.** `redis.asyncio.Redis`
привязывает пул соединений к loop, в котором открыл первое соединение.
Второй loop (следующий тест `pytest-asyncio`, поток `TestClient`) получает
`RuntimeError: got Future attached to a different loop` — и это не «редкий
случай»: Chore P-6 уже документировал ту же ошибку для тестов SMS-хранилища,
где её лечили ручным сбросом `_client`. Ручной сброс в каждом тесте —
дисциплина, а не механизм: первый же новый тест о нём забывает. Поэтому
проверка loop'а живёт в `get_redis`.

**Почему старый клиент не закрывается здесь.** Закрытие требует `await`,
а `get_redis` синхронный и вызывается из середины горячего пути. Клиент
чужого (уже закрытого) loop'а не держит живых сокетов — закрывать нечего;
его освободит `dispose_redis` на остановке приложения, а сборщик мусора
уберёт пул.
"""

from __future__ import annotations

import asyncio
import contextlib

from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None
#: Loop, к которому привязан текущий клиент (см. модульный докстринг).
_client_loop: asyncio.AbstractEventLoop | None = None
#: Клиенты прошлых loop'ов. НЕ выбрасываются молча (CR 6.5): каждый держит
#: пул соединений к Redis, и сборка мусора освободила бы сокеты
#: недетерминированно — за долгий прогон тестов это упирается в `maxclients`
#: сервера. Закрываются в `dispose_redis` (единственная async-точка).
_stale_clients: list[Redis] = []


def get_redis() -> Redis:
    """Ленивая инициализация клиента. `decode_responses=True` — работаем строками.

    Пересоздаёт клиент, если сменился running loop: иначе второй тест (или
    второй поток приложения) получает `got Future attached to a different loop`.
    """
    global _client, _client_loop
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        # Синхронный контекст (например, модульная инициализация): loop'а нет,
        # привязку не проверяем — клиент создаст соединение при первом await.
        loop = None

    loop_changed = _client_loop is not None and loop is not None and loop is not _client_loop
    if _client is not None and loop_changed:
        # Старый клиент привязан к закрытому loop'у: его пул больше не оживёт,
        # но и молча терять его нельзя — закрытие ниже, в `dispose_redis`.
        _stale_clients.append(_client)
    if _client is None or loop_changed:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
        _client_loop = loop
    return _client


async def dispose_redis() -> None:
    """Закрывает соединения. Вызывается из lifespan рядом с `dispose_engine()`."""
    global _client, _client_loop, _stale_clients
    stale = list(_stale_clients)
    _stale_clients = []
    for old_client in stale:
        # `aclose()` безопасен и на клиенте закрытого loop'а: он лишь помечает
        # пул закрытым, не дожидаясь событий loop'а. Уборка не должна ронять
        # остановку приложения — отсюда подавление.
        with contextlib.suppress(Exception):
            await old_client.aclose()
    if _client is not None:
        await _client.aclose()
    _client = None
    _client_loop = None
