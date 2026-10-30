"""Признак «моя заявка» в контракте карточки (стори 11.5; AC-2, AC-5, AC-6).

Без этого поля UI не может ни показать «Моя заявка», ни предложить отмену
по `request_uuid` (FR82): `viewer_participates` отвечает на другой вопрос
(«я в компании») и при `capacity = 1` до выбора автора всегда `false`.

Проверяются три роли на ОДНОЙ карточке: заявитель (`true` + `uuid`),
автор (`false` + `null`), посторонний (`false` + `null`). Отдельно — что
`selected` НЕ даёт `viewer_requested` (отменять нечего, отмена вернула бы
`409 JoinRequestSelectedError`), то есть признак читается, а не константа.
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


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
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


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession, *, author_id: int, place_id: int, capacity: int = 1
) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', 0, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": datetime.now(UTC) + timedelta(minutes=60),
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, status: str = "pending"
) -> str:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
            "VALUES (:card, :applicant, 'Привет', :status, now() + interval '1 day') "
            "RETURNING uuid"
        ),
        {"card": card_id, "applicant": applicant_id, "status": status},
    )
    return str(row.scalar_one())


async def _get_card(client: AsyncClient, token: str, card_uuid: str) -> Any:
    return await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})


async def test_applicant_sees_own_pending_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-2, AC-5: заявитель видит `viewer_requested=true` и `uuid` своей заявки.

    Краснеет от: удаления поля из схемы, подстановки константы `False`,
    потери самого `uuid` (тогда отмена по `DELETE /requests/{uuid}` невозможна).
    """
    author, _author_token = await _new_user(session)
    applicant, applicant_token = await _new_user(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=await _place(session)
    )
    request_uuid = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _get_card(client, applicant_token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["viewer_requested"] is True
    assert body["viewer_request_uuid"] == request_uuid
    # Заявитель до решения автора НЕ участник — второй признак остаётся false.
    assert body["viewer_participates"] is False


async def test_author_sees_no_request_marker(client: AsyncClient, session: AsyncSession) -> None:
    """AC-2, FR81: у автора признак пуст — состав заявившихся идёт другим экраном."""
    author, author_token = await _new_user(session)
    applicant, _applicant_token = await _new_user(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=await _place(session)
    )
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _get_card(client, author_token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["viewer_requested"] is False
    assert body["viewer_request_uuid"] is None


async def test_stranger_sees_no_request_marker(client: AsyncClient, session: AsyncSession) -> None:
    """AC-2, FR81: чужую заявку посторонний не видит ни флагом, ни `uuid`."""
    author, _author_token = await _new_user(session)
    applicant, _applicant_token = await _new_user(session)
    stranger, stranger_token = await _new_user(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=await _place(session)
    )
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _get_card(client, stranger_token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["viewer_requested"] is False
    assert body["viewer_request_uuid"] is None


async def test_selected_request_is_not_marker(client: AsyncClient, session: AsyncSession) -> None:
    """AC-5, P-3: `selected` — не «моя заявка», а участие; отмена невозможна.

    Краснеет, если предикат взять по `JOIN_REQUEST_OCCUPYING_STATUSES`
    (`pending` + `selected`): UI показал бы «Отменить заявку» на выбранной
    заявке, а сервер ответил бы `409 JoinRequestSelectedError`.
    """
    author, _author_token = await _new_user(session)
    applicant, applicant_token = await _new_user(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=await _place(session)
    )
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id, status="selected")

    response = await _get_card(client, applicant_token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["viewer_requested"] is False
    assert body["viewer_request_uuid"] is None
    assert body["viewer_participates"] is True


async def test_feed_fills_marker_without_n_plus_one(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-2: лента заполняет признак ПАКЕТНО (один запрос на страницу).

    Краснеет, если поля нет в схеме ленты или если признак считается только
    на детальном экране: карточка в ленте обязана совпадать с карточкой на
    своём экране (один контракт `EventCardRead`).
    """
    author, _author_token = await _new_user(session)
    applicant, applicant_token = await _new_user(session)
    place_id = await _place(session)
    first_uuid, first_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    _second_uuid, _second_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid = await _insert_request(session, card_id=first_id, applicant_id=applicant.id)

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: applicant_token})

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    by_uuid = {item["uuid"]: item for item in items}
    assert by_uuid[first_uuid]["viewer_requested"] is True
    assert by_uuid[first_uuid]["viewer_request_uuid"] == request_uuid
    assert len(items) == 2
    second = next(item for item in items if item["uuid"] != first_uuid)
    assert second["viewer_requested"] is False
    assert second["viewer_request_uuid"] is None
