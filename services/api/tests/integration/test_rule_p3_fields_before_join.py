"""П3: до подачи заявки заявитель видит всё, что нужно для решения (FR79).

**Что доказывается.** `GET /cards/{uuid}` ДО подачи отдаёт: время
(`starts_at`), место (`place.name` + `place.address_text`, NFR34 — без
координат), активность (`activity`), тип (`kind`), рамки аудитории
(`gender_filter`/`age_min`/`age_max`), свободные места (`capacity` минус
`filled_count`) и описание автора (`description`).

Это правило П3: «человек решает, идти ли, по карточке, а не после подачи».
Без состава полей экран вынуждал бы подать заявку вслепую.

**Почему нового теста не хватало (замер CS 12.9).** Состав `EventCardRead`
проверялся косвенно: `test_feed_statement_*` смотрит SQL ленты,
`test_card_author_profile_api` — витрину автора. Замка «какие поля
приходят ДО подачи» не было: удаление `activity` из ответа или подмена
`address_text` на координаты прошли бы приёмку.

**Почему пара «с рамками / без».** Рамки — необязательная часть карточки.
Односторонний тест («рамки есть → пришли») не отличил бы «поля заполнены»
от «поля всегда `null`». Второй тест доказывает ровно обратное: у карточки
без рамок эти поля `null`, а не `KeyError` и не выдуманные значения.

Метка `П3` в докстринге обязательна: реестр правил
(`test_rules_registry.py`) находит тест по ней (AC-1).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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
    """HTTP-клиент на общей сессии: `commit()` сервиса освобождает SAVEPOINT."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7911{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession, *, name: str, address: str) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', :a, :s, true) RETURNING id"
        ),
        {"n": name, "a": address, "s": settings.default_city_slug},
    )
    return int(row.scalar_one())


async def _card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    kind: str = "company",
    activity: str = "coffee",
    description: str | None = "Ищу компанию на кофе",
    capacity: int = 4,
    filled_count: int = 1,
    gender_filter: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity, "
            "gender_filter, age_min, age_max) "
            "VALUES (:a, :p, :s, 'Кофе', :d, 'cafe', :st, :cap, 60, :e, 'published', "
            ":filled, 'approved', :pub, :kind, :act, :gf, :amin, :amax) RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_city_slug,
            "d": description,
            "st": starts,
            "cap": capacity,
            "e": starts - timedelta(hours=1),
            "filled": filled_count,
            "pub": starts - timedelta(days=1),
            "kind": kind,
            "act": activity,
            "gf": gender_filter,
            "amin": age_min,
            "amax": age_max,
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def test_all_decision_fields_present_before_join(
    client: AsyncClient, session: AsyncSession
) -> None:
    """П3: до подачи приходит время, место, активность, тип, рамки, места, описание.

    Краснеет от: удаления `activity`/`kind` из `EventCardRead`; замены
    `place.address_text` на координаты (NFR34); выкидывания `description`;
    исчезновения `capacity`/`filled_count` (свободные места клиент считает
    из них, второго источника `has_seats` нет — решение D-5a.3-3).
    """
    author, author_token = await _user(session)
    _, viewer_token = await _user(session)
    place_id = await _place(session, name="Кофейня на Ленина", address="ул. Ленина, 42")
    card_uuid, card_id = await _card(
        session,
        author_id=author.id,
        place_id=place_id,
        kind="company",
        activity="coffee",
        description="Ищу компанию на кофе после работы",
        capacity=4,
        filled_count=1,
        gender_filter="female",
        age_min=25,
        age_max=40,
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    # 1. Время встречи — по нему решают «могу ли я тогда».
    assert body["starts_at"]
    assert body["expires_at"]
    # 2. Место: имя и адрес текстом, координат нет (NFR34).
    assert body["place"]["name"] == "Кофейня на Ленина"
    assert body["place"]["address_text"] == "ул. Ленина, 42"
    assert "lat" not in body["place"] and "lon" not in body["place"]
    # 3. Ось смысла: активность и тип.
    assert body["activity"] == "coffee"
    assert body["kind"] == "company"
    # 4. Рамки аудитории — мягкий фильтр, но заявитель вправе их видеть.
    assert body["gender_filter"] == "female"
    assert body["age_min"] == 25
    assert body["age_max"] == 40
    # 5. Свободные места: capacity − filled_count, второго поля нет.
    assert body["capacity"] == 4
    assert body["filled_count"] == 1
    assert "has_seats" not in body
    # 6. Описание автора — половина решения (UX §865).
    assert body["description"] == "Ищу компанию на кофе после работы"
    # 7. Признаки зрителя: до подачи — «не участвую, не заявлен».
    assert body["viewer_participates"] is False
    assert body["viewer_requested"] is False


async def test_card_without_bounds_gives_nulls_not_error(
    client: AsyncClient, session: AsyncSession
) -> None:
    """П3 (пара): карточка БЕЗ рамок → поля `null`, ответ всё равно `200`.

    Пара к тесту выше: без неё «рамки приходят» не отличить от «поля всегда
    заполнены чем-то». Краснеет, если сериализатор начнёт падать на
    `gender_filter IS NULL` или подставлять умолчания (`"any"`, `18`/`99`):
    умолчание сделало бы «рамок нет» и «рамка 18–99» неотличимыми.
    """
    author, author_token = await _user(session)
    _, viewer_token = await _user(session)
    place_id = await _place(session, name="Парк", address="Центральный вход")
    card_uuid, _ = await _card(
        session, author_id=author.id, place_id=place_id, capacity=3, filled_count=0
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["gender_filter"] is None
    assert body["age_min"] is None
    assert body["age_max"] is None
    # Остальные поля на месте: отсутствие рамок не обнуляет карточку.
    assert body["capacity"] == 3
    assert body["filled_count"] == 0
    assert body["place"]["address_text"] == "Центральный вход"
