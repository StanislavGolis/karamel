"""Модель `refresh_tokens` (Task 3.4–3.6).

AC-05: в БД лежит только SHA-256 хеш токена (32 байта), не сам токен.
`uuid` у таблицы нет: по API она не адресуется (архитектура, «Правила общие
для всех таблиц»). Отозванная запись не удаляется — она нужна для аудита «выход/бан».
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base):
    """Токен продления сессии. Один пользователь — несколько активных устройств."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # AC-05: удаление пользователя удаляет его токены каскадом в БД
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # AC-05: SHA-256, 32 байта; открытый токен в БД не появляется
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    # AC-05: expires_at = issued_at + 30 дней; timestamptz — сравнение с now() в UTC
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # D-7: lazy="raise"; D-6: без ORM-cascade — удаление делает БД
    user: Mapped[User] = relationship(
        back_populates="refresh_tokens", lazy="raise", passive_deletes=True
    )

    __table_args__ = (
        # AC-01: uq_refresh_tokens_token_hash — один хеш = один токен
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        # AC-01: idx_refresh_tokens_user_id — отзыв всех токенов пользователя
        Index("idx_refresh_tokens_user_id", "user_id"),
        # Частичный индекс под revoke_all_for_user: фильтр по revoked_at IS NULL
        Index(
            "idx_refresh_tokens_active_user",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Индекс под очистку истёкших токенов (retention worker)
        Index("idx_refresh_tokens_expires_at", "expires_at"),
        # bytea без ограничения длины принимает любой мусор: 5 байт вставятся молча,
        # а найти и отличить такую строку от валидной потом невозможно.
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
    )
