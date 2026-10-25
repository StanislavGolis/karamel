"""Гонка двух модераторов на одной жалобе (стори 8.4; AC-08, D-47).

**§18.3: только ДВА независимых соединения с настоящими коммитами.** Фикстура
`session` держит одну транзакцию, и второе соединение не увидело бы
незакоммиченных изменений первого — гонка была бы фиктивной.

**Что доказывается.** Два модератора решают одну жалобу одновременно.
Легальный исход ровно один: побеждает первый, второй получает
`ReportAlreadyResolvedError`. Нелегальны два:

1. две строки `sanctions` по одной жалобе;
2. `reports.status`/`resolution` расходятся с `sanctions.type` — то есть
   проигравший перезаписал решение победителя.

**Почему утверждение — состояние БД, а не коды ответов** (урок M-1 из CR 8.3):
коды можно получить и при рассинхроне. Проверяется ФУНКЦИЯ СЕРВИСА
(`resolve_report`), а не сырой SQL: тест, дёргающий `SELECT ... FOR UPDATE`
руками, остаётся зелёным при снятии блокировки из сервиса.

**Честная граница (проверено мутацией).** Снятие `FOR UPDATE` НЕ даёт двух
санкций: её держат `UNIQUE(report_id)` + `ON CONFLICT` и CAS
(`AND status='open'`). Мутация, которая краснит этот тест, — снятие
`AND status='open'` из `UPDATE reports` (M2): тогда проигравший гонку
перезаписывает `resolution`, и `reports` расходится с `sanctions`.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.moderation_resolve_service import (
    ReportAlreadyResolvedError,
    resolve_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Собственный движок: два соединения с настоящими коммитами."""
    created = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield created
    await created.dispose()


@pytest.fixture
async def factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _cleanup(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Уборка: санкции → уведомления → жалобы → чаты → карточки → юзеры (§18.12)."""
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_USERS:
        return
    async with factory() as session:
        await session.execute(
            text(
                "DELETE FROM sanctions WHERE user_id = ANY(:ids) OR moderator_id = ANY(:ids) "
                "OR report_id IN (SELECT id FROM reports WHERE reporter_id = ANY(:ids))"
            ),
            {"ids": _CREATED_USERS},
        )
        await session.execute(
            text("DELETE FROM notifications WHERE user_id = ANY(:ids)"), {"ids": _CREATED_USERS}
        )
        await session.execute(
            text("DELETE FROM reports WHERE reporter_id = ANY(:ids)"), {"ids": _CREATED_USERS}
        )
        await session.execute(
            text(
                "DELETE FROM chats WHERE event_card_id IN "
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
            text("DELETE FROM event_cards WHERE author_id = ANY(:ids)"), {"ids": _CREATED_USERS}
        )
        await session.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
        )
        # **`places` обязателен в уборке.** Этот файл коммитит РЕАЛЬНО
        # (собственный движок, §18.3), поэтому созданные места переживают тест
        # и попадают в чужие файлы: `test_places_schema` падал на
        # `assert 10 == 8` (два лишних места из двух race-тестов) и на
        # `MultipleResultsFound` в `test_is_active_defaults_to_true`.
        # SAVEPOINT-изоляции §18.8 здесь нет по построению — компенсируется
        # явной уборкой.
        if _CREATED_PLACES:
            await session.execute(
                text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
            )
        await session.commit()


async def _user(session: AsyncSession, *, role: str = "user") -> User:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role=role, status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    return user


async def _open_message_report(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid_lib.UUID, int, int]:
    """Диалог + открытая жалоба на переписку. Возвращает `(report_uuid, reporter, member)`."""
    async with factory() as session:
        author = await _user(session)
        member = await _user(session)
        place = await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug, is_active) "
                "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
            ),
            {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
        )
        place_id = int(place.scalar_one())
        _CREATED_PLACES.append(place_id)
        starts = datetime.now(UTC) + timedelta(days=3)
        chat_uuid = str(uuid_lib.uuid4())
        card = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, category, "
                "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
                "verification_status, published_at, chat_uuid) VALUES (:a, :p, :s, 'Встреча', "
                "'cafe', :st, 2, 60, :e, 'full', 2, 'approved', :pub, :chat) RETURNING id"
            ),
            {
                "a": author.id,
                "p": place_id,
                "s": settings.default_community_slug,
                "st": starts,
                "e": starts - timedelta(hours=1),
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
                "expires_at, decided_at) VALUES (:c, :a, '', 'selected', now(), "
                "now() + interval '1 day', now())"
            ),
            {"c": card_id, "a": member.id},
        )
        report = await session.execute(
            text(
                "INSERT INTO reports (reporter_id, chat_id, target_type, status, reported_at) "
                "VALUES (:r, :chat, 'message', 'open', now()) RETURNING uuid"
            ),
            {"r": author.id, "chat": chat_id},
        )
        report_uuid = report.scalar_one()
        await session.commit()
        return report_uuid, int(author.id), int(member.id)


async def _resolve(
    factory: async_sessionmaker[AsyncSession],
    *,
    report_uuid: uuid_lib.UUID,
    moderator_id: int,
    resolution: str,
    hold_seconds: float = 0.0,
) -> str:
    """Один вызов сервиса в СВОЕМ соединении. Возвращает исход строкой.

    `hold_seconds` держит транзакцию открытой ПОСЛЕ блокировки — иначе первая
    транзакция успела бы закоммититься до старта второй, и гонки бы не было.
    """
    async with factory() as session:
        try:
            await resolve_report(
                session,
                report_uuid=report_uuid,
                moderator_id=moderator_id,
                resolution=resolution,
                reason="Гонка двух модераторов",
            )
            if hold_seconds:
                await session.execute(text("SELECT pg_sleep(:s)"), {"s": hold_seconds})
            await session.commit()
            return "resolved"
        except ReportAlreadyResolvedError:
            await session.rollback()
            return "already_resolved"


async def test_two_moderators_race_leaves_one_consistent_decision(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-08: два модератора одновременно → одна санкция, решение согласовано.

    `hold_seconds` у первого вызова гарантирует РЕАЛЬНОЕ перекрытие: вторая
    транзакция стартует, пока первая ещё не закоммичена, и упирается в
    блокировку строки жалобы.
    """
    report_uuid, _, _ = await _open_message_report(factory)

    async with factory() as session:
        first_moderator = await _user(session, role="moderator")
        second_moderator = await _user(session, role="moderator")
        await session.commit()

    first, second = await asyncio.gather(
        _resolve(
            factory,
            report_uuid=report_uuid,
            moderator_id=int(first_moderator.id),
            resolution="warning",
            hold_seconds=0.3,
        ),
        _resolve(
            factory,
            report_uuid=report_uuid,
            moderator_id=int(second_moderator.id),
            resolution="ban",
        ),
    )

    # Ровно один победил: второй увидел закрытую жалобу.
    assert sorted([first, second]) == ["already_resolved", "resolved"]

    async with factory() as session:
        sanctions = (
            await session.execute(
                text(
                    "SELECT type FROM sanctions "
                    " WHERE report_id = (SELECT id FROM reports WHERE uuid = :u)"
                ),
                {"u": report_uuid},
            )
        ).all()
        report = (
            await session.execute(
                text("SELECT status, resolution FROM reports WHERE uuid = :u"), {"u": report_uuid}
            )
        ).one()
        notifications = (
            await session.execute(
                text("SELECT count(*) FROM notifications  WHERE payload->>'report_uuid' = :u"),
                {"u": str(report_uuid)},
            )
        ).scalar_one()

    # 1. Одна санкция: `UNIQUE(report_id)` + `ON CONFLICT DO NOTHING`.
    assert len(sanctions) == 1, sanctions
    sanction_type = sanctions[0][0]

    # 2. Согласованность: решение проигравшего не перезаписало решение
    # победителя. Это и есть проверка CAS (мутация M2 краснит именно её).
    status, resolution = report
    expected_status = "resolved_warning" if sanction_type == "warning" else "resolved_ban"
    assert resolution == sanction_type, (status, resolution, sanction_type)
    assert status == expected_status, (status, resolution, sanction_type)

    # 3. Ровно два уведомления: победитель уведомил обе стороны один раз.
    assert int(notifications) == 2, notifications


async def test_sequential_second_moderator_gets_already_resolved(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05: последовательный повтор — тот же исход и та же целостность.

    Контрольный сценарий к гонке: без перекрытия результат обязан быть тем же.
    Если бы `409` рождался только из гонки, тест выше не отличал бы защиту от
    случайного порядка.
    """
    report_uuid, _, _ = await _open_message_report(factory)

    async with factory() as session:
        moderator = await _user(session, role="moderator")
        await session.commit()

    first = await _resolve(
        factory,
        report_uuid=report_uuid,
        moderator_id=int(moderator.id),
        resolution="warning",
    )
    second = await _resolve(
        factory,
        report_uuid=report_uuid,
        moderator_id=int(moderator.id),
        resolution="ban",
    )

    assert first == "resolved"
    assert second == "already_resolved"

    async with factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM sanctions "
                    " WHERE report_id = (SELECT id FROM reports WHERE uuid = :u)"
                ),
                {"u": report_uuid},
            )
        ).scalar_one()
    assert int(count) == 1
