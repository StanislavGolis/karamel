"""tighten notifications read_order check (story 7.3, CR H-2)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-24

**Дефект, который закрывает эта миграция** (CR 7.3, H-2). `CHECK (read_at >=
delivered_at)` из `0021` НЕ запрещает прочтение без доставки: при
`delivered_at IS NULL` выражение даёт `UNKNOWN`, а `CHECK` отвергает только
`FALSE`. Значит `read_at = now()` при `delivered_at IS NULL` проходил —
инвариант AC-01 «прочтение не раньше доставки» не был обеспечен (проверено
`INSERT` на живой БД: строка создалась).

**Почему новая миграция, а не правка `0021`.** `0021` уже применена (стенд,
локальные базы, потенциально прод): правка применённой ревизии не доедет до
тех, кто её прогнал, и разойдётся с `alembic_version`. Alembic-контракт:
изменение ограничения = новая ревизия.

**Форма нового ограничения — бикондиционал, а не два раздельных `CHECK`.**
Один `CHECK` с явной ветвью `delivered_at IS NULL` читается как утверждение
«нет доставки ⇒ нет прочтения» и не оставляет места для третьего состояния:

    (delivered_at IS NULL AND read_at IS NULL)
    OR (delivered_at IS NOT NULL AND (read_at IS NULL OR read_at >= delivered_at))

**Порядок обновления существующих строк.** В `0021` дефект позволял создать
строки с `read_at` без `delivered_at`. На чистой базе их нет, но на рабочей
они могли появиться (`read_at` пишет эпик 9, которого ещё нет, — то есть
практически их быть не должно). Миграция НЕ удаляет такие строки молча: она
сначала `UPDATE ... SET read_at = NULL` для «прочитанных без доставки»
(прочтение без доставки — противоречивое состояние, единственный корректный
выход — считать непрочитанным), и только потом ставит ограничение. Иначе
`ADD CONSTRAINT` упал бы на существующих данных с невнятной ошибкой валидации.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
_down_revision = "0021"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: CR 7.3 H-2: имя БЕЗ префикса конвенции (`ck_notifications_` добавит
#: `naming_convention` из `models/base.py`). Литерал с префиксом дал бы
#: двойной префикс — класс дефекта 3.1/4.1.
READ_ORDER_CHECK_NAME = "read_order"

#: Бикондиционал: «нет доставки ⇒ нет прочтения», «есть доставка ⇒ прочтение
#: не раньше неё (или отсутствует)».
READ_ORDER_EXPRESSION = (
    "(delivered_at IS NULL AND read_at IS NULL) "
    "OR (delivered_at IS NOT NULL AND (read_at IS NULL OR read_at >= delivered_at))"
)

#: `0021`-версия выражения — нужна для `downgrade`.
LEGACY_READ_ORDER_EXPRESSION = "read_at >= delivered_at"


def upgrade() -> None:
    """Заменяет слабый `read_order` на бикондиционал (CR H-2)."""
    op.drop_constraint(READ_ORDER_CHECK_NAME, "notifications", type_="check")

    # Приводим данные к состоянию, которое новое ограничение допускает:
    # «прочитано без доставки» невозможно, значит это непрочитанное.
    op.get_bind().execute(
        sa.text(
            "UPDATE notifications SET read_at = NULL "
            "WHERE delivered_at IS NULL AND read_at IS NOT NULL"
        )
    )

    op.create_check_constraint(READ_ORDER_CHECK_NAME, "notifications", READ_ORDER_EXPRESSION)


def downgrade() -> None:
    """Возвращает слабое выражение `0021` (данные не трогаются).

    Откат НЕ восстанавливает «прочитано без доставки»: такие строки были
    противоречивы, и придумывать им `delivered_at` задним числом значило бы
    утверждать доставку, которой не было. Откат схемы — не восстановление
    мусора.
    """
    op.drop_constraint(READ_ORDER_CHECK_NAME, "notifications", type_="check")
    op.create_check_constraint(READ_ORDER_CHECK_NAME, "notifications", LEGACY_READ_ORDER_EXPRESSION)
