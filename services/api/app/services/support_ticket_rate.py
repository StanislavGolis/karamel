"""Технический rate limit обращений в поддержку (стори 9.5; AC-01, D-9.5-9).

**Только технический лимит — доменного НЕТ.** У заявок на участие два
раздельных лимита (доменный FR77 «не больше двух активных» + технический
FR61), потому что там считаются ЖИВЫЕ строки состояния. Здесь FR55 говорит
лишь «пользователь может отправить обращение», числа не задаёт, а состояние
обращения (`open`) не мешает отправить следующее — значит и доменного
счётчика нет: 5/ч отсекает флуд, не мешая уточнить проблему (R-1).

**Ключ — аккаунт, не IP и не тело обращения** (D-9.5-9). IP на мобильном
операторе собрал бы весь город в один счётчик (NAT); тело — ключ, который
клиент меняет сам, то есть лимита нет вовсе.

**Механика — копия приёма `join_request_rate.py`** (`SET … NX` + `INCR`).
Вынести общий лимитер — долг `X-44`: три реализации (SMS, заявки, поддержка)
различаются ключом и набором ошибок, а не логикой окна, и слияние внутри 9.5
значило бы менять поведение SMS-лимитов без потребности стори.

**Сбой Redis НЕ пробрасывается** (ADR-11, NFR57): легальное действие не
должно падать из-за недоступного хранилища. Факт сбоя логируется — иначе
анти-абьюз молча выключился бы вместе с Redis.
"""

from __future__ import annotations

import structlog
from redis.exceptions import RedisError

from app.core.redis import get_redis
from app.services.join_request_rate import RateLimiter

__all__ = ["RateLimiter", "SupportTicketRateLimiter"]

logger = structlog.get_logger(__name__)

#: AC-01, D-9.5-9: namespace ключа. Отдельный от `join_request:` — иначе
#: обращение в поддержку расходовало бы лимит подачи заявок (и наоборот),
#: и пользователь получал бы `429` на действие, которого не совершал.
_KEY_PREFIX = "support_ticket:submit:"


class SupportTicketRateLimiter:
    """AC-01, D-9.5-9: fixed-window счётчик обращений по аккаунту.

    Реализует протокол `RateLimiter` (`join_request_rate`): второй протокол
    дал бы два описания одной зависимости, и фейк в тестах пришлось бы
    писать дважды (R-5-класс).
    """

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        """Значения приходят из настроек (`support_ticket_rate_*`), не из
        литералов: R-1 требует менять число без пересборки образа — только
        `.env` (FR55, §18.6 про деплой).
        """
        self._limit = limit
        self._window = window_seconds

    async def hit(self, user_id: int) -> int | None:
        """AC-01: инкремент счётчика; `None` — можно, число — `Retry-After`.

        `SET key 0 EX window NX` ставит TTL только на создании ключа: ключ с
        истёкшим окном создастся заново и начнёт новый отсчёт (D-7).
        Отдельный `EXPIRE` после `INCR` дал бы окно, продлеваемое каждым
        запросом, — то есть не fixed-window, а «не больше N подряд».
        """
        key = f"{_KEY_PREFIX}{user_id}"
        try:
            client = get_redis()
            await client.set(key, 0, ex=self._window, nx=True)
            count = int(await client.incr(key))
            if count <= self._limit:
                return None
            ttl = int(await client.ttl(key))
        except RedisError:
            # AC-01, ADR-11: лог, а не исключение. `user_id` — не ПД: это
            # внутренний bigint, и без него сбой не сопоставить с аккаунтом.
            logger.warning("support_ticket.rate.failed", user_id=user_id)
            return None
        # `ttl` = -1/-2 значит «срок неизвестен»: `Retry-After: 0` заставил бы
        # клиента повторить немедленно, поэтому отдаём полное окно.
        return ttl if ttl > 0 else self._window
