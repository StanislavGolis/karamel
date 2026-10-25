"""`GET /api/v1/profiles/me` (Task 8.1–8.6; AC-04, AC-05).

Реальный PostgreSQL, схема через Alembic. Проверяется не только код ответа,
но и точный набор ключей: «в ответе нет id» — это AC-04, и оно должно
падать, если поле появится в схеме (тест 8.2).
"""

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.profiles import ProfileRepository
from app.repositories.users import UserRepository
from app.schemas.profile import ProfileOwn, ProfilePublic
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-04: ответ содержит ровно эти ключи. Равенство, а не подмножество:
# появление `id` или `user_id` делает тест красным.
# CR 2.1, Решение 3 + патч 14: `is_hidden` и `status` — только у владельца.
EXPECTED_KEYS = {
    "uuid",
    "name",
    "age",
    "city",
    "bio",
    "gender",
    "is_hidden",
    "status",
    "created_at",
    "updated_at",
}


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


async def _new_user(
    session: AsyncSession, *, status: str = "active", consent: bool = True
) -> tuple[User, str]:
    """Создаёт учётку с живой сессией; возвращает `(user, access_token)`.

    Возвращается ORM-объект, а не только `uuid`: тесту нужен `user.id` для
    вставки анкеты. После выдачи сессии объект не истёк (`expire_on_commit=False`
    в фикстуре), поэтому чтение `id` в синхронном контексте безопасно.

    CR 2.1, Решение 2: по умолчанию учётка проходит онбординг (`age_confirmed`
    + `consent_given_at`), иначе `/profiles/me` отвечает `403 consent_required`.
    `consent=False` нужен тесту, который проверяет сам этот запрет.

    CR 2.1, Решение 1: пол обязателен — составной FK ссылается на
    `users(id, gender)`, и учётка без пола не примет анкету.
    """
    phone = f"+7902{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        gender="female",
        status=status,
    )
    if consent:
        # `UserRepository.create` (стори 1.2) не принимает consent-поля: он
        # появился раньше 1.6. Ставим их UPDATE-ом — CHECK `ck_users_consent_pair`
        # требует пару «age_confirmed + consent_given_at» одновременно.
        await session.execute(
            text("UPDATE users SET age_confirmed = true, consent_given_at = now() WHERE id = :id"),
            {"id": user.id},
        )
        await session.refresh(user)
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _insert_profile(session: AsyncSession, user_id: int, **values: Any) -> uuid_lib.UUID:
    """Вставляет анкету сырым SQL: создание анкеты — стори 2.3 (D-10)."""
    params: dict[str, Any] = {"user_id": user_id, "gender": values.pop("gender", "female")}
    columns = ["user_id", "gender"]
    for column, value in values.items():
        columns.append(column)
        params[column] = value
    placeholders = ", ".join(f":{column}" for column in columns)
    result = await session.execute(
        text(f"INSERT INTO profiles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING uuid"),
        params,
    )
    return result.scalar_one()


# --- AC-04: успешное чтение ---


async def test_returns_profile_without_internal_ids(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: 200, набор ключей ровно `EXPECTED_KEYS`, без `id` и `user_id`.

    CR 2.1, патч 3: в базе три учётки — моя с анкетой, чужая с анкетой и
    третья без анкеты. Прежняя версия использовала одного пользователя,
    и замена `get_by_user_id(user.id)` на `select(Profile).limit(1)` оставляла
    тест зелёным: он не доказывал, что вернулась именно МОЯ анкета.
    """
    user, token = await _new_user(session)
    my_uuid = await _insert_profile(
        session, user.id, name="Анна", city="Томск", age=25, bio="Люблю горы"
    )
    other, _ = await _new_user(session)
    foreign_uuid = await _insert_profile(
        session, other.id, name="Мария", city="Томск", age=30, bio="Люблю кофе"
    )
    await _new_user(session)  # третья учётка без анкеты

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    assert "id" not in body
    assert "user_id" not in body
    # AC-04: вернулась МОЯ анкета, а не первая попавшаяся в таблице.
    assert body["uuid"] == str(my_uuid)
    assert body["uuid"] != str(foreign_uuid)
    assert body["name"] == "Анна"
    assert body["city"] == "Томск"
    assert body["gender"] == "female"


async def test_user_without_profile_gets_404_while_others_have_profiles(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05, CR 2.1, патч 3: у третьего пользователя анкеты нет — `404`.

    Анкеты других пользователей в таблице есть. Если бы роут возвращал
    «любую анкету», третий получил бы `200` с чужим `uuid`.
    """
    first, _ = await _new_user(session)
    await _insert_profile(session, first.id, name="Анна", city="Томск", age=25, bio="Горы")
    _lonely, token = await _new_user(session)

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 404
    assert response.json() == {"code": "profile_not_found"}


async def test_response_is_not_cacheable(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: успешный ответ тоже `cache-control: no-store`.

    Ответ привязан к сессии: без запрета кэша прокси или браузер мог бы
    отдать чужой профиль.
    """
    user, token = await _new_user(session)
    await _insert_profile(session, user.id, name="Анна", city="Томск", age=25, bio="Горы")

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.headers["cache-control"] == "no-store"


async def test_dates_are_iso_utc_with_z(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04, project-context §4: даты — ISO 8601 UTC с `Z`.

    Проверяется наблюдаемое значение, а не факт наличия поля: `iso_utc`
    единственный источник формата, и расхождение сломало бы клиент.
    """
    user, token = await _new_user(session)
    await _insert_profile(session, user.id, name="Анна", city="Томск", age=25, bio="Горы")

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})
    body = response.json()

    assert body["created_at"].endswith("Z")
    assert body["updated_at"].endswith("Z")


# --- CR 2.1, патч 12: NULL-поля сериализуются, а не падают ---


async def test_draft_with_null_fields_is_returned(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 2.1, патч 12: черновик (`name/city/age/bio = NULL`) читается как `null`.

    D-8 разрешает пустые поля, но сериализация `str | None` через API не
    проверялась: `model_validate` упал бы на `None` только после того, как
    первый пользователь откроет кабинет до заполнения анкеты.
    """
    user, token = await _new_user(session)
    await _insert_profile(session, user.id)

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] is None
    assert body["city"] is None
    assert body["age"] is None
    assert body["bio"] is None
    # Черновик: флаг полноты `draft`, анкета не скрыта по умолчанию.
    assert body["status"] == "draft"
    assert body["is_hidden"] is False


# --- CR 2.1, патч 13: скрытая анкета видна владельцу ---


async def test_hidden_profile_is_visible_to_owner(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 2.1, патч 13: `is_hidden=true` — владелец получает `200` и флаг.

    Скрытие убирает анкету из чужой выборки (эпик 4), но не из своего
    кабинета: иначе клиент не смог бы её снова показать. Если следующий
    разработчик добавит фильтр `is_hidden = false` в репозиторий, тест
    покраснеет — сейчас такой регрессии ничто не мешало.
    """
    user, token = await _new_user(session)
    await _insert_profile(
        session, user.id, name="Анна", city="Томск", age=25, bio="Горы", is_hidden=True
    )

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    assert response.json()["is_hidden"] is True


# --- AC-05: учётка без анкеты ---


async def test_missing_profile_returns_404(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: учётка без анкеты — `404 profile_not_found`, не `500`.

    Тело — закрытый конверт `{\"code\": ...}` как в `auth.py`; клиент разбирает
    одно поле. `cache-control: no-store` проверяется здесь же: 404 привязан
    к сессии так же, как 200.
    """
    _user, token = await _new_user(session)

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 404
    assert response.json() == {"code": "profile_not_found"}
    assert response.headers["cache-control"] == "no-store"


async def test_no_session_returns_401(client: AsyncClient) -> None:
    """AC-05: без cookie — `401 unauthenticated` (поведение `get_current_user`).

    Страховка от рассинхронизации: если зависимость авторизации когда-нибудь
    перестанет требовать токен, этот тест покраснеет раньше, чем откроется
    доступ к анкетам.
    """
    response = await client.get("/api/v1/profiles/me")

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_banned_user_returns_403(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: забаненный получает `403 user_blocked`.

    Проверка бана не дублируется в роутере анкеты: она живёт в зависимости
    (`deps.py`, стори 1.5). Тест фиксирует, что бан действует и на этом пути.
    """
    _user, token = await _new_user(session, status="banned")

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 403
    assert response.json() == {"code": "user_blocked"}


# --- CR 2.1, Решение 2: consent-гейт ---


async def test_user_without_consent_gets_403(client: AsyncClient, session: AsyncSession) -> None:
    """CR 2.1, Решение 2 (FR9, 152-ФЗ): без согласия — `403 consent_required`.

    Анкета в базе есть, сессия валидна: единственная причина отказа —
    отсутствие 18+ и согласия на обработку ПД. До решения правило жило
    только в Next-гарде, и прямой вызов API отдавал имя, город, возраст
    и био.
    """
    user, token = await _new_user(session, consent=False)
    await _insert_profile(session, user.id, name="Анна", city="Томск", age=25, bio="Горы")

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 403
    assert response.json() == {"code": "consent_required"}
    # Тело ответа не должно содержать данных анкеты.
    assert "Томск" not in response.text


async def test_user_with_consent_gets_200(client: AsyncClient, session: AsyncSession) -> None:
    """Позитивный контроль к предыдущему тесту: с согласием — `200`.

    Без него `403` можно было бы получить из-за любой другой ошибки
    зависимости, и запрет «доказывался» бы поломкой.
    """
    user, token = await _new_user(session)
    await _insert_profile(session, user.id, name="Анна", city="Томск", age=25, bio="Горы")

    response = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200


# --- CR 2.1, Решение 3: схема чужой карточки без служебных флагов ---


def test_public_schema_has_no_hidden_or_status() -> None:
    """CR 2.1, Решение 3: `ProfilePublic` не раскрывает `is_hidden` и `status`.

    Схема эпика 4 переиспользует `ProfilePublic`; если флаг скрытия попадёт
    в неё (или в общего предка), каждая чужая карточка начнёт сообщать,
    что анкета скрыта или не заполнена.
    """
    assert "is_hidden" not in ProfilePublic.model_fields
    assert "status" not in ProfilePublic.model_fields
    # CR 2.3, патч 6: обратная сторона проверяется по ФАКТИЧЕСКОМУ ответу, а не
    # по константе `EXPECTED_KEYS` из этого же файла. Прежняя строка сравнивала
    # константу с константой: удаление поля из `ProfileOwn` оставляло её зелёной.
    assert "is_hidden" in ProfileOwn.model_fields


# --- CR 2.1, патч 9: публичного POST нет (AC-03) ---


async def test_profiles_surface_has_no_path_params_or_delete(client: AsyncClient) -> None:
    """AC-03 стори 2.1 (в редакции 2.3 и 2.2): адресации анкеты по id нет.

    Исходная посылка теста — «метода `POST` на анкетах нет» — отменена стори
    2.3: создание анкеты теперь `POST /api/v1/profiles` (AC-02). Тест не
    удалён, а переписан: защита, ради которой он существовал, — запрет
    адресации анкеты по внутреннему `id` — остаётся в силе.

    Стори 2.2 (Task 7.4, AC-09) добавляет подресурс фото:
    `DELETE /api/v1/profiles/me/photos/{photo_uuid}`. Параметр пути здесь —
    `uuid` ФОТО, а не анкеты, и он обязателен: удаление адресуется публичным
    идентификатором объекта, а не позицией. Поэтому проверка уточнена, а не
    ослаблена: путь с `{` разрешён РОВНО один — фото-подресурс; `DELETE`
    разрешён только на нём; `/api/v1/profiles/{uuid}` и удаление самой анкеты
    по-прежнему запрещены.

    Проверяется `app.openapi()`, а не код ответа: `POST /api/v1/profiles/1`
    дал бы `422` просто потому, что `1` не число в пути, а не потому, что
    маршрута нет. Схема перечисляет все операции, поэтому появление
    `/profiles/{uuid}` (эпик 4) делает тест красным.
    """
    paths = app.openapi()["paths"]
    profile_paths = {
        path: ops for path, ops in paths.items() if path.startswith("/api/v1/profiles")
    }

    # AC-02 стори 2.3: создание анкеты есть, и оно без параметров пути.
    assert "post" in profile_paths["/api/v1/profiles"]
    # Стори 2.2, Task 7.4: единственный допустимый параметр пути — uuid фото.
    photos_path = "/api/v1/profiles/me/photos/{photo_uuid}"
    assert photos_path in profile_paths
    for path in profile_paths:
        if "{" in path:
            assert path == photos_path, f"адресация анкеты параметром пути запрещена: {path}"

    # Запрет адресации по внутреннему id: перебор /1..N выгружал бы город
    # в обход выборки из 20 и модерации (D-2 стори 2.1).
    assert not any(
        path.startswith("/api/v1/profiles/{") or path == "/api/v1/profiles/{uuid}"
        for path in profile_paths
    )

    # Удаление разрешено только для фото (AC-09). Удаление анкеты появится
    # вместе с удалением данных в эпике 8, а не раньше.
    delete_paths = {path for path, ops in profile_paths.items() if "delete" in ops}
    assert delete_paths == {photos_path}


# --- CR 2.1, патч 4: `get_by_uuid` покрыт тестом ---


async def test_get_by_uuid_finds_and_misses(session: AsyncSession) -> None:
    """AC-04, CR 2.1, патч 4: `get_by_uuid` находит анкету и не находит чужой uuid.

    Метод уезжает в эпик 4 как единственный способ адресовать анкету снаружи.
    Без теста опечатка в колонке (`Profile.user_id` вместо `Profile.uuid`)
    прошла бы незамеченной: `scalar_one_or_none` вернул бы `None`, а это
    выглядит как «анкеты нет».
    """
    user, _ = await _new_user(session)
    profile_uuid = await _insert_profile(
        session, user.id, name="Анна", city="Томск", age=25, bio="Горы"
    )
    repository = ProfileRepository(session)

    found = await repository.get_by_uuid(profile_uuid)
    missing = await repository.get_by_uuid(uuid_lib.uuid4())

    assert found is not None
    assert found.uuid == profile_uuid
    assert found.city == "Томск"
    assert missing is None
