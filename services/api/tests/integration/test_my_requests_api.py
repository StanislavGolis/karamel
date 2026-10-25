"""Список «Мои заявки» через API (стори 5а.3, Task 9.8, 9.9; AC-03, AC-06, AC-07).

Реальный PostgreSQL, схема поднята Alembic. Главное утверждение — FR81:
заявка `not_selected` ЛЕЖИТ в БД, но в выдаче её нет ни на одной странице.
Это проверяется наличием строки в БД до запроса: тест без такой подготовки
проверял бы пустоту, а не фильтр (§18.2).
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

from app.api.v1.join_requests import get_join_request_rate_limiter
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


def _community() -> str:
    return settings.default_community_slug


class _NoLimit:
    async def hit(self, applicant_id: int) -> int | None:
        return None


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_join_request_rate_limiter] = _NoLimit
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_join_request_rate_limiter, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession, *, slug: str | None = None) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": slug or _community()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    title: str = "Встреча",
    community_slug: str | None = None,
) -> str:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, :title, NULL, 'cafe', :starts, 1, 60, :expires, "
            "'published', 0, 'approved', :published_at) RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": community_slug or _community(),
            "title": title,
            "starts": starts,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
        },
    )
    return str(row.scalar_one())


async def _insert_request(
    session: AsyncSession,
    *,
    card_id: int,
    applicant_id: int,
    status: str = "pending",
    submitted_at: datetime | None = None,
    message: str = "привет",
) -> str:
    # `decided_at` передаётся значением, а не через `CASE WHEN :status = ...`:
    # asyncpg выводит тип параметра по контексту и на двух употреблениях одного
    # `:status` (значение + сравнение) падает `inconsistent types deduced`.
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:card, :applicant, :message, :status, "
            ":submitted_at, now() + interval '1 day', :decided_at) RETURNING uuid"
        ),
        {
            "card": card_id,
            "applicant": applicant_id,
            "message": message,
            "status": status,
            "submitted_at": submitted_at or datetime.now(UTC),
            "decided_at": None if status == "pending" else datetime.now(UTC),
        },
    )
    return str(row.scalar_one())


async def _card_id(session: AsyncSession, card_uuid: str) -> int:
    row = await session.execute(
        text("SELECT id FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    return int(row.scalar_one())


async def _get(client: AsyncClient, token: str, cursor: str | None = None) -> Any:
    params = {"cursor": cursor} if cursor else None
    return await client.get(
        "/api/v1/my/requests", params=params, cookies={ACCESS_COOKIE_NAME: token}
    )


# --- AC-03: not_selected не отдаётся --------------------------------------


async def test_not_selected_is_never_returned(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (FR81): `not_selected` ЛЕЖИТ в БД и НЕ появляется в выдаче.

    Краснеет, если заменить `JOIN_REQUEST_VISIBLE_STATUSES` на все статусы
    или на `status <> 'not_selected'` (последнее протекает на будущих
    значениях): «вас не выбрали» стало бы видно заявителю, а FR81
    запрещает это показывать.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    rejected = await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, status="not_selected"
    )
    await session.flush()
    present = await session.execute(
        text("SELECT count(*) FROM join_requests WHERE uuid = :uuid"), {"uuid": rejected}
    )
    assert present.scalar_one() == 1

    response = await _get(client, token)

    assert response.status_code == 200
    assert [item["uuid"] for item in response.json()["items"]] == []
    assert response.json()["total"] == 0


@pytest.mark.parametrize("status", ["pending", "selected", "cancelled", "expired"])
async def test_visible_statuses_are_returned(
    client: AsyncClient, session: AsyncSession, status: str
) -> None:
    """AC-03: четыре видимых статуса присутствуют в выдаче.

    Краснеет, если из `JOIN_REQUEST_VISIBLE_STATUSES` пропадёт значение:
    заявитель перестал бы видеть свою же заявку после решения автора.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    request_uuid = await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, status=status
    )
    await session.flush()

    response = await _get(client, token)

    assert response.status_code == 200
    body = response.json()
    assert [item["uuid"] for item in body["items"]] == [request_uuid]
    assert body["items"][0]["status"] == status
    assert body["total"] == 1


# --- AC-06: сортировка и пагинация ----------------------------------------


async def test_items_are_ordered_by_submitted_at(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: порядок `submitted_at ASC` — без него листание невоспроизводимо.

    Краснеет, если убрать `ORDER BY submitted_at`: страницы теряли бы
    и дублировали строки, а `WHERE (submitted_at, uuid) >` перестал бы
    соответствовать порядку выдачи.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    base = datetime.now(UTC) - timedelta(days=1)
    uuids: list[str] = []
    for index in range(3):
        card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
        card_id = await _card_id(session, card_uuid)
        uuids.append(
            await _insert_request(
                session,
                card_id=card_id,
                applicant_id=applicant.id,
                submitted_at=base + timedelta(minutes=index),
            )
        )
    await session.flush()

    response = await _get(client, token)

    assert response.status_code == 200
    assert [item["uuid"] for item in response.json()["items"]] == uuids


async def test_pagination_by_cursor_has_no_overlap_and_keeps_total(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: страницы не пересекаются, `total` не зависит от листания.

    Краснеет, если `total` считать С курсором или курсор строить от
    неправильной строки: `total` уменьшался бы, а строки терялись/дублировались.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    base = datetime.now(UTC) - timedelta(days=2)
    for index in range(3):
        card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
        card_id = await _card_id(session, card_uuid)
        await _insert_request(
            session,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=base + timedelta(minutes=index),
        )
    await session.flush()

    first_page = (await _get(client, token)).json()
    assert first_page["total"] == 3

    # Курсор строится вручную от середины, чтобы не зависеть от размера
    # страницы: проверяется сам предикат, а не совпадение с константой.
    from app.services.join_request_cursor import encode_my_requests_cursor

    middle_at = base + timedelta(minutes=1)
    middle_uuid = uuid_lib.UUID(first_page["items"][1]["uuid"])
    second = (await _get(client, token, encode_my_requests_cursor(middle_at, middle_uuid))).json()

    assert second["total"] == 3
    # Сравнение с УЖЕ ОТДАННЫМИ строками, а не со всей первой страницей: первая
    # страница короткая (3 заявки при размере 20), и пересечение с ней
    # неизбежно — проверяется, что вторая страница не повторяет отданное.
    delivered = {item["uuid"] for item in first_page["items"][:2]}
    second_ids = {item["uuid"] for item in second["items"]}
    assert second_ids & delivered == set()
    assert len(second_ids) == 1


async def test_broken_cursor_gives_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-06: битый курсор — `422 invalid_cursor`, а не `500`.

    Краснеет, если пробросить `binascii.Error` наружу: клиент получил бы
    `500` на собственной опечатке в query-параметре.
    """
    _, token = await _new_user(session)

    response = await _get(client, token, "!!!")

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_cursor"}


# --- AC-07: автор, место, фото --------------------------------------------


async def test_foreign_community_requests_are_not_returned(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07, ADR-01: заявка на карточку чужого сообщества не попадает в выдачу.

    Краснеет, если убрать срез сообщества из page-statement: заявитель
    видел бы карточки города, в котором не состоит.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session, slug="other-city")
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place_id, community_slug="other-city"
    )
    card_id = await _card_id(session, card_uuid)
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)
    await session.flush()

    response = await _get(client, token)

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


async def test_author_without_profile_has_null_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07: автор без анкеты — элемент присутствует, поля `null`.

    Краснеет, если заменить `outerjoin(Profile)` на `join`: заявка исчезла бы
    из списка заявителя из-за пустой анкеты автора.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)
    await session.flush()

    response = await _get(client, token)

    body = response.json()
    assert len(body["items"]) == 1
    author_block = body["items"][0]["author"]
    assert author_block["uuid"] == str(author.uuid)
    assert author_block["name"] is None
    assert author_block["age"] is None
    assert author_block["photo_url"] is None


async def test_message_and_card_place_are_returned(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06, AC-07: своё сообщение и вложенная карточка с местом отдаются.

    Краснеет, если убрать `message` из схемы: экран «что я написал» потерял бы
    текст, а в ответе на подачу его и не было (OQ-5a.2-2).
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id, title="Кино")
    card_id = await _card_id(session, card_uuid)
    await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, message="пойдём вместе"
    )
    await session.flush()

    response = await _get(client, token)

    item = response.json()["items"][0]
    assert item["message"] == "пойдём вместе"
    assert item["card"]["uuid"] == card_uuid
    assert item["card"]["title"] == "Кино"
    assert item["card"]["place"]["name"].startswith("Кафе")
    assert "expires_at" in item


async def test_anonymous_is_unauthorized(client: AsyncClient) -> None:
    """AC-08 (FR64): без сессии — `401 unauthenticated`."""
    response = await client.get("/api/v1/my/requests")

    assert response.status_code == 401
