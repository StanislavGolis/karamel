"""create reports for no_show complaints (story 7.4, AC-01, AC-02)

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-24

**Почему создаётся заново, а не «расширяется».** Шард эпика 7 и
`architecture.md` §4.2 описывают `reports` как СУЩЕСТВУЮЩУЮ таблицу, которую
надо расширить на неявку (`0012 extend_reports_for_no_show.py`). Проверено
2026-10-24 на живой базе:

| Проверка | Результат |
|---|---|
| `\\dt` в `karamel_db` | 12 таблиц; `reports`/`sanctions` отсутствуют |
| `SELECT to_regclass('public.reports')` | `NULL` |
| `grep -rn -iE 'complaint\\|abuse\\|target_type\\|report_' app/ alembic/` | совпадений нет |
| `architecture-v2-action-first.md:359` | v1-таблицы `applications`, `likes`, `reports`,
  `sanctions`, `notifications`, `support_tickets` **никогда не создавались** |

Значит «опереться на существующую подсистему жалоб» невозможно: опоры нет.
Принят вариант (б) — **минимальная** `reports` под ветку `no_show` (D-7.4-1).
Прецедент того же класса — решение 1 пакета: минимальная `notifications`
в `0021`, полный центр уведомлений в эпике 9.

**Минимальность реализована ограничениями.** `CHECK target_type IN ('no_show')`
— точное выражение FR90. Жалоба на сообщение (8.1) добавит второе значение
и `chat_id` с бикондиционалом; до этого домен уже, и это осознанно (D-7.4-8,
X-7.4-1).

**Почему `attendance_id NOT NULL`, а не `attendance_id` + `user_id`.**
ADR-04: компания 2–6 человек, и «жалоба на карточку» не говорит, КТО не пришёл;
жалоба на пользователя без пары неотличима от жалобы на карточку, где он
участвовал дважды (D-7.4-2). Обвиняемый вычислим из пары (у автора карточки —
`participant_id`, у участника — `author_id`), поэтому денормализации нет:
две копии факта разошлись бы при изменении состава.

**`UNIQUE (attendance_id, reporter_id)` — единственная защита от дубля.**
Сервис пишет `INSERT ... ON CONFLICT (attendance_id, reporter_id) DO NOTHING
RETURNING`; пустой `RETURNING` — признак «уже пожаловано», дающий `409`
(D-7.4-3, P-3: предпроверка запрещена — окно между `SELECT EXISTS` и
`INSERT` дало бы две строки).

**Индекс — составной `(status, reported_at)`, не по одному `reported_at`.**
Очередь модератора 8.3 всегда фильтрует по статусу и сортирует по времени
подачи; предикат `WHERE status = :s ORDER BY reported_at` покрывается
составным индексом целиком (D-7.4-5, NFR52). Тот же индекс обслуживает
будущую выборку просроченных (NFR63).

**Бикондиционал, а не два раздельных `CHECK`.** `(status = 'open') =
(resolved_at IS NULL)` запрещает «разобрана, но время разбора неизвестно» —
состояние, из которого очередь не выбралась бы. Второе ограничение
`resolved_at >= reported_at` само по себе достаточно только вместе с первым:
при `resolved_at IS NULL` сравнение даёт `UNKNOWN`, а `CHECK` отвергает лишь
`FALSE` (урок CR 7.3 H-2 — та же ошибка формы).

**`downgrade` с guard.** Непустая `reports` останавливает откат с внятным
сообщением: жалобы — это данные, которые нельзя уничтожать молча (AC-02,
§12.3). Пустая таблица удаляется целиком вместе с индексом и ограничениями —
остатков нет.

**Backfill не выполняется.** Жалоб прошлых периодов не существует: таблица
создаётся с нуля, и данных, которые надо было бы перенести, нет.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
_down_revision = "0022"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Имена БЕЗ префикса конвенции: `naming_convention` из `models/base.py`
#: добавит `ck_reports_`. Литерал с префиксом дал бы двойной префикс —
#: класс дефекта 3.1/4.1, ловится тестом имён ограничений.
ID_POSITIVE_CHECK_NAME = "id_positive"
TARGET_TYPE_CHECK_NAME = "target_type"
STATUS_CHECK_NAME = "status"
STATUS_RESOLVED_CHECK_NAME = "status_resolved_consistency"
RESOLVED_AFTER_REPORTED_CHECK_NAME = "resolved_after_reported"

#: AC-05: объект жалобы — только неявка. Дублируется из
#: `app/models/report.py` осознанно: миграция обязана читать только `sa`
#: и не импортировать приложение. Расхождение ловится тестом схемы.
_TARGET_TYPES = ("no_show",)

#: AC-10: состояния разбора. `open` создаёт код, остальные — модератор (8.3/8.4).
_STATUSES = ("open", "resolved_warning", "resolved_ban", "dismissed")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col IN ('a','b')` из кортежа значений."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """`reports` + `UNIQUE` адреса и пары + бикондиционал разбора + индекс."""
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "uuid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("reporter_id", sa.BigInteger(), nullable=False),
        sa.Column("attendance_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # AC-01: адресат и предмет. `CASCADE` с обеих сторон: жалоба без
        # жалобщика или без факта явки не адресуется и не проверяется.
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["users.id"],
            name=op.f("fk_reports_reporter_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attendance_id"],
            ["attendance.id"],
            name=op.f("fk_reports_attendance_id_attendance"),
            ondelete="CASCADE",
        ),
        # AC-01, D-TD-2: единственная защита от явной вставки неположительного id.
        sa.CheckConstraint("id > 0", name=ID_POSITIVE_CHECK_NAME),
        # AC-01: публичный адрес уникален. Имя по конвенции `uq_reports_uuid`.
        sa.UniqueConstraint("uuid", name=op.f("uq_reports_uuid")),
        # AC-07: идемпотентность. Имя обязано совпасть с конвенцией модели:
        # `ON CONFLICT (attendance_id, reporter_id)` резолвится по колонкам,
        # но тест сверяет имя ограничения с моделью.
        sa.UniqueConstraint(
            "attendance_id",
            "reporter_id",
            name=op.f("uq_reports_attendance_id_reporter_id"),
        ),
        # AC-05: объект жалобы.
        sa.CheckConstraint(_in_clause("target_type", _TARGET_TYPES), name=TARGET_TYPE_CHECK_NAME),
        # AC-01, AC-10: состояния разбора.
        sa.CheckConstraint(_in_clause("status", _STATUSES), name=STATUS_CHECK_NAME),
        # AC-11: разбор начат ⟺ метка разбора пуста.
        sa.CheckConstraint(
            "(status = 'open') = (resolved_at IS NULL)", name=STATUS_RESOLVED_CHECK_NAME
        ),
        # AC-11: разбор не раньше жалобы.
        sa.CheckConstraint("resolved_at >= reported_at", name=RESOLVED_AFTER_REPORTED_CHECK_NAME),
    )

    # AC-10, NFR52: очередь модератора 8.3 — `WHERE status = :s ORDER BY reported_at`.
    op.create_index("ix_reports_status_reported_at", "reports", ["status", "reported_at"])


def downgrade() -> None:
    """Откат с guard: непустая `reports` останавливает удаление (AC-02).

    Guard обязателен: жалобы — это данные, а `DROP TABLE` уничтожил бы их
    молча. Оператор решает судьбу строк осознанно (§12.3), а не узнаёт
    о потере из логов.
    """
    context = op.get_context()
    # Guard НЕ обходится в offline-режиме (урок CR 7.3): при `downgrade --sql`
    # генерировался бы `DROP TABLE` без единой проверки — защита отсутствовала
    # бы ровно там, где скрипт отдают оператору.
    if context.as_sql:
        raise RuntimeError(
            "Откат 0023 в offline-режиме (`--sql`) запрещён: проверить наличие "
            "строк в reports невозможно, а `DROP TABLE` уничтожил бы жалобы "
            "молча. Выполните откат онлайн (без `--sql`) — guard проверит "
            "count(*) и остановится при непустой таблице."
        )
    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM reports")).scalar_one()
    if rows:
        raise RuntimeError(
            f"Откат 0023 остановлен: в reports {rows} строк(и). Жалобы — это "
            "данные, и `DROP TABLE` уничтожил бы их молча. Перенесите строки "
            "или удалите их осознанно, затем повторите откат (§12.3)."
        )
    op.drop_index("ix_reports_status_reported_at", table_name="reports")
    op.drop_table("reports")
