"""add messages delivered_at (story 6.5)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-24

Стори 6.5 (Task 6.1, AC-06; FR32, NFR10). Доставка сообщения — ОТДЕЛЬНЫЙ
факт от прочтения: `delivered_at` заполняется, когда событие дошло до
участника; `read_at` появится в 6.6 (FR35) и здесь его быть НЕ ДОЛЖНО.
Смешение двух фактов сделало бы невозможным ответ «доставлено, но не
прочитано», а именно он нужен интерфейсу (NFR12).

**`NULL` — не «не доставлено вообще», а «ещё не доставлялось».** Колонка
`nullable` осознанно: сообщения, отправленные до 6.5, и сообщения без
подписчика (пустая комната) остаются `NULL`, и это корректное состояние,
а не долг.

**CAS-обновление требует частичного индекса.** `mark_delivered` делает
`UPDATE ... WHERE delivered_at IS NULL`: без индекса по недоставленным
строкам каждый вызов сканирует все сообщения чата, включая давно
доставленные. Индекс `(chat_id, delivered_at) WHERE delivered_at IS NULL`
маленький: он содержит только «долги» и исчезает по мере доставки.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет `delivered_at` и частичный индекс недоставленных."""
    op.add_column(
        "messages",
        # `timezone=True` — как у `created_at`: сравнение `timestamp` без зоны
        # с `now()` дало бы сдвиг на часовой пояс сервера.
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_messages_chat_undelivered"),
        "messages",
        ["chat_id", "delivered_at"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    """Обратный порядок: сначала индекс, потом колонка."""
    op.drop_index(op.f("ix_messages_chat_undelivered"), table_name="messages")
    op.drop_column("messages", "delivered_at")
