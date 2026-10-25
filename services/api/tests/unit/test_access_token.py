"""Unit-тесты access-токена (Task 10.3; AC-06).

D6 — главная проверка здесь: `users.id` не должен попадать в payload.
Payload виден любому, у кого есть токен, и попадает в скриншоты баг-репортов.
"""

import time
import uuid as uuid_lib
from typing import Any

import jwt
import pytest

from app.core.config import settings
from app.core.security import (
    InvalidTokenError,
    decode_access_token,
    encode_access_token,
)
from app.models.user import User


def _user(**overrides: Any) -> User:
    """Минимальный объект `User` без БД: токену нужны только `uuid` и `role`."""
    user = User(provider="yandex", provider_user_id="1")  # type: ignore[arg-type]
    user.id = 42  # внутренний bigint — в токен попасть не должен
    user.uuid = overrides.get("uuid", uuid_lib.uuid4())
    user.role = overrides.get("role", "user")
    return user


def test_round_trip() -> None:
    """AC-06: токен декодируется и отдаёт `sub` = uuid, `role`, `exp`, `jti`."""
    user = _user()

    payload = decode_access_token(encode_access_token(user))

    assert payload["sub"] == str(user.uuid)
    assert payload["role"] == "user"
    assert {"iat", "exp", "jti"} <= set(payload)


def test_payload_has_no_internal_identifiers() -> None:
    """D6 (AR21/AR22): `id`, `phone`, `provider_user_id` в токен не попадают."""
    token = encode_access_token(_user())

    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])

    assert "id" not in payload
    assert "phone" not in payload
    assert "provider_user_id" not in payload
    # Ни одно значение не равно внутреннему bigint.
    assert 42 not in payload.values()


def test_expired_token_rejected() -> None:
    """AC-06: истёкший токен не декодируется.

    Стори 1.5, Task 1.3: наружу отдаётся `InvalidTokenError`, а не классы PyJWT.
    Иначе истёкший access доходил бы до потребителя как `ExpiredSignatureError`
    и превращался в `500` вместо штатного `401` и обновления сессии.
    """
    expired = jwt.encode(
        {"sub": str(uuid_lib.uuid4()), "exp": int(time.time()) - 120},
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(expired)


def test_wrong_signature_rejected() -> None:
    """AC-06: токен, подписанный другим ключом, отклоняется.

    Стори 1.5, Task 1.3: единый `InvalidTokenError` (см. выше).
    """
    foreign = jwt.encode(
        {"sub": str(uuid_lib.uuid4()), "exp": int(time.time()) + 60},
        "чужой-ключ",
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(foreign)


def test_algorithm_none_rejected() -> None:
    """AC-06: `alg=none` отклоняется — иначе подпись можно просто снять.

    Стори 1.5, Task 1.3: единый `InvalidTokenError` (см. выше).
    """
    unsigned = jwt.encode(
        {"sub": str(uuid_lib.uuid4()), "exp": int(time.time()) + 60},
        key="",
        algorithm="none",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned)


def test_missing_sub_rejected() -> None:
    """Task 7.2: токен без `sub` не проходит — иначе непонятно, чья сессия.

    Стори 1.5, Task 1.3: единый `InvalidTokenError` (см. выше).
    """
    no_sub = jwt.encode({"exp": int(time.time()) + 60}, settings.jwt_secret, algorithm="HS256")

    with pytest.raises(InvalidTokenError):
        decode_access_token(no_sub)
