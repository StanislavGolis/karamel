"""add messages uuid and client_message_id (story 6.4)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-24

Стори 6.4 (AC-01, AC-02, AC-04; FR31, NFR18, NFR31). Таблицы `chats` и
`messages` созданы в `0014` (6.1) — здесь ТОЛЬКО недостающее для обмена
сообщениями, без пересоздания таблиц.

**`uuid` — публичный адрес сообщения (D-2).** Внутренний `id` наружу не
выходит никогда: `messages` адресуются клиентом (оптимистичное добавление,
квитанции 6.5), и отдавать `bigint` значило бы разрешить перебор `/1..N`
в обход состава компании. `UNIQUE` обязателен: `uuid` — адрес, а не
«украшение».

**`client_message_id` — идемпотентность отправки (NFR18).** Клиент
генерирует UUIDv4 ДО отправки и повторяет запрос с ТЕМ ЖЕ значением после
обрыва сети. Уникальность ЧАСТИЧНАЯ (`WHERE client_message_id IS NOT NULL`):
безусловный `UNIQUE (chat_id, client_message_id)` в PostgreSQL допускает
много `NULL` (NULL не равен NULL), но частичный индекс делает намерение
явным и не мешает будущим путям записи без клиентского идентификатора
(системные сообщения 6.7).

**`NOT NULL` без «+nullable колонки» (§12.3).** Таблица пуста: 6.1 создала
схему без данных, 6.4 — первая стори, которая в неё пишет. Добавление
`nullable=True` + backfill + `SET NOT NULL` тремя шагами было бы нужно
только при живых строках — здесь это лишний риск и лишние проходы.

`downgrade` уносит оба ограничения и обе колонки — без остатков.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # AC-01, D-2: публичный адрес сообщения. `server_default` НЕТ: значение
    # приходит из сервиса (`uuid4`), и дефолт БД скрыл бы путь, где его забыли.
    op.add_column(
        "messages",
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # AC-04, NFR18: клиентский идентификатор для идемпотентного ретрая.
    op.add_column(
        "messages",
        sa.Column("client_message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Таблица пуста (6.1 данных не вставляла) — `NOT NULL` ставится сразу,
    # без промежуточного backfill-прохода.
    op.alter_column("messages", "uuid", nullable=False)
    op.create_unique_constraint(op.f("uq_messages_uuid"), "messages", ["uuid"])
    # AC-04: идемпотентность по паре «чат + клиентский идентификатор».
    # Частичный уникальный индекс, а не `UniqueConstraint`: PostgreSQL не
    # умеет частичный `UNIQUE CONSTRAINT`, а намерение («только для
    # непустых») обязано быть видно в схеме.
    op.create_index(
        "uq_messages_chat_client_id",
        "messages",
        ["chat_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )
    # AC-02, D-2: ключ чтения истории под ПУБЛИЧНЫЙ курсор. Курсор проекта
    # кодирует `(created_at, uuid)`, а не `(created_at, id)`: внутренний
    # `bigint` в query-параметре стал бы публичным адресом и разрешил бы
    # перебор `/1..N` (тот же довод, что в `card_feed_cursor`). Индекс
    # `ix_messages_chat_created` из `0014` сортировку по `uuid` НЕ покрывает,
    # поэтому нужен свой ключ — иначе на каждой странице `Sort` в памяти.
    op.create_index(
        "ix_messages_chat_created_uuid",
        "messages",
        ["chat_id", "created_at", "uuid"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_messages_chat_client_id", table_name="messages")
    op.drop_constraint(op.f("uq_messages_uuid"), "messages", type_="unique")
    op.drop_column("messages", "client_message_id")
    op.drop_column("messages", "uuid")
