"""Тип встречи и активность через API (стори 12.4; AC-3, AC-4, AC-5, AC-7).

Реальный PostgreSQL, схема поднята Alembic. Карточки для ленты кладутся
СЫРЫМ SQL: `POST /cards` не даёт задать `kind`/`activity` в обход формы,
а проверяются именно фильтры выдачи по этим колонкам.

Краснеет при:
- снятии обязательности `kind`/`activity` в `EventCardCreate` — U-1;
- пропуске `other` через API — U-4;
- связке «`capacity=1` → `date`» — U-6 (AC-4);
- отсутствии фильтра в `list_feed_page` — U-7;
- `or` вместо `and` между фильтрами — U-8;
- отсутствии фильтра в `count_feed` — U-9.
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


def _city() -> str:
    return settings.default_city_slug


def _future(**delta: int) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    kind: str = "company",
    activity: str = "coffee",
    category: str = "cafe",
    capacity: int = 2,
) -> str:
    """Карточка в ленте: `published` со свободным местом."""
    starts = _future(days=2)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, :category, :starts, :capacity, "
            "60, :expires, 'published', 0, 'approved', :published_at, :kind, :activity) "
            "RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": _city(),
            "category": category,
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
            "kind": kind,
            "activity": activity,
        },
    )
    return str(row.scalar_one())


def _payload(place_uuid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "cafe",
        "place_uuid": place_uuid,
        "starts_at": _future(days=2).isoformat(),
        "capacity": 2,
        "kind": "date",
        "activity": "coffee",
    }
    base.update(overrides)
    return base


async def _place_uuid(client: AsyncClient, token: str, place_id: int) -> str:
    row = await client.get("/api/v1/places", cookies={ACCESS_COOKIE_NAME: token})
    assert row.status_code == 200, row.text
    for item in row.json()["items"]:
        return str(item["uuid"])
    raise AssertionError("место не найдено в справочнике")


async def test_post_without_kind_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-3 (FR117): без типа карточка не создаётся — U-1."""
    _, token = await _new_user(session)
    place_id = await _place(session)
    await session.flush()
    place_uuid = await _place_uuid(client, token, place_id)
    payload = _payload(place_uuid)
    payload.pop("kind")

    response = await client.post("/api/v1/cards", json=payload, cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 422, response.text


async def test_post_without_activity_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3 (FR118): без активности карточка не создаётся — U-1."""
    _, token = await _new_user(session)
    place_id = await _place(session)
    await session.flush()
    place_uuid = await _place_uuid(client, token, place_id)
    payload = _payload(place_uuid)
    payload.pop("activity")

    response = await client.post("/api/v1/cards", json=payload, cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 422, response.text


async def test_post_with_other_activity_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3: легаси `other` через API недостижим — U-4."""
    _, token = await _new_user(session)
    place_id = await _place(session)
    await session.flush()
    place_uuid = await _place_uuid(client, token, place_id)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, activity="other"),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422, response.text


async def _created_card_body(client: AsyncClient, token: str, response: Any) -> dict[str, Any]:
    """Читает созданную карточку детальным эндпоинтом (AC-7).

    Чтение обязательно: `EventCardRef` (ответ `POST`) не содержит
    `kind`/`activity`, поэтому проверка одного `status_code == 201` ничего
    не говорит о том, ЧТО сохранено. Именно на этом выжила мутация U-6
    в CR 12.4 (H-3).
    """
    card_uuid = str(response.json()["uuid"])
    fetched = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})
    assert fetched.status_code == 200, fetched.text
    return dict(fetched.json())


async def test_capacity_one_with_company_is_stored_as_company(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-4 (ADR-20, FR117): `capacity=1` + `company` — сохранён именно `company`.

    **CR 12.4 H-3.** До правки тест проверял только `201`, поэтому мутация
    «`if capacity == 1: kind = 'date'`» выживала: `201` возвращался и при
    подменённом типе. Связка «тип из вместимости» — ровно тот продуктовый
    дефект, который устраняла стори 12.4, и единственный гард против его
    возврата — чтение сохранённого значения.

    Пара к тестам выше: без неё «всегда 422» не отличить от работающей
    валидации (класс одностороннего теста, урок 11.10).
    """
    _, token = await _new_user(session)
    place_id = await _place(session)
    await session.flush()
    place_uuid = await _place_uuid(client, token, place_id)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(
            place_uuid,
            capacity=1,
            kind="company",
            activity="bar",
            decision_window_minutes=60,
        ),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201, response.text
    body = await _created_card_body(client, token, response)
    # AC-4: `capacity` — механика присоединения, `kind` — смысл. Тип не выведен
    # из вместимости: `capacity=1` не превращает встречу в свидание.
    assert body["kind"] == "company", body["kind"]
    assert body["activity"] == "bar", body["activity"]
    assert body["capacity"] == 1


async def test_capacity_one_with_date_is_stored_as_date(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-4 (ADR-20): `capacity=1` + `date` тоже сохраняется как `date`.

    Пара к тесту выше и вторая половина защиты от мутации U-6: если бы код
    жёстко ставил `kind='date'` при `capacity=1`, тест компании покраснел бы,
    но если бы код жёстко ставил `company` — краснеет этот. Вместе они
    доказывают, что тип берётся из запроса, а не из вместимости.
    """
    _, token = await _new_user(session)
    place_id = await _place(session)
    await session.flush()
    place_uuid = await _place_uuid(client, token, place_id)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(
            place_uuid,
            capacity=1,
            kind="date",
            activity="cinema",
            decision_window_minutes=60,
        ),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201, response.text
    body = await _created_card_body(client, token, response)
    assert body["kind"] == "date", body["kind"]
    assert body["activity"] == "cinema", body["activity"]


async def test_feed_filters_by_kind(client: AsyncClient, session: AsyncSession) -> None:
    """AC-5 (FR117): `?kind=date` отдаёт только свидания — U-7."""
    user, token = await _new_user(session)
    place_id = await _place(session)
    date_uuid = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="date", activity="coffee"
    )
    company_uuid = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="company", activity="bar"
    )
    await session.flush()

    response = await client.get("/api/v1/cards?kind=date", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    body = response.json()
    returned = {item["uuid"] for item in body["items"]}
    assert date_uuid in returned
    assert company_uuid not in returned
    # AC-7: поля пришли в ответе и совпадают с фильтром.
    for item in body["items"]:
        assert item["kind"] == "date"


async def test_feed_filters_by_activity(client: AsyncClient, session: AsyncSession) -> None:
    """AC-5 (FR118): `?activity=cinema` отдаёт только кино — U-7."""
    user, token = await _new_user(session)
    place_id = await _place(session)
    cinema_uuid = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="company", activity="cinema"
    )
    sport_uuid = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="company", activity="sport"
    )
    await session.flush()

    response = await client.get(
        "/api/v1/cards?activity=cinema", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200, response.text
    returned = {item["uuid"] for item in response.json()["items"]}
    assert cinema_uuid in returned
    assert sport_uuid not in returned


async def test_feed_filters_are_conjunctive(client: AsyncClient, session: AsyncSession) -> None:
    """AC-5: `kind` и `activity` — конъюнкция, а не `or` — U-8.

    Краснеет, если между фильтрами поставить `or`: под запрос попала бы
    карточка, совпавшая только по одному признаку.
    """
    user, token = await _new_user(session)
    place_id = await _place(session)
    both = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="date", activity="cinema"
    )
    only_kind = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="date", activity="sport"
    )
    only_activity = await _insert_card(
        session, author_id=user.id, place_id=place_id, kind="company", activity="cinema"
    )
    await session.flush()

    response = await client.get(
        "/api/v1/cards?kind=date&activity=cinema", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200, response.text
    returned = {item["uuid"] for item in response.json()["items"]}
    assert both in returned
    assert only_kind not in returned
    assert only_activity not in returned


async def test_feed_total_respects_filters(client: AsyncClient, session: AsyncSession) -> None:
    """AC-5: `total` считается по тому же фильтру, что страница — U-9.

    Краснеет, если фильтр добавить только в `build_feed_statement`:
    `total` покажет все карточки города («5 всего, 1 на странице»).
    """
    user, token = await _new_user(session)
    place_id = await _place(session)
    for _ in range(2):
        await _insert_card(
            session, author_id=user.id, place_id=place_id, kind="date", activity="coffee"
        )
    for _ in range(3):
        await _insert_card(
            session, author_id=user.id, place_id=place_id, kind="company", activity="bar"
        )
    await session.flush()

    response = await client.get("/api/v1/cards?kind=date", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2


async def test_invalid_kind_returns_machine_code(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-5: мусор в `kind` — `422` с машинным кодом, а не `500`."""
    _, token = await _new_user(session)
    await session.flush()

    response = await client.get("/api/v1/cards?kind=meeting", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_request"
