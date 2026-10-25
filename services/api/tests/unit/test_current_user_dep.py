"""Зависимость авторизации `get_current_user` (Task 11.7; AC-01, AC-05).

D-1/D-3 проверяются без базы: сессия заменена заглушкой, которая отдаёт
заранее заданного пользователя. База здесь не нужна — предмет теста
порядок «токен → декодирование → запись → статус», а не SQL.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import ACCESS_TOKEN_TTL, encode_access_token
from app.models.user import User
from app.services.session_service import ACCESS_COOKIE_NAME


def _user(**overrides: Any) -> User:
    """Отсоединённый объект: ORM-сессии у него нет, поля заданы явно."""
    user = User(
        provider="phone",
        provider_user_id="+79001234567",
        phone="+79001234567",
        **overrides,
    )
    user.uuid = uuid_lib.uuid4()
    user.role = overrides.get("role", "user")
    user.status = overrides.get("status", "active")
    user.deletion_requested_at = overrides.get("deletion_requested_at")
    # Решение 1 CR 1.5: метка отзыва сессий. По умолчанию NULL — сессии живут.
    user.sessions_revoked_at = overrides.get("sessions_revoked_at")
    return user


class _Result:
    """Мини-замена `Result`: `UserRepository` читает `scalar_one_or_none`."""

    def __init__(self, user: User | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> User | None:
        return self._user


class _Session:
    """Сессия-заглушка: любой `execute` отдаёт заданного пользователя."""

    def __init__(self, user: User | None) -> None:
        self._user = user

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result(self._user)


def _request(*, cookie: str | None = None, authorization: str | None = None) -> Request:
    """`Request` без ASGI-сервера — только заголовки."""
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", f"{ACCESS_COOKIE_NAME}={cookie}".encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "headers": headers})


def _expired_token(user: User) -> str:
    """Подписанный, но просроченный токен: `leeway=30`, значит минус 5 минут мало."""
    now = datetime.now(UTC) - ACCESS_TOKEN_TTL - timedelta(minutes=5)
    payload = {
        "sub": str(user.uuid),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TOKEN_TTL).timestamp()),
        "jti": uuid_lib.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def test_cookie_token_returns_user() -> None:
    """AC-01: основной транспорт — cookie `karamel_access`."""
    user = _user()
    token = encode_access_token(user)

    result = await get_current_user(_request(cookie=token), _Session(user))

    assert result is user


async def test_bearer_header_returns_user() -> None:
    """D-2: `Authorization: Bearer` — второй транспорт (раздельные поддомены)."""
    user = _user()
    token = encode_access_token(user)

    result = await get_current_user(_request(authorization=f"Bearer {token}"), _Session(user))

    assert result is user


async def test_cookie_wins_over_header() -> None:
    """D-2: порядок обязателен — заголовок не может перекрыть cookie."""
    user = _user()
    other = _user()
    cookie_token = encode_access_token(user)
    header_token = encode_access_token(other)

    result = await get_current_user(
        _request(cookie=cookie_token, authorization=f"Bearer {header_token}"), _Session(user)
    )

    assert result is user


async def test_no_token_gives_401_unauthenticated() -> None:
    """AC-05/D-1: нет токена — `401 unauthenticated`."""
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(), _Session(None))

    assert exc.value.status_code == 401
    assert exc.value.detail == {"code": "unauthenticated"}


async def test_garbage_token_gives_401_not_500() -> None:
    """Task 1.3: битый токен — тот же `401`, а не `500` из PyJWT."""
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(cookie="not-a-jwt"), _Session(_user()))

    assert exc.value.status_code == 401
    assert exc.value.detail == {"code": "unauthenticated"}


async def test_expired_token_gives_401() -> None:
    """Task 1.3: истёкший access — `401`, клиент идёт обновляться."""
    user = _user()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(cookie=_expired_token(user)), _Session(user))

    assert exc.value.status_code == 401
    assert exc.value.detail == {"code": "unauthenticated"}


async def test_unknown_sub_gives_401() -> None:
    """Task 1.4: подпись верна, учётки нет (удалена) — `401`."""
    token = encode_access_token(_user())

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(cookie=token), _Session(None))

    assert exc.value.status_code == 401
    assert exc.value.detail == {"code": "unauthenticated"}


async def test_token_issued_before_revoke_gives_401() -> None:
    """Решение 1 CR 1.5: выход гасит и уже выданный access.

    Без этой проверки после «Выйти» на чужом устройстве доступ к переписке
    сохранялся до истечения токена — до часа. `iat` токена сравнивается
    с `users.sessions_revoked_at`.
    """
    user = _user(sessions_revoked_at=datetime.now(UTC))
    # Токен выпущен раньше отзыва: `encode_access_token` берёт текущее время,
    # поэтому отзыв сдвигается в будущее — тот же эффект, что и «вошли, потом вышли».
    token = encode_access_token(user)
    user.sessions_revoked_at = datetime.now(UTC) + timedelta(seconds=5)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(cookie=token), _Session(user))

    assert exc.value.status_code == 401
    assert exc.value.detail == {"code": "unauthenticated"}


async def test_token_issued_after_revoke_works() -> None:
    """Решение 1 CR 1.5: повторный вход после выхода выдаёт рабочий токен.

    Проверка «отозвано навсегда» ломала бы продукт: пользователь не смог бы
    войти заново, пока метка не сброшена.
    """
    user = _user(sessions_revoked_at=datetime.now(UTC) - timedelta(minutes=5))
    token = encode_access_token(user)

    result = await get_current_user(_request(cookie=token), _Session(user))

    assert result is user


async def test_banned_user_gives_403_user_blocked() -> None:
    """AC-05/D-3: бан виден на живом access — `403`, не `401`.

    `401` заставил бы клиента идти обновлять сессию и создал шторм refresh.
    """
    user = _user(status="banned")
    token = encode_access_token(user)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(cookie=token), _Session(user))

    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "user_blocked"}


async def test_deletion_requested_gives_403() -> None:
    """AC-05: запрошенное удаление блокирует доступ так же, как бан."""
    user = _user(deletion_requested_at=datetime.now(UTC))
    token = encode_access_token(user)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(cookie=token), _Session(user))

    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "user_blocked"}
