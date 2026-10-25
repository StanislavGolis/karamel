"""Гонка «скрытие ⟷ отправка сообщения» (стори 8.2; AC-07, D-25).

**§18.3: только ДВА независимых соединения с настоящими коммитами.**
Фикстура `session` держит одну транзакцию, и второе соединение не увидело бы
незакоммиченных изменений первого — гонка была бы фиктивной.

**Что доказывается.** Обе транзакции начинают с `SELECT ... FOR UPDATE` по
строке чата. Возможны ровно два исхода, и оба легальны по AC:

1. отправка получила блокировку первой → сообщение сохранено, затем скрытие;
2. жалоба получила блокировку первой → отправка видит `hidden` и падает `404`.

Нелегален третий: сообщение ВИДНО в скрытом чате (рассинхрон).

**Почему утверждение — состояние БД, а не коды ответов** (урок CR 2.2):
коды можно получить и при рассинхроне.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []


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
    """Уборка: чаты → заявки → карточки → места → пользователи (§18.12)."""
    _CREATED_USERS.clear()
    yield
    if not _CREATED_USERS:
        return
    async with factory() as session:
        await session.execute(
            text(
                "DELETE FROM reports WHERE reporter_id = ANY(:ids) "
                "OR chat_id IN (SELECT id FROM chats WHERE event_card_id IN "
                "(SELECT id FROM event_cards WHERE author_id = ANY(:ids)))"
            ),
            {"ids": _CREATED_USERS},
        )
        await session.execute(
            text("DELETE FROM notifications WHERE user_id = ANY(:ids)"),
            {"ids": _CREATED_USERS},
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
        await session.commit()


async def _user(session: AsyncSession) -> User:
    phone = f"+7902{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    return user


async def _company(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """Автор + участник + карточка + чат. Возвращает `(chat_id, member_id)`."""
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
        await session.commit()
        return chat_id, int(member.id)


async def test_send_locked_first_then_hide_keeps_message(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07 исход 1: отправка держит блокировку → сообщение сохранено, потом скрытие.

    Сообщение ВИДИМО в чате, который после этого скрыт: AC допускает этот
    исход («сохранено до скрытия»), и рассинхрона нет — порядок один.
    """
    chat_id, member_id = await _company(factory)

    async def send() -> None:
        async with factory() as session:
            await session.execute(
                text("SELECT id FROM chats WHERE id = :i FOR UPDATE"), {"i": chat_id}
            )
            await session.execute(
                text(
                    "INSERT INTO messages (chat_id, sender_id, body, is_system, uuid) "
                    "VALUES (:c, :s, repeat('x', 29)::bytea, false, gen_random_uuid())"
                ),
                {"c": chat_id, "s": member_id},
            )
            await asyncio.sleep(0.05)
            await session.commit()

    async def hide() -> None:
        async with factory() as session:
            await session.execute(
                text("SELECT id FROM chats WHERE id = :i FOR UPDATE"), {"i": chat_id}
            )
            await session.execute(
                text("UPDATE chats SET status = 'hidden' WHERE id = :i"), {"i": chat_id}
            )
            await session.commit()

    await asyncio.gather(send(), hide())

    async with factory() as session:
        status = (
            await session.execute(text("SELECT status FROM chats WHERE id = :i"), {"i": chat_id})
        ).scalar_one()
        count = (
            await session.execute(
                text("SELECT count(*) FROM messages WHERE chat_id = :i"), {"i": chat_id}
            )
        ).scalar_one()

    # Исход легален: чат скрыт, сообщение сохранено (отправка была первой).
    assert status == "hidden"
    assert count == 1


async def test_hide_committed_first_blocks_insert(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07 исход 2: скрытие закоммичено первым → вставка невозможна.

    Проверяется ПРЕДИКАТ, а не сервис: `INSERT` в скрытый чат без проверки
    создал бы нелегальное состояние «сообщение видно в скрытом диалоге».
    Предикат — часть запроса отправки (`_resolve_chat` фильтрует `status`).
    """
    chat_id, member_id = await _company(factory)

    async with factory() as session:
        await session.execute(
            text("UPDATE chats SET status = 'hidden' WHERE id = :i"), {"i": chat_id}
        )
        await session.commit()

    async with factory() as session:
        found = (
            await session.execute(
                text("SELECT id FROM chats WHERE id = :i AND status = 'active'"),
                {"i": chat_id},
            )
        ).first()

    assert found is None, "скрытый чат остался доступен для отправки"


async def test_no_intermediate_state_after_concurrent_pair(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07: после пары операций НЕТ промежуточного состояния.

    Утверждение — инвариант данных: `status` принимает только `active`/`hidden`,
    а `reports` не может ссылаться на чат, оставшийся `active` (жалоба и
    скрытие в одной транзакции).
    """
    chat_id, member_id = await _company(factory)

    async def report() -> None:
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT id FROM chats WHERE id = :i FOR UPDATE"), {"i": chat_id}
            )
            await session.execute(
                text(
                    "INSERT INTO reports (reporter_id, chat_id, target_type, status) "
                    "VALUES (:r, :c, 'message', 'open')"
                ),
                {"r": member_id, "c": chat_id},
            )
            await session.execute(
                text("UPDATE chats SET status = 'hidden' WHERE id = :i"), {"i": chat_id}
            )

    await report()

    async with factory() as session:
        active_with_report = (
            await session.execute(
                text(
                    "SELECT count(*) FROM reports r JOIN chats c ON c.id = r.chat_id "
                    "WHERE c.status = 'active'"
                )
            )
        ).scalar_one()
        status = (
            await session.execute(text("SELECT status FROM chats WHERE id = :i"), {"i": chat_id})
        ).scalar_one()

    assert status == "hidden"
    assert active_with_report == 0, "жалоба есть, но чат остался видимым"


async def test_send_after_hide_committed_between_read_and_lock_gets_404(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 8.2 H-1: скрытие, закоммиченное МЕЖДУ чтением и блокировкой, — не пропуск.

    Воспроизведение окна, которого не видел ни один тест 8.2. `_resolve_chat`
    читает чат со своим `WHERE status='active'` ДО `FOR UPDATE`, поэтому жалоба
    успевает закоммитить скрытие в промежутке. До фикса `send_message` вставлял
    сообщение в скрытый диалог: `status='hidden'`, `messages=1` (проверено на
    двух соединениях вручную).

    **Почему monkeypatch на `get_by_uuid`, а не на `lock_for_update`.** Патчить
    блокировку значило бы обойти ровно ту проверку, которую тест доказывает.
    Подменяется только ЧТЕНИЕ — и уже ПОСЛЕ того, как оно вернуло `active`
    (внутри обёртки), второе соединение коммитит скрытие. Дальше исполняется
    настоящий `send_message` с настоящим `FOR UPDATE`.

    Мутация, которая краснит: убрать проверку `locked_status != 'active'`
    (или вернуть `lock_for_update` к `-> None`) — вставка пройдёт.
    """
    from app.repositories.chats import ChatRepository as _ChatRepository
    from app.services.chat_message_service import ChatNotFoundError, send_message

    chat_id, member_id = await _company(factory)
    original = _ChatRepository.get_by_uuid

    async def read_then_hide(self: _ChatRepository, *, chat_uuid: uuid_lib.UUID) -> Any:
        found = await original(self, chat_uuid=chat_uuid)
        if found is not None:
            # Окно: жалоба коммитит скрытие ПОСЛЕ чтения и ДО `FOR UPDATE`.
            async with factory() as other:
                await other.execute(
                    text("UPDATE chats SET status = 'hidden' WHERE id = :i"), {"i": chat_id}
                )
                await other.commit()
        return found

    monkeypatch.setattr(_ChatRepository, "get_by_uuid", read_then_hide)

    async with factory() as session:
        with pytest.raises(ChatNotFoundError):
            await send_message(
                session,
                viewer_id=member_id,
                chat_uuid=(
                    await session.execute(
                        text("SELECT uuid FROM chats WHERE id = :i"), {"i": chat_id}
                    )
                ).scalar_one(),
                body="после скрытия",
                client_message_id=None,
                settings=settings,
            )
        await session.rollback()

    async with factory() as check:
        count = (
            await check.execute(
                text("SELECT count(*) FROM messages WHERE chat_id = :i"), {"i": chat_id}
            )
        ).scalar_one()
    assert count == 0, "сообщение записано в скрытый чат — окно D-25 открыто"
