"""create profile_photos table and pending index

Стори 2.2 (AC-01). Фото анкеты — отдельная таблица, а не JSONB в `profiles`:
нужна очередь модерации (частичный индекс `WHERE status = 'pending'`),
фильтр «только одобренные» для выборки эпика 4 и блокировка на уровне фото
(D-1).

Сверх списка колонок эпика добавлены три служебные: `content_type`,
`size_bytes`, `consent_given_at`. Без них не выполнить AC-05 (валидация
файла переживает запрос) и AC-07 (время согласия хранится вместе с фото,
FR64). Обоснование — D-1, D-4 стори.

`uq_profiles_uuid` и остальные ограничения `profiles` не трогаются: 0005
применена на стенде, и её правка потребовала бы ручного `downgrade 0004`.

Имена ограничений заданы через `op.f` (§18.7): без него naming convention
из `models/base.py` складывается с именем и даёт двойной префикс
(`ck_profile_photos_ck_profile_photos_status`), который не совпадает
с моделью и ломает `downgrade`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# CR 2.1, патч 1: явный набор «пробельных» символов для btrim. Без второго
# аргумента btrim режет только пробел (U+0020), и `E'photos/x\t'` проходил бы
# CHECK, навсегда выпадая из поиска по ключу.
_WS = "E' \t\r\n\f\v'"


def upgrade() -> None:
    """Создаёт `profile_photos`, ограничения и частичный индекс (AC-01)."""
    op.create_table(
        "profile_photos",
        # D-2: внутренний PK, наружу не выходит никогда.
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        # D-2: публичный идентификатор; server_default — для вставок вне ORM.
        sa.Column(
            "uuid",
            PGUUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # D-1: FK на анкету. ON DELETE CASCADE — удаление анкеты удаляет фото.
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        # D-3: ключ объекта в приватном бакете, не имя файла.
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        # D-12: порядок показа 1..3; дырки допустимы (удаление фото).
        sa.Column("position", sa.SmallInteger(), nullable=False),
        # AC-01: статус модерации, по умолчанию `pending`.
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        # AC-01: поля модерации заполняет 2.4.
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        # D-6: проверенный MIME из сигнатуры, а не заявленный клиентом.
        sa.Column("content_type", sa.String(length=32), nullable=False),
        # D-6: фактический размер файла; предел сверху держит сервис валидации.
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        # D-4, AC-07: время согласия на показ фото другим пользователям.
        sa.Column("consent_given_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_photos")),
        sa.UniqueConstraint("uuid", name=op.f("uq_profile_photos_uuid")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_profile_photos_storage_key")),
        # AC-01, D-12: одна позиция — одно фото на анкету.
        sa.UniqueConstraint(
            "profile_id", "position", name=op.f("uq_profile_photos_profile_id_position")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name=op.f("fk_profile_photos_profile_id_profiles"),
            ondelete="CASCADE",
        ),
        # AC-01, D-12: позиция 1..3 — часть лимита «не больше трёх фото».
        sa.CheckConstraint("position BETWEEN 1 AND 3", name=op.f("ck_profile_photos_position")),
        # AC-01: статус — закрытый набор значений.
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_profile_photos_status"),
        ),
        # AC-01: `approved` без времени модерации неотличим от дефолта.
        sa.CheckConstraint(
            "(status = 'pending' AND moderated_at IS NULL AND rejection_reason IS NULL)"
            " OR (status = 'approved' AND moderated_at IS NOT NULL"
            " AND rejection_reason IS NULL)"
            " OR (status = 'rejected' AND moderated_at IS NOT NULL"
            " AND rejection_reason IS NOT NULL)",
            name=op.f("ck_profile_photos_status_consistency"),
        ),
        # D-3: ключ не пустой и без пробельных символов по краям.
        sa.CheckConstraint(
            f"storage_key <> '' AND storage_key = btrim(storage_key, {_WS})",
            name=op.f("ck_profile_photos_storage_key"),
        ),
        # AC-05, D-5: только проверенные MIME-типы, HEIC/HEIF не принимаются.
        sa.CheckConstraint(
            "content_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name=op.f("ck_profile_photos_content_type"),
        ),
        # AC-05: нулевой размер — не фото.
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_profile_photos_size")),
        # AC-01: причина отказа непустая, если она есть.
        sa.CheckConstraint(
            f"rejection_reason IS NULL OR btrim(rejection_reason, {_WS}) <> ''",
            name=op.f("ck_profile_photos_rejection_reason"),
        ),
    )
    # AC-01, project-context §7: очередь модерации. Имя полное — индексы
    # в naming convention не участвуют. Частичный индекс обязателен для p95.
    op.create_index(
        "idx_profile_photos_status_pending",
        "profile_photos",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Удаляет индекс и таблицу (обратный порядок создания)."""
    op.drop_index("idx_profile_photos_status_pending", table_name="profile_photos")
    op.drop_table("profile_photos")
