"""AC-01, AC-02: репозиторий пользователей и ограничения схемы."""

import os
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.users import UserRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def test_create_and_find_by_uuid(session: AsyncSession) -> None:
    """AC-02: доступ к записи — через `uuid`.

    `session.expire_all()` обязателен: без него объект берётся из identity map,
    и сломанное условие WHERE в запросе тест не поймает.
    """
    repo = UserRepository(session)

    user = await repo.create(provider="phone", phone="+79001112233")
    user_id = user.id
    user_uuid = user.uuid
    session.expire_all()

    found = await repo.get_by_uuid(user_uuid)

    assert found is not None
    assert found.id == user_id
    assert found.uuid == user_uuid
    assert isinstance(user_uuid, uuid_lib.UUID)


async def test_get_by_uuid_returns_none_for_unknown(session: AsyncSession) -> None:
    """AC-02: чужой/несуществующий uuid не раскрывает существование записи."""
    repo = UserRepository(session)

    assert await repo.get_by_uuid(uuid_lib.uuid4()) is None


async def test_phone_is_unique(session: AsyncSession) -> None:
    """AC-01: `uq_users_phone` — второй пользователь с тем же телефоном невозможен."""
    repo = UserRepository(session)
    await repo.create(provider="phone", phone="+79004445566")

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await repo.create(provider="phone", phone="+79004445566")


async def test_provider_user_id_is_unique(session: AsyncSession) -> None:
    """AC-01: `uq_users_provider_user_id` — один внешний id на провайдера."""
    repo = UserRepository(session)
    await repo.create(provider="yandex", provider_user_id="ya-1")

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await repo.create(provider="yandex", provider_user_id="ya-1")


async def test_phone_required_for_phone_provider(session: AsyncSession) -> None:
    """AC-01: `ck_users_phone_required` — вход по телефону без телефона запрещён."""
    repo = UserRepository(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await repo.create(provider="phone")


async def test_invalid_status_rejected(session: AsyncSession) -> None:
    """AC-01: `ck_users_status` — только active/warned/banned, не произвольная строка."""
    repo = UserRepository(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await repo.create(provider="phone", phone="+79007778899", status="deleted")


async def test_database_generates_uuid(session: AsyncSession) -> None:
    """D-2: `gen_random_uuid()` работает для вставок вне ORM (psql, скрипты)."""
    await session.execute(
        text("INSERT INTO users (provider, phone) VALUES ('phone', '+79009990011')")
    )

    generated = await session.scalar(text("SELECT uuid FROM users WHERE phone = '+79009990011'"))

    assert generated is not None


async def test_orm_delete_user_cascades_tokens(session: AsyncSession) -> None:
    """AC-05: `session.delete(user)` удаляет токены через БД-каскад.

    Отдельно от Core-удаления: без `passive_deletes=True` ORM пытается обнулить
    `refresh_tokens.user_id` и падает с NotNullViolation, а каскад БД не срабатывает.
    """

    from sqlalchemy import select

    from app.core.security import hash_refresh_token
    from app.models.refresh_token import RefreshToken
    from app.repositories.refresh_tokens import RefreshTokenRepository

    users = UserRepository(session)
    tokens = RefreshTokenRepository(session)

    user = await users.create(provider="phone", phone="+79007778899")
    user_id = user.id
    await tokens.create_token(user_id, hash_refresh_token("token-orm-delete"))

    # Загружаем объект заново, чтобы relationship был доступен для ORM-удаления
    stored = await session.get(type(user), user_id)
    assert stored is not None
    await session.delete(stored)
    await session.flush()

    remaining = await session.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))
    assert remaining.scalars().all() == []


async def test_token_hash_length_enforced(session: AsyncSession) -> None:
    """AC-05: хеш короче 32 байт отвергается до записи в БД."""
    from app.repositories.refresh_tokens import RefreshTokenRepository

    users = UserRepository(session)
    tokens = RefreshTokenRepository(session)

    user = await users.create(provider="phone", phone="+79009990011")
    with pytest.raises(ValueError, match="32 байт"):
        await tokens.create_token(user.id, b"short")


async def test_banned_user_token_not_active(session: AsyncSession) -> None:
    """AC-05: бан делает токен неактивным даже без явного отзыва."""
    from app.core.security import hash_refresh_token
    from app.repositories.refresh_tokens import RefreshTokenRepository

    users = UserRepository(session)
    tokens = RefreshTokenRepository(session)

    user = await users.create(provider="phone", phone="+79001234567")
    raw_hash = hash_refresh_token("token-banned")
    await tokens.create_token(user.id, raw_hash)
    assert await tokens.get_active(raw_hash) is not None

    user.status = "banned"
    await session.flush()

    assert await tokens.get_active(raw_hash) is None
