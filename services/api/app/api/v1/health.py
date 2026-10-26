"""Глубокий health-check: процесс + БД + Redis (стори 10.7, AC-01, D-10.7-1).

**Почему это ОТДЕЛЬНЫЙ маршрут, а не расширение `/health`.** `/health`
возвращает `{"status":"ok"}` без единого внешнего вызова — это контракт
стори 1.1, и `tests/unit/test_health.py:19-36` его защищает: тест подменяет
`app.core.db.get_engine` функцией, бросающей `AssertionError`, и требует 200.
Проверка БД внутри `/health` покрасила бы этот тест, а удалить его нельзя:
compose (`restart: unless-stopped`) начал бы ПЕРЕЗАПУСКАТЬ api при лежачей
базе, а `web` ждёт `api: service_healthy` — недоступность БД стала бы
недоступностью продукта. Поэтому «процесс жив» и «зависимости живы» —
разные вопросы и разные маршруты.

**Кого дёргает.** Только воркер `health-probe` (`app/workers/health_probe.py`),
отдельным контейнером (D-10.7-5): проба внутри api исчезает ровно тогда,
когда api упал, — в единственном случае, ради которого существует. В
`healthcheck:` compose этот маршрут НЕ входит (D-10.7-1).

**Ничего не пишет.** Только `SELECT 1` и `PING`: проверка наблюдаемости не
должна сама менять наблюдаемое состояние. Аутентификации нет намеренно —
порт api опубликован только на `127.0.0.1:8000` (`docker-compose.yml:94-95`),
а тело ответа не содержит ни ПД, ни имён хостов, ни строк подключения.

**Изоляция проверок.** Каждая зависимость проверяется отдельно и в своём
`wait_for`: отказ Redis обязан дать `checks.redis = fail:…` при `checks.db
= "ok"`, иначе диагноз «лежит всё» появится там, где легла одна база.
Таймаут превращается в строку `fail:<тип>`, а не в `500`: `500` от
health-маршрута неотличим от падения процесса, и алерт соврёт о причине.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.db import get_engine
from app.core.redis import get_redis

# Предел на одну проверку. Три секунды: `PING` и `SELECT 1` отвечают за
# миллисекунды, а «висит дольше» уже само по себе инцидент. Пять секунд
# дали бы probe два разных таймаута (свой HTTP и этот) и неразличимую
# причину в алерте.
_CHECK_TIMEOUT_SECONDS = 3.0

# Публичная форма отказов — те же имена, что в structlog-событиях
# `health.probe.failed` (D-10.7-9): `check` в логе и ключ в `checks`
# обязаны совпадать, иначе разбор инцидента требует таблицы перевода.
OK = "ok"

router = APIRouter(tags=["health"])

logger = structlog.get_logger(__name__)


async def _check_db() -> str:
    """`SELECT 1` через ленивый движок. Возвращает `ok` или `fail:<тип>`.

    Ошибка не пробрасывается: маршрут отвечает 503 с диагнозом, а не 500.
    `async with` на движке не открывается: соединение берётся из пула и
    возвращается в него, иначе каждый прогон probe (раз в минуту) тянул бы
    новое TCP-соединение к PostgreSQL.
    """
    engine = get_engine()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return OK


async def _check_redis() -> str:
    """`PING` тем же ленивым клиентом, что и остальное приложение (Task 2.1).

    Отдельный клиент здесь дал бы вторую точку правды о `REDIS_URL` и
    собственный пул, который никто не закрывает (`dispose_redis`).
    """
    client = get_redis()
    await client.ping()
    return OK


async def _run_check(name: str, check: Callable[[], Awaitable[str]]) -> str:
    """Одна проверка в своём `wait_for`: отказ изолирован по имени.

    Широкий `except` намеренный и единственный в модуле: любая ошибка
    драйвера (`RedisError`, `OSError`, `TimeoutError`, ошибка разбора DSN)
    означает одно — «эта зависимость недоступна сейчас». Перечисление типов
    дало бы дыру: незнакомое исключение вылетело бы наружу и превратило
    диагноз в `500`.
    """
    try:
        return await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
    except Exception as error:
        logger.warning(
            "health.deep.check_failed",
            check=name,
            error_type=type(error).__name__,
        )
        return f"fail:{type(error).__name__}"


@router.get("/health/deep", include_in_schema=False)
async def health_deep() -> JSONResponse:
    """AC-01: 200 если живы процесс, БД и Redis; 503 `degraded` иначе.

    `include_in_schema=False`: маршрут для воркера, а не для клиента.
    Публичный контракт API (`openapi.json`) не должен обещать его
    потребителям — иначе появится клиент, которого мы не планировали.

    Обе проверки идут ОДНОВРЕМЕННО (`gather`): последовательные сложили бы
    свои таймауты (до 6 с), а probe ждёт ответ 5 с — «лежит всё» выглядело
    бы как таймаут probe без диагноза.
    """
    db_result, redis_result = await asyncio.gather(
        _run_check("db", _check_db),
        _run_check("redis", _check_redis),
    )
    checks = {"db": db_result, "redis": redis_result}
    healthy = db_result == OK and redis_result == OK
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
        headers={"Cache-Control": "no-store"},
    )
