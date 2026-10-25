"""Подготовка данных для решения по жалобе (стори 8.4).

**Почему сид жалобы пишется напрямую, а не через сервисы 7.4/8.2.** Проверяется
РЕШЕНИЕ (8.4). Подача жалобы закрыта и покрыта своими тестами; её падение
валило бы 8.4 не по делу. Прямой `INSERT` пишет ровно те поля, которые нужны
решению, и позволяет задать `attendance.status` (`disputed`/`confirmed`),
которое через сервис не выставить.

**Почему `reason` — константа.** Причина обязана дойти до двух таблиц и до
`payload` уведомления; сравнение с литералом в тесте ловит потерю значения,
а не его пересборку.

**Почему модератор и обычный пользователь — разные функции** (как в 8.3):
`403`-тест краснеет ровно при снятии `require_moderator`, и единственное
различие между ними обязано быть `role`.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session
from tests.integration.report_helpers import make_company
from tests.integration.report_message_helpers import make_message_chat

__all__ = [
    "REASON",
    "build_client",
    "cookie_headers",
    "dismiss_report",
    "get_user_status",
    "make_message_report",
    "make_moderator",
    "make_no_show_report",
    "make_plain_user",
    "post_resolve",
    "resolve_payload",
]

#: Причина решения: одна строка на все проверки «дошло ли значение».
REASON = "Оскорбления в переписке"


@asynccontextmanager
async def build_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """ASGI-клиент на тестовой сессии (SAVEPOINT-изоляция, §18.8).

    Сессия подменяется целиком: `resolve_report` пишет в `reports`, `sanctions`,
    `users` и `notifications`, и тест обязан видеть результаты ВСЕХ записей
    из той же транзакции, что и роут.
    """

    # **Почему `commit` подменяется на `flush`.** Роут решения коммитит
    # (ADR-11: публикация после фиксации). На тестовой сессии этот `commit`
    # закрыл бы ВНЕШНЮЮ транзакцию теста и вынес наружу весь сид — `places`,
    # `users`, `event_cards` из соседних файлов. Проверено: после прогона
    # тестов 8.4 `test_places_schema` падал на `assert 10 == 8` (две чужие
    # строки в `places`), изолированно — 31 passed. `flush` сохраняет
    # SAVEPOINT-изоляцию §18.8 и оставляет данные видимыми внутри теста.
    original_commit = session.commit

    async def _flush_instead_of_commit() -> None:
        await session.flush()

    session.commit = _flush_instead_of_commit  # type: ignore[method-assign]

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        session.commit = original_commit  # type: ignore[method-assign]


def cookie_headers(token: str) -> dict[str, str]:
    """Заголовок аутентификации по access-cookie."""
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


async def _make_user(session: AsyncSession, *, role: str) -> tuple[User, str]:
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
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


def resolve_payload(*, resolution: str = "warning", reason: str = REASON) -> dict[str, str]:
    """Тело запроса решения."""
    return {"resolution": resolution, "reason": reason}


async def post_resolve(
    client: AsyncClient,
    *,
    token: str,
    report_uuid: uuid_lib.UUID | str,
    resolution: str = "warning",
    reason: str = REASON,
) -> Response:
    """`POST /api/v1/moderation/reports/{uuid}/resolve` с cookie-аутентификацией."""
    return await client.post(
        f"/api/v1/moderation/reports/{report_uuid}/resolve",
        json=resolve_payload(resolution=resolution, reason=reason),
        headers=cookie_headers(token),
    )


async def dismiss_report(
    client: AsyncClient, *, token: str, report_uuid: uuid_lib.UUID | str
) -> Response:
    """Сокращение для самого частого исхода без санкции."""
    return await post_resolve(client, token=token, report_uuid=report_uuid, resolution="dismissed")


async def get_user_status(session: AsyncSession, *, user_id: int) -> str:
    """Статус учётной записи — читается из БД, а не из объекта `User`.

    Объект мог быть загружен ДО решения и остаться в identity map старым;
    проверять надо фактическую строку (иначе тест зелёный при снятом `UPDATE`).
    """
    result = await session.execute(text("SELECT status FROM users WHERE id = :id"), {"id": user_id})
    return str(result.scalar_one())


async def _insert_report(
    session: AsyncSession,
    *,
    reporter_id: int,
    target_type: str,
    attendance_id: int | None = None,
    event_card_id: int | None = None,
    chat_id: int | None = None,
) -> uuid_lib.UUID:
    """Жалоба в состоянии `open` — единственное, что решает 8.4."""
    row = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, attendance_id, event_card_id, chat_id, "
            "target_type, status, reported_at) "
            "VALUES (:reporter, :attendance, :card, :chat, :target, 'open', now()) "
            "RETURNING uuid"
        ),
        {
            "reporter": reporter_id,
            "attendance": attendance_id,
            "card": event_card_id,
            "chat": chat_id,
            "target": target_type,
        },
    )
    return row.scalar_one()


async def make_no_show_report(
    session: AsyncSession, *, pair_status: str = "no_show", accused_id: int | None = None
) -> dict[str, Any]:
    """Компания с парой `attendance` + жалоба на неявку от АВТОРА карточки.

    Жалобщик — автор: тогда обвиняемый — участник пары, и подмена адресата
    (мутация D-57) сразу видна по `sanctions.user_id`.

    **`accused_id`** подменяет участника пары. Нужен ровно одному сценарию —
    «вторая санкция не понижает действующий бан» (D-49): обвиняемый обязан
    быть ТЕМ ЖЕ пользователем, а компания создаётся заново (жёлтая пара
    уникальна по `(card_id, participant_id)`, повторно ту же не переиспользовать).
    """
    company = await make_company(
        session, pair_status=pair_status, members=1, card_status="finished"
    )
    member = company["members"][0]
    attendance_id = company["attendance"][member["user"].id]
    if accused_id is not None:
        # Подмена участника пары: жалобщик остаётся автором, обвиняемый —
        # заданный пользователь. Консистентность компании здесь не проверяется
        # намеренно: сценарий про статус учётной записи, а не про состав.
        await session.execute(
            text("UPDATE attendance SET participant_id = :p WHERE id = :a"),
            {"p": accused_id, "a": attendance_id},
        )
        member = {"user": type("U", (), {"id": accused_id, "uuid": None})()}
    report_uuid = await _insert_report(
        session,
        reporter_id=company["author"].id,
        target_type="no_show",
        attendance_id=attendance_id,
        event_card_id=company["card_id"],
    )
    await session.flush()
    return {
        "report_uuid": report_uuid,
        "card_uuid": company["card_uuid"],
        "attendance_id": attendance_id,
        "reporter": company["author"],
        "accused": member["user"],
        "company": company,
    }


async def make_message_report(session: AsyncSession) -> dict[str, Any]:
    """Диалог + жалоба на переписку от АВТОРА карточки.

    Обвиняемый — выбранный участник. Второй тип жалобы нужен, чтобы D-57
    (вычисление адресата) проверялся на ОБЕИХ ветвях: одна ветвь прошла бы и
    при жёстко зашитом «участник пары».
    """
    chat = await make_message_chat(session)
    chat_id_row = await session.execute(
        text("SELECT id FROM chats WHERE uuid = :u"), {"u": chat.chat_uuid}
    )
    chat_id = int(chat_id_row.scalar_one())
    report_uuid = await _insert_report(
        session, reporter_id=chat.author.id, target_type="message", chat_id=chat_id
    )
    await session.flush()
    return {
        "report_uuid": report_uuid,
        "chat_uuid": chat.chat_uuid,
        "chat_id": chat_id,
        "reporter": chat.author,
        "accused": chat.member,
        "chat": chat,
    }
