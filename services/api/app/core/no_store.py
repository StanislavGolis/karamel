"""Гарантия `cache-control: no-store` на КАЖДОМ ответе приложения (QD A6).

CR 3.2 (`deferred-work.md`): `PATCH /api/v1/places/{uuid}/` отвечал `307`
редиректом на путь без слэша, и у этого ответа заголовка `no-store` не было.
Редирект генерирует сам роутер Starlette (`redirect_slashes=True`) ДО
обработчиков исключений, поэтому ни `_error()` в роутерах, ни
`http_error_handler` в `main.py` на него не влияют.

Почему middleware, а не `redirect_slashes=False`: отключение редиректов
превратило бы `POST /places/` в `405` и сломало бы клиентов, которые уже
ходят со слэшем. Кэшируемый редирект на ресурс под 401 — это утечка
состояния между пользователями, поэтому нужен ровно заголовок, а не
смена маршрутизации.

Реализован как чистый ASGI-middleware, а не `BaseHTTPMiddleware`: последний
буферизует тело и ломает стриминг. Здесь `send` оборачивается и заголовок
дописывается только если его ещё нет — значение из приложения не
перезаписывается.
"""

from __future__ import annotations

from typing import Any


class NoStoreMiddleware:
    """Дописывает `cache-control: no-store`, если приложение его не поставило."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)
