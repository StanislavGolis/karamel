"""Видимость анкет по городу пилота (Task 4.1–4.2, 5.1–5.4; AC-01, AC-02, AC-03, AC-05).

Реальный PostgreSQL, схема через Alembic. Проверяется не только набор `uuid`,
но и то, что регистрация в чужом городе не ломается (AC-02: FR16 ограничивает
показ, а не регистрацию).

§18.1: каждое условие предиката обязано красить свой тест — доказательство
откатом зафиксировано в Completion Notes стори.
"""

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.profiles import ProfileRepository
from app.repositories.users import UserRepository
from app.services.profile_visibility import list_visible_profiles
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

PILOT = "Томск"
OTHER = "Москва"


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


async def _new_user(session: AsyncSession, *, gender: str = "female") -> tuple[User, str]:
    """Создаёт учётку с согласием и живой сессией; возвращает `(user, token)`."""
    phone = f"+7904{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        gender=gender,
        status="active",
    )
    await session.execute(
        text("UPDATE users SET age_confirmed = true, consent_given_at = now() WHERE id = :id"),
        {"id": user.id},
    )
    await session.refresh(user)
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _profile(
    session: AsyncSession,
    *,
    city: str,
    status: str = "complete",
    is_hidden: bool = False,
    gender: str = "female",
    text_fields: bool = True,
) -> str:
    """Создаёт анкету напрямую через репозиторий; возвращает `uuid` строкой.

    CR 2.6, патч 6 (AC-05, Task 5.1): `text_fields=False` создаёт **настоящий**
    черновик — заполнен только город, `name`/`age`/`bio` пусты. Прошлая версия
    всегда писала текст и «доказывала» не тот кейс: обоснование фильтра
    `status='complete'` звучит как «CHECK `completeness` разрешает `draft`
    с заполненным городом и пустыми полями, такая строка показала бы пустую
    карточку», а проверялся `draft` с полным текстом.
    """
    user, _ = await _new_user(session, gender=gender)
    profile = await ProfileRepository(session).create(
        user_id=user.id,
        gender=gender,
        city=city,
        status=status,
        is_hidden=is_hidden,
        **(
            {
                "name": "Аня" if gender == "female" else "Иван",
                "age": 30,
                "bio": "текст",
            }
            if text_fields
            else {}
        ),
    )
    return str(profile.uuid)


# --- Task 5.1: правило видимости -----------------------------------------------


async def test_only_complete_visible_pilot_city_profile_is_listed(
    session: AsyncSession,
) -> None:
    """AC-01, AC-05: из четырёх анкет видна ровно одна.

    Откат любого из трёх условий предиката делает этот тест красным:
    без `city` — видна и «Москва», без `status` — виден черновик,
    без `is_hidden` — видна скрытая.
    """
    visible = await _profile(session, city=PILOT)
    await _profile(session, city=OTHER)
    # CR 2.6, патч 6: настоящий черновик — город есть, текста нет.
    await _profile(session, city=PILOT, status="draft", text_fields=False)
    await _profile(session, city=PILOT, is_hidden=True)

    found = [
        str(profile.uuid)
        for profile in await list_visible_profiles(session, pilot_city=PILOT, limit=50)
    ]

    assert found == [visible]


# --- Task 4.1: смена города пилота без правки кода -------------------------------


async def test_pilot_city_change_switches_the_visible_set(session: AsyncSession) -> None:
    """AC-03: другой аргумент — другой набор при неизменных строках.

    Город приходит параметром, поэтому ни `monkeypatch.setenv`, ни очистка
    кэша `get_settings` здесь не нужны: если бы сервис читал настройки сам,
    тест требовал бы правки кода, а не значения.
    """
    tomsk = await _profile(session, city=PILOT)
    moscow = await _profile(session, city=OTHER)

    in_tomsk = [
        str(profile.uuid)
        for profile in await list_visible_profiles(session, pilot_city=PILOT, limit=50)
    ]
    in_moscow = [
        str(profile.uuid)
        for profile in await list_visible_profiles(session, pilot_city=OTHER, limit=50)
    ]

    assert in_tomsk == [tomsk]
    assert in_moscow == [moscow]
    assert tomsk != moscow


# --- CR 2.6, патч 3: ORDER BY и LIMIT производственного пути ---------------------


async def test_limit_and_order_are_applied_by_the_repository(session: AsyncSession) -> None:
    """AC-01 (§18.2): `LIMIT` усекает, `ORDER BY` стабилизирует набор.

    CR 2.6, патч 3: раньше ни один тест не вызывал `list_visible_in_city`
    дважды с разным `limit`, поэтому удаление `.limit(limit)` или
    `.order_by(Profile.uuid)` оставляло набор зелёным.
    """
    created = [await _profile(session, city=PILOT) for _ in range(3)]

    limited = await list_visible_profiles(session, pilot_city=PILOT, limit=2)
    everything = await list_visible_profiles(session, pilot_city=PILOT, limit=50)

    limited_uuids = [str(profile.uuid) for profile in limited]
    all_uuids = [str(profile.uuid) for profile in everything]

    assert len(limited_uuids) == 2, "limit не применён"
    assert all_uuids == sorted(all_uuids), "ORDER BY по uuid не применён"
    assert limited_uuids == all_uuids[:2], "limit обязан отдавать префикс упорядоченного набора"
    assert set(created) == set(all_uuids)


# --- Task 5.2: AC-02 ------------------------------------------------------------


async def test_create_in_other_city_succeeds_and_stays_invisible(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: регистрация в чужом городе успешна и не попадает в выдачу.

    FR16 ограничивает показ, а не регистрацию: доменного отказа по городу
    быть не должно ни в одном слое. Владелец видит свою анкету через `/me`,
    а выборка города пилота её не содержит.
    """
    _, token = await _new_user(session)

    created = await client.post(
        "/api/v1/profiles",
        json={"name": "Аня", "city": OTHER, "age": 32, "bio": "люблю горы"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert created.status_code == 201
    assert "code" not in created.json()
    uuid_value = created.json()["uuid"]

    own = await client.get("/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token})
    assert own.status_code == 200
    assert own.json()["uuid"] == uuid_value
    assert own.json()["city"] == OTHER

    found = [
        str(profile.uuid)
        for profile in await list_visible_profiles(session, pilot_city=PILOT, limit=50)
    ]
    assert uuid_value not in found


# --- Task 5.3: AC-05, FR63 ------------------------------------------------------}


async def test_visibility_does_not_depend_on_gender(session: AsyncSession) -> None:
    """AC-05, FR63: пол не влияет на попадание в выдачу.

    Видимость зависит от статуса фото, а не от `gender`. Скрытая мужская
    анкета не видна по `is_hidden` (поведение 2.1), а не по полу.
    """
    female = await _profile(session, city=PILOT, gender="female")
    male = await _profile(session, city=PILOT, gender="male")
    hidden_male = await _profile(session, city=PILOT, gender="male", is_hidden=True)

    found = [
        str(profile.uuid)
        for profile in await list_visible_profiles(session, pilot_city=PILOT, limit=50)
    ]

    assert set(found) == {female, male}
    assert hidden_male not in found
