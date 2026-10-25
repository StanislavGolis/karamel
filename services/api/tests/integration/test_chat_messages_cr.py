"""CR 6.4: независимые проверки (не доверяем отчёту DS)."""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_message_body, encrypt_message_body
from app.core.db import get_session
from app.main import app
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _user(session: AsyncSession) -> tuple[int, str]:
    phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
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
    return user.id, issued.access_token


async def _company(session: AsyncSession) -> tuple[str, int, str, int]:
    author_id, _author_token = await _user(session)
    member_id, member_token = await _user(session)
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, :cat, :addr, :s, true) RETURNING id"
        ),
        {
            "n": "P" + uuid_lib.uuid4().hex[:8],
            "cat": "cafe",
            "addr": "addr",
            "s": settings.default_community_slug,
        },
    )
    place_id = int(place.scalar_one())
    starts = datetime.now(UTC) + timedelta(days=3)
    chat_uuid = str(uuid_lib.uuid4())
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, chat_uuid) VALUES (:a, :p, :s, :t, :cat, "
            ":st, 2, 60, :e, :status, 2, :ver, :pub, :chat) RETURNING id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_community_slug,
            "t": "T",
            "cat": "cafe",
            "st": starts,
            "e": starts - timedelta(hours=1),
            "status": "full",
            "ver": "approved",
            "pub": starts - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_id = int(card.scalar_one())
    chat = await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (:u, :c) RETURNING id"),
        {"u": chat_uuid, "c": card_id},
    )
    chat_id = int(chat.scalar_one())
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:c, :a, :m, :s, now(), "
            "now() + interval '1 day', now())"
        ),
        {"c": card_id, "a": member_id, "m": "", "s": "selected"},
    )
    await session.flush()
    return chat_uuid, chat_id, member_token, member_id


def _auth(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


def test_ciphertext_not_transferable_between_chats() -> None:
    blob = encrypt_message_body("секрет", chat_id=1, sender_id=10)

    with pytest.raises(InvalidTag):
        decrypt_message_body(blob, chat_id=2, sender_id=10)


def test_ciphertext_not_transferable_between_senders() -> None:
    blob = encrypt_message_body("секрет", chat_id=1, sender_id=10)

    with pytest.raises(InvalidTag):
        decrypt_message_body(blob, chat_id=1, sender_id=11)


def test_nonce_differs_for_same_body() -> None:
    first = encrypt_message_body("одинаково", chat_id=1, sender_id=10)
    second = encrypt_message_body("одинаково", chat_id=1, sender_id=10)

    assert first[:12] != second[:12]
    assert first != second


async def test_replay_returns_stored_body_not_new_one(
    client: AsyncClient, session: AsyncSession
) -> None:
    chat_uuid, _chat_id, member_token, _member_id = await _company(session)
    client_id = str(uuid_lib.uuid4())

    first = await client.post(
        f"/api/v1/chats/{chat_uuid}/messages",
        json={"body": "первый текст", "client_message_id": client_id},
        headers=_auth(member_token),
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/chats/{chat_uuid}/messages",
        json={"body": "исправленный текст", "client_message_id": client_id},
        headers=_auth(member_token),
    )

    assert second.status_code == 201
    assert second.json()["uuid"] == first.json()["uuid"]
    assert second.json()["body"] == first.json()["body"]


async def test_cursor_with_sql_injection_is_422(client: AsyncClient, session: AsyncSession) -> None:
    chat_uuid, _chat_id, member_token, _member_id = await _company(session)

    response = await client.get(
        f"/api/v1/chats/{chat_uuid}/messages",
        params={"cursor": "1; DROP TABLE messages;--"},
        headers=_auth(member_token),
    )

    assert response.status_code == 422
    alive = await session.execute(text("SELECT count(*) FROM messages"))
    assert alive.scalar_one() >= 0
