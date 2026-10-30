"""Признак участия зрителя в карточке (стори 5б.2; AC-09, D-5b.2-7, FR104).

Без этого поля UI не отличит участника от наблюдателя, и FR104
(«Отменить участие») недостижим в вебе. Проверяются три роли на ОДНОЙ
карточке: участник (`true`), наблюдатель (`false`), автор (`false`).

Отдельно доказывается пакетность ленты: 20 карточек не должны давать 20
дополнительных `SELECT` (N+1) — иначе самый частый GET продукта подорожал
бы на порядок.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
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
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    title: str = "Встреча",
    capacity: int = 1,
    filled_count: int = 0,
    status: str = "published",
) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, :title, NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "title": title,
            "starts": starts,
            "capacity": capacity,
            "filled": filled_count,
            "expires": datetime.now(UTC) + timedelta(minutes=60),
            "status": status,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_selected(session: AsyncSession, *, card_id: int, applicant_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, decided_at, "
            "expires_at) VALUES (:card, :applicant, '', 'selected', now(), "
            "now() + interval '1 day')"
        ),
        {"card": card_id, "applicant": applicant_id},
    )


async def _get_card(client: AsyncClient, token: str, card_uuid: str) -> Any:
    return await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})


async def test_participant_sees_flag_true(client: AsyncClient, session: AsyncSession) -> None:
    """AC-09 (M9): участник получает `viewer_participates=true`.

    Краснеет от константы `False` (M9): кнопка «Отменить участие» не
    появилась бы никогда, и FR104 остался бы недостижим.
    """
    author, _ = await _new_user(session)
    participant, participant_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, filled_count=1, status="full"
    )
    await _insert_selected(session, card_id=card_id, applicant_id=participant.id)

    response = await _get_card(client, participant_token, card_uuid)

    assert response.status_code == 200, response.text
    assert response.json()["viewer_participates"] is True


async def test_observer_and_author_see_flag_false(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09: наблюдатель и автор получают `false`.

    Автор — не участник своей компании: `selected`-заявки у него нет, и
    кнопка «Отменить участие» ему не показывается. Наблюдатель не подавал.
    """
    author, author_token = await _new_user(session)
    _observer, observer_token = await _new_user(session)
    participant, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, filled_count=1, status="full"
    )
    await _insert_selected(session, card_id=card_id, applicant_id=participant.id)

    observer_response = await _get_card(client, observer_token, card_uuid)
    author_response = await _get_card(client, author_token, card_uuid)

    assert observer_response.status_code == 200, observer_response.text
    assert observer_response.json()["viewer_participates"] is False
    assert author_response.status_code == 200, author_response.text
    assert author_response.json()["viewer_participates"] is False


async def test_pending_request_does_not_count_as_participation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09: `pending`-заявка — ещё не участие, флаг `false`.

    Краснеет от предиката `JOIN_REQUEST_OCCUPYING_STATUSES` вместо
    `selected`: кнопка «Отменить участие» появилась бы у заявителя, который
    места не занимает, — а отменить ему нечего (`DELETE /join` → `404`).
    """
    author, _ = await _new_user(session)
    applicant, applicant_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
            "VALUES (:card, :applicant, '', 'pending', now() + interval '1 day')"
        ),
        {"card": card_id, "applicant": applicant.id},
    )

    response = await _get_card(client, applicant_token, card_uuid)

    assert response.status_code == 200, response.text
    assert response.json()["viewer_participates"] is False


async def test_feed_resolves_participation_with_one_batch_query(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09 (D-5b.2-7): лента — ОДИН пакетный запрос на страницу, не N+1.

    Пять карточек в выдаче: без пакетности `viewer_participation_card_ids`
    вызывался бы пять раз. Считается число запросов, обращающихся к
    `join_requests` с предикатом `card_id IN (...)`.
    """
    author, _ = await _new_user(session)
    participant, participant_token = await _new_user(session)
    place_id = await _place(session)
    card_ids: list[int] = []
    for index in range(5):
        _, card_id = await _insert_card(
            session,
            author_id=author.id,
            place_id=place_id,
            title=f"Встреча {index}",
            filled_count=1,
            status="full",
        )
        card_ids.append(card_id)
        await _insert_selected(session, card_id=card_id, applicant_id=participant.id)

    statements: list[str] = []
    connection = await session.connection()

    def _capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
    ) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(connection.sync_connection, "before_cursor_execute", _capture)
    try:
        response = await client.get(
            "/api/v1/cards", cookies={ACCESS_COOKIE_NAME: participant_token}
        )
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", _capture)

    assert response.status_code == 200, response.text
    # Стори 11.5 добавила ВТОРОЙ признак зрителя (`viewer_requested`), но
    # считается он ТЕМ ЖЕ запросом: число SQL на `GET /cards/{uuid}` и на
    # ленту зафиксировано контрактом анти-оракула
    # (`test_oracle_exact_query_count`). Отдельный `SELECT` на второй признак
    # краснил бы его при верном JSON — поэтому ожидание осталось ровно 1.
    participation_queries = [
        sql
        for sql in statements
        if "join_requests" in sql.lower() and "card_id in (" in sql.lower()
    ]
    assert len(participation_queries) == 1, participation_queries

    items = response.json()["items"]
    assert items, "лента пуста — тест проверяет не то"
    assert all(item["viewer_participates"] is True for item in items)


async def test_feed_participation_is_per_card_not_global(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09 (M9, D-5b.2-7): признак участия в ленте считается ПО КАРТОЧКЕ.

    Наблюдатель `selected` в первой карточке и НЕ участник второй: лента
    обязана вернуть `[true, false]`. Мутация «`viewer_participates` всегда
    `true`» выживала у прежнего теста ленты, потому что тот проверял только
    участника — все карточки в нём были его. Здесь ложное значение
    обязательно присутствует.
    """
    author, _ = await _new_user(session)
    viewer, viewer_token = await _new_user(session)
    place_id = await _place(session)
    _, joined_card_id = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        title="Компания",
        filled_count=1,
        status="full",
    )
    await _insert_selected(session, card_id=joined_card_id, applicant_id=viewer.id)
    _, foreign_card_id = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        title="Чужая встреча",
        filled_count=1,
        status="full",
    )

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: viewer_token})

    assert response.status_code == 200, response.text
    flags = {item["title"]: item["viewer_participates"] for item in response.json()["items"]}
    assert flags.get("Компания") is True, flags
    assert flags.get("Чужая встреча") is False, flags
