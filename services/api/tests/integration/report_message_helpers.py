"""Подготовка данных для тестов жалобы на диалог (стори 8.2).

**Почему компания из ДВУХ человек.** FR37 говорит «у обеих сторон»: третий
человек не нужен, но и лишним не был бы — он ловит ошибку вычисления
адресата. Оставлено два: жалоба адресуется собеседнику, а компания 2–6
(ADR-04); проверка «не на себя» покрыта отдельным тестом.

**Почему `chats.status` выставляется данными, а не вызовом сервиса.**
Скрытие — часть проверяемого поведения; собирать его через тот же сервис
значило бы проверять сервис его же кодом. Сид пишет состояние напрямую,
как это делает `reports_migration_cycle`.

**Время — инъекцией `now`** (`testing-rules.md`): `freezegun`/`sleep`
запрещены.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

__all__ = [
    "MessageChat",
    "cookie_headers",
    "make_client",
    "make_message_chat",
    "make_user",
    "post_report",
]


@asynccontextmanager
async def make_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """ASGI-клиент на тестовой сессии (SAVEPOINT-изоляция, §18.8)."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


async def make_user(session: AsyncSession) -> tuple[User, str]:
    """Пользователь с согласием (нужен `ConsentUser`) и access-токеном."""
    phone = f"+7901{uuid_lib.uuid4().int % 10**7:07d}"
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


def cookie_headers(token: str) -> dict[str, str]:
    """Заголовок аутентификации по access-cookie."""
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


@dataclass
class MessageChat:
    """Компания с открытым диалогом: автор и выбранный участник."""

    author: User
    author_token: str
    member: User
    member_token: str
    card_uuid: str
    card_id: int
    chat_uuid: str


async def make_message_chat(session: AsyncSession) -> MessageChat:
    """Автор + участник + карточка + чат + `join_requests.selected`."""
    author, author_token = await make_user(session)
    member, member_token = await make_user(session)

    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_id = int(place.scalar_one())

    starts = datetime.now(UTC) + timedelta(days=3)
    chat_uuid = str(uuid_lib.uuid4())
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, chat_uuid,"
            "kind, activity) VALUES (:a, :p, :s, 'Встреча', "
            "'cafe', :st, 2, 60, :e, 'full', 2, 'approved', :pub, :chat,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "a": author.id,
            "p": place_id,
            "s": settings.default_city_slug,
            "st": starts,
            "e": starts - timedelta(hours=1),
            "pub": starts - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_uuid, card_id = card.one()

    await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (:u, :c)"),
        {"u": chat_uuid, "c": int(card_id)},
    )
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:c, :a, '', 'selected', now(), "
            "now() + interval '1 day', now())"
        ),
        {"c": int(card_id), "a": member.id},
    )
    await session.flush()

    return MessageChat(
        author=author,
        author_token=author_token,
        member=member,
        member_token=member_token,
        card_uuid=str(card_uuid),
        card_id=int(card_id),
        chat_uuid=chat_uuid,
    )


async def post_report(
    client: AsyncClient,
    *,
    token: str,
    chat_uuid: str,
    accused_uuid: uuid_lib.UUID | str,
    target_type: str = "message",
) -> Response:
    """`POST /api/v1/reports` с cookie-аутентификацией."""
    return await client.post(
        "/api/v1/reports",
        json={
            "target_type": target_type,
            "chat_uuid": chat_uuid,
            "accused_uuid": str(accused_uuid),
        },
        headers=cookie_headers(token),
    )
