"""Выход `POST /api/v1/auth/logout` (Task 11.6; AC-04).

AC-04 требует трёх вещей одновременно: отзыв **всех** refresh-токенов,
очистку cookie с теми же атрибутами и идемпотентность. Проверяются все три,
причём отзыв — чтением БД, а не кодом ответа.
"""

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.refresh_token import RefreshToken
from app.repositories.users import UserRepository
from app.services.session_service import REFRESH_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


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


async def _session_with_two_tokens(session: AsyncSession) -> tuple[str, int]:
    """Учётка с двумя активными сессиями; возвращает refresh первой и `user_id`."""
    phone = f"+7900{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone
    )
    first = await issue_session(session, user)
    await issue_session(session, user)
    await session.flush()
    return first.refresh_token, user.id


async def test_logout_returns_204_and_revokes_all(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04/D-6: `204`, все сессии пользователя отозваны, тело пустое."""
    raw, user_id = await _session_with_two_tokens(session)

    response = await client.post("/api/v1/auth/logout", cookies={REFRESH_COOKIE_NAME: raw})

    assert response.status_code == 204
    assert response.content == b""

    active = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert active == []


async def test_logout_clears_cookies_with_max_age_zero(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: cookie очищены — `Max-Age=0` и те же `Path`/`HttpOnly`/`SameSite`.

    CR 1.5, патч 19: строк три, а не две — `karamel_oauth` тоже гасится.
    Незавершённый OAuth-флоу не должен переживать выход: в браузере оставался
    `state`, пригодный для повторной попытки login CSRF в пределах TTL.
    """
    raw, _ = await _session_with_two_tokens(session)

    response = await client.post("/api/v1/auth/logout", cookies={REFRESH_COOKIE_NAME: raw})

    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 3
    assert any(cookie.startswith("karamel_oauth=") for cookie in cookies)
    for cookie in cookies:
        lowered = cookie.lower()
        assert "max-age=0" in lowered
        assert "path=/" in lowered
        assert "httponly" in lowered
        assert "samesite=lax" in lowered


async def test_logout_without_cookie_is_204(client: AsyncClient) -> None:
    """Task 4.3: без cookie — всё равно `204` (идемпотентно)."""
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert response.content == b""


async def test_logout_with_garbage_cookie_is_204(client: AsyncClient) -> None:
    """Task 4.3: мусорная cookie — не ошибка, `204`."""
    response = await client.post(
        "/api/v1/auth/logout", cookies={REFRESH_COOKIE_NAME: "not-a-token"}
    )

    assert response.status_code == 204


async def test_repeated_logout_is_204(client: AsyncClient, session: AsyncSession) -> None:
    """Task 4.3/AC-04: повторный выход с отозванной cookie — `204`."""
    raw, _ = await _session_with_two_tokens(session)

    first = await client.post("/api/v1/auth/logout", cookies={REFRESH_COOKIE_NAME: raw})
    second = await client.post("/api/v1/auth/logout", cookies={REFRESH_COOKIE_NAME: raw})

    assert first.status_code == 204
    assert second.status_code == 204
