"""Запись анкеты: `POST`, `PATCH`, `locate-city` (Task 8.1–8.13; AC-01..AC-06).

Реальный PostgreSQL, схема через Alembic. Проверяется не только код ответа,
но и состояние БД: «анкета не создалась при пустом городе» — это AC-01,
и оно должно падать, если сервис всё-таки записал строку.

`test_profiles_api.py` из 2.1 остаётся на чтение: там проверяется, что
в ответе нет `id`/`user_id`, и эта проверка не должна раствориться в тестах
записи.
"""

import os
import time
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-02: ответ содержит ровно эти ключи. Равенство, а не подмножество:
# появление `id` или `user_id` делает тест красным (AC-04 стори 2.1).
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

VALID = {"name": "Аня", "city": "Томск", "age": 32, "bio": "люблю горы"}


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
    session: AsyncSession, *, consent: bool = True, gender: str = "female"
) -> tuple[User, str]:
    """Создаёт учётку с живой сессией; возвращает `(user, access_token)`.

    `consent=False` нужен тестам запрета: без согласия `ConsentUser` отдаёт
    `403 consent_required`, и это должно проверяться на живом запросе,
    а не на моке зависимости.
    """
    phone = f"+7903{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        gender=gender,
        status="active",
    )
    if consent:
        # CHECK `ck_users_consent_pair` требует пару «18+/согласие» одновременно.
        await session.execute(
            text("UPDATE users SET age_confirmed = true, consent_given_at = now() WHERE id = :id"),
            {"id": user.id},
        )
        await session.refresh(user)
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _profile_row(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    """Строка анкеты из БД как словарь — для проверки «что реально записалось».

    Читается сырым SQL, а не ORM: тесты не должны зависеть от identity map,
    иначе они подтверждали бы состояние объекта в памяти, а не в PostgreSQL.
    """
    result = await session.execute(
        text("SELECT name, city, age, bio, gender, status FROM profiles WHERE user_id = :id"),
        {"id": user_id},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _count_profiles(session: AsyncSession, user_id: int) -> int:
    """Сколько строк анкеты у пользователя — для проверки «второй не создалась»."""
    result = await session.execute(
        text("SELECT count(*) FROM profiles WHERE user_id = :id"), {"id": user_id}
    )
    return int(result.scalar_one())


# --- AC-02: создание ---------------------------------------------------------------


async def test_create_returns_201_with_full_profile(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: `201`, ровно `EXPECTED_KEYS`, `status='complete'`, город нормализован.

    `'  ТОМСК '` проверяет AC-04 на живом пути: без канонизации по `PILOT_CITY`
    в БД лежало бы `'ТОМСК'`, и выборка 2.6 по городу не нашла бы анкету.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles",
        json={**VALID, "city": "  ТОМСК "},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    assert body["status"] == "complete"
    assert body["city"] == "Томск"
    # AC-02: пол из `users.gender`, а не из тела запроса.
    assert body["gender"] == "female"

    row = await _profile_row(session, user.id)
    assert row is not None
    assert row["city"] == "Томск"
    assert row["status"] == "complete"


async def test_second_create_returns_409_and_keeps_data(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, D-1: повторный `POST` после `complete` — `409`, данные не перезаписаны.

    Проверяется и код, и строка в БД: без второй половины `409` мог бы
    возвращаться уже ПОСЛЕ перезаписи, и пользователь терял бы анкету,
    видя «конфликт».
    """
    user, token = await _new_user(session)
    first = await client.post("/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token})
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/profiles",
        json={**VALID, "name": "Другое", "bio": "другой текст"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert second.status_code == 409
    assert second.json()["code"] == "profile_exists"
    row = await _profile_row(session, user.id)
    assert row is not None
    assert row["name"] == "Аня"
    assert await _count_profiles(session, user.id) == 1


async def test_create_for_man_sets_is_hidden_true(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 2.3, Решение 3: анкета мужчины создаётся скрытой.

    В пилоте пол один — женский. Мужчина проходит тот же онбординг (запрещать
    создание нельзя: экран общий), но его анкета не должна попасть в выборку
    эпика 4, когда та начнёт читать `profiles` по городу и полу. Флаг ставится
    при создании, а не в выборке: иначе забытый фильтр в одном из будущих
    запросов показывал бы мужские анкеты.

    Тест краснеет, если убрать `is_hidden=(gender == Gender.MALE)`:
    в БД останется `false`, и `status` при этом остаётся `complete` —
    анкета заполнена, но скрыта.
    """
    user, token = await _new_user(session, gender="male")

    response = await client.post(
        "/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 201
    assert response.json()["is_hidden"] is True
    # Решение 3: статус не меняется — анкета заполнена, скрытие это не отменяет.
    assert response.json()["status"] == "complete"

    result = await session.execute(
        text("SELECT is_hidden, status FROM profiles WHERE user_id = :id"), {"id": user.id}
    )
    row = result.mappings().one()
    assert row["is_hidden"] is True
    assert row["status"] == "complete"


async def test_create_for_woman_keeps_is_hidden_false(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Решение 3, обратная сторона: женская анкета не скрывается.

    Без этой проверки `is_hidden=True` для всех прошёл бы зелёным, и выборка
    эпика 4 оказалась бы пустой при полностью рабочем коде создания.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 201
    assert response.json()["is_hidden"] is False

    result = await session.execute(
        text("SELECT is_hidden FROM profiles WHERE user_id = :id"), {"id": user.id}
    )
    assert result.scalar_one() is False


async def test_create_fills_existing_draft_with_200(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, D-1: `POST` по черновику из 2.2 — `200`, одна строка, `complete`.

    Черновик создаётся сырым SQL — ровно как это сделает загрузка фото в 2.2.
    Без этой ветки пользователь, загрузивший фото, не смог бы сохранить
    анкету вообще (тупик онбординга).
    """
    user, token = await _new_user(session)
    await session.execute(
        text("INSERT INTO profiles (user_id, gender, status) VALUES (:id, 'female', 'draft')"),
        {"id": user.id},
    )

    response = await client.post(
        "/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert await _count_profiles(session, user.id) == 1


# --- AC-01, AC-05: валидация до БД --------------------------------------------------


@pytest.mark.parametrize("city", ["", "   ", "\u00a0", "\t\n "])
async def test_blank_city_returns_422_and_creates_nothing(
    client: AsyncClient, session: AsyncSession, city: str
) -> None:
    """AC-01: пустой город — `422 city_required`, строки в БД нет.

    Проверяется и БД: код `422` без второй половины прошёл бы, если бы
    сервис записал строку, а потом вернул ошибку — то есть AC-01 («в БД
    не появляется строка со `status='complete'` и пустым городом») не выполнялся.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles", json={**VALID, "city": city}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "city_required"
    assert await _count_profiles(session, user.id) == 0


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("bio", "я" * 501, "bio_too_long"),
        ("name", "я" * 51, "name_too_long"),
        ("city", "я" * 101, "city_too_long"),
    ],
)
async def test_too_long_value_returns_domain_code(
    client: AsyncClient, session: AsyncSession, field: str, value: str, code: str
) -> None:
    """AC-04, AC-05: превышение длины — `422` с доменным кодом, не `500`.

    Двойной слой: Pydantic отбивает большую часть, но `city_too_long`
    и `name_too_long` — доменные коды стори. Ошибка `500` здесь означала бы,
    что запрос дошёл до `varchar`, то есть защита не работает.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles", json={**VALID, field: value}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    # CR 2.3, патч 5: точное равенство, а не `in {code, "invalid_request"}`.
    # Множество делало тест зелёным при полностью удалённой доменной проверке:
    # отказ приходил от Pydantic, и оба исхода считались успехом. После
    # Решения 1 (пределы только в сервисе) `invalid_request` здесь означает
    # регрессию — предел вернулся в схему.
    assert response.json()["code"] == code
    assert await _count_profiles(session, user.id) == 0


@pytest.mark.parametrize("age", [17, 100])
async def test_invalid_age_returns_422(
    client: AsyncClient, session: AsyncSession, age: int
) -> None:
    """AC-05: возраст вне 18..99 — `422 invalid_age`, анкета не создана.

    CR 2.3, патч 7: проверяется именно доменный код. Раньше тест подтверждал
    только статус `422`, и удаление `_check_age` из сервиса оставляло его
    зелёным: отказ приходил бы от CHECK в БД (`500`) или от Pydantic
    (`invalid_request`) — ни то, ни другое не является AC-05.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles", json={**VALID, "age": age}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_age"
    assert await _count_profiles(session, user.id) == 0


async def test_huge_body_is_rejected_before_database(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Фикс OOM (2026-10-22): тело на 5 МБ — `413 request_too_large`, не `422`.

    До фикса тело читалось целиком и нормализовалось синхронно: 5 МБ давали
    `422` за ~1 с (event loop занят), 10 МБ роняли контейнер по OOM.
    Теперь отказ приходит от гарда размера тела до чтения в память.

    Проверяется и БД: отказ обязан быть ДО какой-либо записи.
    """
    user, token = await _new_user(session)

    started = time.monotonic()
    response = await client.post(
        "/api/v1/profiles",
        json={**VALID, "bio": "я" * (5 * 1024 * 1024)},
        cookies={ACCESS_COOKIE_NAME: token},
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    # Метрика дефекта: раньше ~1 с (нормализация 5 млн символов). Порог
    # 0.5 с ловит возврат синхронной нормализации в async-обработчике.
    assert elapsed < 0.5, f"отказ занял {elapsed:.3f} с — тело читается/обрабатывается целиком"
    assert await _count_profiles(session, user.id) == 0


async def test_ten_megabyte_body_does_not_reach_application(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Фикс OOM (2026-10-22): 10 МБ — `413`, приложение не читает тело.

    Воспроизведение дефекта стенда: тело ~10 МБ роняло контейнер `api` по OOM
    (`Killed` в логах), после чего `/health` был недоступен. Тест проверяет
    не только код ответа, но и то, что запрос не дошёл до БД.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles",
        json={**VALID, "bio": "я" * (10 * 1024 * 1024)},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert await _count_profiles(session, user.id) == 0


async def test_body_just_below_limit_reaches_application(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Фикс OOM (2026-10-22): тело меньше лимита гард пропускает.

    Граница проверяется по коду ответа: тело ~400 КиБ (лимит 1 МиБ) обязано
    дойти до приложения и получить доменный отказ `422 bio_too_long`, а не
    `413`. Иначе гард с заниженным лимитом отклонял бы нормальные запросы,
    а дефект «анкета не создаётся» выглядел бы как успешный фикс.

    `201` здесь невозможен по построению: анкета — единицы килобайт, и любое
    тело у границы лимита содержит заведомо невалидное поле. Это и есть
    желаемое поведение: законная анкета отбивается доменным кодом, а не OOM.
    """
    user, token = await _new_user(session)
    # Лишний вес — в `bio`: после гарда его длина проверяется доменно.
    # 200 КиБ при пределе 500×20 = 10 000 символов — заведомый `bio_too_long`.
    padding = "я" * (200 * 1024)

    response = await client.post(
        "/api/v1/profiles",
        json={**VALID, "bio": padding},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    # Гард пропустил (не 413), доменная проверка отбила.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bio_too_long"
    assert await _count_profiles(session, user.id) == 0


async def test_valid_small_profile_still_created(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Фикс OOM (2026-10-22): гард не ломает нормальную анкету.

    Без этого теста «413 на всё» тоже считался бы успешным фиксом: дефект
    закрыт, а продукт не работает.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 201, response.text
    assert response.json()["city"] == "Томск"


async def test_gender_in_body_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, D-4: `gender` в теле — `422 invalid_request`, не `500` из FK.

    Без `extra="forbid"` значение `male` у женской учётки дошло бы до
    составного FK и вернуло `IntegrityError` → `500`.
    """
    user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles",
        json={**VALID, "gender": "male"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert await _count_profiles(session, user.id) == 0


# --- AC-03: правка ------------------------------------------------------------------


async def test_patch_changes_only_provided_field(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: `PATCH` меняет одно поле, остальные сохраняются.

    Сверяются все четыре значения в БД: иначе частичная правка могла бы
    обнулить соседнее поле, и тест остался бы зелёным, проверяя только `bio`.
    """
    user, token = await _new_user(session)
    await client.post("/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token})

    response = await client.patch(
        "/api/v1/profiles/me",
        json={"bio": "новый текст"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    row = await _profile_row(session, user.id)
    assert row is not None
    assert row["bio"] == "новый текст"
    assert row["name"] == "Аня"
    assert row["city"] == "Томск"
    assert row["age"] == 32
    assert row["status"] == "complete"


async def test_patch_without_profile_returns_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: `PATCH` без анкеты — `404 profile_not_found`, не `422`.

    Клиент без анкеты должен узнать «анкеты ещё нет», а не получить ошибку
    формы о пустом городе.
    """
    _, token = await _new_user(session)

    response = await client.patch(
        "/api/v1/profiles/me", json={"bio": "текст"}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "profile_not_found"


async def test_patch_does_not_touch_other_users_profile(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: правка не задевает чужую анкету (три пользователя).

    Один пользователь не доказывает изоляцию: замена `user.id` на константу
    оставила бы такой тест зелёным. Здесь две анкеты, и проверяется, что
    изменилась ровно моя.
    """
    me, my_token = await _new_user(session)
    other, other_token = await _new_user(session)
    await client.post("/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: my_token})
    await client.post(
        "/api/v1/profiles",
        json={**VALID, "name": "Мария", "bio": "чужой текст"},
        cookies={ACCESS_COOKIE_NAME: other_token},
    )

    response = await client.patch(
        "/api/v1/profiles/me",
        json={"bio": "мой новый текст"},
        cookies={ACCESS_COOKIE_NAME: my_token},
    )

    assert response.status_code == 200
    mine = await _profile_row(session, me.id)
    theirs = await _profile_row(session, other.id)
    assert mine is not None and theirs is not None
    assert mine["bio"] == "мой новый текст"
    assert theirs["bio"] == "чужой текст"


@pytest.mark.parametrize("field", ["name", "city", "bio", "age"])
async def test_patch_explicit_null_returns_field_required(
    client: AsyncClient, session: AsyncSession, field: str
) -> None:
    """AC-03, D-11: `null` в переданном поле — `422 field_required`, значение прежнее.

    D-3: стереть поле нельзя — иначе CHECK полноты дал бы `IntegrityError`
    → `500`. Проверяется и БД: отказ обязан быть ДО записи.
    """
    user, token = await _new_user(session)
    await client.post("/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token})

    response = await client.patch(
        "/api/v1/profiles/me", json={field: None}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "field_required"
    row = await _profile_row(session, user.id)
    assert row is not None
    assert row[field] is not None


async def test_patch_blank_city_returns_city_required(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, AC-03: `PATCH` с городом из пробелов — `422 city_required`.

    Правка — не обходной путь для AC-01: пустой город не должен попадать
    в БД ни при создании, ни при изменении.
    """
    user, token = await _new_user(session)
    await client.post("/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token})

    response = await client.patch(
        "/api/v1/profiles/me", json={"city": "   "}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "city_required"
    row = await _profile_row(session, user.id)
    assert row is not None
    assert row["city"] == "Томск"


# --- доступ -------------------------------------------------------------------------


async def test_create_without_consent_returns_403(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 2.1, Решение 2: без согласия на ПД запись анкеты запрещена (`403`).

    Проверяется на живом запросе, а не на моке зависимости: именно здесь
    `ConsentUser` должен остановить обработчик до сервиса (152-ФЗ).
    """
    user, token = await _new_user(session, consent=False)

    response = await client.post(
        "/api/v1/profiles", json=VALID, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "consent_required"
    assert await _count_profiles(session, user.id) == 0


async def test_patch_without_consent_returns_403(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 2.1, Решение 2: правка без согласия — `403 consent_required`."""
    _, token = await _new_user(session, consent=False)

    response = await client.patch(
        "/api/v1/profiles/me", json={"bio": "текст"}, cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "consent_required"


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_write_without_session_returns_401(client: AsyncClient, method: str) -> None:
    """AC-07: без сессии — `401`, а не `403` и не запись анонимной анкеты.

    `401` заставляет клиент обновить пару или уйти на `/login`; `403`
    показал бы «нет прав» вошедшему пользователю с истёкшей сессией.
    """
    url = "/api/v1/profiles" if method == "post" else "/api/v1/profiles/me"
    response = await getattr(client, method)(url, json=VALID)

    assert response.status_code == 401


# --- AC-06: город по координатам -----------------------------------------------------


async def test_locate_city_inside_pilot_returns_city(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: точка в Томске → `{"city": "Томск", "reason": null}`.

    Координаты приходят в теле (`POST`), а не в query: query попадает
    в журналы прокси, а хранение координат запрещено (FR59).
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/locate-city",
        json={"lat": 56.49, "lng": 84.97, "accuracy": 10},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json() == {"city": "Томск", "reason": None}


async def test_locate_city_outside_pilot_returns_reason(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: точка в Москве → `city=null` и `reason=outside_pilot_city`.

    `200` с `city=null`, а не `4xx`: гость из другого города — норма,
    клиент предложит ввести город вручную.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/locate-city",
        json={"lat": 55.75, "lng": 37.61, "accuracy": 10},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json() == {"city": None, "reason": "outside_pilot_city"}


@pytest.mark.parametrize(("lat", "lng"), [(91, 84.97), (56.49, 181), (-91, 84.97)])
async def test_locate_city_invalid_coordinates_returns_422(
    client: AsyncClient, session: AsyncSession, lat: float, lng: float
) -> None:
    """AC-06: координаты вне глобуса — `422 invalid_coordinates`.

    Без проверки диапазона `lat=91` вернула бы `outside_pilot_city` —
    «валидный» ответ на мусор.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/locate-city",
        json={"lat": lat, "lng": lng, "accuracy": 10},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    # AC-06: доменный код, не общий `invalid_request`. Клиент различает
    # «мусор в координатах» (повторить определение) и «сломанную форму».
    assert response.json()["code"] == "invalid_coordinates"


async def test_locate_city_does_not_store_coordinates(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06, FR59: после запроса в БД нет ни колонок, ни значений с координатами.

    Проверяется схема: колонки `lat`/`lng` в `profiles` не должно появиться
    ни в одной будущей миграции. Запрос в `information_schema` — единственный
    способ поймать это независимо от ORM-модели.
    """
    _, token = await _new_user(session)
    await client.post(
        "/api/v1/profiles/locate-city",
        json={"lat": 56.49, "lng": 84.97, "accuracy": 10},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    result = await session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'profiles'")
    )
    columns = {row[0] for row in result}
    assert "lat" not in columns
    assert "lng" not in columns
    assert "coordinates" not in columns


# --- стори 2.5: правка после одобрения (AC-01, AC-03) -----------------------------


async def test_patch_bio_keeps_profile_visible(client: AsyncClient, session: AsyncSession) -> None:
    """Task 6.1, 9.6 (AC-01): `PATCH bio` полной анкеты не сбрасывает видимость.

    Проверяется НАБЛЮДАЕМОЕ состояние строки: `status='complete'` и прежний
    `is_hidden` после правки. §18.1: если бы `update_profile` пересчитывал
    `status` по факту «все поля заполнены» с ошибкой или сбрасывал `is_hidden`,
    тест краснеет. `uuid` и `gender` не меняются — адрес и пол не являются
    редактируемыми полями (D-4 стори 2.1).
    """
    user, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post("/api/v1/profiles", json=VALID, cookies=cookies)
    assert created.status_code == 201
    before = created.json()

    # §18.8: тест живёт в ОДНОЙ внешней транзакции (SAVEPOINT), а
    # `onupdate=func.now()` — это `transaction_timestamp()`. Внутри одной
    # транзакции create и patch видят одно и то же время, поэтому «растёт»
    # неотличимо от «не обновляется». Сдвигаем значение в прошлое SQL-ом:
    # тогда рост доказывает именно UPDATE, а не совпадение времени.
    # §18.1: без `onupdate` (или при UPDATE в обход ORM) `updated_at`
    # остался бы часовой давности и тест краснеет.
    await session.execute(
        text("UPDATE profiles SET updated_at = now() - interval '1 hour' WHERE user_id = :id"),
        {"id": user.id},
    )
    await session.flush()
    # Значение ДО правки, прочитанное из БД как `datetime`. Сравнение идёт
    # типизированно, а не строками: `iso_utc` может опустить микросекунды,
    # и строковое сравнение дало бы ложный отказ на границе секунды.
    shifted_raw = await session.execute(
        text("SELECT updated_at FROM profiles WHERE user_id = :id"),
        {"id": user.id},
    )
    shifted_updated_at = shifted_raw.scalar_one()

    response = await client.patch(
        "/api/v1/profiles/me", json={"bio": "люблю горы и лыжи"}, cookies=cookies
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    assert body["bio"] == "люблю горы и лыжи"
    # AC-01: анкета остаётся видимой — статус и скрытие не изменились.
    assert body["status"] == "complete"
    assert body["is_hidden"] == before["is_hidden"]
    # Прочие поля не тронуты: частичная правка не переписывает анкету целиком.
    assert body["uuid"] == before["uuid"]
    assert body["gender"] == before["gender"]
    assert body["name"] == before["name"]
    assert body["city"] == before["city"]
    assert body["age"] == before["age"]
    # AC-01: `updated_at` вырос ОТНОСИТЕЛЬНО сдвинутого значения. Сравнение
    # с `before` здесь невозможно: `onupdate=func.now()` — это
    # `transaction_timestamp()`, а тест целиком живёт в одной транзакции,
    # поэтому create и patch получают одинаковую метку времени.
    updated_after = datetime.fromisoformat(body["updated_at"].replace("Z", "+00:00"))
    assert updated_after > shifted_updated_at

    # То же наблюдаемо в БД: правка не сбросила статус в `draft`.
    row = await _profile_row(session, user.id)
    assert row is not None
    assert row["status"] == "complete"
    assert row["bio"] == "люблю горы и лыжи"
