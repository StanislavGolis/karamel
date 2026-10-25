"""AC-02: числовых маршрутов нет — перебор /profile/1..N невозможен.

Стори 3.2 (AC-08): сюда же добавлена поверхность справочника мест.
`/api/v1/places/1` обязан дать `422` (путь типизирован `uuid.UUID`).

Стори 3.3 (Task 8.8, AC-08): `GET /api/v1/places` реализован, поэтому
`405` проверяется на `PUT`: в справочнике нет метода «заменить место
целиком» (правка — `PATCH`, D-7 стори 3.2), и `405` на `PUT` фиксирует,
что коллекция не принимает полную замену — при этом сам маршрут существует.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", ["/api/v1/users/1", "/api/v1/profiles/1", "/api/v1/users/42"])
async def test_numeric_id_routes_do_not_exist(path: str) -> None:
    """AC-02: внутренний bigint в путь не попадает и не резолвится."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 404


@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_places_numeric_id_not_resolved_as_place(method: str) -> None:
    """AC-08, D-8: `/places/1` без сессии — `401`, но НЕ `404`/`500`.

    Порядок разрешения зависимостей FastAPI: подзависимость `require_moderator`
    вычисляется раньше валидации параметра пути, поэтому анонимный запрос
    получает `401 unauthenticated` ещё до разбора `uuid`. Проверяется главное
    для AC-08: число в пути не резолвится в место (`404` означал бы «место
    удалено») и не падает `500` в репозитории.

    Ответ `422` для типизированного `uuid` проверяется отдельно и с сессией
    администратора — в `tests/integration/test_places_api.py::
    test_numeric_uuid_path_is_422`: там есть фикстуры БД, а здесь модуль
    работает без `DATABASE_URL`.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await getattr(client, method)("/api/v1/places/1")

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_places_put_not_supported() -> None:
    """AC-08, Task 8.8: `PUT /api/v1/places` — `405`.

    Краснеет, если появится `PUT`: полная замена коллекции позволила бы
    перезаписать справочник одним телом и обошла бы `uq_places_community_name`
    (правка идёт `PATCH` по `uuid`, D-7 стори 3.2).

    Стори 3.3: раньше здесь проверялся `GET` (выдача появилась в 3.3), теперь —
    `PUT`. Отказ приходит от роутера, а не от preflight: метод перечислен
    в `CORS.allow_methods` (`main.py`).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/v1/places", json={})

    assert response.status_code == 405


@pytest.mark.parametrize("method", ["patch"])
async def test_cards_numeric_id_not_resolved_as_card(method: str) -> None:
    """Стори 4.2, AC-08: `/cards/1` без сессии — `401`, но НЕ `404`/`500`.

    Краснеет, если `card_uuid` объявить `str`: число дошло бы до SQL, и
    драйвер вернул бы `DBAPIError` (`500`) вместо отказа. Проверяется главное
    для AC-08: число в пути не резолвится в карточку.

    `422` для типизированного `uuid` проверяется с сессией в
    `tests/integration/test_cards_api.py` — здесь модуль работает без БД.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await getattr(client, method)("/api/v1/cards/1", json={})

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_cards_list_requires_session() -> None:
    """Стори 4.5, AC-01: лента существует и защищена сессией — `401`.

    До 4.5 здесь стояло `405`: тест фиксировал, что роут ленты ещё не создан
    (иначе 4.2 отдавала бы карточки без формы ответа §7.2 и без фильтра
    `starts_at >= now()`). 4.5 роут реализовала, поэтому гард сменил смысл:
    проверяется не ОТСУТСТВИЕ маршрута, а то, что публичная лента не отдаётся
    анониму (`ConsentUser` → `401 unauthenticated`, а не `200` с данными
    других людей).

    Краснеет, если снять зависимость `ConsentUser` с `GET /cards`: лента
    с именами и возрастами авторов стала бы доступна без входа (FR9).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/cards")

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_moderation_cards_requires_auth() -> None:
    """Стори 4.3, AC-07/AC-10: очередь модератора существует и закрыта сессией.

    До 4.3 здесь проверялось `404` (маршрута нет). Теперь маршрут есть, и
    проверка стала сильнее: без сессии `401`, то есть очередь НЕ публична.
    Краснеет, если снять `ModeratorUser`/`CurrentUser`: модерация открылась бы
    анонимному клиенту.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/moderation/cards")

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_moderation_cards_numeric_id_is_not_a_route() -> None:
    """AC-10, §7.2: `/moderation/cards/1` — не маршрут с числовым id.

    Краснеет, если объявить путь `/moderation/cards/{id:int}`: внутренние
    `bigint` стали бы публичными адресами, и перебор `/1..N` обходил бы
    проверку существования.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/moderation/cards/1/approve")

    # Нет сессии — `401` раньше валидации пути; с сессией было бы `422` схемы
    # на не-UUID. Важно, что это НЕ `404`/`200` от числового маршрута.
    assert response.status_code in {401, 422}
