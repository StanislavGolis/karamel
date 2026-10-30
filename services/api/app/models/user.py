"""Модель учётной записи `users` (Task 3.2, 3.3, 3.5).

AC-01: состав колонок зафиксирован стори. `id` — внутренний PK, наружу не выходит;
публичный идентификатор — `uuid` (AC-02).
Поля `age_confirmed`, `consent_given_at` (FR9, 152-ФЗ) добавлены сейчас: они
участвуют в атомарных операциях и добавлять их позже — лишняя миграция на живой
таблице.

История 10.1: механика лайков (`likes_used_today`, `likes_counted_on`, CHECK
`likes_non_negative`) удалена вместе с колонками — разворот Action-first снял
ось лайков, обращений в коде не осталось. Взамен добавлен `city_slug`
(ADR-01): ось показа — сообщество, а не город.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AuthProvider, Gender, UserRole, UserStatus

if TYPE_CHECKING:
    from app.models.refresh_token import RefreshToken


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений StrEnum — единый источник списка."""
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class User(TimestampMixin, Base):
    """Учётная запись. Анкета живёт отдельно (`profiles`, стори 2.x)."""

    __tablename__ = "users"

    # AC-01: bigint identity, наружу не отдаётся (AC-02).
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # D-2: uuid генерируется и в БД (для вставок вне ORM), и в Python (объект готов до flush).
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid_lib.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(
        String, nullable=False, default=UserRole.USER, server_default=text("'user'")
    )
    gender: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=UserStatus.ACTIVE, server_default=text("'active'")
    )
    age_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Стори 11.4 (AC-1, AC-2; NFR36; долг `W-10.4-b`): версия текста правил,
    # которую пользователь подтвердил ПОСЛЕДНЕЙ. `NULL` — версия неизвестна
    # (все учётки до миграции `0034`): выдуманная «1.0» была бы утверждением
    # о юридически значимом факте, которого никто не проверял.
    #
    # `String(16)` и `nullable=True` обязаны совпадать с миграцией `0034`,
    # иначе `alembic check` предложит удалить колонку (P-13). `server_default`
    # НЕ ставится по той же причине: миграция его не создаёт.
    consent_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # AC-01, ADR-01: срез сообщества. FK нет — таблицы `communities` не
    # существует. Форму держит regex-CHECK ниже; значения сверяет
    # `UserRepository.create` (`_validate_city_slug`, DN-3 CR 10.1) —
    # то есть только путь через репозиторий, а не сырой SQL и не воркер.
    # `server_default` НЕ ставится: миграция 0032 снимает дефолт, иначе
    # `alembic check` разойдётся со схемой.
    city_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Решение 1 CR 1.5: момент отзыва всех сессий (выход, бан). Access-токен
    # живёт час и не сверяется с БД по `jti`, поэтому отзыв доказывается
    # сравнением `payload['iat']` с этой меткой в `get_current_user`.
    sessions_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # D-7: lazy="raise" — ленивая загрузка в async падает MissingGreenlet в рантайме.
    # D-6: cascade в ORM не задан; удаление токенов делает БД (ON DELETE CASCADE).
    # passive_deletes=True обязателен: без него session.delete(user) заставляет ORM
    # обнулять refresh_tokens.user_id, что даёт NotNullViolation и не даёт БД-каскаду сработать.
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", lazy="raise", passive_deletes=True
    )

    __table_args__ = (
        # AC-01: uq_users_uuid — публичный идентификатор уникален
        UniqueConstraint("uuid", name="uq_users_uuid"),
        # AC-01: uq_users_phone — в PG несколько NULL в UNIQUE допустимы, частичный индекс не нужен
        UniqueConstraint("phone", name="uq_users_phone"),
        # AC-01: один внешний id на провайдера
        UniqueConstraint("provider", "provider_user_id", name="uq_users_provider_user_id"),
        # CR 2.1, Решение 1: цель составного FK `profiles (user_id, gender)`.
        # Составной UNIQUE на (PK, col) в PostgreSQL допустим и нужен, иначе
        # FK ссылался бы на пару без уникального индекса (ошибка 42830).
        # Зеркало миграции 0005: модель и миграция обязаны совпадать, иначе
        # автогенерация следующей миграции предложит удалить ограничение.
        UniqueConstraint("id", "gender", name="uq_users_id_gender"),
        # AC-01: роль и статус — строки с CHECK, не числа (D-3)
        CheckConstraint(_in_clause("provider", tuple(AuthProvider)), name="provider"),
        CheckConstraint(_in_clause("role", tuple(UserRole)), name="role"),
        CheckConstraint(_in_clause("status", tuple(UserStatus)), name="status"),
        # gender — NULL до заполнения анкеты, поэтому CHECK допускает NULL (IN даёт NULL → пропуск)
        CheckConstraint(f"gender IS NULL OR {_in_clause('gender', tuple(Gender))}", name="gender"),
        # Решение 1 код-ревью 1.6: пара «18+ / согласие на ПД» не может разойтись.
        # Зеркало миграции 0003: модель и миграция обязаны совпадать, иначе
        # автогенерация следующей миграции предложит удалить ограничение.
        # CHECK разрешает false+NULL и true+дата, но не false+дата и true+NULL.
        CheckConstraint("age_confirmed = (consent_given_at IS NOT NULL)", name="consent_pair"),
        # AC-01: вход по телефону требует телефона
        CheckConstraint("provider <> 'phone' OR phone IS NOT NULL", name="phone_required"),
        # OAuth-провайдер требует внешнего идентификатора: NULL в UNIQUE не конфликтует,
        # поэтому без этого CHECK можно создать сколько угодно аккаунтов (yandex, NULL).
        CheckConstraint(
            "provider = 'phone' OR provider_user_id IS NOT NULL",
            name="provider_user_id_required",
        ),
        # Пустой телефон неотличим от отсутствующего, но занимает слот в UNIQUE навсегда.
        CheckConstraint("phone IS NULL OR length(btrim(phone)) > 0", name="phone_not_blank"),
        # AC-01, ADR-01: тот же шаблон, что в `ck_places_city_slug`.
        # Расхождение с `Settings._COMMUNITY_SLUG_RE` дало бы 500 на сохранении.
        CheckConstraint("city_slug ~ '^[a-z0-9_-]{3,64}$'", name="city_slug"),
        # индексы очереди модерации/бан-листов
        Index("idx_users_status", "status"),
        # Стори 10.2, D-10.2-2: частичный индекс под предикат воркера ретенции
        # (`deletion_requested_at <= now() - 7d`). Частичный, а не полный: NULL —
        # состояние подавляющего большинства строк. Зеркало миграции `0033`:
        # расхождение модели и миграции ломает `alembic check` (P-13).
        Index(
            "idx_users_deletion_requested_at",
            "deletion_requested_at",
            postgresql_where=text("deletion_requested_at IS NOT NULL"),
        ),
    )
