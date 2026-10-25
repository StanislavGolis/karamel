"""Подготовка данных для карточки пользователя (стори 8.5).

**Почему сид пишет жалобы и санкции напрямую.** Проверяется ЧТЕНИЕ сводки.
Подача закрыта и покрыта 7.4/8.2, решение — 8.4; их падение валило бы 8.5 не
по делу. Прямой `INSERT` пишет ровно нужные поля и позволяет задать
`accused_id`, `created_at` и `moderator_id = NULL` — состояния, которых через
сервисы не получить.

**Почему `message`-ветвь, а не `no_show`.** `target_scope` (8.1, D-15)
требует для `no_show` ОБА `attendance_id` и `event_card_id`, а уникальность
`UNIQUE(attendance_id, reporter_id)` не даёт двум жалобам одного жалобщика
жить на одной паре. `message`-ветвь требует только `chat_id`, и её
уникальность — `(chat_id, reporter_id)`, поэтому каждая жалоба получает СВОЙ
чат. Пара «карточка + чат» на жалобу — цена соблюдения схемы, а не удобство.

**Почему модератор и обычный пользователь — разные функции.** `403`-тест
краснеет ровно при снятии `require_moderator`, и единственное различие между
ними обязано быть `role`.

**Почему `make_target_user` отдельно от `make_moderator`.** Карточка читает
ОБЫЧНОГО пользователя; смешать роли значило бы проверять не тот объект.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

__all__ = [
    "add_attendance",
    "add_reports",
    "add_sanctions",
    "build_client",
    "cookie_headers",
    "get_card",
    "make_moderator",
    "make_target_user",
    "set_profile",
    "set_user_status",
]


@asynccontextmanager
async def build_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """ASGI-клиент на тестовой сессии (SAVEPOINT-изоляция, §18.8).

    `commit` подменяется на `flush` — как в `moderation_resolve_helpers`
    (D-59): карточка только читает, но общий приём держит изоляцию, если
    маршрут когда-нибудь начнёт писать (аудит доступа из 8.6).
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    original_commit = session.commit

    async def _flush_instead_of_commit() -> None:
        await session.flush()

    session.commit = _flush_instead_of_commit  # type: ignore[method-assign]
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
    phone = f"+7910{uuid_lib.uuid4().int % 10**7:07d}"
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
    """Модератор — единственная роль, которой карточка доступна."""
    return await _make_user(session, role=str(UserRole.MODERATOR))


async def make_target_user(session: AsyncSession) -> tuple[User, str]:
    """Пользователь, чью карточку читают. Обычная роль."""
    return await _make_user(session, role=str(UserRole.USER))


async def set_profile(
    session: AsyncSession, *, user: User, name: str = "Анна", city: str = "Москва", age: int = 28
) -> None:
    """Заполненная анкета (AC-05: имя, город, возраст в карточке).

    `status='complete'` — инвариант модели: черновик требует пустых полей.
    """
    # `bio` обязателен: `ck_profiles_completeness` требует
    # `num_nulls(name, city, age, bio) = 0` при `status='complete'`. Без него
    # сид падает `CheckViolationError` — проверено прогоном.
    await session.execute(
        text(
            "INSERT INTO profiles (uuid, user_id, name, city, age, bio, gender, status) "
            "VALUES (:u, :uid, :n, :c, :a, :bio, 'female', 'complete')"
        ),
        {
            "u": uuid_lib.uuid4(),
            "uid": user.id,
            "n": name,
            "c": city,
            "a": age,
            "bio": "Люблю горы и кофе",
        },
    )
    await session.flush()


async def set_user_status(session: AsyncSession, *, user: User, status: str) -> None:
    """Статус учётной записи (`active`/`warned`/`banned`) для AC-09."""
    await session.execute(
        text("UPDATE users SET status = :s WHERE id = :i"), {"s": status, "i": user.id}
    )
    await session.flush()


async def _place_id(session: AsyncSession) -> int:
    """Место для карточек-хозяев чатов.

    Создаётся при отсутствии: в чистой тестовой БД `places` пуста, а
    `event_cards.place_id` — обязательный FK.
    """
    existing = await session.execute(text("SELECT id FROM places LIMIT 1"))
    found = existing.scalar_one_or_none()
    if found is not None:
        return int(found)
    created = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
    )
    return int(created.scalar_one())


async def _message_report(
    session: AsyncSession, *, reporter_id: int, accused_id: int, reported_at: datetime
) -> uuid_lib.UUID:
    """Одна жалоба на переписку со СВОИМ чатом.

    Чат нужен, потому что `target_scope` (D-15) требует `chat_id` у
    `message`-жалобы, а уникальность `(chat_id, reporter_id)` не пустила бы
    две жалобы одного жалобщика в один чат. Свой чат на жалобу снимает оба
    ограничения сразу.
    """
    place_id = await _place_id(session)
    chat_uuid = uuid_lib.uuid4()
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, chat_uuid) "
            "VALUES (:a, :p, :s, 'Встреча', 'cafe', :st, 2, 60, :e, 'full', 2, 'approved', "
            "        :pub, :chat) RETURNING id"
        ),
        {
            "a": reporter_id,
            "p": place_id,
            "s": settings.default_community_slug,
            "st": reported_at,
            "e": reported_at - timedelta(hours=1),
            "pub": reported_at - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_id = int(card.scalar_one())
    chat = await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (:u, :c) RETURNING id"),
        {"u": chat_uuid, "c": card_id},
    )
    chat_id = int(chat.scalar_one())
    report = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, accused_id, chat_id, target_type, status, "
            "reported_at) VALUES (:r, :a, :chat, 'message', 'open', :t) RETURNING uuid"
        ),
        {"r": reporter_id, "a": accused_id, "chat": chat_id, "t": reported_at},
    )
    return report.scalar_one()


async def add_reports(
    session: AsyncSession,
    *,
    user: User,
    filed: int = 0,
    against: int = 0,
    other_id: int | None = None,
) -> dict[str, list[uuid_lib.UUID]]:
    """Жалобы по двум осям: поданные пользователем и поданные на него.

    `reported_at` сдвигается на секунды по возрастанию: жалобы создаются от
    старых к новым, поэтому ответ «новые сверху» — это РЕВЕРС списка
    создания. Одинаковые метки не годятся: порядок тогда определял бы `uuid`,
    и тест проверял бы сортировку БД, а не сервиса.

    `other_id` — вторая сторона для жалоб пользователя; по умолчанию сам
    пользователь (для тестов, где важен только счётчик, а не адресат).
    """
    base = datetime.now(UTC) - timedelta(hours=1)
    filed_uuids: list[uuid_lib.UUID] = []
    against_uuids: list[uuid_lib.UUID] = []
    counterpart = other_id if other_id is not None else user.id

    for index in range(filed):
        filed_uuids.append(
            await _message_report(
                session,
                reporter_id=user.id,
                accused_id=counterpart,
                reported_at=base + timedelta(seconds=index),
            )
        )

    for index in range(against):
        against_uuids.append(
            await _message_report(
                session,
                reporter_id=counterpart,
                accused_id=user.id,
                reported_at=base + timedelta(minutes=30, seconds=index),
            )
        )

    await session.flush()
    return {"filed": filed_uuids, "against": against_uuids}


async def add_sanctions(
    session: AsyncSession,
    *,
    user: User,
    count: int = 1,
    moderator_id: int | None = None,
    reporter_id: int | None = None,
    reason: str = "Причина санкции",
) -> list[uuid_lib.UUID]:
    """Санкции пользователю. `moderator_id=None` — «автор удалён» (AC-07).

    Каждая санкция требует своей жалобы: `UNIQUE(report_id)` (D-17).

    **`reporter_id` — ОТДЕЛЬНЫЙ человек, не сам наказанный.** Жалоба-основание
    подана кем-то другим: иначе эти жалобы попали бы в `reports.filed`
    наказанного и раздули бы счётчик «сколько он пожаловался» — проверено
    прогоном (`filed_total` дал 55 вместо 30). По умолчанию жалобщик — сам
    пользователь только для сценариев, где ось `filed` не проверяется.
    """
    base = datetime.now(UTC) - timedelta(hours=2)
    reporter = user.id if reporter_id is None else reporter_id
    uuids: list[uuid_lib.UUID] = []
    for index in range(count):
        moment = base + timedelta(seconds=index)
        report_uuid = await _message_report(
            session, reporter_id=reporter, accused_id=user.id, reported_at=moment
        )
        # Жалоба сразу разобрана — иначе `status_resolution_consistency`
        # отвергнет `resolution` при `status='open'`.
        await session.execute(
            text(
                "UPDATE reports SET status = 'resolved_warning', resolution = 'warning', "
                "       reason = :reason, resolved_at = :t WHERE uuid = :u"
            ),
            {"reason": reason, "t": moment, "u": report_uuid},
        )
        row = await session.execute(
            text("SELECT id FROM reports WHERE uuid = :u"),
            {"u": report_uuid},
        )
        report_id = int(row.scalar_one())
        created = await session.execute(
            text(
                "INSERT INTO sanctions (user_id, report_id, moderator_id, type, reason, "
                "created_at) "
                "VALUES (:u, :rep, :mod, 'warning', :reason, :t) RETURNING uuid"
            ),
            {
                "u": user.id,
                "rep": report_id,
                "mod": moderator_id,
                "reason": reason,
                "t": moment,
            },
        )
        uuids.append(created.scalar_one())
    await session.flush()
    return uuids


async def add_attendance(
    session: AsyncSession, *, user: User, count: int = 1, status: str = "confirmed"
) -> int:
    """Факты явки, где пользователь — участник. Возвращает полное число."""
    place_id = await _place_id(session)
    for index in range(count):
        starts = datetime.now(UTC) - timedelta(days=index + 1)
        card = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, category, "
                "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
                "verification_status, published_at, chat_uuid) "
                "VALUES (:a, :p, :s, :title, 'cafe', :st, 2, 60, :st, 'full', 2, 'approved', "
                "        :st, :chat) RETURNING id"
            ),
            {
                "a": user.id,
                "p": place_id,
                "s": settings.default_community_slug,
                "title": f"Встреча {index}",
                "st": starts,
                "chat": uuid_lib.uuid4(),
            },
        )
        card_id = int(card.scalar_one())
        # Флаги обязаны соответствовать статусу: `ck_attendance_status_consistency`
        # отвергает `confirmed` без обоих `true` (проверено прогоном).
        # `status` — ПРОИЗВОДНОЕ от пары флагов (7.1), а не независимое поле.
        flags = {
            "confirmed": (True, True),
            "no_show": (False, False),
            "disputed": (True, False),
            "pending": (None, None),
        }[status]
        await session.execute(
            text(
                "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
                "participant_confirmed, status, confirmed_at) "
                "VALUES (:c, :p, :a, :pc, :s, :ts)"
            ),
            {
                "c": card_id,
                "p": user.id,
                "a": flags[0],
                "pc": flags[1],
                "s": status,
                # `confirmed_at` — бикондиционал: `NULL` <=> `pending`.
                "ts": None if status == "pending" else datetime.now(UTC),
            },
        )
    await session.flush()
    total = await session.execute(
        text("SELECT count(*) FROM attendance WHERE participant_id = :p"), {"p": user.id}
    )
    return int(total.scalar_one())


async def get_card(client: AsyncClient, *, token: str, user_uuid: uuid_lib.UUID | str) -> Response:
    """`GET /api/v1/moderation/users/{uuid}` с cookie-аутентификацией."""
    return await client.get(f"/api/v1/moderation/users/{user_uuid}", headers=cookie_headers(token))
