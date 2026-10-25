"""add chats.status for hiding a dialogue on complaint (story 8.2, AC-01)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-25

**Почему скрытие — колонка ЧАТА, а не участника** (D-21). FR37 говорит «у
обеих сторон»: персональный флаг (две строки на чат) дал бы расхождение
списков сторон и не выразил бы «общение прекратилось у обоих». Одна строка
`chats.status` делает скрытие атомарным — `UPDATE ... WHERE status='active'`
либо проходит, либо нет, третьего состояния не существует.

**Почему `hidden_at` НЕ вводится.** Дата уже есть в `reports.reported_at`:
вторая копия того же факта разошлась бы при переносе, а потребителя у неё
нет (список скрытых диалогов в продукте не показывается).

**Почему индекс по `status` НЕ создаётся.** Домен из двух значений
(`active`/`hidden`) неизбирателен: планировщик всё равно пойдёт по предикату
участия, а лишний объект замедлит запись. Предикат видимости уже стоит в
`WHERE` списка чатов (`_visible_chat_status_predicate`, D-22).

**`server_default='active'`** — потому что существующие чаты обязаны остаться
видимыми: `ADD COLUMN NOT NULL` без дефолта упал бы на непустой таблице.
В PostgreSQL 11+ константный дефолт не переписывает таблицу.

**`downgrade` без guard, но offline-падает.** Скрытие — это СОСТОЯНИЕ, а не
данные: удаление колонки возвращает все чаты в «видимые», и терять нечего
(жалобы остаются в `reports`). Но в offline-режиме (`--sql`) откат падает
явно: проверить, что скрытых чатов нет, невозможно, а молчаливое удаление
колонки из-под работающего кода — тот же класс дефекта, что обходимый guard
CR 7.3.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
_down_revision = "0025"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: AC-01: домен состояния чата. Дублируется из `app/models/chat.py` осознанно:
#: миграция не импортирует приложение. Расхождение ловит unit-тест модели.
_STATUSES = ("active", "hidden")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col IN ('a','b')` из кортежа значений."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """`chats.status` + `CHECK` на домен (AC-01)."""
    op.add_column(
        "chats",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )
    # AC-01: имя bare-константой — конвенция `ck` соберёт `ck_chats_status`.
    op.create_check_constraint("status", "chats", _in_clause("status", _STATUSES))


def downgrade() -> None:
    """Откат: снять `CHECK`, затем колонку; в offline-режиме падает явно.

    Guard не обходится в `--sql`: проверить наличие скрытых чатов невозможно,
    а удаление колонки из-под работающего кода — тот же класс дефекта, что
    обходимый guard CR 7.3. Выполняйте откат онлайн.
    """
    if op.get_context().as_sql:
        raise RuntimeError(
            "Откат 0026 в offline-режиме (`--sql`) запрещён: проверить наличие "
            "скрытых чатов невозможно, а `DROP COLUMN` вернул бы их в список "
            "у обеих сторон молча. Выполните откат онлайн (без `--sql`)."
        )
    op.drop_constraint(op.f("ck_chats_status"), "chats", type_="check")
    op.drop_column("chats", "status")
