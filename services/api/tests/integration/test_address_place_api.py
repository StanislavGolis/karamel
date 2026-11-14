"""Автосоздание места по результату поиска (стори 13.1, Task 3.5, Task 7.3; AC-9).

Реальный PostgreSQL, схема поднята Alembic. Проверяется не только код ответа,
но и СОСТОЯНИЕ БД: `200` не доказывает, что место создано ровно одним, и не
доказывает, что координат в строке нет (`NFR34`).

§18.2: каждый тест краснеет от снятия конкретной защиты. Пара «активное →
`created:false`» и «скрытое → `422` без `existing_uuid`» — две стороны одного
свойства: успех не должен молча превращаться в отказ, а отказ не должен
утекать чужим UUID.
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
    phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        status="active",
    )
    if city_slug is not None:
        # Слаг живёт в `users`, не в `profiles` (там `city` текстом):
        # `cards.py:451` читает `user.city_slug`, и наш маршрут обязан
        # брать город оттуда же (`Task 3.4`).
        await session.execute(
            text("UPDATE users SET city_slug = :slug WHERE id = :id"),
            {"slug": city_slug, "id": user.id},
        )
        await session.flush()
        # ORM-объект не знает о правке сырым SQL: без `refresh` тест читает
        # `user.city_slug` из identity map и видит СТАРЫЙ слаг. Проверка
        # `assert user.city_slug == ...` тогда падала бы на верном коде.
        await session.refresh(user)
    issued = await issue_session(session, user)
    return user, issued.access_token


def _payload(**overrides: Any) -> dict[str, Any]:
    """Минимальный валидный набор: тест меняет ровно одно поле."""
    base: dict[str, Any] = {
        "name": "Кинотеатр «Прага»",
        "address_text": "улица Ленина, 12, Томск, Россия",
        "category_hint": "cinema",
    }
    base.update(overrides)
    return base


async def _post(client: AsyncClient, token: str | None, payload: dict[str, Any]) -> Any:
    cookies = {ACCESS_COOKIE_NAME: token} if token else {}
    return await client.post("/api/v1/address/place", json=payload, cookies=cookies)


async def _rows(session: AsyncSession, city_slug: str) -> list[dict[str, Any]]:
    """Строки `places` города — сырым SQL: проверяется PostgreSQL, не identity map."""
    result = await session.execute(
        text(
            "SELECT uuid, name, category, address_text, city_slug, is_active "
            "FROM places WHERE city_slug = :slug ORDER BY name"
        ),
        {"slug": city_slug},
    )
    return [dict(row) for row in result.mappings().all()]


# --- AC-2: авторизация ----------------------------------------------------------


async def test_anonymous_gets_401(client: AsyncClient) -> None:
    """AC-9: создание места анонимом запрещено — `401`, а не `422`."""
    response = await _post(client, None, _payload())
    assert response.status_code == 401


# --- AC-9: активация и повторный выбор -------------------------------------------


async def test_new_place_is_created_active_without_coordinates(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-9, AC-5: место создаётся активным, в строке НЕТ координат.

    Краснеет, если добавить `lat`/`lng` в `Place` — то есть нарушить `NFR34`
    ради удобства отрисовки. Проверяется ФАКТ в `information_schema`, а не
    отсутствие поля в схеме ответа.
    """
    user, token = await _new_user(session)
    response = await _post(client, token, _payload())

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert set(body) == {"place_uuid", "created"}

    rows = await _rows(session, user.city_slug)
    assert len(rows) == 1
    assert rows[0]["is_active"] is True
    assert rows[0]["name"] == "Кинотеатр «Прага»"
    assert str(rows[0]["uuid"]) == body["place_uuid"]

    columns = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'places' AND column_name IN ('lat','lng','lon','coordinates')"
        )
    )
    assert columns.scalars().all() == []


async def test_repeated_choice_returns_existing_place(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-9, O-1: повторный выбор активного места → `created: false`, тот же uuid.

    Краснеет, если на дубль отдавать `409 PLACE_DUPLICATE`: для автора выбор
    кинотеатра из подсказок — успех, и требовать от него «придумайте другое
    название» значит ломать главный сценарий стори.
    """
    user, token = await _new_user(session)

    first = await _post(client, token, _payload())
    second = await _post(client, token, _payload())

    assert first.status_code == 200 and first.json()["created"] is True
    assert second.status_code == 200 and second.json()["created"] is False
    assert second.json()["place_uuid"] == first.json()["place_uuid"]
    assert len(await _rows(session, user.city_slug)) == 1


async def test_hidden_place_gets_422_without_existing_uuid(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-9, решение 3: скрытое место → `422 PLACE_HIDDEN` БЕЗ `existing_uuid`.

    Краснеет от двух разных правок, и это намеренно:
    1. если отдать `200` — автор увидит в карточке место, скрытое модератором
       (обход модерации одним запросом);
    2. если положить `existing_uuid` в тело — перебором имён можно узнать,
       какие скрытые места существуют (оракул по невидимым данным, D-6).
    Второе проверяется ОТСУТСТВИЕМ ключа, а не значением: `null` тоже
    раскрывает существование объекта формой ответа.
    """
    user, token = await _new_user(session)
    await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cinema', :address, :slug, false)"
        ),
        {
            "name": "Кинотеатр «Прага»",
            "address": "улица Ленина, 12",
            "slug": user.city_slug,
        },
    )
    await session.flush()

    response = await _post(client, token, _payload())

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "PLACE_HIDDEN"
    assert "existing_uuid" not in body
    assert "existing_uuid" not in body.get("details", {})
    # Реактивации нет (решение 3): строка осталась скрытой.
    rows = await _rows(session, user.city_slug)
    assert rows[0]["is_active"] is False


# --- AC-9: категория и город ----------------------------------------------------


async def test_category_hint_outside_enum_becomes_other(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-9: `category_hint` вне enum → `other` силами СХЕМЫ (`422` на мусор).

    Контракт: значение вне `PlaceCategory` отклоняется `422`, а не тихо
    превращается в `other`. Причина: «вне enum» бывает двух видов —
    (а) `other` как ЯВНЫЙ выбор (легально) и (б) мусор от клиента (ошибка).
    Молчаливая подмена скрыла бы второе от разработчика.

    Фолбэк `other` живёт в ГЕОКОДЕРЕ (там OSM `class/type` не совпадает
    с нашим справочником) — см. `test_category_hint_outside_enum_becomes_other`
    в `tests/unit/test_address_geocoder.py`.
    """
    _, token = await _new_user(session)
    response = await _post(client, token, _payload(category_hint="bus_stop"))

    assert response.status_code == 422


async def test_other_category_is_accepted(client: AsyncClient, session: AsyncSession) -> None:
    """AC-9: пара к отказу — `other` явным значением принимается.

    Краснеет, если запретить `other` целиком: геокодер возвращает его на любой
    неизвестный тип OSM, и выбор такого места стал бы невозможен.
    """
    user, token = await _new_user(session)
    response = await _post(client, token, _payload(category_hint="other"))

    assert response.status_code == 200
    rows = await _rows(session, user.city_slug)
    assert rows[0]["category"] == "other"


async def test_city_slug_comes_from_profile_not_body(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-9, Task 3.4: город — из профиля; `city_slug` в теле запрещён `422`.

    Краснеет двумя правками сразу:
    1. если читать `city_slug` из тела — клиент создаст место в чужом городе,
       и оно будет невидимо в ленте, но займёт имя в `uq_places_city_name`;
    2. если поле пропустить (`extra="forbid"` снят) — клиент будет уверен,
       что создал место в выбранном городе.
    """
    _, token = await _new_user(session)
    response = await _post(client, token, _payload(city_slug="kemerovo"))

    assert response.status_code == 422
    # Форма ответа — конверт §4 `{"code": ...}`, не сырой Pydantic: глобальный
    # `validation_error_handler` (`main.py:265`) приводит ВСЕ ошибки валидации
    # к одному виду. Здесь именно `invalid_request`, а не `place_city_immutable`:
    # `_place_field_error` ограничен префиксом `/api/v1/places` (`main.py:336`),
    # и распространять админский код на маршрут автора значило бы учить клиента
    # второму смыслу на том же поле.
    assert response.json()["code"] == "invalid_request"
    assert response.headers["cache-control"] == "no-store"


async def test_place_is_created_in_users_city(client: AsyncClient, session: AsyncSession) -> None:
    """AC-9: место попадает в город АВТОРА (пара к запрету поля в теле)."""
    user, token = await _new_user(session)
    response = await _post(client, token, _payload())

    assert response.status_code == 200
    rows = await _rows(session, user.city_slug)
    assert len(rows) == 1
    assert rows[0]["city_slug"] == user.city_slug == settings.city_slugs[0]


async def test_place_goes_to_non_default_user_city(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9, Task 3.4: город берётся из профиля, а НЕ совпадает с дефолтом.

    **Зачем нужен второй город.** Предыдущий тест
    (`test_place_is_created_in_users_city`) сравнивает с
    `settings.city_slugs[0]`, то есть с дефолтом `tomsk`. Мутация
    `city_slug=user.city_slug` → `city_slug="tomsk"` (M18) выжила: у
    тестового пользователя город И ЕСТЬ `tomsk`, поэтому подмена неотличима
    от нормы. Здесь пользователь из другого города: подстановка литерала
    кладёт место в `tomsk`, и запрос в город автора возвращает пусто.
    """
    # `kemerovo` обязателен в `city_slugs`: иначе `create_or_get_place` честно
    # ответит `422 place_invalid` (слаг вне белого списка), и тест проверял бы
    # защиту от неизвестного города, а не источник `city_slug`.
    monkeypatch.setattr(settings, "city_slugs", ("tomsk", "kemerovo"))

    user, token = await _new_user(session, city_slug="kemerovo")
    assert user.city_slug == "kemerovo"

    response = await _post(client, token, _payload())
    assert response.status_code == 200

    # Место — в городе автора.
    own = await _rows(session, "kemerovo")
    assert len(own) == 1
    assert own[0]["city_slug"] == "kemerovo"
    # И его НЕТ в дефолтном городе: подстановка литерала ловится здесь.
    assert await _rows(session, "tomsk") == []


# --- M-3 CR 2026-10-31: лимит на создание места ---------------------------------


class _ExhaustedLimiter:
    """Лимитер, у которого окно исчерпано: `hit` возвращает остаток в секундах."""

    async def hit(self, user_id: int) -> int | None:
        return 42


async def test_create_place_without_limit_is_429(
    client: AsyncClient, session: AsyncSession
) -> None:
    """M-3 CR: исчерпанный лимит → `429` с `retry_after` и `Retry-After`.

    Краснеет, если лимитер не подключён к `POST /address/place` (именно так
    было до правки: AC-6 закрывал только `GET /search`). Без лимита один
    аккаунт создаёт неограниченное число мест в справочнике города, а
    модерации мест нет (`W-13.1-a`).

    **Почему подменяется `hit`, а не `limit`.** Проверяется ФАКТ вызова
    зависимости маршрутом, а не арифметика Redis: с настоящим лимитером тест
    зависел бы от `REDIS_URL` и от порядка соседних тестов.
    """
    from app.api.v1.address import get_address_place_rate_limiter

    app.dependency_overrides[get_address_place_rate_limiter] = lambda: _ExhaustedLimiter()
    try:
        _, token = await _new_user(session)
        response = await _post(client, token, _payload())
    finally:
        app.dependency_overrides.pop(get_address_place_rate_limiter, None)

    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert response.json()["retry_after"] == 42
    assert response.headers["Retry-After"] == "42"
    assert response.headers["cache-control"] == "no-store"
    # Место НЕ создано: лимит стоит до обращения к БД.
    user, _ = await _new_user(session)
    assert await _rows(session, user.city_slug) == []


# --- H-2 CR 2026-10-31: гонка в обёртке ------------------------------------------


async def test_archived_race_returns_422_without_uuid(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-2 CR: `PlaceArchivedError` из окна гонки → `422 PLACE_HIDDEN` БЕЗ UUID.

    Краснеет, если перехвата нет: исключение уходит наверх как `500`, а его
    текст содержит `existing_uuid` скрытого места — то есть перебором имён
    можно узнать, какие скрытые места существуют (D-6, решение 3).

    **Почему подменяется `create_or_get_place`, а не `create_place`.** Окно
    гонки живёт между первым поиском и вставкой ВНУТРИ обёртки; подмена
    внешней функции воспроизводит результат окна (`PlaceArchivedError` с
    uuid), не завися от таймингов и блокировок БД.
    """
    import uuid as uuid_lib

    import app.api.v1.address as address_module
    from app.services.place_service import PlaceArchivedError

    hidden_uuid = uuid_lib.uuid4()

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise PlaceArchivedError(hidden_uuid)

    monkeypatch.setattr(address_module, "create_or_get_place", _raise)

    _, token = await _new_user(session)
    response = await _post(client, token, _payload())

    assert response.status_code == 422
    assert response.json()["code"] == "PLACE_HIDDEN"
    # UUID скрытого места НЕ утёк — ни в теле, ни в `details`.
    assert "existing_uuid" not in response.text
    assert str(hidden_uuid) not in response.text


async def test_duplicate_race_returns_200_created_false(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-2 CR: `PlaceDuplicateError` из окна гонки → `200 {created:false}`.

    Краснеет, если перехвата нет: второй автор одного имени получает `500`
    вместо успеха. Для него это НЕ конфликт — он выбрал это место, и оно
    существует. `409 PLACE_DUPLICATE` — код администратора («придумайте
    другое имя»), автору карточки он не годится.
    """
    import app.api.v1.address as address_module
    from app.services.place_service import PlaceDuplicateError

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise PlaceDuplicateError

    _, token = await _new_user(session)
    payload = _payload(name="Кофейня гонки")
    # Настоящая гонка: место УЖЕ создано (другим автором), и повторное чтение
    # обязано его найти. Без строки ответ был бы `422 place_invalid`, и тест
    # проверял бы выдуманный UUID, а не исход гонки.
    first = await _post(client, token, payload)
    assert first.status_code == 200

    monkeypatch.setattr(address_module, "create_or_get_place", _raise)
    response = await _post(client, token, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is False
    assert body["place_uuid"]


async def test_duplicate_race_without_row_returns_422(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-2 CR: пара к предыдущему — строки нет, выдуманного UUID не будет.

    Краснеет, если на `PlaceDuplicateError` отдавать `200` с `place_uuid=None`
    или с чужим UUID: клиент сохранил бы карточку со ссылкой на место,
    которого нет, и получил бы `500` на создании карточки.
    """
    import app.api.v1.address as address_module
    from app.services.place_service import PlaceDuplicateError

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise PlaceDuplicateError

    monkeypatch.setattr(address_module, "create_or_get_place", _raise)

    _, token = await _new_user(session)
    response = await _post(client, token, _payload(name="Место без строки"))

    # H-2 CR 2026-10-31: не `422 place_invalid`, а `409 PLACE_CONFLICT`.
    # `422` означает ошибку ВВОДА (пользователь исправит поле), а здесь ввод
    # верен — изменилось состояние (параллельное действие). Клиент по `409`
    # повторяет запрос, по `422` — меняет данные. Один код на два смысла
    # завёл бы вторую ветку обработки.
    assert response.status_code == 409
    assert response.json()["code"] == "PLACE_CONFLICT"
