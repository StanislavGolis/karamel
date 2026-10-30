"""Запись справочника мест через API (Task 9; AC-01…AC-08).

Реальный PostgreSQL, схема поднята Alembic. Проверяется не только код ответа,
но и состояние БД: `204` на `DELETE` не доказывает, что строка жива, — поэтому
AC-04 проверяется `SELECT is_active` после запроса.

§18.1: каждый тест краснеет от снятия конкретной защиты. Разница между `user`
и `moderator` — ровно `role` в БД, и это единственное отличие фикстур, поэтому
`403`-тест падает при снятии проверки роли (§18.2).

§18.3: гонка двух вставок через API не воспроизводится в одной сессии;
ветка `IntegrityError` покрыта unit-тестом с реальной формой ошибки asyncpg,
здесь проверяется то, что видит админ: коды и состояние БД.
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
from app.models.enums import PlaceCategory, UserRole
from app.models.user import User
from app.repositories.places import PlaceRepository
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-02: ответ содержит ровно эти ключи. Равенство, а не подмножество: появление
# `id` (внутренний bigint) или координат делает тест красным (D-2 стори 3.1).
EXPECTED_KEYS = {"uuid", "name", "category", "address_text", "is_active"}


def _city() -> str:
    """Слаг сообщества из конфига пилота: тест не должен знать `.env` наизусть."""
    return settings.city_slugs[0]


def _payload(**overrides: Any) -> dict[str, Any]:
    """Минимальный валидный набор: тест меняет ровно одно поле."""
    base: dict[str, Any] = {
        "name": "Кафе „Прага“",
        "category": "cafe",
        "address_text": "пр. Ленина, 1",
        "city_slug": _city(),
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


async def _new_user(session: AsyncSession, *, role: str = UserRole.MODERATOR) -> tuple[User, str]:
    """Создаёт учётку с живой сессией; возвращает `(user, access_token)`.

    Анкеты и согласия на ПД у администратора нет намеренно (D-3): гард стоит
    на `CurrentUser`, и модератор без анкеты обязан вести справочник.
    """
    phone = f"+7904{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role=str(role),
        status="active",
    )
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place_row(session: AsyncSession, place_uuid: str) -> dict[str, Any] | None:
    """Строка места из БД сырым SQL: проверяется PostgreSQL, а не identity map."""
    result = await session.execute(
        text(
            "SELECT name, category, address_text, city_slug, is_active "
            "FROM places WHERE uuid = :uuid"
        ),
        {"uuid": place_uuid},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _insert_place(session: AsyncSession, **overrides: Any) -> str:
    """Кладёт место напрямую в БД и возвращает его `uuid`.

    Нужно для сценариев, где важно исходное состояние (`is_active=false`),
    а не путь создания: `POST` всегда создаёт активное место.
    """
    values = {
        "name": "Кино „Победа“",
        "category": "cinema",
        "address_text": "пр. Ленина, 2",
        "city_slug": _city(),
        "is_active": True,
        **overrides,
    }
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, :category, :address_text, :city_slug, :is_active) "
            "RETURNING uuid"
        ),
        values,
    )
    return str(row.scalar_one())


# --- AC-01: гард администратора ---------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("post", ""), ("patch", "/{uuid}"), ("delete", "/{uuid}")],
)
async def test_regular_user_forbidden(
    client: AsyncClient, session: AsyncSession, method: str, suffix: str
) -> None:
    """AC-01: `user` получает `403 {"code": "forbidden"}` без `details`.

    §18.2: единственное отличие от админа — `role` в БД, поэтому тест краснеет
    ровно при снятии проверки роли и ни при чём другом. Заголовок `no-store`
    проверяется здесь же: отказ не должен кэшироваться.
    """
    _, token = await _new_user(session, role=UserRole.USER)
    place_uuid = await _insert_place(session)
    path = "/api/v1/places" + suffix.format(uuid=place_uuid)

    # AC-01: тело есть только у `POST`/`PATCH`; `DELETE` его не принимает вовсе.
    body = {} if method == "delete" else {"json": _payload()}
    response = await getattr(client, method)(path, cookies={ACCESS_COOKIE_NAME: token}, **body)

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("post", ""), ("patch", "/{uuid}"), ("delete", "/{uuid}")],
)
async def test_no_session_unauthenticated(
    client: AsyncClient, session: AsyncSession, method: str, suffix: str
) -> None:
    """AC-01: без сессии — `401 unauthenticated`, а не `403`.

    Краснеет, если гард модератора начнёт отвечать `403` до проверки сессии:
    клиент показал бы «нет прав» вместо «войдите», и вход не предложился бы.
    """
    place_uuid = await _insert_place(session)
    path = "/api/v1/places" + suffix.format(uuid=place_uuid)

    body = {} if method == "delete" else {"json": _payload()}
    response = await getattr(client, method)(path, **body)

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


# --- AC-02: создание --------------------------------------------------------------


async def test_create_returns_201_without_internal_id(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: `201`, ровно `EXPECTED_KEYS`, место сразу активно.

    Краснеет, если в `PlaceRead` появится `id` или координаты (D-2, NFR34),
    либо новое место будет создано скрытым (админ не увидел бы его в выдаче).
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/places",
        json=_payload(name="  Кафе  „Прага“ "),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    assert body["name"] == "Кафе „Прага“"
    assert body["is_active"] is True
    assert response.headers["cache-control"] == "no-store"

    row = await _place_row(session, body["uuid"])
    assert row is not None
    assert row["is_active"] is True


async def test_create_duplicate_is_409(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03: активный дубль в сообществе — `409 PLACE_DUPLICATE`.

    Краснеет, если убрать дешёвую проверку дубля в сервисе: `uq` отбил бы
    вставку `IntegrityError`-ом и админ получил бы `500`.
    """
    _, token = await _new_user(session)
    first = await client.post(
        "/api/v1/places", json=_payload(), cookies={ACCESS_COOKIE_NAME: token}
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/places", json=_payload(), cookies={ACCESS_COOKIE_NAME: token}
    )

    assert second.status_code == 409
    assert second.json() == {"code": "PLACE_DUPLICATE"}


async def test_create_hidden_duplicate_is_archived(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, D-6: скрытый дубль — `409 PLACE_ARCHIVED` + `existing_uuid`.

    Краснеет, если скрытый дубль отдавать как `PLACE_DUPLICATE`: админ
    не узнал бы, какое место восстанавливать, и не смог бы выйти из тупика
    «имя занято местом, которого нет в выдаче».
    """
    _, token = await _new_user(session)
    hidden_uuid = await _insert_place(session, name="Кино „Победа“", is_active=False)

    response = await client.post(
        "/api/v1/places",
        json=_payload(name="Кино „Победа“", category="cinema"),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "PLACE_ARCHIVED"
    assert body["details"]["existing_uuid"] == hidden_uuid


# --- AC-05: правка и восстановление -----------------------------------------------


async def test_patch_changes_only_passed_fields(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: `PATCH` меняет переданные поля и не трогает остальные.

    Краснеет, если применять всю схему целиком: правка категории стёрла бы
    имя и адрес (и упала бы `NOT NULL`) — админ не смог бы отредактировать
    ни одно существующее место.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"category": "quiz"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "quiz"
    assert body["name"] == "Кино „Победа“"
    assert body["address_text"] == "пр. Ленина, 2"

    row = await _place_row(session, place_uuid)
    assert row is not None
    assert row["category"] == "quiz"
    assert row["name"] == "Кино „Победа“"


async def test_patch_restores_hidden_place(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05, D-7: `is_active=true` возвращает место в выдачу.

    Краснеет, если `is_active` не применяется (единственный маршрут
    восстановления, предложенный в `PLACE_ARCHIVED`): админ получил бы `200`
    без изменения состояния и повторял бы запрос бесконечно.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, is_active=False)

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"is_active": True},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    row = await _place_row(session, place_uuid)
    assert row is not None
    assert row["is_active"] is True


async def test_patch_rejects_city_slug(client: AsyncClient, session: AsyncSession) -> None:
    """AC-06, R-05: перенос между сообществами — `422`.

    Краснеет, если вернуть `city_slug` в `PlaceUpdate` или снять
    `extra="forbid"`: перенос прошёл бы молча (`200`), и место оказалось бы
    в чужой ленте без действия администратора.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"city_slug": "kemerovo"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    # QD A4 (R-05): доменный код вместо общего `invalid_request` — «перенос
    # между сообществами запрещён» больше не выглядит как сломанная форма.
    assert response.json() == {"code": "place_city_immutable"}
    row = await _place_row(session, place_uuid)
    assert row is not None
    assert row["city_slug"] == _city()


async def test_patch_unknown_uuid_is_404(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: неизвестный `uuid` — `404 PLACE_NOT_FOUND`, а не `500`.

    Краснеет, если сервис получит `None` вместо места: `AttributeError`
    в глубине слоёв отдал бы `500` на обычной опечатке в ссылке.
    """
    _, token = await _new_user(session)

    response = await client.patch(
        f"/api/v1/places/{uuid_lib.uuid4()}",
        json={"category": "quiz"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "PLACE_NOT_FOUND"}


async def test_patch_empty_body_is_explicit_error(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05, RISK-2: пустой `PATCH` — `422 place_nothing_to_update`.

    Краснеет, если отвечать `200`: клиент считал бы изменение применённым,
    хотя не сохранилось ничего, и повторял бы запрос без обратной связи.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.patch(
        f"/api/v1/places/{place_uuid}", json={}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json() == {"code": "place_nothing_to_update"}


async def test_patch_own_name_does_not_conflict(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: переименование «в себя» (нормализация) не даёт `409`.

    Краснеет, если убрать `exclude_uuid`: место нашлось бы по собственному
    имени, и правка адреса отбивалась бы конфликтом с самим собой.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, name="Кафе „Прага“")

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"name": "  Кафе  „Прага“ ", "address_text": "пр. Ленина, 5"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Кафе „Прага“"
    assert response.json()["address_text"] == "пр. Ленина, 5"


async def test_patch_foreign_name_is_duplicate(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05, AC-03: занятое имя при правке — `409 PLACE_DUPLICATE`.

    Краснеет, если не проверять дубль при правке: `uq` отбил бы `UPDATE`
    `IntegrityError`-ом, и админ получил бы `500` вместо понятного `409`.
    """
    _, token = await _new_user(session)
    await _insert_place(session, name="Кафе „Прага“")
    other_uuid = await _insert_place(session, name="Кино „Победа“", category="cinema")

    response = await client.patch(
        f"/api/v1/places/{other_uuid}",
        json={"name": "Кафе „Прага“"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    assert response.json() == {"code": "PLACE_DUPLICATE"}


# --- AC-04: мягкое скрытие --------------------------------------------------------


async def test_delete_keeps_row_and_is_idempotent(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04, R-04: `204`, строка жива с `is_active=false`, повтор тоже `204`.

    §18.1: краснеет, если заменить на физическое удаление (`SELECT` вернул бы
    пусто) или отвечать `404`/`409` на повтор — ретрай клиента после обрыва
    связи выглядел бы сбоем уже выполненной операции.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    first = await client.delete(f"/api/v1/places/{place_uuid}", cookies={ACCESS_COOKIE_NAME: token})
    second = await client.delete(
        f"/api/v1/places/{place_uuid}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert first.status_code == 204
    assert first.headers["cache-control"] == "no-store"
    assert second.status_code == 204

    row = await _place_row(session, place_uuid)
    assert row is not None, "строка удалена физически — FR68 нарушен"
    assert row["is_active"] is False


async def test_delete_unknown_uuid_is_404(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: неизвестный `uuid` — `404 PLACE_NOT_FOUND` с `no-store`.

    Краснеет, если отвечать `204` на любое значение: клиент не отличил бы
    опечатку в ссылке от успешного скрытия.
    """
    _, token = await _new_user(session)

    response = await client.delete(
        f"/api/v1/places/{uuid_lib.uuid4()}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 404
    assert response.json() == {"code": "PLACE_NOT_FOUND"}
    assert response.headers["cache-control"] == "no-store"


# --- AC-07: коды ошибок валидации --------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"name": "\u200b"}, "name_required"),
        ({"address_text": "\u200b"}, "address_text_required"),
        ({"name": "\ufb2c" * 41}, "name_too_long"),
        ({"address_text": "\ufb2c" * 86}, "address_text_too_long"),
        ({"city_slug": "tomsk1"}, "PLACE_UNKNOWN_COMMUNITY"),
    ],
)
async def test_validation_codes(
    client: AsyncClient, session: AsyncSession, overrides: dict[str, Any], expected: str
) -> None:
    """AC-07: доменные коды на невалидном вводе, `500` нет ни в одном случае.

    Значения подобраны так, чтобы пройти Pydantic и упереться в доменное
    правило: ZWSP имеет длину 1 (проходит `min_length`), `\ufb2c` × 41 — 41
    символ до NFC и 123 после (переполнение `varchar(120)` без нормализации
    дало бы `DBAPIError`, то есть `500`).
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/places", json=_payload(**overrides), cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json()["code"] == expected


async def test_category_invalid_carries_allowed_list(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07: `category_invalid` + полный перечень из `PlaceCategory`.

    Краснеет, если обработчик `RequestValidationError` потеряет ветку
    `category` (CR 3.1, патч 9): админ не смог бы показать выпадашку,
    а клиент не отличил бы категорию от прочего мусора в теле.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/places",
        json=_payload(category="bar"),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "category_invalid"
    assert body["details"]["allowed"] == [str(item) for item in PlaceCategory]


async def test_created_place_category_is_enum_value(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: категория отдаётся строкой-значением enum, а не именем члена.

    Краснеет, если сериализовать `PlaceCategory` как имя (`CAFE`): клиент
    с ветвлением по `"cafe"` не нашёл бы ветку отображения.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/places", json=_payload(), cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 201
    assert response.json()["category"] == "cafe"


# --- AC-08: типизированный путь ----------------------------------------------------


@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_numeric_uuid_path_is_422(
    client: AsyncClient, session: AsyncSession, method: str
) -> None:
    """AC-08, D-8: `/places/1` с сессией админа — `422`, не `404`/`500`.

    §18.1: краснеет, если ослабить тип параметра до `str`: перебор `/1..N`
    стал бы возможен, а `404` на число означал бы для клиента «место удалено».
    Без сессии тот же путь даёт `401` (зависимость разрешается раньше
    валидации пути) — это проверяет `test_routes_no_numeric_ids.py`.
    """
    _, token = await _new_user(session)

    response = await getattr(client, method)(
        "/api/v1/places/1", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


# --- CR 3.2: дефекты, найденные код-ревью (патчи 2, 4, 6, 9) ---------------------


async def test_patch_null_name_is_422_not_500(client: AsyncClient, session: AsyncSession) -> None:
    """CR 3.2, патч 2 (AC-05, AC-06, AC-07): явный `null` в `name` — `422`.

    §18.1: КРАСНЕЕТ от отката патча 2. До него `PATCH {"name": null}` давал
    `500 Internal Server Error` без конверта `{code}` и без `no-store`: поля
    `str | None` пропускали `null`, и `normalize_text(None)` падал `TypeError`.
    Заодно проверяется форма ответа: `name_required`, как у пустой строки.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, name="Кафе „Прага“")

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"name": None},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422, "до патча здесь был 500"
    assert response.json() == {"code": "name_required"}
    assert response.headers["cache-control"] == "no-store"
    row = await _place_row(session, place_uuid)
    assert row is not None and row["name"] == "Кафе „Прага“"


async def test_patch_null_address_text_is_422_not_500(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 3.2, патч 2: то же для `address_text` — `422 address_text_required`.

    §18.1: краснеет от отката патча 2 (`TypeError` → `500`).
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"address_text": None},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422, "до патча здесь был 500"
    assert response.json() == {"code": "address_text_required"}


async def test_patch_null_is_active_is_nothing_to_update(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 3.2, патч 2 (RISK-2): `{"is_active": null}` — не молчаливый `200`.

    §18.1: краснеет от отката патча 2. До него такой PATCH возвращал `200`
    с текущим состоянием, то есть «сохранено» без единой правки, и клиент
    считал изменение применённым.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"is_active": None},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "place_nothing_to_update"}


async def test_patch_hidden_duplicate_is_archived_on_api(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 3.2, патч 6 (AC-03): `PLACE_ARCHIVED` + `existing_uuid` и на `PATCH`.

    §18.1: краснеет от отката патча 6. До него обращение к `place.city_slug`
    в логе после отката SAVEPOINT давало `MissingGreenlet` → `500`, и админ не
    получал единственного действия — «восстановить вот это место».
    Unit-тест покрывал сервис, здесь проверяется HTTP-форма `details`.
    """
    _, token = await _new_user(session)
    hidden_uuid = await _insert_place(session, name="Кино „Победа“", is_active=False)
    active_uuid = await _insert_place(session, name="Кафе „Прага“")

    response = await client.patch(
        f"/api/v1/places/{active_uuid}",
        json={"name": "Кино „Победа“"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "PLACE_ARCHIVED",
        "details": {"existing_uuid": hidden_uuid},
    }


async def test_post_unknown_city_slug_shape_matches_service(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 3.2, патч 4 (AC-07): слаг не по шаблону и слаг вне конфига — один код.

    §18.1: краснеет от отката патча 4. До него `"Tomsk"` (Pydantic,
    `string_pattern_mismatch`) давал `invalid_request`, а `"zzzz"` (сервис) —
    `PLACE_UNKNOWN_COMMUNITY`: два кода на одно действие админа.
    """
    _, token = await _new_user(session)

    upper = await client.post(
        "/api/v1/places",
        json=_payload(name="Место 1", city_slug="Tomsk"),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    unknown = await client.post(
        "/api/v1/places",
        json=_payload(name="Место 2", city_slug="zzzz"),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert upper.status_code == unknown.status_code == 422
    assert upper.json() == unknown.json() == {"code": "PLACE_UNKNOWN_COMMUNITY"}


async def test_post_null_name_is_domain_code(client: AsyncClient, session: AsyncSession) -> None:
    """CR 3.2, патч 4 (AC-07): `null` в `name` на `POST` — `name_required`.

    §18.1: краснеет от отката патча 4 (ветка `string_type`). До него ответ был
    `invalid_request`, а сервис на том же вводе отвечает `name_required`.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/places",
        json=_payload(name=None),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "name_required"}


async def test_patch_updates_updated_at(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05, QD B4: `PATCH` реально сдвигает `updated_at` в БД.

    §18.1: КРАСНЕЕТ, если снять `onupdate=func.now()` у `TimestampMixin` —
    метка осталась бы прежней, и админка показывала бы «не менялось».

    Почему строка создаётся с `updated_at` в прошлом: тест идёт внутри ОДНОЙ
    транзакции (§18.8), а PostgreSQL `now()` — время НАЧАЛА транзакции. Если
    строку вставить «сейчас», `onupdate` даст ровно ту же метку до микросекунды,
    и разница не докажет ничего (именно так предыдущая версия теста была
    тавтологией: `after > before` не мог выполниться в принципе). Здесь строка
    кладётся с меткой `now() - interval '1 hour'`, поэтому `onupdate` обязан
    дать значение заметно больше — и `epoch`-сравнение работает.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, name="Кафе „Прага“")
    await session.execute(
        text("UPDATE places SET updated_at = now() - interval '1 hour' WHERE uuid = :uuid"),
        {"uuid": place_uuid},
    )
    before = (
        await session.execute(
            text("SELECT updated_at FROM places WHERE uuid = :uuid"), {"uuid": place_uuid}
        )
    ).scalar_one()

    response = await client.patch(
        f"/api/v1/places/{place_uuid}",
        json={"name": "Новое имя"},
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert response.status_code == 200

    after = (
        await session.execute(
            text("SELECT updated_at FROM places WHERE uuid = :uuid"), {"uuid": place_uuid}
        )
    ).scalar_one()
    assert after > before, (
        f"updated_at не сдвинулся: было {before}, стало {after} — onupdate миксина не сработал"
    )
    delta = (after - before).total_seconds()
    assert delta > 60, f"updated_at сдвинулся на {delta}s — это не onupdate, а старая метка"

    row = await _place_row(session, place_uuid)
    assert row is not None and row["name"] == "Новое имя"


async def test_banned_moderator_cannot_write(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: забаненный модератор не пишет в справочник (`403 user_blocked`).

    §18.1: краснеет, если `require_moderator` перестанет опираться на
    `get_current_user`: проверка роли прошла бы, и забаненный админ сохранил
    бы доступ к записи. Тест 3.2 покрывает именно новый маршрут (в 1.2/1.5
    бан проверялся на `/auth/me`).
    """
    user, token = await _new_user(session)
    user.status = "banned"
    await session.flush()

    response = await client.post(
        "/api/v1/places", json=_payload(), cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 403
    assert response.json() == {"code": "user_blocked"}


async def test_update_duplicate_race_keeps_session_alive(
    session: AsyncSession,
) -> None:
    """CR 3.2, патчи 3 и 6 (AC-03, AC-07): гонка в `PATCH` даёт `409`, не `500`.

    §18.1/§18.3: КРАСНЕЕТ от отката патча 3 и от отката патча 6, проверено
    на живом PostgreSQL 18.

    - без патча 3 присваивание полей шло ДО `begin_nested()`: `UPDATE` уходил
      вне SAVEPOINT, `IntegrityError` откатывал всю транзакцию, и вместо
      `PlaceDuplicateError` вылетал `PendingRollbackError` (`500`);
    - без патча 6 `except`-ветка читала `place.city_slug` у объекта,
      экспиренного откатом SAVEPOINT → `MissingGreenlet` (`500`).

    Дешёвая проверка дубля обходится намеренно: она не видит конкурента —
    ровно так выглядит гонка двух `PATCH`. Гарантию даёт `uq_places_city_name`.
    """
    from app.services.place_service import PlaceDuplicateError, update_place

    first_uuid = await _insert_place(session, name="Кафе „Прага“")
    await _insert_place(session, name="Кино „Победа“")
    place = await PlaceRepository(session).get_by_uuid(uuid_lib.UUID(first_uuid))
    assert place is not None

    async def _no_conflict(*args: Any, **kwargs: Any) -> None:
        """Дешёвый путь «конкурента нет» — как в настоящей гонке."""
        return None

    original = PlaceRepository.exists_in_city_excluding
    PlaceRepository.exists_in_city_excluding = _no_conflict  # type: ignore[method-assign]
    try:
        with pytest.raises(PlaceDuplicateError):
            await update_place(session, place=place, changes={"name": "Кино „Победа“"})
    finally:
        PlaceRepository.exists_in_city_excluding = original  # type: ignore[method-assign]

    # Сессия обязана остаться рабочей: иначе следующий запрос в той же
    # транзакции падал бы `PendingRollbackError`, и весь составной сценарий
    # («место + карточка») рушился бы из-за ожидаемого конфликта имени.
    alive = (await session.execute(text("SELECT count(*) FROM places"))).scalar_one()
    assert alive >= 2, "сессия непригодна после IntegrityError в update_place"
