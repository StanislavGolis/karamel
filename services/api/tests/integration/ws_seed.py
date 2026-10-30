"""Посев данных для WS-тестов (стори 6.5).

**Почему посев идёт через `second_session`, а не через `session`.** WS-тесты
идут под `starlette.testclient.TestClient`: приложение поднимается в
отдельном потоке со своим event loop и открывает СВОИ соединения к БД
(`get_session_factory`). Данные, записанные в savepoint-сессию теста,
приложению не видны вообще — это доказано в Chore P-6 (savepoint-commit
даёт 0 строк снаружи, реальный commit — 1).

**Почему без DDL.** Первая попытка P-6 создавала служебную таблицу и уронила
`tests/unit/test_alembic_revisions.py::test_alembic_check_reports_no_pending_operations`
(«Detected removed table»): `unit` идёт после `integration`, а
session-teardown ещё не выполнен. Здесь используются только доменные
таблицы, которые уже есть в моделях и в TRUNCATE-списке `clean_database`.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import issue_session

__all__ = ["WsCompany", "seed_company", "seed_stranger"]


@dataclass(frozen=True)
class WsCompany:
    """Собранная компания: автор и участник, чат открыт.

    `*_token` — access-токены для cookie `karamel_access` (WS авторизуется
    только cookie, токен в query отвергается).
    """

    author: User
    author_token: str
    member: User
    member_token: str
    stranger: User
    stranger_token: str
    card_uuid: str
    card_id: int
    chat_uuid: str

    @property
    def chat_uuid_obj(self) -> uuid_lib.UUID:
        return uuid_lib.UUID(self.chat_uuid)


async def _create_user(session: AsyncSession) -> tuple[User, str]:
    """Пользователь с согласием и выданной сессией (копия хелпера 6.4).

    Копия, а не импорт из `test_chat_messages_api`: импорт тест-модуля в
    тест-модуль тянет его фикстуры и делает два набора связанными — падение
    в одном ломает другой по причине, к нему не относящейся.
    """
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
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


async def seed_company(session: AsyncSession, *, capacity: int = 2) -> WsCompany:
    """Создаёт автора, участника, постороннего, карточку, чат и заявку.

    `capacity=2` — минимальная компания: автор + один `selected`. Посторонний
    нужен для проверки отказа (AC-06): он существует, у него валидная сессия,
    и единственное отличие от участника — отсутствие заявки.
    """
    author, author_token = await _create_user(session)
    member, member_token = await _create_user(session)
    stranger, stranger_token = await _create_user(session)

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
            "'cafe', :st, :cap, 60, :e, 'full', :cap, 'approved', :pub, :chat,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "a": author.id,
            "p": place_id,
            "s": settings.default_city_slug,
            "st": starts,
            "cap": capacity,
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
    # Реальный коммит: без него приложение под TestClient не увидит ни чата,
    # ни участия (см. модульный докстринг).
    await session.commit()

    return WsCompany(
        author=author,
        author_token=author_token,
        member=member,
        member_token=member_token,
        stranger=stranger,
        stranger_token=stranger_token,
        card_uuid=str(card_uuid),
        card_id=int(card_id),
        chat_uuid=chat_uuid,
    )


async def seed_stranger(session: AsyncSession) -> tuple[User, str]:
    """Отдельный пользователь без участия — для проверок отказа."""
    user, token = await _create_user(session)
    await session.commit()
    return user, token
