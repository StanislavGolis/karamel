"""Модель фото анкеты `profile_photos` (стори 2.2, Task 1.2–1.5).

D-1: фото — отдельная таблица, а не JSONB в `profiles`. Причины: нужна
очередь модерации (частичный индекс `WHERE status = 'pending'`), фильтр
«только одобренные» для выборки эпика 4 и блокировка на уровне фото.
Цена — один join, он дешевле потери индекса.

D-3: `storage_key` строится из UUID (`photos/{profile_uuid}/{photo_uuid}.{ext}`),
имя исходного файла не используется нигде: в нём бывают ФИО, телефоны
и `../`. Ключ не покидает сервер — в ответе API его нет.

D-4: `consent_given_at` NOT NULL — фото без согласия на показ другим
пользователям физически не создаётся (AC-06, AC-07, FR64). Согласие
привязано к конкретному фото, а не к учётке: общий флаг в `users`
доказывал бы согласие на всё сразу.

D-6: `content_type` и `size_bytes` — служебные колонки, без них AC-05
(валидация файла) не проверяется после загрузки и модерация (2.4) не
видит, что именно пришло. Пределы `size_bytes` держит CHECK.

AC-01: сверх списка колонок эпика добавлены `content_type`, `size_bytes`
и `consent_given_at` — обоснование в AC-01 стори и в D-1/D-4.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import PhotoStatus

# AC-01, D-5: единственные принимаемые типы. HEIC/HEIF из iPhone на пилоте
# не принимаются осознанно: клиент конвертирует в JPEG. Список строится из
# MIME-allowlist сервиса валидации, чтобы CHECK и allowlist не разошлись.
_ALLOWED_CONTENT_TYPES: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")

# CR 2.1, патч 1: `btrim(x)` без второго аргумента режет только пробел.
# Набор «пробельных» символов задан явно — как в `profile.py`.
_WHITESPACE = "E' \\t\\r\\n\\f\\v'"


def _trimmed(column: str) -> str:
    """`btrim(col, <пробельные>)` — единая запись для строковых CHECK."""
    return f"btrim({column}, {_WHITESPACE})"


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`CHECK (col IN ('a','b'))` из значений StrEnum — единый источник списка."""
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class ProfilePhoto(TimestampMixin, Base):
    """Фото анкеты в приватном хранилище (AC-01, AC-02).

    Строка создаётся под блокировкой анкеты (`D-7`): лимит 3 фото и выбор
    позиции считаются в одной транзакции, иначе две параллельные загрузки
    видят `count=2` и обе вставляют `position=3`.
    """

    __tablename__ = "profile_photos"

    # D-2: внутренний PK, наружу не выходит никогда.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # D-2: публичный идентификатор — адресация в API идёт только по нему.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid_lib.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # FK объявлен в `__table_args__` (`ForeignKeyConstraint`), чтобы имя
    # строилось по convention из `models/base.py` без ручного дубля.
    profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # D-3: ключ объекта в S3. Не имя файла: `photos/{profile_uuid}/{photo_uuid}.{ext}`.
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # D-12: порядок показа, 1..3. Дырки допустимы (удаление фото).
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # AC-01: статус модерации. Значение по умолчанию — `pending`.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=str(PhotoStatus.PENDING),
        server_default=text("'pending'"),
    )
    # AC-01: поля модерации заполняет 2.4; консистентность со статусом
    # проверяется CHECK-ом `status_consistency`.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # D-6: проверенный MIME из сигнатуры, а не заявленный клиентом.
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # D-6: фактический размер файла — предел сверху держит сервис валидации,
    # CHECK страхует от нуля и мусора из воркера.
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # D-4, AC-07: время согласия на показ фото другим пользователям.
    consent_given_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # AC-01: публичный идентификатор уникален — адресация им однозначна.
        UniqueConstraint("uuid"),
        # D-3: коллизия ключей невозможна, повторное использование ключа запрещено.
        UniqueConstraint("storage_key"),
        # AC-01, D-12: одна позиция — одно фото на анкету.
        UniqueConstraint("profile_id", "position"),
        # AC-01: удаление анкеты удаляет фото каскадом в БД (страховка от
        # сирот: сервис удаляет объект в S3 после commit, D-10).
        ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            ondelete="CASCADE",
        ),
        # AC-01, D-12: позиция 1..3. Верхняя граница — часть лимита «не больше
        # трёх фото» и страховка от `max+1` после удаления среднего.
        CheckConstraint("position BETWEEN 1 AND 3", name="position"),
        # AC-01: статус — закрытый набор значений из StrEnum.
        CheckConstraint(_in_clause("status", tuple(PhotoStatus)), name="status"),
        # AC-01: консистентность статуса и полей модерации. `approved` без
        # `moderated_at` сделал бы решение модератора неотличимым от дефолта.
        CheckConstraint(
            "(status = 'pending' AND moderated_at IS NULL AND rejection_reason IS NULL)"
            " OR (status = 'approved' AND moderated_at IS NOT NULL"
            " AND rejection_reason IS NULL)"
            " OR (status = 'rejected' AND moderated_at IS NOT NULL"
            " AND rejection_reason IS NOT NULL)",
            name="status_consistency",
        ),
        # D-3: ключ не пустой и без пробельных символов по краям.
        CheckConstraint(
            f"storage_key <> '' AND storage_key = {_trimmed('storage_key')}",
            name="storage_key",
        ),
        # AC-05, D-5: только проверенные MIME-типы.
        CheckConstraint(_in_clause("content_type", _ALLOWED_CONTENT_TYPES), name="content_type"),
        # AC-05: нулевой размер — не фото; верхний предел держит сервис
        # (иначе CHECK жёстко привязал бы БД к текущему `MAX_PHOTO_BYTES`).
        CheckConstraint("size_bytes > 0", name="size"),
        # AC-01: причина отказа непустая, если она есть.
        CheckConstraint(
            f"rejection_reason IS NULL OR {_trimmed('rejection_reason')} <> ''",
            name="rejection_reason",
        ),
        # AC-01: очередь модерации. Частичный индекс — в project-context §7
        # прямо назван обязательным для p95 выборки. Имя полное: индексы
        # в naming convention не участвуют.
        Index(
            "idx_profile_photos_status_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
        # Х-01 (закрыт сторией 4.3, AC-09): очередь фото читается в порядке
        # поступления (`ORDER BY created_at, id`), а `idx_..._status_pending`
        # покрывает только фильтр — сортировка шла отдельным `Sort`.
        # Ключ `(created_at, id)`, а НЕ `uuid`: порядок очереди фото исторически
        # по `id`, и менять его значило бы менять поведение модерации фото вне
        # скоупа 4.3. `id` здесь внутренний, но в курсор не попадает (у фото
        # своего курсора нет).
        Index(
            "idx_profile_photos_created_at_id",
            "created_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # Task 1.5: `relationship` на `Profile` НЕ добавляется. В 2.1 связь
    # объявлена со стороны `profiles` с `lazy="raise"` и без `back_populates`,
    # и обратная сторона потребовала бы правки `profile.py` — файла вне
    # этой стори. Фото читается репозиторием, а не ленивой загрузкой.
