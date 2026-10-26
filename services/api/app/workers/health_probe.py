"""Воркер health-probe: опрос `/health/deep` и алерты (стори 10.7, AC-01; D-10.7-5).

**Почему отдельный контейнер, а не поток в api.** Проба внутри api-процесса
исчезает ровно тогда, когда api упал, — в единственном случае, ради которого
она существует. Поэтому `health-probe` — свой контейнер (`docker-compose.yml`,
`mem_limit: 128m`), без `healthcheck:`: проверка probe — сам алерт, а
healthcheck добавил бы второй, независимый сигнал «контейнер жив», который
ничего не говорит о наблюдаемом сервисе.

**Цикл как у `card_windows.main`** (`card_windows.py:168-189`): прогон, пауза,
падение одного прогона не убивает процесс. Падение probe при живом api — это
ложный алерт «всё лежит», то есть шум, из-за которого настоящий алерт
пропустят.

**Что именно проверяется.** `GET {api_url}/health/deep` (Task 2.1): 200 =
успех, ЛЮБОЙ другой исход (503, 500, таймаут, отказ соединения) = неуспех.
Тело не разбирается: разбор дал бы ветку «форма ответа не та», а это тоже
неуспех — решение о причине принимает человек по `checks` в логе api.

**Недоступность Redis — алерт немедленно** (D-10.7-3). Счётчик «2 подряд»
живёт в Redis; если Redis лёг, считать нечем, а Redis — обязательная
зависимость (rate limiting, OAuth-state, WS-брокер). Асимметрия намеренна:
«2 подряд» защищает от флейка сети, а отказ Redis флейком не бывает.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis import get_redis
from app.services.alert_service import send_alert
from app.services.health_state import register_failure, register_success

# Таймаут HTTP-запроса к api. Пять секунд: `/health/deep` сам ограничен
# тремя секундами на проверку, и probe обязан получить его ответ, а не
# оборвать раньше. Меньше — получили бы «неуспех» на здоровом сервисе.
PROBE_TIMEOUT_SECONDS = 5.0

# Ключ дедупа алерта о неудачах probe. Отдельный от окна
# (`alert:window_overdue:sent`): иначе один инцидент глушил бы другой.
HEALTH_ALERT_KEY = "alert:health:sent"

# Ключ дедупа алерта о недоступности Redis. Отдельный: он приходит при
# лежачем Redis, и общий ключ (который тоже лежит в Redis) не записался бы —
# то есть «Redis лёг» не было бы замечено никогда.
REDIS_ALERT_KEY = "alert:redis_unavailable:sent"

logger = structlog.get_logger(__name__)


def _deep_health_url() -> str:
    """Адрес `/health/deep` из `API_URL` (тот же, что у web в compose)."""
    return f"{settings.api_url.rstrip('/')}/health/deep"


async def _probe(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Один HTTP-запрос. Возвращает `(успех, тип_ошибки_или_пусто)`.

    Широкий `except` намеренный: любая ошибка транспорта означает одно —
    api не ответил. Перечисление типов дало бы дыру на незнакомом исключении,
    и probe упал бы вместо того, чтобы посчитать неудачу.
    """
    try:
        response = await client.get(_deep_health_url())
    except httpx.HTTPError as error:
        return False, type(error).__name__
    if response.status_code != 200:
        return False, f"http_{response.status_code}"
    return True, ""


async def run_once(redis: Redis, *, client: httpx.AsyncClient | None = None) -> bool:
    """Один прогон probe. `True` — сервис здоров, `False` — неудача.

    Возвращает результат прогона, а не «ушёл ли алерт»: тесту и оператору
    нужен факт здоровья, а решение об алерте — производное (Task 5.3/5.4).
    """
    started = time.monotonic()
    own_client = client is None
    http_client = client if client is not None else httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS)
    try:
        healthy, error_type = await _probe(http_client)
    finally:
        if own_client:
            await http_client.aclose()

    if healthy:
        await register_success(redis)
        logger.info(
            "health.probe.ok",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return True

    # Неудача: считаем серию. Отказ Redis здесь НЕ ловится общей веткой — он
    # означает «считать нечем», а не «сервис недоступен», и обрабатывается
    # отдельно ниже (D-10.7-3).
    try:
        consecutive = await register_failure(redis)
    except RedisError:
        logger.error(
            "alert.redis_unavailable",
            error_type="RedisError",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        # Алерт отправляется сразу и без работающего дедупа: Redis лежит,
        # записать метку некуда — дедуп здесь был бы мёртвым кодом, который
        # выглядит защитой. `send_alert` сам пробует `SET NX`, получает
        # `RedisError` и ГАСИТ его внутри (`alert_service.py`, ветка
        # `alert.dedup_unavailable`), после чего всё равно отправляет.
        #
        # P-4 (CR 10.7): здесь стоял `try/except RedisError` с событием
        # `alert.skipped`. Ветка была НЕДОСТИЖИМА: `send_alert` не
        # пробрасывает `RedisError` наружу (проверено мутацией M8 ревьюера —
        # `alert.skipped` не появился ни в одном прогоне). Обработчик,
        # который никогда не выполняется, читается как защита, но ею не
        # является: он маскирует будущую регрессию, если `send_alert` начнёт
        # пробрасывать ошибку. Мёртвая ветка удалена, обещание снято.
        await send_alert(
            f"env={settings.env} check=redis error_type=RedisError "
            f"duration_ms={int((time.monotonic() - started) * 1000)}",
            key=REDIS_ALERT_KEY,
            redis=redis,
            client=client,
        )
        return False

    logger.error(
        "health.probe.failed",
        check="deep",
        error_type=error_type,
        consecutive=consecutive,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    if consecutive >= settings.alert_consecutive_failures:
        # Порог достигнут. Дедуп внутри `send_alert`: третья неудача подряд
        # не должна слать второе сообщение сразу же — алерт раз в час, пока
        # инцидент не закрыт (Task 5.4).
        await send_alert(
            f"env={settings.env} check=deep error_type={error_type} "
            f"consecutive={consecutive} duration_ms={int((time.monotonic() - started) * 1000)}",
            key=HEALTH_ALERT_KEY,
            redis=redis,
        )
    return False


async def main() -> None:
    """Цикл воркера: прогон, пауза. Ошибка прогона не убивает процесс."""
    redis = get_redis()
    while True:
        try:
            await run_once(redis)
        except Exception as error:  # noqa: BLE001 — воркер обязан жить
            logger.error(
                "worker.health.run.failed",
                error=type(error).__name__,
                detail=str(error)[:500],
            )
        await asyncio.sleep(settings.alert_probe_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
