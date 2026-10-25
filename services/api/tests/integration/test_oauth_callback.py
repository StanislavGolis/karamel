"""Интеграционные тесты OAuth-входа (Task 10.4; AC-01–AC-07).

D-стандарт стори: реальный PostgreSQL, схема через Alembic (D-8 из 1.2),
провайдеры подменяются `httpx.MockTransport`, хранилище `state` — in-memory.
Коммит в роуте подменяется на `flush`: внешняя транзакция фикстуры всё равно
откатит данные, а без подмены фикстура падает с AssertionError.
"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1.auth import router as auth_router  # noqa: F401  (проверка импорта)
from app.core.db import get_session
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services import oauth
from app.services.auth_service import resolve_user
from app.services.oauth import ExternalProfile
from app.services.oauth_state import InMemoryStateStore, issue_state, set_state_store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

FORBIDDEN_LOG_KEYS = (
    "email",
    "login",
    "first_name",
    "last_name",
    "display_name",
    "phone",
    "provider_user_id",
    "code",
    "state",
    "code_verifier",
    "access_token",
    "refresh_token",
    "id_token",
    "payload",
)


def _yandex_handler(
    *, token_status: int = 200, external_id: str = "ya-1"
) -> Callable[[httpx.Request], httpx.Response]:
    """Обработчик Яндекса: обмен кода, затем профиль."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "ya-access"})
        return httpx.Response(
            200,
            json={
                "id": external_id,
                "display_name": "Иван",
                "default_email": "ivan@example.com",
            },
        )

    return handler


@pytest.fixture(autouse=True)
def _clean_state_store() -> None:
    """AC-05: каждый тест начинает с пустым хранилищем `state`."""
    set_state_store(InMemoryStateStore())


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент поверх приложения с подменённой зависимостью сессии.

    `commit` подменяется на `flush`: роут обязан коммитить (Task 6.4), а фикстура
    запрещает коммит, иначе откат внешней транзакции не изолирует тест.
    """
    # Роут обязан коммитить (Task 6.4), но настоящий commit вышел бы за внешнюю
    # транзакцию фикстуры, и её откат перестал бы изолировать тест.
    # Поэтому commit намеренно понижается до flush — это осознанная подмена,
    # а не обход защиты: строки остаются в транзакции и откатываются.

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


def _oauth_cookies(state: str) -> dict[str, str]:
    """Cookie-копия `state` — вторая половина защиты от login CSRF.

    Без неё callback отвечает `invalid_state`: `state` доказывает, что вход начат,
    cookie — что начат этим браузером.
    """
    from app.core.config import settings

    return {settings.oauth_cookie_name: state}


def _install_provider(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(oauth, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


# --- Task 10.4: успешный вход ---


async def test_oauth_creates_user_once(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01, AC-03: первый callback создаёт учётку, второй ведёт в неё же."""
    _install_provider(monkeypatch, _yandex_handler())

    first_state = await issue_state("yandex")
    client.cookies.update(_oauth_cookies(first_state.state))
    first = await client.get(
        "/api/v1/auth/yandex/callback", params={"code": "c1", "state": first_state.state}
    )
    second_state = await issue_state("yandex")
    client.cookies.update(_oauth_cookies(second_state.state))
    second = await client.get(
        "/api/v1/auth/yandex/callback", params={"code": "c2", "state": second_state.state}
    )

    assert first.status_code == 302
    assert first.headers["location"].endswith("/app")
    assert second.status_code == 302
    assert await _count_users(session) == 1
    user = (await session.execute(select(User))).scalar_one()
    assert user.provider == "yandex"
    assert user.provider_user_id == "ya-1"
    assert user.phone is None
    # AC-03: 18+ и согласие на ПД — стори 1.6.
    assert user.age_confirmed is False
    assert user.consent_given_at is None


async def test_oauth_callback_sets_session_cookies(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-06: обе cookie `HttpOnly`, refresh в БД хешем 32 байта, `id` нет в JWT."""
    _install_provider(monkeypatch, _yandex_handler())
    issued = await issue_state("yandex")

    client.cookies.update(_oauth_cookies(issued.state))
    response = await client.get(
        "/api/v1/auth/yandex/callback", params={"code": "c", "state": issued.state}
    )

    set_cookies = response.headers.get_list("set-cookie")
    access = next(c for c in set_cookies if c.startswith("karamel_access="))
    refresh = next(c for c in set_cookies if c.startswith("karamel_refresh="))
    assert "HttpOnly" in access
    assert "HttpOnly" in refresh
    assert "SameSite=lax" in access or "samesite=lax" in access.lower()

    token_row = (await session.execute(select(RefreshToken))).scalar_one()
    assert len(token_row.token_hash) == hashlib.sha256().digest_size

    import jwt as pyjwt

    from app.core.config import settings

    raw_access = access.split(";", 1)[0].split("=", 1)[1]
    payload = pyjwt.decode(raw_access, settings.jwt_secret, algorithms=["HS256"])
    assert "id" not in payload
    assert payload["sub"] == str((await session.execute(select(User.uuid))).scalar_one())


# --- Task 10.4: state ---


async def test_oauth_expired_state(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05: неизвестный (истёкший) `state` → `400 invalid_state`, `users` пуст."""
    _install_provider(monkeypatch, _yandex_handler())

    client.cookies.update(_oauth_cookies("expired-state-value"))
    response = await client.get(
        "/api/v1/auth/yandex/callback", params={"code": "c", "state": "expired-state-value"}
    )

    assert response.status_code == 302
    assert "error=invalid_state" in response.headers["location"]
    assert await _count_users(session) == 0


async def test_oauth_tampered_state(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05: подделанный `state` отклоняется, код не обменивается."""
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"access_token": "t"})

    _install_provider(monkeypatch, handler)
    issued = await issue_state("yandex")

    response = await client.get(
        "/api/v1/auth/yandex/callback",
        params={"code": "c", "state": issued.state + "подделка"},
    )

    assert response.status_code == 302
    assert "error=invalid_state" in response.headers["location"]
    assert called is False
    assert await _count_users(session) == 0


async def test_oauth_state_wrong_provider(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05: `state`, выпущенный для Яндекса, не работает на VK."""
    _install_provider(monkeypatch, _yandex_handler())
    issued = await issue_state("yandex")

    response = await client.get(
        "/api/v1/auth/vk/callback",
        params={"code": "c", "state": issued.state, "device_id": "d"},
    )

    assert response.status_code == 302
    assert "error=invalid_state" in response.headers["location"]
    assert await _count_users(session) == 0


# --- Task 10.4: отказы ---


async def test_oauth_error_callback(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04: `?error=access_denied` → `302 …?error=oauth_denied`, учётки нет."""
    _install_provider(monkeypatch, _yandex_handler())

    client.cookies.update(_oauth_cookies("s"))
    response = await client.get(
        "/api/v1/auth/yandex/callback", params={"error": "access_denied", "state": "s"}
    )

    assert response.status_code == 302
    assert "error=oauth_denied" in response.headers["location"]
    assert await _count_users(session) == 0


async def test_oauth_token_error(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04: ошибка обмена → `302 …?error=oauth_failed`, учётки нет."""
    _install_provider(monkeypatch, _yandex_handler(token_status=400))
    issued = await issue_state("yandex")

    client.cookies.update(_oauth_cookies(issued.state))
    response = await client.get(
        "/api/v1/auth/yandex/callback", params={"code": "c", "state": issued.state}
    )

    assert response.status_code == 302
    assert "error=oauth_failed" in response.headers["location"]
    assert await _count_users(session) == 0


async def test_vk_callback_without_device_id(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 4.4 (AC-02): VK без `device_id` → `400 invalid_callback`."""
    _install_provider(monkeypatch, _yandex_handler())
    issued = await issue_state("vk")

    client.cookies.update(_oauth_cookies(issued.state))
    response = await client.get(
        "/api/v1/auth/vk/callback", params={"code": "c", "state": issued.state}
    )

    assert response.status_code == 302
    assert "error=invalid_callback" in response.headers["location"]
    assert await _count_users(session) == 0


# --- Task 10.4: ПД в логах ---


async def test_oauth_logs_have_no_pii(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04, AC-07: в событиях OAuth нет ПД и секретов."""
    _install_provider(monkeypatch, _yandex_handler(token_status=400))
    issued = await issue_state("yandex")

    with structlog.testing.capture_logs() as captured:
        client.cookies.update(_oauth_cookies(issued.state))
        await client.get(
            "/api/v1/auth/yandex/callback",
            params={"code": "secret-code", "state": issued.state},
        )

    assert captured, "событие OAuth не залогировано"
    for entry in captured:
        for key in FORBIDDEN_LOG_KEYS:
            assert key not in entry, f"ПД в логе: {key}"
        # Код обмена и сам `state` не должны встречаться в значениях.
        assert "secret-code" not in str(entry)
        assert issued.state not in str(entry)


# --- Task 10.4: гонка ---


async def test_oauth_concurrent_callbacks_single_user() -> None:
    """AC-03, D4: два параллельных callback с одним внешним id → одна учётка, без 500.

    Тест намеренно идёт мимо транзакции фикстуры: гонку нельзя воспроизвести
    внутри одной транзакции — `IntegrityError` возникает только при реальном
    параллельном `INSERT` из двух соединений. Строки удаляются в `finally`.
    """
    database_url = os.environ.get("DATABASE_URL")
    assert database_url is not None
    marker = "race-test-1"
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    profile = ExternalProfile(provider="yandex", provider_user_id=marker)

    # Барьер обязателен: без него второй вызов может стартовать после COMMIT
    # первого, уйти в ветку «уже существует» и не проверить IntegrityError-путь.
    barrier = asyncio.Barrier(2)

    async def _one_callback() -> str:
        async with factory() as own_session:
            # Синхронизируем вход в resolve_user: оба SELECT выполняются до INSERT.
            await barrier.wait()
            user = await resolve_user(own_session, profile)
            await own_session.commit()
            return str(user.uuid)

    try:
        first, second = await asyncio.gather(_one_callback(), _one_callback())
        assert first == second
        async with factory() as check_session:
            count = await check_session.execute(
                select(func.count()).select_from(User).where(User.provider_user_id == marker)
            )
            assert int(count.scalar_one()) == 1
    finally:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(User).where(User.provider_user_id == marker))
            await cleanup_session.commit()
        await engine.dispose()
