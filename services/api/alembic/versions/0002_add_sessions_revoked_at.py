"""add users.sessions_revoked_at

Решение 1 код-ревью стори 1.5: access-токен невозможно отозвать без метки
на пользователе. Колонка хранит момент последнего отзыва всех сессий
(выход, бан); `get_current_user` сравнивает её с `iat` токена.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21 21:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("sessions_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "sessions_revoked_at")
