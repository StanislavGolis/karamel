"""create join requests table and partial indexes

Стори 5а.1 (AC-01…AC-05, AC-10). Заявки на участие в карточках режима
«заявки» (вместимость 1, 4.1): таблица `join_requests`, два `CHECK`,
`uq`-uuid, три индекса, из них один уникальный частичный.

**Почему `0012`, а не `0009` (D-5a.1-0).** Шард эпика 5а и
`architecture §14.1` («Шаг 3 → `0009`») называют миграцию `0009`, но
`0009`–`0011` заняты эпиком 4 (`0009_moderation_indexes`,
`0010_card_active_limit_indexes`, `0011_card_full_capacity_and_archive`).
Переименовывать применённые ревизии нельзя: в `0011` живёт починенный
`ck_event_cards_full_capacity` (X-35), и смена `revision` сломала бы
уже развёрнутые базы. §14.1 считается смещённой на +3 с шага 3
(долг документации — в `deferred-work.md`).

**Почему `uq` ЧАСТИЧНЫЙ (AC-04, D-5a.1-1, FR82).** Безусловный
`UNIQUE (card_id, applicant_id)` запретил бы повторную подачу после
отмены (FR82) — терминальная заявка навсегда занимала бы пару.
Предикат `status IN ('pending','selected')` держит уникальность ровно
для активных: `selected` обязан оставаться под ней (двух активных
ролей на одной карточке быть не может), а `not_selected`/`expired`/
`cancelled` освобождают пару.

**Почему гарда «нельзя на свою карточку» здесь НЕТ (AC-06, D-5a.1-2).**
Правило межтабличное (`card_id → event_cards.author_id`): `CHECK` его
не выражает, триггер запрещён D-5. Проверка — сервис 5а.2; доменный
код ошибки заведён в 5а.1.

**X-39 (AC-09).** `ck_event_cards_full_capacity` из `0011` НЕ трогается.
Ограничение корректно: оно запрещает несогласованное промежуточное
состояние и допускает согласованное одним `UPDATE` («инкремент +
`status='full'` в одном `SET`»). Ломается репозиторий
(`build_mark_full_statement` — отдельный CAS), это чинится в 5а.5.
Здесь достижимость только доказана тест-инвариантом (Task 7).

**Границы.** Ни `attendance`, ни `chats` не создаются (5а.5/эпик 6).
HTTP нет: подача — 5а.2, отмена/список — 5а.3, список заявившихся —
5а.4. `downgrade` уносит таблицу и все три индекса; чужие таблицы
(`users`, `profiles`, `profile_photos`, `places`, `event_cards`) не
меняются.

Имена ограничений — через `op.f` (§18.7): иначе naming convention
модели складывается с именем и даёт двойной префикс
(`ck_join_requests_ck_join_requests_status`), что ломает `alembic check`
и валит `downgrade` на несуществующем имени (дефект 4.1). Имена
индексов — полной строкой: индексы в naming convention не участвуют
(урок `0009`). Список статусов собирается из `JoinRequestStatus`
(AC-02), а не переписывается строкой.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-24 02:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op
from app.models.enums import JoinRequestStatus

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# AC-02, D-3: единственный источник набора статусов — `JoinRequestStatus`.
# Строка CHECK собирается здесь, чтобы добавление значения было одной
# миграцией, а не правкой в двух местах.
_STATUSES: tuple[str, ...] = tuple(str(status) for status in JoinRequestStatus)
_STATUS_IN: str = "status IN (" + ", ".join(f"'{value}'" for value in _STATUSES) + ")"

# AC-03: явный набор «пробельных» для btrim — тот же, что у `places.name`
# (CR 3.1, патч 8) и `event_cards.title`. `btrim(x)` без второго аргумента
# режет только U+0020, и `message` из одного таба прошёл бы CHECK.
_WS: str = "E' \\t\\r\\n\\f\\v\u00a0\u202f'"

_UQ_ACTIVE_INDEX = "uq_join_requests_card_applicant_active"
_APPLICANT_PENDING_INDEX = "ix_join_requests_applicant_pending"
_CARD_QUEUE_INDEX = "ix_join_requests_card_queue"


def upgrade() -> None:
    """Создаёт `join_requests`, её ограничения и три индекса."""
    op.create_table(
        "join_requests",
        # AC-01, D-2: внутренний bigint-PK, наружу не выходит.
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        # AC-01: публичный идентификатор.
        sa.Column(
            "uuid",
            PGUUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # AC-01: каскад — заявка без карточки не имеет смысла.
        sa.Column("card_id", sa.BigInteger(), nullable=False),
        # AC-01: каскад — обработка ПД (NFR1–NFR4).
        sa.Column("applicant_id", sa.BigInteger(), nullable=False),
        # AC-03, FR76: сообщение заявителя.
        sa.Column("message", sa.String(length=300), nullable=False),
        # AC-02: состояние заявки.
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        # AC-01: момент подачи (ключ очереди автовыбора).
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # AC-01, D-5a.1-6: момент решения, NULL пока `pending`.
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        # AC-01, D-5a.1-5: копия окна решения карточки на момент подачи.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # TimestampMixin: те же дефолты, что у остальных таблиц.
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_join_requests")),
        # AC-01: публичный идентификатор уникален.
        sa.UniqueConstraint("uuid", name=op.f("uq_join_requests_uuid")),
        # AC-01: каскады по обеим ссылкам.
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["event_cards.id"],
            name=op.f("fk_join_requests_card_id_event_cards"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["users.id"],
            name=op.f("fk_join_requests_applicant_id_users"),
            ondelete="CASCADE",
        ),
        # AC-02: статус — только значения `JoinRequestStatus`.
        sa.CheckConstraint(_STATUS_IN, name=op.f("ck_join_requests_status")),
        # AC-03: сообщение без пробельных по краям. РАВЕНСТВО, а не `<> ''`:
        # `<> ''` отбивал бы только пустоту (класс дефекта `places.name`).
        sa.CheckConstraint(
            f"message = btrim(message, {_WS})", name=op.f("ck_join_requests_message")
        ),
    )
    # AC-04, FR77/NFR29, D-5a.1-1: одна АКТИВНАЯ заявка на пару. Частичный:
    # безусловный запретил бы повторную подачу после отмены (FR82).
    op.create_index(
        _UQ_ACTIVE_INDEX,
        "join_requests",
        ["card_id", "applicant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','selected')"),
    )
    # AC-05, FR77: подсчёт лимита двух активных и «мои заявки».
    op.create_index(
        _APPLICANT_PENDING_INDEX,
        "join_requests",
        ["applicant_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    # AC-05, ADR-05: очередь автовыбора и список заявившихся. Порядок колонок —
    # ровно ключ сортировки `submitted_at ASC, id ASC`.
    op.create_index(
        _CARD_QUEUE_INDEX,
        "join_requests",
        ["card_id", "submitted_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Удаляет индексы и таблицу (обратный порядок создания)."""
    op.drop_index(_CARD_QUEUE_INDEX, table_name="join_requests")
    op.drop_index(_APPLICANT_PENDING_INDEX, table_name="join_requests")
    op.drop_index(_UQ_ACTIVE_INDEX, table_name="join_requests")
    op.drop_table("join_requests")
