"""Общие хелперы integration-тестов стори 7.3 (запрос подтверждения явки).

Почему отдельный модуль, а не копия в каждом файле: сценариев S01–S49 + A01–A08
шесть десятков, и они живут в пяти файлах. Копия фабрики/сидов в каждом дала
бы пять расходящихся представлений о «правильной» фикстуре — а именно
расхождение фикстур (один участник вместо трёх) TD 7.3 называет причиной
пяти HIGH, пропущенных в 7.2.

**Минимальная фикстура адресности — 1 автор + 3 участника** (R15, score 9):
на одном участнике `UNION`/`UNION ALL`, `participant_id <> author_id` и
«автор ровно один раз» неразличимы.

Коммиты здесь НАСТОЯЩИЕ (собственный движок), поэтому строки убираются
фикстурой `_cleanup_seed` в порядке, обратном FK-зависимостям. `session`-
фикстура для этих тестов не годится: она живёт в SAVEPOINT, и `commit`
внутри неё не виден из второго соединения (§18.3, R17).
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository

#: X-7.1-6: глобальные `count(*)` в тестах хрупки. Считаем только по своим
#: фикстурам, а уборка идёт по спискам созданных id.
_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


def database_url() -> str:
    import os

    return os.environ.get("DATABASE_URL", "")


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие, соединения разные."""
    engine = create_async_engine(database_url(), pool_size=8, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Удаляет созданные тестом строки в порядке, обратном FK-зависимостям."""
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_USERS and not _CREATED_PLACES:
        return
    async with factory() as session:
        if _CREATED_USERS:
            # Стори 7.3: уведомления адресные, `card_id` в них нет — убираем
            # по адресатам своих фикстур (payload->>'card_uuid' недоступен,
            # потому что карточки удаляются ниже). Порядок: notifications →
            # attendance → join_requests → event_cards → users.
            await session.execute(
                text("DELETE FROM notifications WHERE user_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text(
                    "DELETE FROM attendance WHERE card_id IN "
                    "(SELECT id FROM event_cards WHERE author_id = ANY(:ids))"
                ),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text(
                    "DELETE FROM join_requests WHERE card_id IN "
                    "(SELECT id FROM event_cards WHERE author_id = ANY(:ids))"
                ),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text("DELETE FROM event_cards WHERE author_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
        if _CREATED_PLACES:
            await session.execute(
                text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
            )
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
            )
        await session.commit()


# ——— помощники ————————————————————————————————————————————————————————————


async def new_user(session: AsyncSession) -> User:
    """Пользователь с уникальным телефоном (номер не участвует в логике теста)."""
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    return user


async def make_place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    place_id = int(row.scalar_one())
    _CREATED_PLACES.append(place_id)
    return place_id


async def insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    starts_at: datetime,
    status: str = "finished",
    capacity: int = 4,
    filled_count: int = 3,
    chat_uuid: uuid_lib.UUID | None = None,
    prompted_at: datetime | None = None,
) -> tuple[str, int]:
    """Карточка с ЯВНЫМ `starts_at` и `status`: границы задаются точно.

    `expires_at` всегда в прошлом: окно решения закрыто, карточка состоялась.
    """
    moment = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid, attendance_prompted_at,"
            "kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at, :chat, :prompted,"
            "'company', 'coffee') "
            "RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts_at,
            "capacity": capacity,
            "expires": min(starts_at - timedelta(minutes=5), moment - timedelta(minutes=1)),
            "status": status,
            "filled": filled_count,
            "published_at": starts_at - timedelta(days=1),
            "chat": chat_uuid,
            "prompted": prompted_at,
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, status: str = "selected"
) -> int:
    moment = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, '', :status, :submitted, :expires) "
            "RETURNING id"
        ),
        {
            "card": card_id,
            "applicant": applicant_id,
            "status": status,
            "submitted": moment - timedelta(hours=2),
            "expires": moment + timedelta(days=1),
        },
    )
    return int(row.scalar_one())


async def company(
    session: AsyncSession,
    *,
    starts_at: datetime,
    status: str = "finished",
    chat_uuid: uuid_lib.UUID | None = None,
    selected: int = 2,
    rejected: int = 1,
) -> dict[str, Any]:
    """Минимальная фикстура адресности (R15): автор + `selected` + `rejected`.

    Возвращает всё, что нужно сценариям: id/uuid карточки, участников по ролям.
    По умолчанию 2 принятых + 1 отклонённый: отклонённый обязан НЕ получить
    уведомления, иначе тест пройдёт при `INSERT ... FROM users` (уведомить
    всех вместо адресатов — реальный класс дефекта).
    """
    author = await new_user(session)
    place_id = await make_place(session)
    # AC-01 стори 4.2 (`ck_event_cards_full_capacity`): у статуса `full`
    # `filled_count` ОБЯЗАН равняться `capacity` — «полная» карточка без
    # заполненной вместимости нарушает инвариант. Для остальных статусов
    # берём заведомо большую вместимость: тест проверяет адресность, а не
    # арифметику мест.
    capacity = selected if status == "full" else max(2, selected + 1)
    card_uuid, card_id = await insert_card(
        session,
        author_id=int(author.id),
        place_id=place_id,
        starts_at=starts_at,
        status=status,
        capacity=capacity,
        filled_count=selected,
        chat_uuid=chat_uuid,
    )
    accepted = [await new_user(session) for _ in range(selected)]
    refused = [await new_user(session) for _ in range(rejected)]
    for member in accepted:
        await insert_request(
            session, card_id=card_id, applicant_id=int(member.id), status="selected"
        )
    for member in refused:
        # Домен `join_requests.status` (стори 5а.1): `pending`/`selected`/
        # `not_selected`/`expired`/`cancelled`. «Отклонённая» — это
        # `not_selected`; литерала `rejected` в схеме нет.
        await insert_request(
            session, card_id=card_id, applicant_id=int(member.id), status="not_selected"
        )
    return {
        "author": author,
        "card_id": card_id,
        "card_uuid": card_uuid,
        # CR 7.3 H-1: `chat_uuid` нужен тесту публикации — без него нельзя
        # проверить путь «0 адресатов + есть чат» (он не исполнялся вовсе).
        "chat_uuid": chat_uuid,
        "accepted": accepted,
        "refused": refused,
        "place_id": place_id,
    }


async def marker(session: AsyncSession, card_id: int) -> Any:
    row = await session.execute(
        text("SELECT attendance_prompted_at FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    return row.scalar_one()


async def notification_rows(
    session: AsyncSession, card_uuid: str
) -> list[tuple[int, str, dict[str, Any]]]:
    """`(user_id, type, payload)` всех уведомлений карточки — по адресату."""
    result = await session.execute(
        text(
            "SELECT user_id, type, payload FROM notifications "
            "WHERE payload->>'card_uuid' = :card_uuid ORDER BY user_id"
        ),
        {"card_uuid": card_uuid},
    )
    return [(int(row[0]), str(row[1]), dict(row[2])) for row in result.all()]


async def attendance_rows(session: AsyncSession, card_id: int) -> list[tuple[int, str]]:
    result = await session.execute(
        text(
            "SELECT participant_id, status FROM attendance "
            "WHERE card_id = :id ORDER BY participant_id"
        ),
        {"id": card_id},
    )
    return [(int(row[0]), str(row[1])) for row in result.all()]


async def count_notifications(session: AsyncSession, card_uuid: str) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM notifications WHERE payload->>'card_uuid' = :card_uuid"),
        {"card_uuid": card_uuid},
    )
    return int(result.scalar_one())
