"""create notification_mutes table (story 9.3)

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-25

Стори 9.3 (AC-01, AC-02; FR46; NFR60).

**Новая таблица, а не колонка в `users`/`profiles`** (D-9.3-4). Проверено
фактически: полей настроек уведомлений в `users`/`profiles` нет
(`grep -rniE 'preferences|notification_settings|muted|disabled_types' app/ alembic/`
→ 0 совпадений). Настройка — МНОЖЕСТВО `(user_id, type)`, а не скаляр,
и gate обязан читаться одним предикатом внутри `INSERT` (D-9.3-2).

**Хранится отключённое (opt-out)** (D-9.3-3). Пустая таблица = «всё включено».
Backfill не нужен, и новый тип (9.4, 9.6) автоматически включён — миграция
при добавлении типа не требуется (перечень в CHECK придётся расширить, но
это `ALTER`, а не заполнение строк на каждого пользователя).

**`PRIMARY KEY (user_id, type)`** — одновременно уникальность (нельзя
отключить дважды; `ON CONFLICT DO NOTHING` из D-9.3-12 опирается на неё)
и индекс под gate-предикат `m.user_id = :u AND m.type = :t`.

**Отдельный индекс НЕ создаётся — и это решение, а не пропуск** (Task 1.3).
Gate-предикат `NOT EXISTS (SELECT 1 FROM notification_mutes m WHERE
m.user_id = <user> AND m.type = <type>)` фильтрует ровно по двум колонкам,
которые образуют PK. B-tree по `(user_id, type)` покрывает его целиком
(равенство по обоим столбцам, порядок колонок в PK совпадает с порядком
предиката). Любой дополнительный индекс был бы мёртвой записью: он ускорял
бы запрос, который уже ускорен, но занимал бы место и замедлял вставки.

**Два CHECK на `type`, и оба нужны** (D-9.3-5, D-9.1-3):

1. `ck_notification_mutes_type` — ПЕРЕЧЕНЬ 11 типов, тот же, что у
   `notifications.type` (`0030`). Копия литералом, а не импорт из `app`:
   историческая ревизия обязана быть воспроизводимой при любом переименовании
   в приложении. Расхождение ловится тестом сверки трёх источников.
2. `ck_notification_mutes_required` — ЗАПРЕТ на `report.resolved` и
   `sanction.issued`. Это не декоративная страховка: гард API (`409`)
   не закрывает прямую вставку мимо API (ручной SQL, будущий внутренний
   вызов, миграция данных). Отключение этих типов сделало бы FR38/FR39
   невыполнимыми по воле пользователя.

**`downgrade` удаляет ТОЛЬКО созданное здесь.** `notifications` и
`support_tickets` он не касается: они принадлежат `0021`/`0030`. Таблица
`notification_mutes` ни на что не ссылается, кроме `users`, поэтому
`DROP TABLE` не требует `CASCADE` и не может утянуть чужие объекты.
Порядок обратный `upgrade` не нужен: объект один.

**Имена БЕЗ префикса конвенции.** `naming_convention` из `models/base.py`
добавит `ck_notification_mutes_`/`fk_notification_mutes_user_id_users`/
`pk_notification_mutes`. Литерал с префиксом дал бы двойной префикс —
класс дефекта 3.1/4.1 (§18.7).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031"
_down_revision = "0030"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: AC-01: ПЕРЕЧЕНЬ допустимых типов. Дублируется из
#: `app.models.enums.NOTIFICATION_TYPES` осознанно: миграция обязана читать
#: только `sa` и не импортировать приложение (иначе историческая ревизия
#: сломается при любом переименовании в `app/`). Третья копия перечня —
#: литерал `0030`; расхождение ловится тестом сверки трёх источников.
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

#: D-9.3-5: типы, которые НЕЛЬЗЯ отключить. Дублируется из
#: `app.models.enums.REQUIRED_NOTIFICATION_TYPES` по той же причине.
#: Форма ограничения — `type NOT IN (...)`, а не `type IN (<отключаемые>)`:
#: список отключаемых пришлось бы переписывать при каждом новом типе, и
#: миграция 9.4/9.6 упиралась бы в CHECK вместо простого `ALTER`.
REQUIRED_NOTIFICATION_TYPES: tuple[str, ...] = (
    "report.resolved",
    "sanction.issued",
)


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col IN ('a','b')`. Копия `_in_clause` из `0030` — миграция не импортирует `app`."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _not_in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col NOT IN ('a','b')` — предикат «не из неотключаемых»."""
    return f"{column} NOT IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Создаёт `notification_mutes` (AC-01, AC-02)."""
    op.create_table(
        "notification_mutes",
        # AC-01: адресат настройки. `CASCADE` — строка без пользователя не
        # адресуется никому (ADR-07; прецедент `notifications.user_id`).
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # AC-01, NFR60: `сущность.действие`, та же длина, что у
        # `notifications.type`: расхождение сделало бы сравнение типов
        # зависимым от колонки.
        sa.Column("type", sa.String(length=64), nullable=False),
        # AC-01: «когда отключил» — для разбора обращений «я не получал».
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # AC-01: PK `(user_id, type)` — уникальность + индекс под gate.
        sa.PrimaryKeyConstraint("user_id", "type", name=op.f("pk_notification_mutes")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_mutes_user_id_users"),
            ondelete="CASCADE",
        ),
        # AC-01, NFR60: перечень типов поверх формы (вторая копия, D-9.1-3).
        sa.CheckConstraint(
            _in_clause("type", NOTIFICATION_TYPES), name=op.f("ck_notification_mutes_type")
        ),
        # D-9.3-5: неотключаемые типы запрещены СХЕМОЙ, не только API.
        sa.CheckConstraint(
            _not_in_clause("type", REQUIRED_NOTIFICATION_TYPES),
            name=op.f("ck_notification_mutes_required"),
        ),
    )


def downgrade() -> None:
    """Удаляет `notification_mutes`.

    **Только созданное здесь.** `notifications` и `support_tickets` принадлежат
    `0021`/`0030` и переживают этот откат: их дроп сломал бы guard `0021`
    («непустая таблица останавливает откат») и уничтожил бы адресные запросы
    явки, которые 7.5 читает как источник истории.

    `DROP TABLE` без `CASCADE` намеренно: `CASCADE` простил бы будущую
    ссылку на таблицу из чужой ревизии и молча удалил бы объекты, которые
    эта миграция не создавала.
    """
    op.drop_table("notification_mutes")
