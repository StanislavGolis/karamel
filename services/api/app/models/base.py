"""Declarative Base и конвенция имён ограничений (Task 2.1–2.4).

Имена ограничений детерминированы: `uq_users_uuid`, `ck_users_status`, `ix_refresh_tokens_user_id`.
Без конвенции PostgreSQL выдаёт `users_status_check1`, и миграция становится невоспроизводимой
на другой базе (имена генерируются по порядку создания).
"""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# AC-01: имена ограничений и индексов заданы шаблоном, а не автогенерацией PG.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс моделей. `metadata` — единственный источник схемы для Alembic."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Временные метки в `timestamptz` с серверным `now()`.

    AC-01: все временные колонки — `timestamptz`, не `timestamp`: сравнение срока
    действия токена идёт с `now()` в UTC, а `timestamp` без зоны даёт сдвиг.

    D-5: `updated_at` обновляется в ORM (`onupdate`), триггер в БД не создаётся —
    иначе логика обновления прячется в БД и не видна из кода.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
