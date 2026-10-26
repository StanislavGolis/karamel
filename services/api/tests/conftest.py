"""Корневые фикстуры набора тестов (X-6.7-2).

Зачем файл существует. Изоляция БД (внешняя транзакция + TRUNCATE в
`tests/integration/conftest.py`) откатывает `users.id` между прогонами, но
НЕ откатывает Redis: `join_request:submit:<id>` живёт с TTL 14-23 ч и
переживает прогон. Второй прогон подряд получает ТЕ ЖЕ `applicant_id` и
вместо доменной ошибки `409 CARD_NOT_ACCEPTING` - технический `429
rate_limited`. Дефект воспроизводился дважды в CR 6.7.

Почему не FLUSHDB. Тесты ходят в тот же Redis DB 0, что и dev-среда
(`REDIS_URL=redis://127.0.0.1:6379/0` в CI и локально). `FLUSHDB` снёс бы
ключи живого окружения разработчика. Поэтому чистятся только namespace'
проекта: `sms:`, `join_request:`, `oauth:state:`. Префикс `ws:chat:` -
pub/sub-канал, а не ключ: pub/sub состояния в Redis не хранит.

Почему не отдельный DB index. Соблазнительно, но ломает e2e_multiproc:
поднятые uvicorn-инстансы обязаны видеть ОДИН Redis с тестом (иначе
мутация M12 из TD 6.5 не ловится), а они наследуют `REDIS_URL` из
`os.environ`.

Почему session-scoped. Цель - «прогон начинается на чистом Redis», а не
«каждый тест видит пустой Redis»: внутри прогона тесты сознательно
проверяют накопление счётчиков (fixed-window лимиты, TTL). Чистка перед
каждым тестом убила бы именно эти сценарии.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Generator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

#: Redis, в который ходят тесты. Тот же, что берёт приложение из настроек.
REDIS_URL = os.environ.get("REDIS_URL")

#: Namespace'ы тестовых ключей проекта (см. докстринг модуля).
#: `ws:chat:` сюда НЕ входит: это pub/sub-канал, ключей нет.
_TEST_KEY_PREFIXES = (
    "join_request:",
    "sms:",
    "oauth:state:",
    # Стори 10.7 (P-12): счётчик подряд идущих неудач probe и дедуп-метки
    # алертов. `health:failures:consecutive` живёт с `EXPIRE 86400`, а
    # `alert:*` — с `EX 3600`: без очистки ключи переживают прогон, и первый
    # тест следующего прогона увидит «1 неудачу», которой не было (алерт
    # ушёл бы на первом отказе вместо второго). Дедуп-метка окна
    # (`alert:window_overdue:sent`) дала бы «POST не отправлен» в тесте,
    # который проверяет именно отправку.
    "health:",
    "alert:",
)

#: Размер пачки для UNLINK: ограничивает одну команду.
_UNLINK_BATCH = 200


async def _purge_project_namespaces() -> int:
    """Удаляет ключи тестовых namespace'ов. Возвращает число удалённых.

    `SCAN`, а не `KEYS`: `KEYS` блокирует Redis на всё время обхода, а
    инстанс общий с dev-средой. `UNLINK`, а не `DEL`: освобождение памяти
    уходит в отдельный поток, тест не ждёт его.
    """
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    removed = 0
    try:
        for prefix in _TEST_KEY_PREFIXES:
            batch: list[str] = []
            async for key in client.scan_iter(match=f"{prefix}*", count=500):
                batch.append(key)
                if len(batch) >= _UNLINK_BATCH:
                    removed += int(await client.unlink(*batch))
                    batch.clear()
            if batch:
                removed += int(await client.unlink(*batch))
    finally:
        await client.aclose()
    return removed


def _purge_safely() -> int:
    """Синхронная обёртка.

    Фикстура sync намеренно: session-scoped async-фикстура привязала бы
    собственный event loop и конфликтовала бы с `asyncio_mode = "auto"`
    (тот же приём, что у `clean_database`).

    Ошибка Redis не пробрасывается: часть набора (unit-тесты rate limiter)
    живёт без Redis, и падать на уборке они не должны. Факт сбоя печатается
    в stderr - молча пропущенная уборка вернула бы ровно тот дефект, который
    фикс закрывает.
    """
    if not REDIS_URL:
        return 0
    try:
        return asyncio.run(_purge_project_namespaces())
    except (RedisError, OSError) as exc:
        print(f"[X-6.7-2] очистка Redis не выполнена: {exc!r}")
        return 0


@pytest.fixture(scope="session", autouse=True)
def clean_redis_namespaces() -> Generator[None]:
    """X-6.7-2: прогон начинается и заканчивается на чистом Redis.

    Autouse и session-scoped: воспроизводимость нужна всему набору.
    Явной зависимости в тестах не требуется - фикстура не подменяет
    поведение кода, только убирает состояние между прогонами.

    Что НЕ маскируется. Тесты, проверяющие TTL/накопление
    (`test_sms_store_redis.py`, `test_phone_auth.py::test_phone_rate_limit_*`)
    создают ключи ВНУТРИ своего теста на уникальных телефонах/IP (uuid4) и
    читают их в том же тесте. Между setup и teardown этого хука их нет.
    """
    _purge_safely()
    yield
    _purge_safely()


# ——— Изоляция конфигурации structlog между тестами (X-6.7-2, вторая половина) ———
#
# Дефект, найденный при доказательстве X-6.7-2: полный прогон стабильно давал
# `1 failed` в `test_geo_service.py::test_structlog_leak_is_visible_to_the_guard`,
# при этом `tests/unit` целиком и тот же тест одиночно — зелёные.
#
# Причина: `app/main.py` в lifespan вызывает `configure_logging()`, который
# ставит `wrapper_class=make_filtering_bound_logger(WARNING)` для `ENV=test`
# (`LOG_LEVELS = {"development": DEBUG, "test": WARNING}`) и
# `cache_logger_on_first_use=True`. Integration-тесты, перехватывающие события
# structlog, подменяют конфиг и в `finally` зовут `structlog.reset_defaults()` —
# а он сбрасывает к ДЕФОЛТАМ structlog, не к конфигу приложения. Дальше набор
# живёт с чужим конфигом.
#
# Последствие серьёзнее одного красного теста: guard-тест существует, чтобы
# доказать, что `test_coordinates_are_not_logged` НЕ слеп к structlog. С чужим
# конфигом он падал — то есть в полном прогоне защита от утечки координат
# (FR59) не проверялась вообще. Красный тест здесь — правильное поведение:
# он поймал, что проверка ослепла.
#
# Фикс в одной точке вместо восьми: восстанавливаем снимок конфига после
# КАЖДОГО теста. Тогда неважно, чем именно тест испортил глобальное
# состояние (`reset_defaults`, `configure(processors=...)`), — следующий тест
# получает тот конфиг, с которым начал предыдущий.


@pytest.fixture(autouse=True)
def _restore_structlog_config() -> Generator[None]:
    """Возвращает глобальный конфиг structlog к снимку после каждого теста.

    Function-scoped: утечка происходит внутри теста, значит и лечить её надо
    на границе теста. Скоп `session` оставил бы испорченный конфиг на весь
    прогон — ровно то, что наблюдалось.

    Восстановление через `configure(**snapshot)`, а не `reset_defaults()`:
    снимок содержит конфиг, который был ДО теста (обычно — приложения),
    а `reset_defaults()` вернул бы дефолты библиотеки.
    """
    import structlog

    snapshot = structlog.get_config()
    yield
    structlog.configure(**snapshot)


@pytest.fixture(autouse=True)
async def _reset_app_engine() -> AsyncIterator[None]:
    """Закрывает глобальный движок приложения после каждого теста.

    Скоп `function`: соединение живёт ровно один тест. `session` не годится —
    teardown выполнился бы в чужом loop'е и упал бы на том же дефекте.

    `suppress` обязателен: если тест уже сломал пул, падение уборки
    замаскировало бы настоящую причину красного теста.
    """
    yield

    from app.core.db import dispose_engine

    with contextlib.suppress(Exception):
        await dispose_engine()
