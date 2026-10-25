"""Подготовка данных для тестов истории явок (стори 7.5).

**Почему компания из ТРЁХ человек** (урок CR 7.2, AC-11): на одном участнике
«история пользователя» неотличима от «истории карточки», и дефект адресности
(`participant_id` подменён на `author_id` или на `id` карточки) прошёл бы
незамеченным. Третий человек — тот, чья история НЕ должна попасть в ответ,
и именно он ловит ошибку предиката.

**Почему `created_at` задаётся данными.** Порядок истории —
`created_at DESC, card_id DESC` (AC-07, AC-08), поэтому тест tie-break обязан
создать строки с ОДИНАКОВЫМ `created_at`. `now()` из `server_default` такой
возможности не даёт, а `sleep` запрещён (`testing-rules.md`), поэтому метка
пишется явно.

**Почему статус пары задаётся данными, а не вызовом сервиса 7.2.**
`attendance.status` — производное от флагов (таблица истинности 7.1), и сид
обязан быть консистентным по `ck_attendance_status_consistency`. Собирать
`disputed` через `confirm_attendance` значило бы зависеть от чужого кода в
тесте истории: падение 7.2 валило бы 7.5 не по делу.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session
from tests.integration.report_helpers import PAIR_FLAGS

__all__ = [
    "PAIR_FLAGS",
    "add_attendance",
    "build_client",
    "cookie_headers",
    "make_card",
    "make_user",
]


@asynccontextmanager
async def build_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """ASGI-клиент на тестовой сессии (SAVEPOINT-изоляция, §18.8)."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        app.dependency_overrides.pop(get_session, None)


async def make_user(session: AsyncSession, *, role: str = "user") -> tuple[User, str]:
    """Пользователь с согласием и access-токеном (`role='moderator'` — для гарда)."""
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role=role,
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


async def make_card(
    session: AsyncSession,
    *,
    author_id: int,
    title: str = "Встреча",
    starts_at: datetime | None = None,
) -> tuple[uuid_lib.UUID, int]:
    """Карточка события в состоянии `full` (стабильная форма для числа SQL)."""
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
    )
    place_id = int(place.scalar_one())
    moment = starts_at if starts_at is not None else datetime.now(UTC) - timedelta(hours=1)
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:a, :p, :s, :t, NULL, 'cafe', :st, 2, 60, :e, 'full', 2, 'approved', :pub) "
            "RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_community_slug,
            "t": title,
            "st": moment,
            "e": moment - timedelta(hours=1),
            "pub": moment - timedelta(days=1),
        },
    )
    card_uuid, card_id = card.one()
    return card_uuid, int(card_id)


async def add_attendance(
    session: AsyncSession,
    *,
    card_id: int,
    participant_id: int,
    status: str = "no_show",
    created_at: datetime | None = None,
) -> int:
    """Строка факта явки с КОНСИСТЕНТНЫМИ флагами и явным `created_at`."""
    author_flag, participant_flag, _ = PAIR_FLAGS[status]
    confirmed_at = None if status == "pending" else datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
            "participant_confirmed, status, confirmed_at, created_at) "
            "VALUES (:c, :p, :af, :pf, :s, :ca, COALESCE(:cr, now())) RETURNING id"
        ),
        {
            "c": card_id,
            "p": participant_id,
            "af": author_flag,
            "pf": participant_flag,
            "s": status,
            "ca": confirmed_at,
            "cr": created_at,
        },
    )
    return int(row.scalar_one())
