"""add messages system flag and nullable sender (story 6.7)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-24

Стори 6.7 (AC-04; FR106, NFR11). Снимает блокер X-6.6-5: до этой миграции
`sender_id` был `NOT NULL`, поэтому системное сообщение без автора было
физически невозможно (`IntegrityError` на вставке).

**Признак системности — ОТДЕЛЬНАЯ колонка `is_system` (D-6.7-1), а не вывод
из `NULL`.** `sender_id IS NULL` не отличает «системное сообщение» от
«строка, у которой FK обнулился при смене политики удаления пользователей».
`server_default 'false'` даёт существующим строкам корректное значение без
backfill-скрипта, `NOT NULL` запрещает третье состояние.

**`CHECK` — БИКОНДИЦИОНАЛ.** `(is_system AND sender_id IS NULL) OR
(NOT is_system AND sender_id IS NOT NULL)`. Односторонний вариант
(`NOT is_system OR sender_id IS NULL`) разрешил бы обычное сообщение без
автора: клиент вычислил бы «своё» по `sender_uuid == viewerUuid`, получил бы
`null` и отрисовал бы чужое как своё, а счётчик непрочитанных считал бы его
вечно новым.

**`downgrade` не удаляет данные ради возврата к `NOT NULL`.** Если в БД есть
системные строки, откат обязан упасть с внятной ошибкой, а не молча стереть
факты переписки (§12.3: откат без остатков, но не ценой потери истории).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Имя БЕЗ префикса конвенции: `naming_convention` из `models/base.py`
#: добавит `ck_messages_`. Литерал с префиксом дал бы двойной
#: `ck_messages_ck_messages_...` — класс дефекта 3.1/4.1, поймано тестом
#: `test_constraint_names_match_model`.
CHECK_NAME = "system_sender_exclusive"


def upgrade() -> None:
    """`sender_id` → nullable; `is_system`; CHECK-инвариант."""
    op.alter_column("messages", "sender_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column(
        "messages",
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        CHECK_NAME,
        "messages",
        "(is_system AND sender_id IS NULL) OR (NOT is_system AND sender_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Обратный порядок; при наличии системных строк — явная ошибка.

    Молчаливое удаление системных сообщений потеряло бы факты («участник
    отменил участие»), ради которых стори и делалась. Поэтому откат падает
    с понятным сообщением: сначала оператор решает, что делать с историей.
    """
    connection = op.get_bind()
    remaining = connection.execute(
        sa.text("SELECT count(*) FROM messages WHERE is_system")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"Откат 0019 невозможен: в messages {remaining} системных сообщений. "
            "Удалите их осознанно (DELETE FROM messages WHERE is_system) и повторите."
        )
    op.drop_constraint(CHECK_NAME, "messages", type_="check")
    op.drop_column("messages", "is_system")
    op.alter_column("messages", "sender_id", existing_type=sa.BigInteger(), nullable=False)
