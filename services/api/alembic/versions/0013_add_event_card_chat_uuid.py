"""add event_cards.chat_uuid and its partial unique index

Стори 5б.2 (AC-01, D-5b.2-1; FR30, FR78). Автор выбирает одного заявившегося,
карточка ОДНИМ `UPDATE` становится `full` и получает идентификатор чата
компании. Таблицы `chats` здесь НЕТ: она создаётся в эпике 6 (арх. §496),
и контракт между стори фиксирован явно —
`chats.uuid = event_cards.chat_uuid`, эпик 6 НЕ генерирует новое значение.

**Почему `chat_uuid` в `event_cards`, а не в `join_requests` (D-5b.2-1).**
Чат принадлежит СОБЫТИЮ, а не заявке: после отмены участия (5а.6) и
повторного выбора чат остаётся тем же, и `selected`-строка к нему не
привязана. Хранение в заявке означало бы, что ровно одна строка владеет
чатом, и любая замена выбранного (5б.3) теряла бы идентификатор.

**Почему индекс ЧАСТИЧНЫЙ (AC-01).** `WHERE chat_uuid IS NOT NULL`:
открытых карточек без чата — подавляющее большинство, и держать их в
уникальном индексе незачем. Поведенчески `UNIQUE` в PostgreSQL и так
допускает много `NULL`; предикат нужен ради имени и ради контракта эпику 6
(вставка чата не наткнётся на `NULL`-строки). Имя ПОЛНОЕ и через `op.f`
(§18.7): иначе convention складывается с именем и даёт двойной префикс
(дефект 4.1, `alembic check` краснеет).

**X-39 не трогается.** `ck_event_cards_full_capacity` из `0011` остаётся:
`full` и `chat_uuid` выставляются ТЕМ ЖЕ `UPDATE`, что и инкремент
`filled_count`, поэтому несогласованного промежуточного состояния нет.

**Границы.** Ни `chats`, ни `messages`, ни `notifications` не создаются.
`downgrade` уносит индекс и колонку; прочие таблицы не меняются.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# AC-01, D-5b.2-1: имя индекса — константа, чтобы `downgrade` не разошёлся
# с `upgrade` после правки одной строки (приём `0012`).
_CHAT_UUID_INDEX = "uq_event_cards_chat_uuid"


def upgrade() -> None:
    """Добавляет колонку и частичный уникальный индекс."""
    # AC-01: колонка nullable — открытая карточка чата не имеет; `server_default`
    # НЕТ намеренно: значение обязано появиться только при выборе автора, а
    # дефолт `gen_random_uuid()` создал бы чат каждой карточке при вставке и
    # сломал бы смысл «компания собрана».
    op.add_column(
        "event_cards",
        sa.Column("chat_uuid", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # AC-01: уникальность чата — частичная. Предикат объявлен `sa.text`,
    # а не строкой-литералом: `op.create_index` принимает только `TextClause`.
    op.create_index(
        op.f(_CHAT_UUID_INDEX),
        "event_cards",
        ["chat_uuid"],
        unique=True,
        postgresql_where=sa.text("chat_uuid IS NOT NULL"),
    )


def downgrade() -> None:
    """Удаляет индекс и колонку (обратный порядок создания)."""
    op.drop_index(op.f(_CHAT_UUID_INDEX), table_name="event_cards")
    op.drop_column("event_cards", "chat_uuid")
