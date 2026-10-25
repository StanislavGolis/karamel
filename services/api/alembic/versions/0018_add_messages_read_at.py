"""add messages read_at (story 6.6)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-24

Стори 6.6 (Task 1, AC-02, AC-06; FR35, NFR12). Прочтение — ВТОРОЙ факт,
отдельный от доставки (0017). `delivered_at` отвечает «событие дошло до
устройства», `read_at` — «человек открыл чат». Слияние двух колонок сделало
бы невозможным ответ «доставлено, но не прочитано», который и есть смысл
счётчика непрочитанных.

**`NULL` — «ещё не читали», а не долг.** Колонка `nullable` осознанно: все
сообщения, отправленные до 6.6, имеют `NULL`, и это корректное состояние
(их никто не открывал). Backfill не нужен и вреден: он выдумал бы факт
прочтения, которого не было.

**Частичный индекс, а не безусловный.** `ix_messages_chat_unread
(chat_id, read_at) WHERE read_at IS NULL` обслуживает оба запроса стори:
подсчёт непрочитанных в списке чатов и `UPDATE ... WHERE read_at IS NULL`
при отметке прочтения. Безусловный индекс раздувался бы прочитанными
строками — их со временем большинство, — а частичный остаётся маленьким,
потому что «долги» исчезают по мере открытия чатов.

`downgrade` снимает индекс и колонку в обратном порядке — без остатков
(§12.3).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "ix_messages_chat_unread"


def upgrade() -> None:
    """Добавляет `read_at` и частичный индекс непрочитанных."""
    op.add_column(
        "messages",
        # `timezone=True` — как у `created_at`/`delivered_at`: сравнение
        # `timestamp` без зоны с `now()` дало бы сдвиг на часовой пояс БД.
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f(INDEX_NAME),
        "messages",
        ["chat_id", "read_at"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    """Обратный порядок: сначала индекс, потом колонка."""
    op.drop_index(op.f(INDEX_NAME), table_name="messages")
    op.drop_column("messages", "read_at")
