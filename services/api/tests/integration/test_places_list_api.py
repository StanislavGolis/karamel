"""Выдача справочника мест (Task 8; AC-01…AC-08).

Реальный PostgreSQL, схема поднята Alembic. Проверяется не только код ответа,
но и состав выдачи: `200` не доказывает, что скрытое место не попало
в публичную ленту, поэтому AC-01/AC-03 сверяются по `uuid` из БД.

§18.1: каждый тест краснеет от снятия конкретной защиты — направление
указано в докстринге. §18.2: тест пагинации сверяет ОБЪЕДИНЕНИЕ страниц,
а не количество: иначе дубль на границе и потеря строки не ловятся.

§18.8: изоляция — общая `session` из `conftest` внутри транзакции-отката.
Без `DATABASE_URL` весь модуль — `skip` с причиной, а не «тихо зелёный».
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.enums import PlaceCategory, UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.schemas.place import PLACES_PAGE_SIZE
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-06: элемент выдачи содержит ровно эти ключи. Равенство, а не подмножество:
# появление `id` (внутренний bigint) или координат делает тест красным.
EXPECTED_ITEM_KEYS = {"uuid", "name", "category", "address_text", "is_active"}


def _community() -> str:
    """Слаг сообщества из конфига: тест не переписывает `.env` наизусть."""
    return settings.default_community_slug


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


async def _new_user(session: AsyncSession, *, role: UserRole = UserRole.USER) -> tuple[User, str]:
    """Создаёт учётку с живой сессией; возвращает `(user, access_token)`.

    Анкеты и согласия нет намеренно: выдача справочника стоит на `CurrentUser`,
    а не на `ConsentUser` — у участника может не быть анкеты, а место выбрать
    он обязан.
    """
    phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role=str(role),
        status="active",
    )
    issued = await issue_session(session, user)
    return user, issued.access_token


def _cookies(token: str) -> dict[str, str]:
    return {ACCESS_COOKIE_NAME: token}


async def _insert_place(
    session: AsyncSession,
    *,
    name: str,
    category: str = "cafe",
    community_slug: str | None = None,
    is_active: bool = True,
    minutes_offset: int = 0,
) -> uuid_lib.UUID:
    """Кладёт место в БД напрямую и возвращает его `uuid`.

    `created_at` задаётся явно: порядок и курсор проверяются по нему,
    а `now()` в одной транзакции даёт одинаковую метку для всех строк.
    """
    place_uuid = uuid_lib.uuid4()
    await session.execute(
        text(
            "INSERT INTO places "
            "(uuid, name, category, address_text, community_slug, is_active, "
            "created_at, updated_at) "
            "VALUES (:uuid, :name, :category, :address, :slug, :is_active, "
            "now() + make_interval(mins => :offset), now())"
        ),
        {
            "uuid": place_uuid,
            "name": name,
            "category": category,
            "address": "пр. Ленина, 1",
            "slug": community_slug or _community(),
            "is_active": is_active,
            "offset": minutes_offset,
        },
    )
    await session.flush()
    return place_uuid


# --- AC-01, AC-07: аноним и обычный участник -------------------------------------


async def test_list_without_session_is_401(client: AsyncClient) -> None:
    """AC-07: без сессии — `401 unauthenticated` в конверте §4.

    §18.1: краснеет, если снять зависимость `CurrentUser` с маршрута: справочник
    стал бы публичным, и лимиты выдачи обходились бы без учётки.
    """
    response = await client.get("/api/v1/places")

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_list_returns_only_active_places_of_own_community(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: только активные места своего сообщества, порядок по `created_at`.

    §18.1: краснеет от трёх правок: снять `is_active` из предиката (скрытое
    попало бы в публичную ленту), снять срез сообщества (место чужого слага
    появилось бы у участника) и нарушить порядок (первая страница отдавалась бы
    произвольно).
    """
    _, token = await _new_user(session)
    first = await _insert_place(session, name="Кафе „Прага“", minutes_offset=1)
    second = await _insert_place(session, name="Кино „Победа“", category="cinema", minutes_offset=2)
    hidden = await _insert_place(session, name="Скрытое", is_active=False, minutes_offset=3)
    foreign = await _insert_place(
        session, name="Чужое", community_slug="kemerovo", minutes_offset=4
    )

    response = await client.get("/api/v1/places", cookies=_cookies(token))

    assert response.status_code == 200
    body = response.json()
    uuids = [item["uuid"] for item in body["items"]]
    assert uuids == [str(first), str(second)]
    assert str(hidden) not in uuids
    assert str(foreign) not in uuids
    assert body["total"] == 2
    assert body["cursor"] is None
    assert set(body["items"][0]) == EXPECTED_ITEM_KEYS


# --- AC-02: фильтр категории -----------------------------------------------------


async def test_category_filter_and_total(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: `?category=cinema` сужает выдачу и `total` считается по фильтру.

    §18.1: краснеет, если фильтр применять только к странице, а `total`
    считать без него: клиент увидел бы «2 из 5» при одном элементе, и вывод
    «есть ещё страницы» был бы ложным.
    """
    _, token = await _new_user(session)
    cinema = await _insert_place(session, name="Кино 1", category="cinema", minutes_offset=1)
    await _insert_place(session, name="Кафе 1", category="cafe", minutes_offset=2)
    await _insert_place(session, name="Кино 2", category="cinema", minutes_offset=3)

    response = await client.get(
        "/api/v1/places", params={"category": "cinema"}, cookies=_cookies(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["uuid"] for item in body["items"]][0] == str(cinema)
    assert {item["category"] for item in body["items"]} == {"cinema"}


async def test_unknown_category_is_422_with_allowed_list(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, AC-07: категория вне белого списка — `category_invalid` + `allowed`.

    §18.1: краснеет, если отдавать `422` без перечня: клиент не смог бы
    построить ветку отображения и показал бы общий текст ошибки.
    """
    _, token = await _new_user(session)

    response = await client.get(
        "/api/v1/places", params={"category": "zoo"}, cookies=_cookies(token)
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "category_invalid"
    assert body["details"]["allowed"] == [str(item) for item in PlaceCategory]


# --- AC-03, R-06: include_inactive ------------------------------------------------


async def test_include_inactive_forbidden_for_regular_user(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, R-06: `include_inactive=true` под `user` — ровно `{"code": "forbidden"}`.

    §18.1: краснеет, если убрать `require_moderator` из зависимости: обычный
    участник увидел бы скрытые места, а `total` стал бы админским счётчиком.
    Тело проверяется целиком: `details` быть не должно (ADR-13).
    """
    _, token = await _new_user(session)

    response = await client.get(
        "/api/v1/places", params={"include_inactive": "true"}, cookies=_cookies(token)
    )

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden"}


async def test_moderator_sees_hidden_places(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, AC-05: модератор видит скрытое — «скрыть» ≠ «удалить» (FR68).

    §18.1: краснеет, если флаг теряется по пути: админ не увидел бы скрытое
    место и не смог бы восстановить его через `PATCH is_active=true`.
    Строка при этом жива — проверяется `uuid` из БД, а не только код ответа.
    """
    _, token = await _new_user(session, role=UserRole.MODERATOR)
    visible = await _insert_place(session, name="Видимое", minutes_offset=1)
    hidden = await _insert_place(session, name="Скрытое", is_active=False, minutes_offset=2)

    response = await client.get(
        "/api/v1/places", params={"include_inactive": "true"}, cookies=_cookies(token)
    )

    assert response.status_code == 200
    uuids = {item["uuid"] for item in response.json()["items"]}
    assert uuids == {str(visible), str(hidden)}
    hidden_item = next(item for item in response.json()["items"] if item["uuid"] == str(hidden))
    assert hidden_item["is_active"] is False
    assert hidden_item["name"] == "Скрытое"
    assert hidden_item["address_text"]


async def test_moderator_without_flag_gets_public_view(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: `include_inactive=false` под модератором = выдача участника.

    §18.1: краснеет, если по роли подставлять `True`: нельзя было бы
    посмотреть глазами, что видит участник.
    """
    _, token = await _new_user(session, role=UserRole.MODERATOR)
    visible = await _insert_place(session, name="Видимое", minutes_offset=1)
    await _insert_place(session, name="Скрытое", is_active=False, minutes_offset=2)

    response = await client.get("/api/v1/places", cookies=_cookies(token))

    assert response.status_code == 200
    assert [item["uuid"] for item in response.json()["items"]] == [str(visible)]
    assert response.json()["total"] == 1


# --- AC-04: курсорная пагинация ---------------------------------------------------


async def test_cursor_pagination_covers_everything_once(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: все страницы вместе дают ровно полный набор, без дублей.

    §18.2: сверяется ОБЪЕДИНЕНИЕ `uuid` и отсутствие пересечений, а не
    количество: `>=` на границе дал бы дубль при том же количестве, а сдвинутая
    граница — потерю строки.

    §18.1: краснеет от четырёх правок: `>=` (дубль), отсутствие `ORDER BY uuid`
    (нестабильный порядок при равном `created_at`), отсутствие `+1` в `LIMIT`
    (`cursor` всегда `null`), курсор от выброшенной строки (потеря).
    """
    _, token = await _new_user(session)
    created: set[str] = set()
    # `PLACES_PAGE_SIZE + 2` — две страницы, вторая заполнена частично.
    for index in range(PLACES_PAGE_SIZE + 2):
        place_uuid = await _insert_place(session, name=f"Место {index}", minutes_offset=index)
        created.add(str(place_uuid))

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str] = {}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/v1/places", params=params, cookies=_cookies(token))
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["uuid"] for item in body["items"])
        pages += 1
        cursor = body["cursor"]
        if cursor is None:
            break
        assert pages < 10, "курсор не сходится — защита от бесконечного цикла"

    assert pages == 2
    assert len(seen) == len(set(seen)), "страницы пересеклись"
    assert set(seen) == created


def test_cursor_pagination_does_not_skip_row_on_insert_between_pages() -> None:
    """AC-04: ключ курсора монотонен — арифметика кодека, БЕЗ БД.

    CR 3.3, патч 6: прежний докстринг утверждал, что тест «краснеет, если
    заменить row-value на `OFFSET`». Это было НЕВЕРНО: тест не открывает
    ни БД, ни `build_list_statement`, поэтому `OFFSET` в нём не покраснел бы.
    Тавтологичное доказательство хуже отсутствующего — оно создаёт иллюзию
    покрытия (урок CR 2.2 с `FOR UPDATE`).

    Здесь проверяется ровно одно: кодек сохраняет `(created_at, uuid)` и
    монотонность ключа (новое место с большей меткой встаёт в продолжение).
    Поведенческое доказательство «вставка между страницами не сдвигает
    выданное» — интеграционный тест
    `test_insert_between_pages_does_not_shift_issued_rows` (добавлен CR 3.3).

    §18.1: краснеет, если кодек теряет микросекунды или `uuid`.
    """
    from datetime import UTC, datetime, timedelta

    from app.services.place_cursor import decode_place_cursor, encode_place_cursor

    base = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    second_uuid = uuid_lib.UUID("0f3c1e6a-2b7d-4c8e-9a10-5b6d7e8f9012")
    cursor = encode_place_cursor(base + timedelta(minutes=1), second_uuid)

    moment, place_uuid = decode_place_cursor(cursor)

    # Новое место с более поздним `created_at` попадает в продолжение, а не
    # перед границей: выданное остаётся выданным.
    assert moment == base + timedelta(minutes=1)
    assert place_uuid == second_uuid
    assert (base + timedelta(minutes=2)) > moment


# --- AC-04, AC-07: битый курсор ---------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        "zzz",
        "bm90LWEtanNvbg",  # valid base64url, not JSON
        "eyJjcmVhdGVkX2F0IjoiMjAyNi0xMC0yM1QxMToqMCJ9",  # без ключа `uuid`
    ],
)
async def test_broken_cursor_is_422_not_500(
    client: AsyncClient, session: AsyncSession, cursor: str
) -> None:
    """AC-04, AC-07: битый курсор — `422 invalid_cursor`, не `500`.

    §18.1: краснеет, если пробросить ошибку кодека наверх (`500`) или молча
    вернуть первую страницу (пустой `body["code"]`).
    """
    _, token = await _new_user(session)

    response = await client.get(
        "/api/v1/places", params={"cursor": cursor}, cookies=_cookies(token)
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_cursor"}


async def test_cursor_from_other_filter_does_not_crash(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: курсор от другого фильтра даёт `200`, а не `500`.

    OQ-2: курсор — позиция, а не запрос; смена `category` между страницами
    даёт новую выборку со старой точкой. Это ответственность клиента (эпик 4),
    а сервер обязан ответить конвертом, а не падением.

    §18.1: краснеет, если курсор валидировать против фильтра и падать
    `500`/`AssertionError` при несовпадении.
    """
    _, token = await _new_user(session)
    first = await _insert_place(session, name="Кафе 1", category="cafe", minutes_offset=1)
    await _insert_place(session, name="Кино 1", category="cinema", minutes_offset=2)

    page = await client.get("/api/v1/places", cookies=_cookies(token))
    cursor = page.json()["cursor"]
    # `cursor` может быть `None` (одна страница) — тогда берём курсор от
    # единственного элемента вручную, чтобы проверка была не пустой.
    if cursor is None:
        from datetime import UTC, datetime

        from app.services.place_cursor import encode_place_cursor

        cursor = encode_place_cursor(datetime(2026, 1, 1, tzinfo=UTC), first)

    response = await client.get(
        "/api/v1/places", params={"category": "cinema", "cursor": cursor}, cookies=_cookies(token)
    )

    assert response.status_code == 200
    assert "items" in response.json()


# --- AC-07, QD A6: заголовок и конверт -------------------------------------------


async def test_no_store_header_on_success(client: AsyncClient, session: AsyncSession) -> None:
    """AC-07, QD A6: `cache-control: no-store` на `200`.

    Один ассерт: сам middleware покрыт отдельными тестами, а ручная установка
    в роутере запрещена — иначе появился бы второй, независимо поддерживаемый
    путь заголовка.
    """
    _, token = await _new_user(session)

    response = await client.get("/api/v1/places", cookies=_cookies(token))

    assert response.headers["cache-control"] == "no-store"


# --- AC-08: поверхность API -------------------------------------------------------


def test_openapi_has_get_places_without_item_route() -> None:
    """AC-08, Task 9: `GET /api/v1/places` есть, `GET /places/{uuid}` — нет.

    §18.1: краснеет, если маршрут выдачи не зарегистрирован (клиент не увидит
    его в сгенерированном TS-типе) или если появился `/places/{uuid}` —
    он отсутствует в контракте §7.1 и сломал бы поверхность API.
    """
    paths = app.openapi()["paths"]

    assert "/api/v1/places" in paths
    assert "get" in paths["/api/v1/places"]
    assert "/api/v1/places/{place_uuid}" in paths
    assert "get" not in paths["/api/v1/places/{place_uuid}"]


def test_openapi_get_places_exposes_category_and_cursor_only() -> None:
    """AC-04, D-7: у `GET` есть `category`/`cursor`, но нет `limit`.

    §18.1: краснеет, если объявить `limit` параметром: `limit=0` дал бы
    молча пустую выдачу, `limit=-1` — `500` (долг DS 2.6 о `Query(ge/le)`).

    `include_inactive` объявлен в зависимости и в схеме параметров тоже
    появляется — поэтому проверяется его наличие, а не отсутствие.
    """
    params = app.openapi()["paths"]["/api/v1/places"]["get"]["parameters"]
    names = {param["name"] for param in params}

    assert {"category", "cursor"} <= names
    assert "limit" not in names


async def test_response_has_no_internal_id_in_items(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: внутренний `id` не появляется ни в одном элементе выдачи.

    §18.1: краснеет, если вернуть ОРМ-объект вместо `PlaceRead` (или добавить
    `id` в схему): `bigint` стал бы публичным адресом и разрешил перебор.
    """
    _, token = await _new_user(session)
    await _insert_place(session, name="Кафе „Прага“", minutes_offset=1)

    response = await client.get("/api/v1/places", cookies=_cookies(token))

    body = response.json()
    assert body["items"]
    for item in body["items"]:
        assert set(item) == EXPECTED_ITEM_KEYS
        assert "id" not in item
        assert not {"lat", "lng", "coords", "geo"} & set(item)


# --- CR 3.3: пробелы доказательной базы, найденные ревью ---------------------


async def test_equal_created_at_is_ordered_by_uuid_across_pages(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (CR 3.3): при РАВНОМ `created_at` обход не теряет и не дублирует.

    §18.1: краснеет, если убрать `Place.uuid.asc()` из `ORDER BY`. `uuid` —
    тай-брейкер ровно для случая «две строки одной транзакции получили
    одинаковый `now()`»; ключ курсора `(created_at, uuid)` без порядка
    по `uuid` перестаёт совпадать с порядком выдачи, и граница страницы
    режет набор не по тому признаку — строки теряются или дублируются.

    До этого теста равный `created_at` проверялся ТОЛЬКО текстом SQL
    (`test_order_by_contains_created_at_and_uuid`), то есть поведенчески
    ловушка спеки (стр. 162) не была закрыта: все вставки шли с разными
    `minutes_offset` и снятие тай-брейкера не краснело.

    Все строки вставляются с `minutes_offset=0`: `now()` в одной транзакции
    даёт одну метку, поэтому `created_at` совпадает до микросекунды.
    """
    _, token = await _new_user(session)
    created: set[str] = set()
    for index in range(PLACES_PAGE_SIZE + 2):
        place_uuid = await _insert_place(session, name=f"Одно время {index}", minutes_offset=0)
        created.add(str(place_uuid))

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str] = {}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/v1/places", params=params, cookies=_cookies(token))
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["uuid"] for item in body["items"])
        pages += 1
        cursor = body["cursor"]
        if cursor is None:
            break
        assert pages < 10, "курсор не сходится — защита от бесконечного цикла"

    assert len(seen) == len(set(seen)), "страницы пересеклись при равном created_at"
    assert set(seen) == created, "часть строк потеряна при равном created_at"


async def test_category_filter_survives_cursor_pagination(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: `cursor` продолжает выборку С фильтром категории.

    §18.1: краснеет, если накладывать курсор ВМЕСТО предиката категории
    (а не поверх): вторая страница вернула бы места других категорий.

    До этого теста `category` проверялся без курсора, а пагинация — без
    категории; AC-02 («пагинация сохраняется») не была покрыта ни одним
    тестом, хотя реализация её поддерживает.
    """
    _, token = await _new_user(session)
    cinema: set[str] = set()
    for index in range(PLACES_PAGE_SIZE + 2):
        place_uuid = await _insert_place(
            session, name=f"Кино {index}", category="cinema", minutes_offset=index
        )
        cinema.add(str(place_uuid))
    # Места другой категории с поздними метками: без фильтра они попали бы
    # во вторую страницу.
    for index in range(3):
        await _insert_place(
            session,
            name=f"Кафе {index}",
            category="cafe",
            minutes_offset=PLACES_PAGE_SIZE + index,
        )

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str] = {"category": "cinema"}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/v1/places", params=params, cookies=_cookies(token))
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == len(cinema), "total потерял фильтр категории на странице с курсором"
        seen.extend(item["uuid"] for item in body["items"])
        pages += 1
        cursor = body["cursor"]
        if cursor is None:
            break
        assert pages < 10, "курсор не сходится — защита от бесконечного цикла"

    assert len(seen) == len(set(seen))
    assert set(seen) == cinema, "курсор отменил фильтр категории"


async def test_moderator_total_counts_hidden_places(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, RISK-2 (CR 3.3): `total` модератора считается СО скрытыми.

    §18.1: краснеет, если `total` считать без того же `include_inactive`,
    что и страницу: админ увидел бы «1 из 1» при двух строках в выдаче
    и не смог бы отличить «скрытых нет» от «счётчик врёт».

    До этого теста `total` с флагом проверялся только текстом SQL
    (`test_count_statement_honours_include_inactive`); значение — ни разу.
    """
    _, token = await _new_user(session, role=UserRole.MODERATOR)
    await _insert_place(session, name="Активное", is_active=True, minutes_offset=1)
    await _insert_place(session, name="Скрытое", is_active=False, minutes_offset=2)

    response = await client.get(
        "/api/v1/places", params={"include_inactive": "true"}, cookies=_cookies(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["is_active"] for item in body["items"]} == {True, False}


async def test_insert_between_pages_does_not_shift_issued_rows(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (CR 3.3): вставка между страницами не сдвигает выданное.

    §18.1: краснеет, если заменить row-value курсор на `OFFSET`: вставка
    новой строки ПЕРЕД границей сдвинула бы весь набор, и вторая страница
    повторила бы уже выданную строку (классический дефект offset-пагинации).

    До этого теста сценарий проверялся арифметикой курсора без БД:
    `build_list_statement` в нём не участвовал, поэтому `OFFSET` не краснел.
    """
    _, token = await _new_user(session)
    # Страница + 3 строки: первая страница заполнена, вторая непустая.
    # Ровно `PLACES_PAGE_SIZE` строк дало бы `cursor=None` (лишней строки нет),
    # и сценарий «вставка между страницами» стал бы непроверяемым.
    for index in range(PLACES_PAGE_SIZE + 3):
        await _insert_place(session, name=f"Первая {index}", minutes_offset=index + 10)

    first = await client.get("/api/v1/places", cookies=_cookies(token))
    assert first.status_code == 200
    first_body = first.json()
    cursor = first_body["cursor"]
    assert cursor is not None, "нужна незаполненная вторая страница"
    issued = {item["uuid"] for item in first_body["items"]}

    # Новая строка с меткой, которая МЕНЬШЕ границы: при `OFFSET` она сдвинула
    # бы набор и вторая страница повторила бы уже выданные строки.
    intruder = await _insert_place(session, name="Вставка", minutes_offset=0)

    second = await client.get("/api/v1/places", params={"cursor": cursor}, cookies=_cookies(token))
    assert second.status_code == 200
    second_body = second.json()
    second_uuids = {item["uuid"] for item in second_body["items"]}

    assert not (issued & second_uuids), "вторая страница повторила уже выданные строки"
    # Вставка с меткой меньше границы курсором НЕ видна — контракт «позиция,
    # а не номер»: клиент не теряет выданное, а новое увидит при перезагрузке.
    assert str(intruder) not in second_uuids
