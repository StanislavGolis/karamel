"""restore refresh_tokens indexes (tech chore, stand drift)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-24

**Почему миграция нужна.** Дефект кода отсутствует: модель
`app/models/refresh_token.py` объявляет три индекса, и миграция
`0001_create_users_and_refresh_tokens.py` их создаёт (строки 105–111 файла).
Дрейф — исключительно состояние БД стенда `129.101.113.246`: база создана
старшей (до правки 0001 от 2026-10-21 19:18) версией файла, поэтому двух
индексов в ней нет. На чистой локальной БД `alembic check` чист —
автогенерация видит соответствие.

Факты прогона P-1: `_bmad-output/implementation-artifacts/stand-p1-run-2026-10-24.md`
(§6.2 — `alembic check` = FAILED, RC=255; `alembic heads` = `0014 (head)`).

**Идемпотентность — главное требование.** Миграция no-op на чистой БД
(индексы уже есть из `0001`) и создающая недостающие на дрейфованной.
`IF NOT EXISTS` / `IF EXISTS` обязательны в обоих направлениях, иначе
миграция падает на одной из двух баз и становится неприменимой.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Частичный индекс под revoke_all_for_user (фильтр revoked_at IS NULL).
    # Совпадает с 0001 байт-в-байт, иначе alembic check снова покажет дрейф.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active_user "
        "ON refresh_tokens (user_id) WHERE revoked_at IS NULL"
    )
    # Индекс под очистку истёкших токенов (retention worker).
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires_at ON refresh_tokens (expires_at)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Симметрично и так же безопасно: на чистой БД индексы удалятся вместе
    # с `0001`, но промежуточное состояние остаётся консистентным.
    op.execute("DROP INDEX IF EXISTS idx_refresh_tokens_expires_at")
    op.execute("DROP INDEX IF EXISTS idx_refresh_tokens_active_user")
