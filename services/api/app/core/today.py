"""Одно «сегодня» на HTTP-запрос (стори 12.3, Task 10; долг `W-12.2-b`).

**Дефект, который закрывается.** Гард 18+ (`app/api/deps.py`) и сериализация
ответа (`event_card_service`, `join_request_service`, `moderation_service`,
`chat_participants_service`, `moderation_user_card_service`) вызывают
`compute_age` в ОДНОМ запросе, и каждый брал `date.today()` заново. В полночь
эти вызовы расходятся: гард посчитал 18 по вчерашней дате, ответ — 17 по
сегодняшней (или наоборот). Пользователь либо получает доступ с неверным
возрастом в карточке, либо отказ при верном.

**Почему middleware, а не параметр в каждом вызове.** `compute_age` сохраняет
обязательный `today` (AC-6 стори 12.2: граница 18 лет проверяется
детерминированно). Протаскивать `today` из HTTP-слоя через пять сервисов
означало бы менять их сигнатуры ради одного значения — и добавлять шестое
место, где его можно забыть.

**Почему не зависимость FastAPI.** Зависимости вычисляются для маршрутов,
а `date.today()` нужен и до её выполнения (гард в цепочке зависимостей),
и после (сериализация в сервисе). Middleware обнимает весь запрос целиком.

`ContextVar` копируется в дочерние задачи asyncio: значение видно во всех
корутинах запроса и не течёт между запросами — в отличие от модульной
переменной, которую пришлось бы сбрасывать вручную.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.age_service import override_today


class TodayMiddleware:
    """Фиксирует «сегодня» на время HTTP-запроса.

    Чистый ASGI-middleware, как `NoStoreMiddleware`: `BaseHTTPMiddleware`
    буферизует тело и ломает стриминг ответа.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with override_today(date.today()):
            await self.app(scope, receive, send)
