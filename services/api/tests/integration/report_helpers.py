"""Общая подготовка данных для тестов жалобы на неявку (стори 7.4).

**Почему компания из ТРЁХ человек** (урок CR 7.2): на одном участнике
«жалоба на пару» неотличима от «жалобы на карточку», и дефект адресности
(жалоба на чужую пару той же карточки) прошёл бы незамеченным. Третий
человек — тот, на кого жаловаться НЕЛЬЗЯ, и именно он ловит ошибку
вычисления `participant_id`.

**Почему статус пары задаётся данными, а не вызовом сервиса.**
`attendance.status` — производное от флагов (таблица истинности 7.1), и
сид обязан быть консистентным по `ck_attendance_status_consistency`.
Собирать `disputed` через `confirm_attendance` значило бы зависеть от
чужого кода в тесте жалобы: падение 7.2 валило бы 7.4 не по делу.

**Время — инъекцией `now`** (`testing-rules.md`): `freezegun`/`sleep` запрещены.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

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
    "PAIR_FLAGS",
    "ReportSettings",
    "build_client",
    "cookie_headers",
    "make_company",
    "make_user",
    "post_report",
]

#: Консистентные наборы флагов для каждого статуса пары (инвариант 7.1):
#: `(author_confirmed, participant_confirmed, status)`. Метка `confirmed_at`
#: выводится из статуса бикондиционалом `pending ⟺ confirmed_at IS NULL`.
PAIR_FLAGS: dict[str, tuple[bool | None, bool | None, str]] = {
    "pending": (None, None, "pending"),
    "no_show": (False, False, "no_show"),
    "disputed": (True, False, "disputed"),
    "confirmed": (True, True, "confirmed"),
}


class ReportSettings:
    """Минимум настроек `AttendanceSettings` без чтения `.env`.

    Поля обязаны совпадать с протоколом ЦЕЛИКОМ: `get_card` читает не только
    срез сообщества, но и окно решения, лимит карточек и стоп-слова. Урезанная
    заглушка падала бы `AttributeError` на живом коде — тест проверял бы себя.
    """

    default_community_slug = "tomsk"
    community_slugs = ("tomsk",)
    card_default_decision_window_minutes = 60
    card_content_stop_words: tuple[str, ...] = ()
    card_active_limit = 5


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


async def make_user(session: AsyncSession) -> tuple[User, str]:
    """Пользователь с согласием (нужен `ConsentUser`) и access-токеном."""
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
    return user, issued.access_token


def cookie_headers(token: str) -> dict[str, str]:
    """Заголовок аутентификации по access-cookie."""
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


async def make_company(
    session: AsyncSession,
    *,
    pair_status: str = "no_show",
    members: int = 2,
    card_status: str = "full",
    starts_in: timedelta = timedelta(hours=-1),
) -> dict[str, Any]:
    """Автор + `members` участников + карточка + по строке `attendance` на каждого.

    `card_status='full'` по умолчанию намеренно: `get_card` лениво закрывает
    окно только у `published`, поэтому число SQL не плавает и анти-оракул
    проверяется на стабильной форме.

    Возвращает `author`/`author_token`, список `members` (`user` + `token`),
    `card_uuid`/`card_id` и `attendance` — `{user_id: attendance_id}`.
    """
    author, author_token = await make_user(session)

    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
    )
    place_id = int(place.scalar_one())

    starts_at = datetime.now(UTC) + starts_in
    capacity = members + 1
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:a, :p, :s, 'Встреча', NULL, 'cafe', :st, :cap, 60, :e, :status, "
            ":filled, 'approved', :pub) RETURNING uuid, id"
        ),
        {
            "a": author.id,
            "p": place_id,
            "s": settings.default_community_slug,
            "st": starts_at,
            "cap": capacity,
            "e": starts_at - timedelta(hours=1),
            "status": card_status,
            "filled": capacity,
            "pub": starts_at - timedelta(days=1),
        },
    )
    card_uuid, card_id = card.one()

    author_flag, participant_flag, status = PAIR_FLAGS[pair_status]
    confirmed_at = None if status == "pending" else datetime.now(UTC)

    people: list[dict[str, Any]] = []
    attendance: dict[int, int] = {}
    for _ in range(members):
        member, member_token = await make_user(session)
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
                "expires_at, decided_at) "
                "VALUES (:c, :a, '', 'selected', now(), now() + interval '1 day', now())"
            ),
            {"c": int(card_id), "a": member.id},
        )
        row = await session.execute(
            text(
                "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
                "participant_confirmed, status, confirmed_at) "
                "VALUES (:c, :p, :af, :pf, :s, :ca) RETURNING id"
            ),
            {
                "c": int(card_id),
                "p": member.id,
                "af": author_flag,
                "pf": participant_flag,
                "s": status,
                "ca": confirmed_at,
            },
        )
        attendance[member.id] = int(row.scalar_one())
        people.append({"user": member, "token": member_token})

    await session.flush()

    return {
        "author": author,
        "author_token": author_token,
        "members": people,
        "card_uuid": str(card_uuid),
        "card_id": int(card_id),
        "starts_at": starts_at,
        "attendance": attendance,
    }


async def post_report(
    client: AsyncClient, company: dict[str, Any], *, token: str, accused_uuid: Any
) -> Response:
    """`POST /cards/{uuid}/report-no-show` с cookie-аутентификацией."""
    return await client.post(
        f"/api/v1/cards/{company['card_uuid']}/report-no-show",
        json={"accused_uuid": str(accused_uuid)},
        headers=cookie_headers(token),
    )
