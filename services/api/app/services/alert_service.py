"""Отправка алертов в бот мессенджера (стори 10.7, AC-01/AC-03; NFR62, D-10.7-4).

**Почему Telegram Bot API, а не generic webhook.** NFR62 (`prd:855`) называет
канал — «бот в мессенджере». Webhook без потребителя — абстракция: её
пришлось бы кому-то настроить, и «настроить» стало бы отдельной задачей,
которую никто не сделал. Telegram — один `POST` без новой зависимости
(`httpx==0.28.1` уже в `pyproject.toml`), NFR65 не нарушен.

**Пустой токен — не ошибка старта.** Probe поднимается в dev и CI, где бота
нет. Алертинг, блокирующий старт, сам стал бы единой точкой отказа — ровно
тем, что он призван обнаруживать. Вместо падения пишется `alert.disabled`.

**Дедуп — часть контракта, а не улучшение.** Зависшее окно живёт часами:
без дедупа бот получал бы сообщение каждую минуту (1440 в сутки) и был бы
замьючен в первый день — то есть алерт перестал бы существовать как канал.

**Тело без ПД.** Только `env`, `check`, `count`, `error_type`, `duration_ms`
(D-10.7-9). Ни телефонов, ни `card_uuid`, ни текстов жалоб: сообщение уходит
во внешний сервис, то есть за периметр 152-ФЗ (NFR34).
"""

from __future__ import annotations

import time

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

# Таймаут отправки. Пять секунд: Telegram отвечает за десятые доли, а
# «висит дольше» — это уже сетевой инцидент, и ждать его дольше нельзя:
# probe живёт в цикле, и один зависший вызов останавливает обнаружение
# следующего отказа.
HTTP_TIMEOUT_SECONDS = 5.0

# Окно дедупа, секунды (час). Час — компромисс: зависшее окно требует
# внимания, но не чаще раза в час; за сутки это 24 сообщения, которые
# читают, а не 1440, после которых бот замьючен (D-10.7-6).
DEDUP_TTL_SECONDS = 3600

# API Telegram. Хост отдельной константой: тест подменяет транспорт, а не
# строку, и `_url()` остаётся единственным местом, где токен попадает в URL
# (оттуда его вырезает `RedactingSecretsProcessor`, `logging.py:98`).
TELEGRAM_API_HOST = "https://api.telegram.org"

logger = structlog.get_logger(__name__)

# P-1 (CR 10.7): «сообщили о выключенном алертинге» — ОДИН раз за процесс.
# Модульный флаг, а НЕ ключ в Redis, и это не упрощение:
# 1) причина выключения часто и есть недоступность Redis (dev, CI) — запись
#    метки туда сама была бы отказом, и защита от шума падала бы ровно в том
#    сценарии, ради которого написана;
# 2) сообщение описывает СОСТОЯНИЕ КОНФИГУРАЦИИ процесса, а не инцидент:
#    оно не «продолжается», его не нужно повторять раз в час. После
#    перезапуска одна строка на старт — это диагностика, а не шум;
# 3) Redis-ключ с TTL дал бы 24 строки в сутки — тот же класс шума (1440
#    строк), от которого защищаемся, только реже.
_disabled_logged = False


class AlertUnavailableError(RuntimeError):
    """Redis недоступен — считать серию неудач нечем (D-10.7-3).

    Отдельный тип, а не `RedisError` наружу: вызывающий обязан отличить
    «счётчик не растёт» (алерт немедленно) от «отправка не удалась» (алерт
    уже был попыткой). Иначе при лежачем Redis воркер молчал бы целиком.
    """


async def send_alert(
    text: str, *, key: str, redis: Redis, client: httpx.AsyncClient | None = None
) -> bool:
    """Отправляет алерт с дедупом по ключу. `True` — сообщение ушло.

    Возвращает `bool`, а не бросает: алерт — диагностика, и его отказ не
    должен останавливать воркер, который этот алерт отправляет.

    `key` — параметр, а не константа: дедуп зависшего окна
    (`alert:window_overdue:sent`) и дедуп health-алертов — разные ключи.
    Один общий ключ глушил бы второй источник на час после первого.
    """
    global _disabled_logged
    if not settings.alert_telegram_bot_token or not settings.alert_telegram_chat_id:
        # P-1 (CR 10.7): ОДИН раз за процесс, а не на каждый вызов. probe зовёт
        # `send_alert` раз в минуту, и строка «алертинг выключен» каждые 60 с
        # давала 1440 строк в сутки — ровно тот шум, от которого защищались.
        # Флаг ставится ПОСЛЕ проверки: если токен появится в конфигурации
        # позже (перезапуск с заполненным `.env`), новый процесс начнёт с
        # чистого флага и скажет о выключенном состоянии ровно один раз.
        if not _disabled_logged:
            _disabled_logged = True
            logger.warning("alert.disabled", key=key)
        return False

    # Дедуп ДО отправки: `SET NX` — атомарная проверка «я первый за час».
    # `None` в ответе означает, что ключ уже был — сообщение не отправляем.
    # Порядок важен: сначала дедуп, потом сеть, иначе два прогона подряд
    # отправили бы два сообщения и только потом выяснили, что одно лишнее.
    #
    # D-10.7-3: отказ Redis НЕ отменяет отправку. Дедуп — удобство (не спамить),
    # а алерт — суть; при лежачем Redis метку записать некуда, и без этой
    # ветки отказ Redis не был бы замечен НИКОГДА: probe падал бы на `INCR`,
    # `send_alert` падал бы на `SET`, и алерт не уходил бы вовсе. Дубликаты
    # при этом ограничены частотой probe (раз в минуту), а не сутками.
    try:
        taken = await redis.set(key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
    except RedisError:
        logger.warning("alert.dedup_unavailable", key=key, error_type="RedisError")
    else:
        if not taken:
            return False

    started = time.monotonic()
    own_client = client is None
    http_client = client if client is not None else httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        response = await http_client.post(
            f"{TELEGRAM_API_HOST}/bot{settings.alert_telegram_bot_token}/sendMessage",
            json={"chat_id": settings.alert_telegram_chat_id, "text": text},
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        # Тело ответа НЕ логируется: Telegram возвращает в `description`
        # параметры запроса, а в запросе — токен (он же в URL). Пишем тип.
        logger.error(
            "alert.failed",
            key=key,
            error_type=type(error).__name__,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        # Ключ дедупа НЕ снимается намеренно: иначе при лежачем Telegram
        # каждый прогон слал бы новую попытку, и «алерт не ушёл» остался бы
        # незамеченным в потоке одинаковых ошибок. Час тишины после отказа —
        # цена, которую платим за разборчивый лог.
        return False
    finally:
        if own_client:
            await http_client.aclose()

    logger.info(
        "alert.sent",
        key=key,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return True
