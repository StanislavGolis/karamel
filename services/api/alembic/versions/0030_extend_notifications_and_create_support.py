"""extend notifications and create support_tickets (story 9.1)

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-25

Стори 9.1 (AC-01…AC-04; FR43–FR46, FR55; NFR11; ADR-11).

**`notifications` УЖЕ СУЩЕСТВУЕТ — эта миграция её РАСШИРЯЕТ** (D-9.1-1).
Таблицу создала `0021` в стори 7.3 как минимальный аутбокс воркера явки.
Эпик 9 был написан до эпика 7 и описывает `notifications` как новую — это
устаревшее описание. `CREATE TABLE notifications` здесь был бы дефектом:
упал бы на существующей таблице (а в offline-режиме дал бы второй `CREATE`
в скрипте, который оператор применил бы руками).

**`support_tickets` создаётся с нуля.** Этой таблицы нет ни в одной ревизии
(`grep -rn 'support_tickets' alembic/` до 0030 даёт только комментарий в
`0023`). Архитектура §4.2 называла миграцию `0013`, но `0013` занята
(`0013_add_event_card_chat_uuid.py`), а фактический head — `0029` (D-9.1-5).

**`notifications.uuid` с серверным дефолтом, и дефолт НЕ снимается**
(D-9.1-8). Строки пишет воркер сырым `INSERT ... SELECT`
(`app/repositories/notifications.py`), ORM-дефолты на этом пути не
срабатывают: без `server_default` первая же вставка упала бы с
`NotNullViolation`. Обратная сторона — `uuid` теперь генерируется БД, и
клиент не может его предсказать; это ровно то, что нужно от публичного ID.

**Два CHECK на `type`, и оба нужны** (D-9.1-3). `ck_notifications_type_format`
(из `0021`) проверяет ФОРМУ `сущность.действие`, новый `ck_notifications_type`
— ПЕРЕЧЕНЬ. Это не дублирование: регекс пропускает `foo.bar` (валидная форма,
чужая сущность), а перечень пропускает `BAD_FORMAT` (форма ломается, но если
бы перечня не было — тоже прошло бы). Критично второе: NFR60 велит клиенту
ИГНОРИРОВАТЬ неизвестный тип, поэтому опечатку в имени не заметит никто —
ни клиент, ни пользователь, ни лог.

**Перечень обязан включать 4 типа, уже записанных в БД** до этой стори:
`attendance.request` (7.3), `report.created` (8.2), `report.resolved` и
`sanction.issued` (8.4). Миграция на живой базе со строками этих типов
упала бы на `ADD CONSTRAINT` с `violates check constraint`.

**Индекс `(user_id, read_at)` — AC-03.** Существующий
`ix_notifications_user_delivery (user_id, delivered_at)` НЕ удаляется
(D-9.1-9): у него другой предикат (`delivered_at IS NULL` в
`count_undelivered`), он обслуживает доставку, а не прочтение. Партиальный
вариант `WHERE read_at IS NULL` не создаётся — AC-03 просит полный состав,
а преждевременная оптимизация без объёма недоказуема (D-9.1-10).

**`downgrade` НЕ удаляет `notifications`** (D-9.1-2). Её удаление — предмет
отката `0021`, и он защищён guard'ом «непустая таблица останавливает откат».
Дроп здесь обошёл бы guard и молча уничтожил адресные запросы явки, которые
7.5 использует как источник истории. `downgrade` снимает только НАДСТРОЙКИ:
новый CHECK, индекс и колонку `uuid`.

**Порядок в `downgrade` обратный `upgrade`:** сначала `support_tickets`
(она независима), затем надстройки `notifications`. Колонка `uuid` удаляется
последней: до неё снимается `UNIQUE`, иначе `DROP COLUMN` потребовал бы
`CASCADE`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
_down_revision = "0029"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: AC-02: ПЕРЕЧЕНЬ допустимых типов. Дублируется из
#: `app.models.enums.NOTIFICATION_TYPES` осознанно: миграция обязана читать
#: только `sa` и не импортировать приложение (иначе историческая ревизия
#: сломается при любом переименовании в `app/`). Расхождение ловится тестом
#: сверки трёх источников: Python-перечень ↔ этот литерал ↔ фактическая схема.
#:
#: Порядок фиксирован: он входит в текст ограничения, и его изменение создало
#: бы диф `alembic check` без изменения смысла.
NOTIFICATION_TYPES: tuple[str, ...] = (
    "attendance.request",
    "attendance.result",
    "card.closed_impersonal",
    "company.assembled",
    "report.created",
    "report.resolved",
    "request.received",
    "request.selected",
    "sanction.issued",
    "support.replied",
    "window.expiring",
)

#: AC-01: домен статуса обращения.
TICKET_STATUSES: tuple[str, ...] = ("open", "answered", "closed")

#: AC-01: бикондиционал «статус ↔ ответ». См. `app/models/support_ticket.py`:
#: наивная форма `answer IS NULL OR answer IS NOT NULL` даёт `UNKNOWN` при
#: `answer IS NULL`, а `CHECK` отвергает только `FALSE`.
ANSWER_PAIR_SQL = (
    "(status = 'open' AND answer IS NULL AND answered_at IS NULL) "
    "OR (status IN ('answered', 'closed') AND answer IS NOT NULL "
    " AND answered_at IS NOT NULL)"
)

#: AC-01: пустое тело запрещено схемой, не только `422` в 9.5.
#:
#: **Две неверные попытки, обе пойманы измерением, а не рассуждением.**
#: (1) `length(btrim(body)) > 0` — `btrim` без аргументов снимает только
#: пробел U+0020, тело из табов проходило. (2) `[^[:space:]]` — класс
#: `[:space:]` зависит от **libc/locale**: при `LC_CTYPE=C` NBSP U+00A0,
#: NEL U+0085, NNBSP U+202F и BOM U+FEFF в него НЕ входят и проходили как
#: «непустые» (CR 9.1 H-2, измерено). Ограничение было непортируемым:
#: на стенде с другой локалью вело бы себя иначе.
#:
#: Здесь — явное перечисление Unicode-пробелов: Zs-класс (пробел, NBSP,
#: EN QUAD…HAIR SPACE, NNBSP, MEDIUM MATHEMATICAL SPACE, IDEOGRAPHIC SPACE)
#: плюс управляющие разделители NEL/LS/PS/BOM. От локали не зависит.
BODY_NOT_BLANK_SQL = (
    "body ~ '[^[:space:]\u00a0\u0085\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]'"
)

#: AC-01: ответ не раньше создания обращения.
ANSWER_ORDER_SQL = "answered_at >= created_at"

#: Имена БЕЗ префикса конвенции: `naming_convention` из `models/base.py`
#: добавит `ck_notifications_`/`ck_support_tickets_`. Литерал с префиксом дал
#: бы двойной префикс — класс дефекта 3.1/4.1 (§18.7).
TYPE_CHECK_NAME = "type"
USER_READ_INDEX = "ix_notifications_user_read"


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col IN ('a','b')`. Копия `_in_clause` из моделей — миграция не импортирует `app`."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Расширение `notifications` + создание `support_tickets` (AC-01…AC-03)."""
    # ---- AC-01, D-9.1-8: публичный ID уведомления ----------------
    # `add_column` с `server_default` заполняет существующие строки одним
    # проходом (в PG 11+ это не перезапись таблицы). Дефолт ОСТАЁТСЯ — см.
    # docstring модуля.
    op.add_column(
        "notifications",
        sa.Column(
            "uuid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.create_unique_constraint(op.f("uq_notifications_uuid"), "notifications", ["uuid"])

    # ---- AC-02, D-9.1-3: перечень типов поверх проверки формы ----
    op.create_check_constraint(
        TYPE_CHECK_NAME, "notifications", _in_clause("type", NOTIFICATION_TYPES)
    )

    # ---- AC-03, D-9.1-10: индекс непрочитанных -------------------
    op.create_index(USER_READ_INDEX, "notifications", ["user_id", "read_at"])

    # ---- AC-01: support_tickets с нуля ---------------------------
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "uuid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'open'")),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # AC-01: автор. `CASCADE` — обращение без автора не адресуется.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_support_tickets_user_id_users"),
            ondelete="CASCADE",
        ),
        # D-9.1-6: кто ответил. `SET NULL` — обращение переживает удаление
        # учётки модератора (прецедент: `sanctions.moderator_id`).
        sa.ForeignKeyConstraint(
            ["answered_by_id"],
            ["users.id"],
            name=op.f("fk_support_tickets_answered_by_id_users"),
            ondelete="SET NULL",
        ),
        # AC-01, P-10: `Identity()` = `GENERATED BY DEFAULT`, явная вставка
        # `id = 0` проходит; единственная защита — этот CHECK.
        sa.CheckConstraint("id > 0", name="id_positive"),
        # AC-01: домен статуса.
        sa.CheckConstraint(_in_clause("status", TICKET_STATUSES), name="status"),
        # AC-01, AC 9.5: пустое тело запрещено схемой.
        sa.CheckConstraint(BODY_NOT_BLANK_SQL, name="body_not_blank"),
        # AC-01: статус и ответ согласованы.
        sa.CheckConstraint(ANSWER_PAIR_SQL, name="answer_pair"),
        # AC-01: ответ не раньше создания.
        sa.CheckConstraint(ANSWER_ORDER_SQL, name="answer_order"),
    )
    op.create_unique_constraint(op.f("uq_support_tickets_uuid"), "support_tickets", ["uuid"])

    # 9.5: список своих обращений — `WHERE user_id = :u ORDER BY created_at DESC`.
    op.create_index(
        "ix_support_tickets_user_created",
        "support_tickets",
        ["user_id", sa.text("created_at DESC")],
    )
    # 9.6: очередь модератора — `WHERE status = 'open' ORDER BY created_at`.
    op.create_index("ix_support_tickets_queue", "support_tickets", ["status", "created_at"])


def downgrade() -> None:
    """Снимает надстройки `notifications` и удаляет `support_tickets` (AC-04).

    **`notifications` НЕ удаляется** (D-9.1-2): её дроп — предмет отката `0021`
    с guard'ом «непустая таблица останавливает откат». Здесь снимаются только
    добавленные этой миграцией объекты, остатков не остаётся.
    """
    # Обратный порядок: сначала независимая таблица.
    op.drop_index("ix_support_tickets_queue", table_name="support_tickets")
    op.drop_index("ix_support_tickets_user_created", table_name="support_tickets")
    op.drop_constraint(op.f("uq_support_tickets_uuid"), "support_tickets", type_="unique")
    op.drop_table("support_tickets")

    # Затем надстройки `notifications`. Индекс до CHECK: оба независимы, но
    # при частичном откате индекс дешевле восстановить.
    op.drop_index(USER_READ_INDEX, table_name="notifications")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.drop_constraint(op.f("uq_notifications_uuid"), "notifications", type_="unique")
    op.drop_column("notifications", "uuid")
