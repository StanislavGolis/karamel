"""`GET /address/city` — рамка города автора (стори 13.1; N-1 CR r2 2026-11-01).

**Почему этот файл появился только после CR r2.** Маршрут введён правкой H-1
(карта на шаге 2 центрируется по городу автора) и не был покрыт НИ ОДНИМ
тестом: `grep -rn 'address/city' services/api/tests/` дал **0**. Три мутации
CR r2 выжили все сразу: снять `no-store`, убрать `CurrentUser` (маршрут
становится анонимным) и зашить `"tomsk"` вместо города из профиля.

**Что может сломаться без тестов.**

1. **Анонимный маршрут.** Рамка города — не секрет, но анонимный эндпоинт
   обходит любую будущую авторизацию раздела. `CurrentUser` обязателен по
   той же причине, что у прокси поиска (AC-2): бесплатный релей к нашим данным.
2. **`no-store`.** Рамка описывает город пользователя; кэш прокси сохранил бы
   её на диск, а `NFR34` запрещает ХРАНИТЬ геоданные. Заголовок ставит САМ
   маршрут, а не `NoStoreMiddleware` — иначе тест зеленел бы от middleware
   (прецедент M12, `test_address_search_headers.py`).
3. **Город из профиля.** Зашитый `"tomsk"` открыл бы карту чужого города:
   автор из Кемерово искал бы своё заведение в Томске и не находил.

Сеть не нужна: маршрут читает только настройки.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession, *, city_slug: str | None = None) -> tuple[Any, str]:
    """Учётка с живой сессией; `city_slug` можно подменить (тест чужого города)."""
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        status="active",
    )
    if city_slug is not None:
        await session.execute(
            text("UPDATE users SET city_slug = :slug WHERE id = :id"),
            {"slug": city_slug, "id": user.id},
        )
        await session.flush()
        # ORM-объект не знает о правке сырым SQL: без `refresh` тест читает
        # `user.city_slug` из identity map и видит СТАРЫЙ слаг.
        await session.refresh(user)
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _get_city(client: AsyncClient, token: str | None) -> Any:
    cookies = {ACCESS_COOKIE_NAME: token} if token else {}
    return await client.get("/api/v1/address/city", cookies=cookies)


async def test_anonymous_gets_401(client: AsyncClient) -> None:
    """N-1 (M02): без токена — `401`.

    Краснеет, если убрать `CurrentUser` из подписи: маршрут становится
    анонимным, и рамка города (геоданные) отдаётся кому угодно без учётки.
    """
    response = await _get_city(client, None)

    assert response.status_code == 401


async def test_no_store_is_set_by_endpoint_itself() -> None:
    """N-1 (M01): `no-store` ставит САМ маршрут, а не middleware.

    `NoStoreMiddleware` дописывает заголовок КАЖДОМУ ответу, поэтому
    интеграционная проверка зеленела бы и при удалённой строке в маршруте
    (прецедент M12). Здесь вызывается функция-эндпоинт напрямую, без
    приложения — middleware не участвует.
    """
    from app.api.v1.address import city_viewbox_endpoint

    class _User:
        city_slug = "tomsk"

    response = await city_viewbox_endpoint(user=_User())  # type: ignore[arg-type]

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert b"viewbox" in response.body


async def test_viewbox_comes_from_users_city_not_literal(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-1 (M03): рамка — города АВТОРА, а не зашитый `tomsk`.

    Пара из двух городов: подмена литералом ловится (Кемерово получил бы
    томскую рамку), а норма не сломана (Томск получает свою). Без пары тест
    зеленел бы на любом значении, совпавшем с городом по умолчанию.
    """
    monkeypatch.setattr(
        settings,
        "geo_city_viewbox",
        {"tomsk": "84.80,56.60,85.20,56.30", "kemerovo": "85.90,55.40,86.30,55.20"},
    )
    monkeypatch.setattr(settings, "city_slugs", ("tomsk", "kemerovo"))

    _, kemerovo_token = await _new_user(session, city_slug="kemerovo")
    kemerovo = await _get_city(client, kemerovo_token)
    assert kemerovo.status_code == 200
    assert kemerovo.json()["viewbox"] == "85.90,55.40,86.30,55.20", (
        "автор из Кемерово получил ЧУЖУЮ рамку — зашитый литерал"
    )

    _, tomsk_token = await _new_user(session, city_slug="tomsk")
    tomsk = await _get_city(client, tomsk_token)
    assert tomsk.status_code == 200
    assert tomsk.json()["viewbox"] == "84.80,56.60,85.20,56.30"


async def test_city_without_viewbox_returns_null(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`W-13.1-f`: город без рамки → `{"viewbox": null}`, а НЕ `500`.

    Краснеет, если использовать `settings.geo_city_viewbox[city_slug]`:
    `KeyError` дал бы `500` на каждом шаге 2 у города без рамки. В пилоте
    два города и одна рамка, поэтому случай не гипотетический.
    """
    monkeypatch.setattr(settings, "geo_city_viewbox", {"tomsk": "84.80,56.60,85.20,56.30"})
    monkeypatch.setattr(settings, "city_slugs", ("tomsk", "kemerovo"))

    _, token = await _new_user(session, city_slug="kemerovo")
    response = await _get_city(client, token)

    assert response.status_code == 200
    assert response.json() == {"viewbox": None}
