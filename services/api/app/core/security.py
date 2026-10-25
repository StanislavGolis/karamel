"""Токены сессии (Task 5.5).

AC-05: в БД попадает только SHA-256 хеш refresh-токена, открытый токен живёт
исключительно в ответе клиенту и в cookie. Пароли здесь не хешируются —
собственных паролей в продукте нет (PRD Security), поэтому bcrypt/argon2
не нужны и запрещены как лишняя зависимость: у 48-байтного случайного значения
256+ бит энтропии, rainbow-таблиц для него не существует.

JWT и cookie-сессии появятся в стори 1.3–1.5; здесь только примитивы токена.
"""

import hashlib
import secrets
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt

from app.core.config import settings

if TYPE_CHECKING:
    from app.models.user import User

# AC-06: PRD Security — access живёт 1 час, refresh — 30 дней (см. repositories).
ACCESS_TOKEN_TTL = timedelta(hours=1)


class InvalidTokenError(ValueError):
    """Токен недействителен: подпись, срок или форма `sub`."""


ACCESS_TOKEN_ALGORITHM = "HS256"

# AC-05: 48 байт энтропии → token_urlsafe даёт 64 символа base64url.
_REFRESH_TOKEN_BYTES = 48

# AC-05: длина дайджеста SHA-256 — 32 байта (колонка token_hash bytea(32)).
REFRESH_TOKEN_HASH_LEN = 32


def hash_refresh_token(raw: str) -> bytes:
    """AC-05: SHA-256 от открытого токена — то, что лежит в `refresh_tokens.token_hash`.

    Детерминирован (без соли): соль здесь не защищает, а ломает поиск по хешу —
    при проверке токена его нужно найти по индексу, а не перебирать все строки.
    """
    return hashlib.sha256(raw.encode("utf-8")).digest()


def new_refresh_token() -> str:
    """AC-05: криптостойкий открытый токен. Возвращается клиенту, в логи не пишется."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def encode_access_token(user: "User") -> str:
    """Task 7.1 (AC-06): HS256, `sub` = публичный `uuid`.

    D6: `users.id` в токен не попадает (AR21/AR22). Payload JWT виден при отладке
    и попадает в скриншоты баг-репортов — внутренний bigint не должен покидать
    сервер даже в таком виде. `jti` даёт возможность точечного отзыва в 1.5.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user.uuid),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TOKEN_TTL).timestamp()),
        "jti": uuid_lib.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ACCESS_TOKEN_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Task 7.2 (AC-06): проверка подписи, обязательных полей и формы `sub`.

    `algorithms=["HS256"]` обязателен: без явного списка PyJWT доверяет `alg`
    из заголовка токена, что открывает подмену на `none` или на асимметричный алгоритм.

    `sub` проверяется как UUID здесь, а не у потребителя: подписанный токен
    с не-UUID `sub` дал бы 500 в глубине обработчика вместо 401 на границе.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ACCESS_TOKEN_ALGORITHM],
            options={"require": ["exp", "sub"]},
            leeway=30,
        )
    except jwt.InvalidTokenError as exc:
        # Стори 1.5, Task 1.3: битая подпись, просроченный `exp`, отсутствие
        # обязательного поля — всё это `401` на границе, а не `500` из
        # потребителя. PyJWT бросает свои классы (`DecodeError`,
        # `ExpiredSignatureError`, ...), и без обёртки первый же истёкший
        # access валил бы запрос пятисоткой вместо штатного обновления.
        raise InvalidTokenError("токен недействителен") from exc
    try:
        uuid_lib.UUID(str(payload["sub"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidTokenError("sub не является UUID") from exc
    return payload
