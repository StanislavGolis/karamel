"""Подготовка данных для очереди жалоб модератора (стори 8.3).

**Почему сид пишет жалобы напрямую, а не через сервис.** Проверяется ЧТЕНИЕ
очереди (8.3). Подача жалобы — закрытые 7.4/8.2, и их падение валило бы 8.3
не по делу. Прямой `INSERT` пишет ровно те поля, которые нужны очереди, и
`reported_at` задаётся значением: возраст жалобы — параметр сценария
(NFR52/NFR63), а не следствие момента прогона.

**Почему `reported_at` передаётся, а не берётся `now()`.** Тест порога
просрочки на границе 20 ч обязан управлять временем жалобы; `sleep`/`freezegun`
в проекте запрещены (`testing-rules.md`).

**Почему модератор и обычный пользователь — разные функции.** `403`-тест
краснеет ровно при снятии `require_moderator`, и различие между ними обязано
быть ЕДИНСТВЕННЫМ — `role`.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_message_body
from app.core.db import get_session
from app.main import app
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session
from tests.integration.report_helpers import make_company
from tests.integration.report_message_helpers import make_message_chat

__all__ = [
    "MODERATOR",
    "build_client",
    "cookie_headers",
    "get_queue",
    "make_message_report",
    "make_moderator",
    "make_no_show_report",
    "make_plain_user",
    "seed_messages",
]

#: Метка профиля для читаемости в тестах.
MODERATOR = UserRole.MODERATOR


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


def cookie_headers(token: str) -> dict[str, str]:
    """Заголовок аутентификации по access-cookie."""
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


async def _make_user(session: AsyncSession, *, role: str) -> tuple[User, str]:
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


async def make_moderator(session: AsyncSession) -> tuple[User, str]:
    """Модератор с согласием и access-токеном."""
    return await _make_user(session, role=str(UserRole.MODERATOR))


async def make_plain_user(session: AsyncSession) -> tuple[User, str]:
    """Обычный пользователь — для проверки `403` на гарде."""
    return await _make_user(session, role=str(UserRole.USER))


async def _insert_report(
    session: AsyncSession,
    *,
    reporter_id: int,
    target_type: str,
    reported_at: datetime,
    attendance_id: int | None = None,
    event_card_id: int | None = None,
    chat_id: int | None = None,
    status: str = "open",
) -> uuid_lib.UUID:
    """Единственный путь сида жалобы в этих тестах.

    `status` параметром, а не литералом: AC-04 требует доказать, что
    разобранные жалобы в очередь НЕ попадают, и тест обязан уметь создать
    `resolved_warning`/`dismissed` со всеми согласованными полями.
    """
    resolution = None
    resolved_at = None
    if status != "open":
        resolution = {
            "resolved_warning": "warning",
            "resolved_ban": "ban",
            "dismissed": "dismissed",
        }[status]
        resolved_at = reported_at + timedelta(hours=1)
    row = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, attendance_id, event_card_id, chat_id, "
            "target_type, status, resolution, resolved_at, reported_at) "
            "VALUES (:reporter, :attendance, :card, :chat, :target, :status, "
            ":resolution, :resolved_at, :reported_at) RETURNING uuid"
        ),
        {
            "reporter": reporter_id,
            "attendance": attendance_id,
            "card": event_card_id,
            "chat": chat_id,
            "target": target_type,
            "status": status,
            "resolution": resolution,
            "resolved_at": resolved_at,
            "reported_at": reported_at,
        },
    )
    return row.scalar_one()


async def make_no_show_report(
    session: AsyncSession,
    *,
    reported_at: datetime | None = None,
    pair_status: str = "no_show",
    status: str = "open",
) -> dict[str, Any]:
    """Компания с парой `attendance` + жалоба на неявку по этой паре.

    Возвращает `report_uuid`, `card_uuid`, `attendance_id`, `company`, чтобы
    тест мог сверить факты явки в очереди с фактическим состоянием пары.
    """
    company = await make_company(
        session, pair_status=pair_status, members=1, card_status="finished"
    )
    member = company["members"][0]
    attendance_id = company["attendance"][member["user"].id]
    report_uuid = await _insert_report(
        session,
        reporter_id=company["author"].id,
        target_type="no_show",
        reported_at=reported_at or datetime.now(UTC),
        attendance_id=attendance_id,
        event_card_id=company["card_id"],
        status=status,
    )
    await session.flush()
    return {
        "report_uuid": report_uuid,
        "card_uuid": company["card_uuid"],
        "attendance_id": attendance_id,
        "company": company,
    }


async def make_message_report(
    session: AsyncSession,
    *,
    reported_at: datetime | None = None,
    status: str = "open",
) -> dict[str, Any]:
    """Диалог + жалоба на переписку."""
    chat = await make_message_chat(session)
    chat_id_row = await session.execute(
        text("SELECT id FROM chats WHERE uuid = :u"), {"u": chat.chat_uuid}
    )
    chat_id = int(chat_id_row.scalar_one())
    report_uuid = await _insert_report(
        session,
        reporter_id=chat.author.id,
        target_type="message",
        reported_at=reported_at or datetime.now(UTC),
        chat_id=chat_id,
        status=status,
    )
    await session.flush()
    return {
        "report_uuid": report_uuid,
        "chat_uuid": chat.chat_uuid,
        "chat_id": chat_id,
        "chat": chat,
    }


async def seed_messages(
    session: AsyncSession, *, chat_id: int, sender_id: int, bodies: list[str]
) -> list[uuid_lib.UUID]:
    """Кладёт `bodies` в `messages` шифртекстом, соблюдая порядок.

    Порядок задаётся `created_at` со сдвигом в секунду: фрагмент сортирует по
    `created_at DESC, uuid DESC`, и одинаковые метки времени дали бы
    недетерминированный хвост — тест проверял бы сортировку БД, а не сервис.
    """
    base = datetime.now(UTC) - timedelta(hours=len(bodies))
    uuids: list[uuid_lib.UUID] = []
    for index, body in enumerate(bodies):
        message_uuid = uuid_lib.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, created_at) "
                "VALUES (:u, :c, :s, :b, :t)"
            ),
            {
                "u": message_uuid,
                "c": chat_id,
                "s": sender_id,
                "b": encrypt_message_body(
                    body, chat_id=chat_id, sender_id=sender_id, key=settings.chat_message_key
                ),
                "t": base + timedelta(seconds=index),
            },
        )
        uuids.append(message_uuid)
    await session.flush()
    return uuids


async def get_queue(client: AsyncClient, *, token: str, **params: Any) -> Any:
    """`GET /api/v1/moderation/reports` с cookie-аутентификацией."""
    return await client.get(
        "/api/v1/moderation/reports", params=params, headers=cookie_headers(token)
    )
