"""Технический rate limit подачи заявок (стори 5а.2; AC-10, FR61, NFR17).

Два лимита живут раздельно, и это продуктовое решение, а не деталь
реализации (OQ-5a.2-1):

- **доменный** — «не больше двух АКТИВНЫХ заявок» (FR77/NFR29). Считается
  по строкам `join_requests` под `FOR UPDATE users` (`join_request_service`);
- **технический** — «не больше N подач за окно» (FR61/AR32, анти-абьюз).
  Считается здесь по Redis, ключ — аккаунт, а не карточка.

Если бы это был ОДИН лимит с числом 2, FR82 стал бы невыполним: участник
отменил заявку (слот освободился), но суточный счётчик подач остался бы
исчерпанным — вторую подачу он бы не сделал. Поэтому технический потолок
(10) строго выше доменного (2), и проверяется он ДО доменных гардов.

Механика счётчика НЕ дублируется: тот же приём `SET … NX` + `INCR`, что
у `sms_store.hit_rate` (D-7, CR 1.4). Общий вынесенный лимитер — долг
`X-44`: две реализации различаются ключом и набором ошибок, а не логикой
окна, и сливать их в 5а.2 значило бы менять поведение SMS-лимитов без
потребности стори.
"""

from __future__ import annotations

from typing import Protocol

import structlog
from redis.exceptions import RedisError

from app.core.redis import get_redis

logger = structlog.get_logger(__name__)

# AC-10: ключ по АККАУНТУ, не по IP и не по карточке.
# - аккаунт — единица NFR17 («с аккаунта»);
# - IP не годится: NAT на мобильном операторе собрал бы всех участников
#   одного города в один счётчик;
# - карточка не годится: обход — подавать по одной заявке на разные карточки.
_KEY_PREFIX = "join_request:submit:"


class RateLimiter(Protocol):
    """AC-10: минимум, который нужен роутеру. Реализация подставляется в тестах.

    `None` — запрос разрешён; число — остаток окна в секундах (как у
    `RedisSmsStore.hit_rate`). Протокол, а не конкретный класс: тесты не
    поднимают Redis, а роутер не знает, что за хранилище внутри.
    """

    async def hit(self, applicant_id: int) -> int | None: ...


class JoinRequestRateLimiter:
    """AC-10: fixed-window счётчик подач по аккаунту."""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        """Значения приходят из настроек (`join_request_rate_*`), не из литералов:
        FR61 требует менять поведение без правок кода."""
        self._limit = limit
        self._window = window_seconds

    async def hit(self, applicant_id: int) -> int | None:
        """AC-10: инкремент счётчика; `None` — можно, число — `retry_after`.

        `SET key 0 EX window NX` ставит TTL только на создании ключа: ключ
        с истёкшим окном создастся заново и начнёт новый отсчёт (D-7).
        Отдельный `EXPIRE` после `INCR` дал бы окно, продлеваемое каждым
        запросом, — то есть не fixed-window, а «не больше N подряд».

        Сбой Redis НЕ пробрасывается: NFR17 — анти-абьюз, а не инвариант
        домена. Легальное действие не должно падать из-за недоступного
        хранилища (ADR-11); факт сбоя логируется, лимит пропускается.
        """
        key = f"{_KEY_PREFIX}{applicant_id}"
        try:
            client = get_redis()
            await client.set(key, 0, ex=self._window, nx=True)
            count = int(await client.incr(key))
            if count <= self._limit:
                return None
            ttl = int(await client.ttl(key))
        except RedisError:
            # AC-10: лог, а не исключение. `error_code` — машинный признак для
            # метрик: «лимит не сработал» должно быть видно в логах, иначе
            # анти-абьюз молча выключается вместе с Redis.
            logger.warning("join_request.rate.failed", applicant_id=applicant_id)
            return None
        # `ttl` = -1/-2 значит «срок неизвестен»: `Retry-After: 0` заставил бы
        # клиента повторить немедленно (CR 1.4, патч 16), поэтому отдаём окно.
        return ttl if ttl > 0 else self._window
