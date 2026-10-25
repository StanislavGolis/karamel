"""create event_cards table, five card indexes and the places cursor index

Стори 4.1 (AC-01…AC-07). Карточка события: что, когда, где, вместимость,
окно решения, описание (FR69). Место — ссылка на проверенный справочник
`places` (FR98), координат нет ни в каком виде (NFR34).

Состав миграции:

1. Таблица `event_cards` с тремя FK (`users` CASCADE, `places` RESTRICT,
   `users` SET NULL для `verified_by`), четырьмя CHECK по значениям enum'ов
   и восемью структурными CHECK (AC-01, AC-05, AC-06).
2. Пять индексов карточки (AC-07): лента, лимит автора, очередь модератора,
   воркер окон, воркер явки.
3. Индекс `idx_places_community_cursor` на `places` — **X-02**, закрывается
   здесь по решению пользователя от 2026-10-23 (п.2): отдельного chore нет.
   Атомарность сохранена: один `revision` — один откат, индекс создаётся и
   удаляется вместе с карточкой. Следствие: `0007` можно откатить только
   после отката `0008` — штатный порядок Alembic, а не дефект.

**Не входит в миграцию (осознанно):**

- `join_requests`, `attendance`, `chats` — свои миграции (0010 и далее),
  карточка ссылается только на `users` и `places`.
- Индекс `profile_photos(created_at, id) WHERE status='pending'` (Х-01) —
  он относится к модерации карточки (4.3), а не к её схеме.

**D-4.1-2:** `expires_at` объявлен `NOT NULL` с момента создания. 4.2
вычислит поле при вставке и ПЕРЕСЧИСЛИТ при публикации
(`LEAST(starts_at, now() + окно)`, ADR-02). «Nullable до публикации»
отклонено: три состояния вместо двух и условный CHECK.

Имена ограничений заданы через `op.f` (§18.7): без него naming convention
из `models/base.py` складывается с именем и даёт двойной префикс
(`ck_event_cards_ck_event_cards_status`), который не совпадает с моделью,
ломает `alembic check` и валит `downgrade` на несуществующем имени.

Списки значений собираются из `EventCardStatus`, `CardVerificationStatus`
и `PlaceCategory`, а не переписываются строкой (AC-06, D-3): иначе
добавление значения в enum не изменило бы ограничение в БД.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23 21:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op
from app.models.enums import CardVerificationStatus, EventCardStatus, PlaceCategory

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col IN ('a','b')` из значений StrEnum — единый источник списка."""
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


_STATUS_IN = _in_clause("status", tuple(str(v) for v in EventCardStatus))
_VERIFICATION_IN = _in_clause("verification_status", tuple(str(v) for v in CardVerificationStatus))
_CATEGORY_IN = _in_clause("category", tuple(str(v) for v in PlaceCategory))

_INDEXES = (
    ("ix_event_cards_community_starts", ["community_slug", "starts_at", "status"], None),
    (
        "ix_event_cards_author_active",
        ["author_id"],
        "status IN ('draft','published')",
    ),
    (
        "ix_event_cards_pending_verification",
        ["created_at"],
        "verification_status = 'pending'",
    ),
    ("ix_event_cards_due_windows", ["expires_at"], "status = 'published'"),
    (
        "ix_event_cards_prompt_due",
        ["starts_at"],
        "attendance_prompted_at IS NULL AND status IN ('full','closed','finished')",
    ),
)


def upgrade() -> None:
    """Создаёт `event_cards`, пять её индексов и индекс X-02 на `places`."""
    op.create_table(
        "event_cards",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "uuid",
            PGUUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # AC-01: удаление учётки уносит карточки — без автора карточка не может
        # ни закрыться, ни отмениться (FR74/FR100).
        sa.Column(
            "author_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # AC-04, FR98: `RESTRICT` — место с карточкой физически не удаляется;
        # «скрыть» (FR68) работает через `is_active`, а не DELETE.
        sa.Column(
            "place_id",
            sa.BigInteger(),
            sa.ForeignKey("places.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("community_slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.SmallInteger(), nullable=False),
        sa.Column("decision_window_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "filled_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "verification_status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        # AC-01, ADR-08: `verified_by IS NULL` = автоматическая проверка.
        # `SET NULL` — удаление модератора не уносит карточку и не теряет
        # факт проверки: остаётся `verified_at`.
        sa.Column(
            "verified_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attendance_prompted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reminded_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_cards")),
        # AC-01: публичный идентификатор уникален.
        sa.UniqueConstraint("uuid", name=op.f("uq_event_cards_uuid")),
        # AC-01: состояния — значения `EventCardStatus`.
        sa.CheckConstraint(_STATUS_IN, name=op.f("ck_event_cards_status")),
        # AC-01, ADR-08: исход проверки контента.
        sa.CheckConstraint(_VERIFICATION_IN, name=op.f("ck_event_cards_verification_status")),
        # AC-06, ADR-12: категория — тот же список, что у `places.category`.
        sa.CheckConstraint(_CATEGORY_IN, name=op.f("ck_event_cards_category")),
        # AC-01, FR70: вместимость 1..6.
        sa.CheckConstraint("capacity BETWEEN 1 AND 6", name=op.f("ck_event_cards_capacity")),
        # AC-05, ADR-10: занятые места не выходят за вместимость.
        sa.CheckConstraint(
            "filled_count BETWEEN 0 AND capacity", name=op.f("ck_event_cards_filled_count")
        ),
        # AC-01, FR79: окно решения 15 минут .. 7 суток.
        sa.CheckConstraint(
            "decision_window_minutes BETWEEN 15 AND 10080",
            name=op.f("ck_event_cards_decision_window"),
        ),
        # AC-01, ADR-02: окно не закрывается позже начала события.
        sa.CheckConstraint("expires_at <= starts_at", name=op.f("ck_event_cards_expires_at")),
        # AC-06, ADR-01: тот же шаблон слага, что у `places`.
        sa.CheckConstraint(
            "community_slug ~ '^[a-z0-9_-]{3,64}$'",
            name=op.f("ck_event_cards_community_slug"),
        ),
        # AC-05, ADR-02: опубликованная карточка имеет момент публикации.
        sa.CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name=op.f("ck_event_cards_published"),
        ),
        # AC-05, FR113: отклонение без причины запрещено.
        sa.CheckConstraint(
            "verification_status <> 'rejected' OR rejection_reason IS NOT NULL",
            name=op.f("ck_event_cards_rejection_reason"),
        ),
        # AC-05, ADR-10: при полной вместимости `published` запрещён.
        sa.CheckConstraint(
            "filled_count < capacity OR status IN ('full','closed','finished','cancelled')",
            name=op.f("ck_event_cards_full_capacity"),
        ),
        # AC-03, FR70: режим заявок занимает ровно одно место.
        sa.CheckConstraint(
            "capacity <> 1 OR filled_count <= 1", name=op.f("ck_event_cards_request_mode")
        ),
    )

    # AC-07: пять индексов карточки. Имена полные — индексы в naming
    # convention не участвуют. Частичные предикаты повторяют модель
    # дословно: расхождение дало бы индекс, который не совпадает с моделью,
    # и `alembic check` показал бы ложное расхождение.
    for name, columns, predicate in _INDEXES:
        op.create_index(
            name,
            "event_cards",
            columns,
            unique=False,
            postgresql_where=sa.text(predicate) if predicate else None,
        )

    # AC-07, X-02 (решение пользователя 2026-10-23, п.2): индекс под курсорную
    # выдачу мест 3.3. `GET /api/v1/places` сортирует по `(created_at, uuid)`
    # и фильтрует по `community_slug` + `is_active`; существующий
    # `idx_places_community_active` покрывает фильтр, но НЕ порядок —
    # PostgreSQL делает `Sort` в памяти.
    #
    # Здесь, а не отдельным chore: пользователь решил включить X-02 в `0008`
    # вместе с таблицей карточек. Атомарность сохраняется — один `revision`,
    # один откат. Откат `0007` возможен только после отката `0008` —
    # штатный порядок Alembic.
    op.create_index(
        "idx_places_community_cursor",
        "places",
        ["community_slug", "created_at", "uuid"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    """Удаляет индексы и таблицу в обратном порядке создания.

    X-02 удаляется вместе с карточкой: он принадлежит этой ревизии.
    Без него `downgrade` оставил бы объект, которого нет в `0007`, и
    повторный `upgrade` падал бы на «индекс уже существует».
    """
    op.drop_index("idx_places_community_cursor", table_name="places")
    for name, _columns, _predicate in reversed(_INDEXES):
        op.drop_index(name, table_name="event_cards")
    op.drop_table("event_cards")
