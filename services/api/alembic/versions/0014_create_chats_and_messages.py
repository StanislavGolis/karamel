"""create chats and messages + debt indexes (story 6.1)

Стори 6.1 (AC-01…AC-03, AC-05, AC-06; FR30, FR85, FR86; NFR31, NFR33).

**`chats` — один чат на карточку.** `UNIQUE (event_card_id)` — инвариант БД,
а не соглашение кода: два чата на одну встречу разошлись бы по составу
участников (ADR-04). Колонок участников (`user_a_id`/`user_b_id`) НЕТ:
компания — 2–6 человек, и состав вычисляется из `join_requests`
(`status='selected'` для режима заявок, фактические присоединения для
открытого набора). Денормализованный список разошёлся бы с заявками при
отмене участия (5а.6) и замене выбранного (5б.3).

**Контракт стори 5б.2 (D-5b.2-1).** `chats.uuid = event_cards.chat_uuid`:
идентификатор УЖЕ создан выбором автора одним `UPDATE`, и эта миграция
новое значение не генерирует. Таблица создаётся пустой — заполнение
относится к стори 6.2 (открытие чата триггером присоединения).

**`messages.body` — шифртекст (NFR31).** Формат `nonce(12) ‖ ct ‖ tag(16)`
(`app/core/crypto.py`, AES-256-GCM): plaintext в БД не попадает никогда,
ключ живёт в настройках (`CHAT_MESSAGE_KEY`). `CHECK octet_length(body) > 28`
отбивает заведомо битый блоб — ключа БД не знает, но «короче nonce+tag»
видит без него.

**Индексы долгов (AC-06).** Закрываются два долга из `deferred-work.md`:

- `X-5a.3-1` — `ix_join_requests_applicant_visible`: страница «мои заявки»
  фильтрует `applicant_id + status IN (4 видимых)` и сортирует
  `(submitted_at, uuid)`; существующий частичный `WHERE status='pending'`
  под `IN` неприменим.
- `X-5b.1-1` — `ix_event_cards_feed_visible`: предикат окна ленты
  (`status IN FEED_STATUSES`) с сортировкой `(starts_at, uuid)`.
  `ix_event_cards_community_starts` НЕ покрывает: `status` стоит третьей
  колонкой (нет предиката) и `uuid` в порядке нет (Sort в памяти).

Оба индекса объявлены и в моделях (`JoinRequest.__table_args__`,
`EventCard.__table_args__`) — иначе `alembic check` увидел бы расхождение.

**Что НЕ создаётся.** `notifications` (6.7), эндпоинты чата (6.2/6.4),
WS/pub-sub (X-43, 6.5). Данные в `chats`/`messages` не вставляются.

`downgrade` уносит обе таблицы, `ix_messages_chat_created` и оба индекса
долгов — обратный порядок, без остатков (§12.3).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.models.enums import FEED_STATUSES, JOIN_REQUEST_VISIBLE_STATUSES

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# AC-03, AC-06: имена индексов — константы, чтобы `downgrade` не разошёлся
# с `upgrade` после правки одной строки (приём 0012/0013).
_MESSAGES_HISTORY_INDEX = "ix_messages_chat_created"
_MESSAGES_SENDER_INDEX = "ix_messages_sender_id"
_APPLICANT_VISIBLE_INDEX = "ix_join_requests_applicant_visible"
_FEED_VISIBLE_INDEX = "ix_event_cards_feed_visible"

# AC-06 (CR 6.1, находка 7): предикаты СОБИРАЮТСЯ из доменных констант, а не
# пишутся «на глаз». `not_selected` в предикате «моих заявок» отсутствует
# намеренно (FR81 — заявителю он не отдаётся никогда), а лента — ровно
# `FEED_STATUSES` (`published`, `full`). Строковые копии разошлись бы с enum
# при первой правке: индекс молча перестал бы покрывать запрос (Sort
# в памяти), а `alembic check` этого не увидел бы (модель и миграция
# содержали бы одинаково устаревшую строку).
#
# Импорт из `app.models.enums` в миграции безопасен: модуль содержит только
# `StrEnum` и кортежи значений, без моделей SQLAlchemy и побочных эффектов.
_APPLICANT_VISIBLE_WHERE = (
    "status IN (" + ",".join(f"'{status.value}'" for status in JOIN_REQUEST_VISIBLE_STATUSES) + ")"
)
_FEED_VISIBLE_WHERE = (
    "status IN (" + ",".join(f"'{status.value}'" for status in FEED_STATUSES) + ")"
)

# AC-03: минимальная длина корректного блоба — nonce(12) + tag(16).
_MIN_BODY_LENGTH = 28
# CR 6.1 (находка 3): верхняя граница тела. `crypto.MAX_BODY_BYTES` (65535
# байт плейнтекста) + nonce(12) + tag(16). Миграция не импортирует приложение,
# поэтому число повторено; расхождение ловит
# `tests/integration/test_chats_schema.py::test_messages_body_upper_bound_check`.
_MAX_BODY_LENGTH = 12 + 65_535 + 16


def upgrade() -> None:
    """Создаёт `chats`, `messages` и индексы долгов X-5a.3-1 / X-5b.1-1."""
    # AC-01, AC-02: чат компании. `id` — внутренний PK (D-2: bigint наружу не
    # выходит), `uuid` — публичный и уникальный. `Identity()`, а не `serial`:
    # `serial` создаёт последовательность без владельца.
    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_card_id", sa.BigInteger(), nullable=False),
        # `TimestampMixin`: обе метки `timestamptz` с серверным `now()`.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
        # AC-01: публичный идентификатор уникален.
        sa.UniqueConstraint("uuid", name=op.f("uq_chats_uuid")),
        # AC-01, ADR-04: ОДИН чат на карточку — инвариант БД.
        sa.UniqueConstraint("event_card_id", name=op.f("uq_chats_event_card_id")),
        sa.ForeignKeyConstraint(
            ["event_card_id"],
            ["event_cards.id"],
            name=op.f("fk_chats_event_card_id_event_cards"),
            ondelete="CASCADE",
        ),
    )

    # AC-03: сообщение чата. `body BYTEA` — ШИФРТЕКСТ (NFR31), не текст.
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["chats.id"],
            name=op.f("fk_messages_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["users.id"],
            name=op.f("fk_messages_sender_id_users"),
            ondelete="CASCADE",
        ),
        # AC-03: «короче nonce+tag» — битый шифртекст, а не сообщение.
        # CR 6.1: плюс верхняя граница — иначе в `BYTEA` помещается сообщение
        # произвольного размера (страница истории тянет блоб в память целиком).
        sa.CheckConstraint(
            f"octet_length(body) > {_MIN_BODY_LENGTH} AND octet_length(body) <= {_MAX_BODY_LENGTH}",
            name=op.f("ck_messages_body_length"),
        ),
    )

    # AC-03: ключ чтения истории чата — фильтр + порядок ОДНИМ индексом.
    op.create_index(
        _MESSAGES_HISTORY_INDEX,
        "messages",
        ["chat_id", "created_at", "id"],
        unique=False,
    )

    # CR 6.1 (находка 8): `sender_id` вне `ix_messages_chat_created`, поэтому
    # без этого индекса каскад от `users` сканирует всю таблицу сообщений.
    op.create_index(
        _MESSAGES_SENDER_INDEX,
        "messages",
        ["sender_id"],
        unique=False,
    )

    # AC-06 (X-5a.3-1): страница «мои заявки». Предикат — четыре видимых
    # статуса; порядок колонок — ровно ключ курсора `(submitted_at, uuid)`.
    op.create_index(
        _APPLICANT_VISIBLE_INDEX,
        "join_requests",
        ["applicant_id", "submitted_at", "uuid"],
        unique=False,
        postgresql_where=sa.text(_APPLICANT_VISIBLE_WHERE),
    )

    # AC-06 (X-5b.1-1): предикат окна ленты. `uuid` в порядке обязателен:
    # без него `WHERE (starts_at, uuid) >` не покрыт и страница сортируется
    # в памяти.
    op.create_index(
        _FEED_VISIBLE_INDEX,
        "event_cards",
        ["community_slug", "starts_at", "uuid"],
        unique=False,
        postgresql_where=sa.text(_FEED_VISIBLE_WHERE),
    )


def downgrade() -> None:
    """Удаляет индексы и обе таблицы (обратный порядок создания)."""
    op.drop_index(_FEED_VISIBLE_INDEX, table_name="event_cards")
    op.drop_index(_APPLICANT_VISIBLE_INDEX, table_name="join_requests")
    op.drop_index(_MESSAGES_SENDER_INDEX, table_name="messages")
    op.drop_index(_MESSAGES_HISTORY_INDEX, table_name="messages")
    op.drop_table("messages")
    op.drop_table("chats")
