"""create profiles table and city/gender index

Стори 2.1 (AC-01, AC-02, AC-07). Анкета — отдельная таблица с FK на `users`:
скрытие или удаление анкеты не ломает вход (D-1).

Состав колонок `users` этой миграцией не меняется ни на одну: анкетные поля
(`city`, `bio`, `age`, `is_hidden`) в `users` не появляются.

`reliability_score` не создаётся — перенесён в пост-MVP (D-9), тест 7.3
фиксирует отсутствие, чтобы «полезная колонка» не появилась по инициативе.

Одна миграция на таблицу и её индекс: дробление на 0004/0005 создало бы окно
с неполной схемой (Project Structure Notes).

Имена ограничений заданы через `op.f` (§18.7): без него naming convention из
`models/base.py` складывается с именем и даёт двойной префикс
`ck_profiles_ck_profiles_gender`, который не совпадает с моделью и ломает
`downgrade`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создаёт `profiles`, ограничения и индекс (AC-01, AC-02, AC-07)."""
    op.create_table(
        "profiles",
        # D-2: внутренний PK, наружу не отдаётся (AC-04).
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        # D-2: публичный идентификатор; server_default — для вставок вне ORM.
        sa.Column(
            "uuid",
            PGUUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # D-7: имя в карточке анкеты, лимит 50 символов.
        sa.Column("name", sa.String(length=50), nullable=True),
        # D-5: город — текст, не FK на справочник.
        sa.Column("city", sa.String(length=100), nullable=True),
        # D-3: smallint, не birth_date.
        sa.Column("age", sa.SmallInteger(), nullable=True),
        # D-4: зеркало users.gender для составного индекса выборки.
        sa.Column("gender", sa.String(length=16), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column(
            "is_hidden",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profiles")),
        sa.UniqueConstraint("uuid", name=op.f("uq_profiles_uuid")),
        # AC-03: одна анкета на учётную запись — второй INSERT отклоняет БД.
        sa.UniqueConstraint("user_id", name=op.f("uq_profiles_user_id")),
        # AC-06: удаление пользователя удаляет анкету каскадом в БД.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        # AC-02: пол — только два значения.
        sa.CheckConstraint("gender IN ('female', 'male')", name=op.f("ck_profiles_gender")),
        # AC-02: возраст либо не заполнен, либо 18..99.
        sa.CheckConstraint(
            "age IS NULL OR (age >= 18 AND age <= 99)", name=op.f("ck_profiles_age")
        ),
        # AC-02: пустое имя неотличимо от отсутствия.
        sa.CheckConstraint(
            "name IS NULL OR (btrim(name) <> '' AND length(name) <= 50)",
            name=op.f("ck_profiles_name"),
        ),
        # AC-07: город без пробелов по краям и непустой. Регистр не проверяется (D-5).
        sa.CheckConstraint(
            "city IS NULL OR (btrim(city) <> '' AND city = btrim(city) AND length(city) <= 100)",
            name=op.f("ck_profiles_city"),
        ),
        sa.CheckConstraint(
            "bio IS NULL OR (btrim(bio) <> '' AND length(bio) <= 500)",
            name=op.f("ck_profiles_bio"),
        ),
    )
    # AC-02, D-6: составной индекс под выборку «город пилота + пол».
    # Имя полное: индекс в naming convention не участвует.
    op.create_index("idx_profiles_city_gender", "profiles", ["city", "gender"], unique=False)


def downgrade() -> None:
    """Удаляет индекс и таблицу (обратный порядок создания)."""
    op.drop_index("idx_profiles_city_gender", table_name="profiles")
    op.drop_table("profiles")
