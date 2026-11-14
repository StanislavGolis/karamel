"""Прокси адресного поиска (стори 13.1, Task 3; AC-2, AC-4, AC-5, AC-6).

Реальный PostgreSQL, схема поднята Alembic. **Сеть не используется ни в одном
тесте**: транспорт Nominatim подменяется `httpx.MockTransport`. Класс
«зелёный локально / красный в CI» (12.6/12.7/12.9) рождался именно из тестов,
читающих внешнее окружение; здесь окружением был бы чужой HTTP-сервис.

§18.2: каждый тест краснеет от снятия конкретной защиты — проверяются коды,
заголовки и ТЕЛО, а не только статус.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.address_geocoder as geocoder_module
from app.core.db import get_session
from app.core.redis import get_redis
from app.main import app
from app.repositories.users import UserRepository
from app.services.address_geocoder import _RATE_KEY, GeoRateLimitedError
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _nominatim_item(**overrides: Any) -> dict[str, Any]:
    """Запись в форме Nominatim `jsonv2`: `lat`/`lon` строками, не числами."""
    base: dict[str, Any] = {
        "display_name": "Кинотеатр «Прага», улица Ленина, 12, Томск, Россия",
        "name": "Кинотеатр «Прага»",
        "lat": "56.4977",
        "lon": "84.9481",
        "class": "amenity",
        "type": "cinema",
    }
    base.update(overrides)
    return base


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой зависимостью сессии."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture(autouse=True)
async def _geocoder_transport(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Транспорт Nominatim — `MockTransport`; без подмены тест пошёл бы в сеть."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_nominatim_item()])

    fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=3.0)
    monkeypatch.setattr(geocoder_module, "_client", fake_client)
    yield
    await fake_client.aclose()
    geocoder_module._client = None


@pytest.fixture(autouse=True)
async def _clear_global_rate_key() -> AsyncIterator[None]:
    """Глобальный ключ лимита чистится: иначе второй тест получил бы `503`.

    Ключ живёт одну секунду (`PX 1000`), но прогон быстрее — без очистки
    тесты были бы связаны порядком (класс `W-11.8-*`: тест читает состояние,
    оставленное соседом).
    """
    redis = get_redis()
    await redis.delete(_RATE_KEY)
    yield
    await redis.delete(_RATE_KEY)


async def _new_user(session: AsyncSession) -> tuple[Any, str]:
    """Учётка с живой сессией; возвращает `(user, access_token)`."""
    phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        status="active",
    )
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _call(client: AsyncClient, token: str | None, query: str = "Прага") -> httpx.Response:
    """GET прокси с cookie сессии (имя — `karamel_access`, не `access_token`)."""
    cookies = {ACCESS_COOKIE_NAME: token} if token else {}
    return await client.get("/api/v1/address/search", params={"q": query}, cookies=cookies)


# --- AC-2: авторизация ----------------------------------------------------------


async def test_anonymous_gets_401(client: AsyncClient) -> None:
    """AC-2, AC-7: анонимный прокси — это релей к Nominatim с нашего IP.

    Краснеет, если убрать `CurrentUser`: любой человек без учётки дёргал бы
    OSM через наш адрес, и блокировка по правилу OSM легла бы на сервис.
    """
    response = await _call(client, None)
    assert response.status_code == 401


# --- AC-10, AC-5: успех ---------------------------------------------------------


async def test_success_returns_coordinates_and_no_store(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-5: единственный маршрут, где `lat`/`lng` в ответе легальны.

    Краснеет от переименования `lng` → `lon`: контракт проекта говорит `lng`,
    и клиент прочитал бы `undefined` молча. Проверяется и `cache-control`:
    тело с координатами не должно осесть в кэше прокси.
    """
    _, token = await _new_user(session)
    response = await _call(client, token)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = response.json()
    assert len(items) == 1
    assert set(items[0]) == {
        "name",
        "address_text",
        "display_name",
        "category_hint",
        "lat",
        "lng",
    }
    assert items[0]["lng"] == pytest.approx(84.9481)
    assert items[0]["lat"] == pytest.approx(56.4977)


# --- AC-2: короткий запрос ------------------------------------------------------


async def test_short_query_gets_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-2: меньше двух символов → `422 QUERY_TOO_SHORT`, а не пустая выдача."""
    _, token = await _new_user(session)
    response = await _call(client, token, query="а")

    assert response.status_code == 422
    assert response.json()["code"] == "QUERY_TOO_SHORT"
    assert response.headers["cache-control"] == "no-store"


# --- AC-4: деградация -----------------------------------------------------------


async def test_provider_failure_gets_503(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: недоступность провайдера → `503 GEO_UNAVAILABLE`.

    Краснеет, если ветку заменить на `-> []`: клиент показал бы «ничего не
    найдено» вместо «поищите вручную», и автор остался бы без подсказки,
    не понимая почему.
    """
    _, token = await _new_user(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    failing = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=3.0)
    monkeypatch.setattr(geocoder_module, "_client", failing)

    response = await _call(client, token)

    assert response.status_code == 503
    assert response.json()["code"] == "GEO_UNAVAILABLE"
    await failing.aclose()


# --- AC-6: лимит аккаунта -------------------------------------------------------


async def test_account_rate_limit_gets_429_with_retry_after(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-6: тело `{"code":"rate_limited","retry_after":N}` + заголовок.

    Код В НИЖНЕМ регистре — как у `join_requests.py:108`: клиент ветвится по
    строке, и второй регистр завёл бы вторую ветку обработки.

    Краснеет, если убрать `Retry-After`: клиент повторил бы запрос сразу и
    снова получил `429`, то есть лимит превратился бы в цикл.
    """
    _, token = await _new_user(session)

    class _AlwaysLimited:
        async def hit(self, applicant_id: int) -> int | None:
            return 7

    from app.api.v1.address import get_address_rate_limiter

    app.dependency_overrides[get_address_rate_limiter] = lambda: _AlwaysLimited()
    try:
        response = await _call(client, token)
    finally:
        app.dependency_overrides.pop(get_address_rate_limiter, None)

    assert response.status_code == 429
    assert response.json() == {"code": "rate_limited", "retry_after": 7}
    assert response.headers["Retry-After"] == "7"
    assert response.headers["cache-control"] == "no-store"


async def test_own_global_limit_gets_503(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3, AC-4: наш собственный 1 req/s исчерпан → `503`, не `429`.

    Различие намеренное: `429` — «вы слишком часто» (действие пользователя),
    `503` — «провайдер сейчас недоступен» (действие системы). `Retry-After`
    отдаётся в обоих случаях, но клиент ведёт себя по-разному: `429` — ждать
    и повторить, `503` — перейти на ручной ввод.
    """
    _, token = await _new_user(session)

    async def _limited() -> None:
        raise GeoRateLimitedError(retry_after=1)

    monkeypatch.setattr(geocoder_module, "_acquire_global_slot", _limited)

    response = await _call(client, token)

    assert response.status_code == 503
    assert response.json()["code"] == "GEO_UNAVAILABLE"
    assert response.headers["Retry-After"] == "1"


async def test_too_long_query_gets_422(client: AsyncClient, session: AsyncSession) -> None:
    """M-4 CR 2026-10-31: `q` длиннее 200 символов — `422`, ДО сети.

    Краснеет при отсутствии `max_length`: 10-КБ запрос нормализовался бы и
    ушёл в Nominatim, сожёгши наш лимит 1 req/s и egress, а провайдер ответил
    бы `414` → `503` для пользователя. Ошибка ввода обязана отбиваться на
    входе и дешёво.
    """
    _, token = await _new_user(session)
    response = await _call(client, token, query="я" * 201)

    assert response.status_code == 422
    # Форма — конверт `{"code": ...}`: клиент ветвится по коду, не по тексту.
    assert response.json()["code"] == "invalid_request"
    assert response.headers["cache-control"] == "no-store"


async def test_query_at_max_length_is_accepted(client: AsyncClient, session: AsyncSession) -> None:
    """M-4: пара к отказу — ровно 200 символов проходит (граница включительна).

    Без пары тест `422` зеленел бы и при `max_length=1`, то есть проверял бы
    «любая длина отклонена», а не границу.
    """
    _, token = await _new_user(session)
    response = await _call(client, token, query="я" * 200)

    assert response.status_code == 200
