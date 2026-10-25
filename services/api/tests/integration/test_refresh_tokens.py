"""AC-05: refresh-токен хранится хешем, отзывается и удаляется каскадом."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_refresh_token, new_refresh_token
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _user(session: AsyncSession) -> User:
    return await UserRepository(session).create(provider="phone", phone="+79001230000")


async def test_token_stored_as_32_byte_hash(session: AsyncSession) -> None:
    """AC-05: в БД лежит SHA-256 (32 байта), а не открытый токен."""
    user = await _user(session)
    raw = new_refresh_token()
    repo = RefreshTokenRepository(session)

    await repo.create_token(user.id, hash_refresh_token(raw))

    stored_hash, stored_len = (
        await session.execute(select(RefreshToken.token_hash, func.length(RefreshToken.token_hash)))
    ).one()
    assert stored_len == 32
    assert stored_hash != raw.encode("utf-8")
    assert stored_hash == hash_refresh_token(raw)


async def test_expires_at_is_30_days(session: AsyncSession) -> None:
    """AC-05: срок жизни refresh-токена — 30 дней (PRD Security)."""
    user = await _user(session)
    before = datetime.now(UTC)

    token = await RefreshTokenRepository(session).create_token(user.id, b"x" * 32)

    delta = token.expires_at - before
    assert timedelta(days=30) - timedelta(minutes=1) < delta < timedelta(days=30, minutes=1)


async def test_revoke_all_for_user_marks_every_active_token(session: AsyncSession) -> None:
    """AC-05: отзыв — UPDATE всех активных токенов, запись не удаляется."""
    user = await _user(session)
    repo = RefreshTokenRepository(session)
    await repo.create_token(user.id, b"a" * 32)
    await repo.create_token(user.id, b"b" * 32)

    revoked = await repo.revoke_all_for_user(user.id)

    assert revoked == 2
    rows = (await session.execute(select(RefreshToken.revoked_at))).scalars().all()
    assert all(value is not None for value in rows)
    assert len(rows) == 2


async def test_revoked_token_is_not_active(session: AsyncSession) -> None:
    """AC-05: отозванный токен не находится как активный."""
    user = await _user(session)
    repo = RefreshTokenRepository(session)
    raw_hash = hash_refresh_token(new_refresh_token())
    await repo.create_token(user.id, raw_hash)
    await repo.revoke_all_for_user(user.id)

    assert await repo.get_active(raw_hash) is None


async def test_expired_token_is_not_active(session: AsyncSession) -> None:
    """AC-05: истёкший срок = неактивный токен."""
    user = await _user(session)
    repo = RefreshTokenRepository(session)
    raw_hash = hash_refresh_token(new_refresh_token())
    await repo.create_token(user.id, raw_hash, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    assert await repo.get_active(raw_hash) is None


async def test_active_token_is_found(session: AsyncSession) -> None:
    """AC-05: активный токен находится по хешу."""
    user = await _user(session)
    repo = RefreshTokenRepository(session)
    raw_hash = hash_refresh_token(new_refresh_token())
    await repo.create_token(user.id, raw_hash)

    found = await repo.get_active(raw_hash)

    assert found is not None
    assert found.user_id == user.id


async def test_tokens_deleted_with_user(session: AsyncSession) -> None:
    """AC-05: `ON DELETE CASCADE` — удаление пользователя удаляет его токены."""
    user = await _user(session)
    await RefreshTokenRepository(session).create_token(user.id, b"c" * 32)
    user_id = user.id

    await session.execute(delete(User).where(User.id == user_id))
    await session.flush()

    remaining = await session.scalar(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    assert remaining == 0
