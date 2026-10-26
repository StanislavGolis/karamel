"""Схема `chats.status` (стори 8.2; AC-01, AC-11).

Проверки идут через `information_schema`/`pg_constraint` и через ФАКТИЧЕСКИЕ
вставки, а не через модель: модель описывает намерение, а AC говорит о схеме.

§18.1: ограничение проверяется НАРУШЕНИЕМ — наличие `CHECK` в каталоге не
доказывает, что он что-то запрещает. §18.8: каждый ожидаемый отказ — в своём
`begin_nested()`.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _user(session: AsyncSession) -> int:
    phone = f"+7903{uuid_lib.uuid4().int % 10**7:07d}"
    row = await session.execute(
        text(
            "INSERT INTO users (uuid, provider, provider_user_id, phone, role, status, "
            "age_confirmed, consent_given_at, community_slug) "
            "VALUES (gen_random_uuid(), 'phone', :p, :p, 'user', 'active', "
            "true, now(), 'tomsk') "
            "RETURNING id"
        ),
        {"p": phone},
    )
    return int(row.scalar_one())


async def _chat(session: AsyncSession) -> int:
    author = await _user(session)
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
    )
    place_id = int(place.scalar_one())
    now = datetime.now(UTC)
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:a, :p, 'tomsk', 'Встреча', NULL, 'cafe', :s, 2, 60, :e, "
            "'published', 0, 'approved', :pub) RETURNING id"
        ),
        {
            "a": author,
            "p": place_id,
            "s": now + timedelta(days=3),
            "e": now + timedelta(days=1),
            "pub": now,
        },
    )
    card_id = int(card.scalar_one())
    row = await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (gen_random_uuid(), :c) RETURNING id"),
        {"c": card_id},
    )
    return int(row.scalar_one())


async def test_status_column_present_with_default(session: AsyncSession) -> None:
    """AC-01: `status VARCHAR(16) NOT NULL DEFAULT 'active'`."""
    result = await session.execute(
        text(
            "SELECT data_type, is_nullable, character_maximum_length, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'chats' AND column_name = 'status'"
        )
    )
    row = result.one()
    assert row[0] == "character varying"
    assert row[1] == "NO"
    assert row[2] == 16
    assert "active" in str(row[3])


async def test_check_constraint_exists(session: AsyncSession) -> None:
    """AC-01: `ck_chats_status` — имя входит в контракт `downgrade`."""
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'chats'::regclass AND contype = 'c'"
        )
    )
    assert "ck_chats_status" in {row[0] for row in result}


async def test_existing_chat_is_active_by_default(session: AsyncSession) -> None:
    """AC-01: чат, созданный без указания статуса, ВИДИМ.

    `server_default` — не украшение: существующие чаты созданы до `0026`, и
    без дефолта они получили бы `NULL` (или миграция упала бы на `NOT NULL`).
    """
    chat_id = await _chat(session)
    result = await session.execute(text("SELECT status FROM chats WHERE id = :i"), {"i": chat_id})
    assert result.scalar_one() == "active"


async def test_hidden_status_accepted(session: AsyncSession) -> None:
    """AC-01: `hidden` — второе допустимое значение (P-8: оба значения)."""
    chat_id = await _chat(session)
    await session.execute(text("UPDATE chats SET status = 'hidden' WHERE id = :i"), {"i": chat_id})
    result = await session.execute(text("SELECT status FROM chats WHERE id = :i"), {"i": chat_id})
    assert result.scalar_one() == "hidden"


async def test_unknown_status_rejected(session: AsyncSession) -> None:
    """AC-01: значение вне домена отвергается `CHECK`-ом.

    Краснеет, если `CHECK` убрать: `archived` прошёл бы, и в списке чатов
    появилось бы состояние, которого код не знает.
    """
    chat_id = await _chat(session)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE chats SET status = 'archived' WHERE id = :i"), {"i": chat_id}
            )
