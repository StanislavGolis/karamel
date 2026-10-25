"""Репозиторий refresh-токенов (Task 5.2).

AC-05: токен хранится хешем; отзыв — UPDATE, а не DELETE: отозванная запись
нужна для аудита «выход/бан».
D-4: сессия передаётся аргументом.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import REFRESH_TOKEN_HASH_LEN
from app.models.refresh_token import RefreshToken
from app.models.user import User

# AC-05: PRD Security — refresh живёт 30 дней.
REFRESH_TOKEN_TTL = timedelta(days=30)


def _as_utc(value: datetime) -> datetime:
    """Naive datetime переинтерпретируется asyncpg в зону сервера — приводим явно."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class RefreshTokenRepository:
    """Выдача, поиск и отзыв токенов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_token(
        self,
        user_id: int,
        token_hash: bytes,
        expires_at: datetime | None = None,
    ) -> RefreshToken:
        """AC-05: `expires_at = issued_at + 30 дней`, если срок не задан явно.

        Длина хеша проверяется здесь, а не только в БД: `bytea` не хранит длину,
        поэтому запись в 5 байт прошла бы CHECK только на уровне миграции с невнятной ошибкой.
        """
        if len(token_hash) != REFRESH_TOKEN_HASH_LEN:
            raise ValueError(
                f"token_hash должен быть {REFRESH_TOKEN_HASH_LEN} байт, получено {len(token_hash)}"
            )
        if expires_at is None:
            expires_at = datetime.now(UTC) + REFRESH_TOKEN_TTL
        token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=_as_utc(expires_at))
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_active(self, token_hash: bytes) -> RefreshToken | None:
        """Активный токен: не отозван, не истёк, пользователь не забанен.

        Проверка статуса пользователя здесь, а не только в бан-пути: если бан
        применили, не вызвав `revoke_all_for_user`, токен обязан перестать работать.
        """
        result = await self._session.execute(
            select(RefreshToken)
            .join(User, User.id == RefreshToken.user_id)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
                User.status != "banned",
            )
        )
        return result.scalar_one_or_none()

    async def consume_for_rotation(self, token_hash: bytes) -> RefreshToken | None:
        """Атомарная ротация: помечает токен отозванным и возвращает его.

        `UPDATE ... WHERE revoked_at IS NULL RETURNING` вместо `SELECT` затем `UPDATE`:
        при двух конкурентных запросах с одним токеном только один получит строку
        (READ COMMITTED перечитывает условие после блокировки), второй увидит пустой результат.
        """
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
            .returning(RefreshToken)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_hash(self, token_hash: bytes) -> RefreshToken | None:
        """Поиск токена по хешу без фильтров срока, отзыва и бана (CR 1.5, патч 17).

        `get_active` для выхода не годится: истёкший токен или токен забаненного
        пользователя давал `None`, и `revoke_all_for_user` пропускался — ровно
        тогда, когда «выйти со всех устройств» нужнее всего. Здесь нужен только
        `user_id`, а он есть у любой сохранённой строки.
        """
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke_all_for_user(self, user_id: int) -> int:
        """AC-05: один UPDATE на все активные токены пользователя (выход/бан).

        `synchronize_session="fetch"` обязателен: без него объекты, уже загруженные
        в сессию, продолжают отдавать `revoked_at=None` и могут быть использованы повторно.
        Время берётся из БД (`func.now()`), чтобы метка не зависела от часов приложения.

        Решение 1 CR 1.5: вместе с refresh отзывается и уже выданный access —
        `users.sessions_revoked_at` сравнивается с `iat` токена в
        `get_current_user`. Без этой отметки выход гасил только refresh,
        а живой access работал до часа.
        """
        result: CursorResult[tuple[()]] = await self._session.execute(  # type: ignore[assignment]
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now()),
            execution_options={"synchronize_session": "fetch"},
        )
        await self._session.execute(
            update(User).where(User.id == user_id).values(sessions_revoked_at=func.now()),
            execution_options={"synchronize_session": "fetch"},
        )
        return int(result.rowcount or 0)
