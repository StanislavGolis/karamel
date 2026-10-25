"""API подтверждения 18+ и согласия на ПД (Task 5; AC-01, AC-03, AC-04, AC-05, AC-07).

Проверяется не только код ответа, но и состояние БД после запроса: `422`
с изменёнными полями был бы хуже отсутствия эндпоинта — пользователь видит
ошибку, а согласие уже записано.

Патчи 7, 8, 9, 20 код-ревью 1.6 закрыли дыры в самих проверках: успех
проверялся по JSON, собранному в памяти до коммита; ветка «без 18+» не
смотрела в БД; блокировка пола тестировалась на состоянии, которое эндпоинт
породить не может (`gender=female` без согласия).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
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
    session: AsyncSession,
    *,
    gender: str | None = None,
    status: str = "active",
) -> tuple[uuid_lib.UUID, str]:
    """Создаёт учётку с живой сессией; возвращает `(uuid, access_token)`.

    Возвращается именно `uuid`, а не ORM-объект: после `session.expire_all()`
    чтение атрибута истёкшего объекта в синхронном контексте пытается сходить
    в БД и падает с `MissingGreenlet`. Значение фиксируется заранее.

    Патч 20 код-ревью 1.6: параметры перечислены явно, `**kwargs` с
    `type: ignore` удалён. Опечатка в имени поля (`gendr=`) давала `TypeError`
    в момент вызова, но проверка типов его не видела — `**kwargs: object`
    принимал что угодно. Теперь mypy ловит лишний или переименованный аргумент.
    """
    phone = f"+7901{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        gender=gender,
        status=status,
    )
    user_uuid = user.uuid
    issued = await issue_session(session, user)
    await session.commit()
    return user_uuid, issued.access_token


async def _reload(session: AsyncSession, user_uuid: uuid_lib.UUID) -> User:
    """Патч 7: читает состояние из БД, а не из объекта, собранного в памяти."""
    session.expire_all()
    stored = await UserRepository(session).get_by_uuid(user_uuid)
    assert stored is not None
    return stored


async def test_consent_requires_session(client: AsyncClient) -> None:
    """AC-03 (через D-1): без cookie — `401 unauthenticated`, не `500`."""
    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


async def test_consent_without_pd_flag_returns_422_and_writes_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: `consent=false` → `422 consent_required`, поля в БД не изменились."""
    user_uuid, token = await _new_user(session)

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": False, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "consent_required"

    stored = await _reload(session, user_uuid)
    assert stored.age_confirmed is False
    assert stored.consent_given_at is None
    assert stored.gender is None


async def test_consent_without_age_flag_returns_422_and_writes_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: не отмечено 18+ → `422 consent_required`, в БД ничего не записано.

    Патч 8 код-ревью 1.6: прежняя версия проверяла только код ответа. Именно
    здесь частичная запись была бы худшим исходом: `age_confirmed=true` без
    согласия на обработку ПД — прямое нарушение 152-ФЗ, при этом пользователь
    видит ошибку и считает, что ничего не произошло.
    """
    user_uuid, token = await _new_user(session)

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": False, "consent": True, "gender": "male"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "consent_required"

    stored = await _reload(session, user_uuid)
    assert stored.age_confirmed is False
    assert stored.consent_given_at is None
    assert stored.gender is None


async def test_consent_without_gender_returns_422(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: пол не выбран → `422 gender_required` (не `400`)."""
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": None},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "gender_required"


@pytest.mark.parametrize("bad_gender", [123, 1.5, ["female"], {"value": "female"}, True])
async def test_non_string_gender_returns_gender_required(
    client: AsyncClient, session: AsyncSession, bad_gender: object
) -> None:
    """Патч 19 код-ревью 1.6: нестроковый `gender` — доменный код, не `invalid_request`.

    До патча `gender: 123` падал на типизации Pydantic, и общий обработчик
    валидации отдавал `422 invalid_request` — тот же код, что и на сломанный
    JSON. Клиент не мог отличить «пол не выбран» от «запрос битый», и форма
    показывала неверный текст.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": bad_gender},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "gender_required"


async def test_female_consent_succeeds_and_is_idempotent(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, AC-04: женщина → 200; повтор → 200 и то же время согласия.

    Патч 7 код-ревью 1.6: время согласия проверяется в БД, а не только
    в JSON-ответе. Ответ сериализуется из объекта до коммита: ошибка в
    маппинге или откат транзакции дали бы `200` с временем в теле и `NULL`
    в таблице — то есть отсутствие факта согласия при формально успешном UX.
    """
    user_uuid, token = await _new_user(session)

    first = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert first.status_code == 200
    payload = first.json()
    assert payload["age_confirmed"] is True
    assert payload["gender"] == "female"
    assert payload["status"] == "active"
    assert payload["is_new"] is True
    assert payload["consent_given_at"].endswith("Z")
    # AR21/AR22: внутренний id и телефон наружу не уходят.
    assert "id" not in payload
    assert "phone" not in payload
    assert "provider_user_id" not in payload

    stored = await _reload(session, user_uuid)
    assert stored.consent_given_at is not None
    assert stored.age_confirmed is True
    assert stored.gender == "female"
    first_time = stored.consent_given_at

    second = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert second.status_code == 200
    assert second.json()["consent_given_at"] == payload["consent_given_at"]
    assert second.json()["is_new"] is False

    # Патч 6: повтор не сдвигает метку — сравнение с исходным значением из БД.
    stored_again = await _reload(session, user_uuid)
    assert stored_again.consent_given_at == first_time


async def test_male_consent_leaves_status_active_and_no_application(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: мужчина получает `active`, но заявка не создаётся."""
    user_uuid, token = await _new_user(session)

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "male"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json()["gender"] == "male"
    assert response.json()["status"] == "active"

    rows = await session.execute(select(User).where(User.uuid == user_uuid))
    assert rows.scalar_one().status == "active"


async def test_gender_change_returns_409_along_real_path(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04/D-5: согласие дано с `female`, повтор с `male` → `409 gender_locked`.

    Патч 9 код-ревью 1.6: прежний тест создавал учётку сразу с `gender=female`
    и `age_confirmed=false` — состояние, которое эндпоинт породить не может
    (пол пишется только вместе с согласием). Проверялся код, но не реальный
    путь пользователя. Теперь смена пола проверяется после настоящего
    `/consent`: только так видно, что блокировка работает при заполненном
    `consent_given_at`.
    """
    user_uuid, token = await _new_user(session)

    first = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert first.status_code == 200
    consent_time = first.json()["consent_given_at"]

    second = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "male"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert second.status_code == 409
    assert second.json()["code"] == "gender_locked"

    stored = await _reload(session, user_uuid)
    assert stored.gender == "female"
    assert stored.consent_given_at is not None
    # Время первого согласия не изменилось и после отказа.
    assert stored.consent_given_at.isoformat().replace("+00:00", "Z") == consent_time


async def test_gender_locked_sees_concurrent_write(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Патч 2 код-ревью 1.6: решение принимается по строке под блокировкой.

    Сценарий гонки воспроизводится без двух параллельных соединений: пол
    записывается «мимо ORM» (`UPDATE` через `text`), поэтому объект в identity
    map остаётся со `gender=None` — ровно как у второго из двух параллельных
    запросов, прочитавших строку до коммита первого.

    Без `populate_existing=True` (и без `FOR UPDATE`) сервис видел бы `None`,
    записал `male` и вернул `200` — то есть запрет смены пола обходился бы
    двумя одновременными вкладками. С патчем второй запрос получает `409`.
    """
    user_uuid, token = await _new_user(session)

    # «Первый запрос» уже зафиксировал пол в БД, но ORM о нём не знает.
    await session.execute(
        text(
            "UPDATE users SET gender = 'female', age_confirmed = true, "
            "consent_given_at = now() WHERE uuid = :uuid"
        ),
        {"uuid": user_uuid},
    )
    await session.commit()

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "male"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "gender_locked"

    stored = await _reload(session, user_uuid)
    assert stored.gender == "female"


async def test_consent_does_not_overwrite_committed_timestamp(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Патч 3 код-ревью 1.6: `is_new` считается по заблокированной строке.

    Согласие уже зафиксировано (в обход ORM, как это сделал бы параллельный
    запрос), объект в сессии об этом не знает. Без перечитывания под
    блокировкой сервис решил бы, что согласие первое, и перезаписал
    `consent_given_at` временем второго нажатия — аудиторский факт по 152-ФЗ
    подменялся бы временем последнего клика.
    """
    user_uuid, token = await _new_user(session)

    await session.execute(
        text(
            "UPDATE users SET gender = 'female', age_confirmed = true, "
            "consent_given_at = TIMESTAMPTZ '2026-01-01 00:00:00+00' WHERE uuid = :uuid"
        ),
        {"uuid": user_uuid},
    )
    await session.commit()

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json()["is_new"] is False
    assert response.json()["consent_given_at"] == "2026-01-01T00:00:00Z"

    stored = await _reload(session, user_uuid)
    assert stored.consent_given_at is not None
    assert stored.consent_given_at.year == 2026
    assert stored.consent_given_at.month == 1


async def test_me_exposes_consent_fields(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05, AC-06: `/auth/me` отдаёт поля согласия — по ним работает гарда.

    Решение 3 код-ревью 1.6: `awaiting_applications` — серверный признак
    «мужчина ждёт открытия заявок». Проверяется, что он приходит из API,
    а не вычисляется на клиенте по `gender`.
    """
    _, token = await _new_user(session)

    before = await client.get("/api/v1/auth/me", cookies={ACCESS_COOKIE_NAME: token})
    assert before.status_code == 200
    assert before.json()["age_confirmed"] is False
    assert before.json()["consent_given_at"] is None
    assert before.json()["gender"] is None
    assert before.json()["awaiting_applications"] is False

    await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "male"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    after = await client.get("/api/v1/auth/me", cookies={ACCESS_COOKIE_NAME: token})
    assert after.status_code == 200
    assert after.json()["age_confirmed"] is True
    assert after.json()["gender"] == "male"
    assert after.json()["consent_given_at"] is not None
    # Решение 3: мужчина ждёт заявок — говорит сервер.
    assert after.json()["awaiting_applications"] is True
    assert after.headers["cache-control"] == "no-store"


async def test_awaiting_applications_false_for_female(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Решение 3: женщина в выборке сразу — флаг ожидания не выставляется (FR1)."""
    _, token = await _new_user(session)

    await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    after = await client.get("/api/v1/auth/me", cookies={ACCESS_COOKIE_NAME: token})
    assert after.status_code == 200
    assert after.json()["awaiting_applications"] is False


async def test_banned_user_gets_403(client: AsyncClient, session: AsyncSession) -> None:
    """AC-07-регрессия 1.5: бан отдаёт `403 user_blocked` от зависимости."""
    user_uuid, token = await _new_user(session)
    banned = await UserRepository(session).get_by_uuid(user_uuid)
    assert banned is not None
    banned.status = "banned"
    await session.commit()

    response = await client.post(
        "/api/v1/auth/consent",
        json={"age_confirmed": True, "consent": True, "gender": "female"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "user_blocked"


async def test_consent_pair_check_rejects_broken_state(session: AsyncSession) -> None:
    """Решение 1: CHECK в БД не даёт паре «18+ / согласие» разойтись.

    Проверяется именно уровень БД, а не сервис: сервис уже проверен тестами
    выше. Ограничение нужно потому, что мимо сервиса идут админ-скрипты,
    будущий отзыв согласия и ручные правки — и любая из них могла оставить
    учётку с подтверждённым возрастом без согласия на обработку ПД.
    """
    user_uuid, _ = await _new_user(session)

    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE users SET age_confirmed = true WHERE uuid = :uuid"),
            {"uuid": user_uuid},
        )
        await session.flush()

    await session.rollback()


async def test_consent_pair_check_rejects_timestamp_without_age(session: AsyncSession) -> None:
    """Решение 1: обратное расхождение (дата есть, возраст не подтверждён) тоже запрещено."""
    user_uuid, _ = await _new_user(session)

    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE users SET consent_given_at = now() WHERE uuid = :uuid"),
            {"uuid": user_uuid},
        )
        await session.flush()

    await session.rollback()


def test_openapi_documents_consent_contracts() -> None:
    """Патч 10: `/auth/consent` и `/auth/me` описаны в OpenAPI.

    До патча оба эндпоинта отдавали `JSONResponse` без `response_model`,
    поэтому в схему не попадали: `ConsentResponse` была недостижима для
    генерации TS-типов, а `/auth/me` не описывался вовсе — фронт держал
    `SessionUser` руками и расхождение ловилось только в рантайме.
    """
    schema = app.openapi()
    components = schema["components"]["schemas"]

    assert "ConsentResponse" in components
    assert "SessionUserResponse" in components
    assert "awaiting_applications" in components["SessionUserResponse"]["properties"]
    assert "session_expired" not in components["SessionUserResponse"]["properties"]

    consent_post = schema["paths"]["/api/v1/auth/consent"]["post"]
    assert consent_post["responses"]["200"]["content"]["application/json"]["schema"]
