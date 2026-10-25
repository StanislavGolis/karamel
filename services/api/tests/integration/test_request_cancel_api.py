"""Отмена заявки через API (стори 5а.3, Task 9.6, 9.7; AC-01, AC-02, AC-04, AC-05, AC-09).

Реальный PostgreSQL, схема поднята Alembic. Заявки в нужных состояниях
(`selected`, `expired`, `not_selected`) кладутся сырым SQL: правила 5а.2
до этих состояний не доводят (выбор автора — 5б), и проверялся бы не тот
сценарий.

§18.1: каждый тест краснеет от снятия конкретной защиты — она названа в
докстринге. §18.2: результат проверяется строкой в БД, а не только кодом.
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
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
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
    status: str = "published",
    capacity: int = 1,
    community_slug: str | None = None,
) -> str:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, "
            "60, :expires, :status, 0, 'approved', :published_at) RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": community_slug or _community(),
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            "status": status,
            "published_at": starts - timedelta(days=1),
        },
    )
    return str(row.scalar_one())


async def _post(client: AsyncClient, card_uuid: str, token: str) -> Any:
    return await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "привет"},
        cookies={ACCESS_COOKIE_NAME: token},
    )


async def _request_row(session: AsyncSession, request_uuid: str) -> dict[str, Any]:
    result = await session.execute(
        text("SELECT status, decided_at, applicant_id FROM join_requests WHERE uuid = :uuid"),
        {"uuid": request_uuid},
    )
    return dict(result.mappings().one())


async def _set_status(session: AsyncSession, request_uuid: str, status: str) -> None:
    """Перевести заявку в состояние, до которого правила 5а.2 не доводят."""
    await session.execute(
        text("UPDATE join_requests SET status = :status, decided_at = now() WHERE uuid = :uuid"),
        {"status": status, "uuid": request_uuid},
    )
    await session.flush()


async def _submit_one(session: AsyncSession, client: AsyncClient) -> tuple[str, str]:
    """Заявка одного заявителя на карточку другого: `(uuid заявки, токен)`."""
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    response = await _post(client, card_uuid, token)
    assert response.status_code == 201, response.text
    return str(response.json()["uuid"]), token


# --- AC-01: отмена pending ------------------------------------------------


async def test_cancel_pending_returns_204_and_sets_cancelled(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: `204`, статус `cancelled`, `decided_at` заполнен.

    Краснеет, если убрать `decided_at` из `SET`: заявка вышла из `pending`,
    а момента решения нет (D-5a.1-6).
    """
    request_uuid, token = await _submit_one(session, client)

    response = await client.delete(
        f"/api/v1/requests/{request_uuid}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 204
    assert response.content == b""
    row = await _request_row(session, request_uuid)
    assert row["status"] == "cancelled"
    assert row["decided_at"] is not None


async def test_repeat_cancel_is_idempotent_and_keeps_decided_at(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05: повторная отмена — `204`, `decided_at` НЕ переписан.

    Краснеет, если ветку `cancelled` вернуть в `409` или выполнить второй
    `UPDATE`: клиент после обрыва сети получил бы «не удалось» о уже
    достигнутом состоянии, а аудит потерял бы момент фактической отмены.
    """
    request_uuid, token = await _submit_one(session, client)
    cookies = {ACCESS_COOKIE_NAME: token}
    first = await client.delete(f"/api/v1/requests/{request_uuid}", cookies=cookies)
    assert first.status_code == 204
    decided_before = (await _request_row(session, request_uuid))["decided_at"]

    second = await client.delete(f"/api/v1/requests/{request_uuid}", cookies=cookies)

    assert second.status_code == 204
    decided_after = (await _request_row(session, request_uuid))["decided_at"]
    assert decided_after == decided_before


# --- AC-02: selected ------------------------------------------------------


async def test_selected_request_cannot_be_cancelled(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR104): `409 REQUEST_SELECTED`, статус не меняется.

    Краснеет, если убрать проверку `selected`: заявитель «отменил» бы
    выбранную заявку, и место в карточке осталось бы занятым без участника.
    """
    request_uuid, token = await _submit_one(session, client)
    await _set_status(session, request_uuid, "selected")

    response = await client.delete(
        f"/api/v1/requests/{request_uuid}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 409
    assert response.json() == {"code": "REQUEST_SELECTED"}
    assert (await _request_row(session, request_uuid))["status"] == "selected"


# --- AC-05: expired / not_selected ---------------------------------------


@pytest.mark.parametrize("status", ["expired", "not_selected"])
async def test_inactive_request_returns_single_code(
    client: AsyncClient, session: AsyncSession, status: str
) -> None:
    """AC-05 (FR81): `expired` и `not_selected` — ОДИН код `REQUEST_NOT_ACTIVE`.

    Краснеет, если развести их на два кода: `DELETE` стал бы оракулом
    «вас не выбрали», и FR81 был бы обойдён через ошибку.
    """
    request_uuid, token = await _submit_one(session, client)
    await _set_status(session, request_uuid, status)

    response = await client.delete(
        f"/api/v1/requests/{request_uuid}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 409
    assert response.json() == {"code": "REQUEST_NOT_ACTIVE"}
    assert (await _request_row(session, request_uuid))["status"] == status


# --- AC-04: чужая заявка --------------------------------------------------


async def test_foreign_request_is_indistinguishable_from_missing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (ADR-13): чужая заявка → то же тело, что несуществующая.

    Краснеет, если убрать `applicant_id` из предиката CAS: чужая заявка
    отменялась бы, а `403` вместо `404` подтвердил бы её существование.
    """
    request_uuid, _ = await _submit_one(session, client)
    _, stranger_token = await _new_user(session)
    missing_uuid = uuid_lib.uuid4()

    foreign = await client.delete(
        f"/api/v1/requests/{request_uuid}", cookies={ACCESS_COOKIE_NAME: stranger_token}
    )
    missing = await client.delete(
        f"/api/v1/requests/{missing_uuid}", cookies={ACCESS_COOKIE_NAME: stranger_token}
    )

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json() == missing.json() == {"code": "request_not_found"}
    assert (await _request_row(session, request_uuid))["status"] == "pending"


async def test_foreign_community_request_is_not_cancellable(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (ADR-13): заявка на карточку чужого сообщества — `404`.

    Краснеет, если убрать срез сообщества из CAS: `uuid` стал бы достаточным
    основанием для записи в чужое сообщество.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session, slug="other-city")
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place_id, community_slug="other-city"
    )
    card_row = await session.execute(
        text("SELECT id FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    card_id = int(card_row.scalar_one())
    request_row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
            "VALUES (:card, :applicant, 'привет', 'pending', now() + interval '1 day') "
            "RETURNING uuid"
        ),
        {"card": card_id, "applicant": applicant.id},
    )
    request_uuid = str(request_row.scalar_one())
    await session.flush()

    response = await client.delete(
        f"/api/v1/requests/{request_uuid}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 404
    assert (await _request_row(session, request_uuid))["status"] == "pending"


# --- AC-01, AC-09: слот лимита -------------------------------------------


async def test_cancel_frees_slot_for_new_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, AC-09 (FR77, NFR29, ADR-09): отмена освобождает слот лимита.

    Краснеет, если `cancelled` останется в `JOIN_REQUEST_ACTIVE_STATUSES`
    или если отмена не меняет статус: третья подача осталась бы `429`.
    «Статус сменился» и «слот свободен» — разные утверждения, поэтому
    проверяется именно третья подача.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place_id = await _place(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    first_card = await _insert_card(session, author_id=author.id, place_id=place_id)
    second_card = await _insert_card(session, author_id=author.id, place_id=place_id)
    third_card = await _insert_card(session, author_id=author.id, place_id=place_id)

    first = await _post(client, first_card, token)
    second = await _post(client, second_card, token)
    assert first.status_code == 201
    assert second.status_code == 201
    blocked = await _post(client, third_card, token)
    assert blocked.status_code == 429

    cancelled = await client.delete(f"/api/v1/requests/{first.json()['uuid']}", cookies=cookies)
    assert cancelled.status_code == 204

    after_cancel = await _post(client, third_card, token)
    assert after_cancel.status_code == 201


async def test_numeric_request_id_is_not_a_valid_path(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (D-2): `/requests/1` → `422`, а не `404`/`500`.

    Краснеет, если путь типизировать строкой: числовой `uuid` резолвился бы
    в выборку и выглядел бы как «заявка удалена».
    """
    _, token = await _new_user(session)

    response = await client.delete("/api/v1/requests/1", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 422


async def test_anonymous_cancel_is_unauthorized(client: AsyncClient) -> None:
    """AC-08 (FR64): без сессии — `401 unauthenticated`."""
    response = await client.delete(f"/api/v1/requests/{uuid_lib.uuid4()}")

    assert response.status_code == 401
