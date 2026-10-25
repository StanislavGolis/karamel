"""Ротация сессии `POST /api/v1/auth/refresh` (Task 11.1–11.5; AC-02, AC-05, AC-06).

Реальный PostgreSQL, схема через Alembic (D-8). Проверяется не только код
ответа, но и состояние БД (`revoked_at`): «ответ 401» сам по себе не доказывает,
что старый токен погашен, а именно это и есть содержание AC-06.

Коммит в роуте понижен до `flush` (фикстура `session`): иначе он вышел бы
за внешнюю транзакцию и её откат перестал бы изолировать тест.
"""

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import hash_refresh_token, new_refresh_token
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.session_service import REFRESH_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _user(session: AsyncSession, **overrides: Any) -> User:
    """Учётка с уникальным телефоном: тесты в одной транзакции, но не мешают друг другу."""
    phone = overrides.pop("phone", f"+7900{uuid_lib.uuid4().int % 10**7:07d}")
    return await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, **overrides
    )


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой зависимостью сессии."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _login(session: AsyncSession, user: User) -> str:
    """Выдаёт сессию и возвращает открытый refresh — то, что лежит в cookie."""
    issued = await issue_session(session, user)
    await session.flush()
    return issued.refresh_token


# --- AC-02, AC-06: успешная ротация ---


async def test_refresh_rotates_pair(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02/AC-06: 200, новая пара в cookie, старый refresh погашен в БД."""
    user = await _user(session)
    raw = await _login(session, user)

    response = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})

    assert response.status_code == 200
    assert response.json() == {"expires_in": 3600}
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any(cookie.startswith("karamel_access=") for cookie in cookies)
    assert any(cookie.startswith("karamel_refresh=") for cookie in cookies)
    # AC-08: токенов в теле ответа нет.
    assert "karamel_access=" not in response.text

    stored = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
        )
    ).scalar_one()
    assert stored.revoked_at is not None


async def test_new_refresh_works_old_does_not(client: AsyncClient, session: AsyncSession) -> None:
    """AC-06: старый refresh погашен, новый — рабочий. Одна цепочка, не два дерева.

    CR 1.5, патч 4: проверяется **replay** старого токена. Раньше тест
    ограничивался `assert new_raw != raw` — сравнением двух случайных
    256-битных значений, которое не может упасть, и дефект «старый токен жив»
    проходил мимо. Теперь старый токен после ротации обязан дать `401`.
    """
    user = await _user(session)
    raw = await _login(session, user)

    first = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})
    new_raw = first.cookies.get(REFRESH_COOKIE_NAME)
    assert new_raw is not None and new_raw != raw

    second = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: new_raw})
    assert second.status_code == 200

    # AC-06: replay старого токена — отказ, и это отказ по существу, а не
    # следствие того, что значение отличается.
    replay = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})
    assert replay.status_code == 401
    assert replay.json() == {"code": "session_expired"}

    # Патч 16: неизвестный токен не ставит `Max-Age=0` — иначе ответ
    # проигравшего в гонке стирал бы живую пару победителя.
    assert replay.headers.get_list("set-cookie") == []


# --- AC-06: повторное использование и отсутствие cookie ---


async def test_reused_refresh_gives_401_and_clears_cookies(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: повторный refresh → `401 session_expired`.

    CR 1.5, патч 16: cookie при этом **не** очищаются. Погашенный токен
    неотличим от «такого не было», и ответ с `Max-Age=0` от проигравшего
    в гонке refresh стирал бы живую пару, которую только что выдал победитель.
    Очистка осталась на ветках «просрочен» и «забанен» — там пара мертва.
    """
    user = await _user(session)
    raw = await _login(session, user)

    await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})
    replay = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})

    assert replay.status_code == 401
    assert replay.json() == {"code": "session_expired"}
    # AC-02: привязка к сессии не кэшируется ни в одном исходе.
    assert replay.headers["cache-control"] == "no-store"
    assert replay.headers.get_list("set-cookie") == []


async def test_no_cookie_gives_401(client: AsyncClient) -> None:
    """Task 3.2: нет cookie — `401 session_expired`."""
    response = await client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json() == {"code": "session_expired"}


async def test_expired_refresh_gives_401(client: AsyncClient, session: AsyncSession) -> None:
    """Task 3.4: просроченный токен гасится и даёт `401`, а не 500."""
    user = await _user(session)
    raw = new_refresh_token()
    repository = RefreshTokenRepository(session)
    await repository.create_token(
        user.id,
        hash_refresh_token(raw),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await session.flush()

    response = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})

    assert response.status_code == 401
    assert response.json() == {"code": "session_expired"}


# --- AC-05: бан ---


async def test_banned_user_gives_403_and_revokes_all(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05: бан между запросами → `403 user_blocked`, все токены отозваны."""
    user = await _user(session)
    raw = await _login(session, user)
    # Вторая активная сессия того же пользователя: бан обязан закрыть и её.
    other_raw = await _login(session, user)
    await session.execute(update(User).where(User.id == user.id).values(status="banned"))
    await session.flush()

    response = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})

    assert response.status_code == 403
    assert response.json() == {"code": "user_blocked"}
    cookies = response.headers.get_list("set-cookie")
    assert all("max-age=0" in cookie.lower() for cookie in cookies)

    active = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert active == []
    assert other_raw != raw


async def test_deletion_requested_gives_403(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: запрошенное удаление блокирует обновление так же, как бан."""
    user = await _user(session)
    raw = await _login(session, user)
    await session.execute(
        update(User).where(User.id == user.id).values(deletion_requested_at=datetime.now(UTC))
    )
    await session.flush()

    response = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE_NAME: raw})

    assert response.status_code == 403
    assert response.json() == {"code": "user_blocked"}


# --- AC-06: параллельные обновления ---


async def test_parallel_refresh_gives_exactly_one_success() -> None:
    """AC-06: два параллельных обновления с одним токеном — ровно один `200`.

    Требование атомарности `consume_for_rotation`: `UPDATE ... WHERE revoked_at IS NULL`
    под READ COMMITTED перечитывает условие после блокировки, поэтому второй
    запрос получает пустой `RETURNING` и `401`.

    Тест идёт в **отдельных** соединениях с коммитом: гонку нельзя показать
    внутри одной транзакции фикстуры — там нет конкуренции за блокировку.
    Созданные строки удаляются каскадом в конце.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+7900{uuid_lib.uuid4().int % 10**7:07d}"
    try:
        async with factory() as setup:
            user = await UserRepository(setup).create(
                provider="phone", provider_user_id=phone, phone=phone
            )
            issued = await issue_session(setup, user)
            await setup.commit()

        async def _refresh_once() -> int:
            async with factory() as own_session:
                from app.services.session_service import (
                    SessionExpiredError,
                    rotate_session,
                )

                try:
                    await rotate_session(own_session, issued.refresh_token)
                except SessionExpiredError:
                    await own_session.rollback()
                    return 401
                await own_session.commit()
                return 200

        import asyncio

        results = await asyncio.gather(_refresh_once(), _refresh_once())
        assert results.count(200) == 1, results
        assert results.count(401) == 1, results
    finally:
        async with factory() as cleanup:
            await cleanup.execute(update(User).where(User.phone == phone).values(status="banned"))
            await cleanup.execute(  # каскад удалит refresh_tokens
                __import__("sqlalchemy").delete(User).where(User.phone == phone)
            )
            await cleanup.commit()
        await engine.dispose()
