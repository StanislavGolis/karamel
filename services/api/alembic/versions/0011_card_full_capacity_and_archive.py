"""card full capacity check and archive index

Стори 4.6 (AC-05, AC-07; X-35). Две правки схемы:

1. **AC-07 (X-35):** `ck_event_cards_full_capacity` переписывается на
   ДВУСТОРОННЕЕ условие `(status <> 'full') OR (filled_count = capacity)`
   вместо прежнего `filled_count < capacity OR status IN (...)`.

   Прежняя редакция (4.1) запрещала только `published` при полной
   вместимости и пропускала обратное: `status='full'` со свободным местом
   проходил CHECK. Тогда `has_seats=true` (4.5) отдавал карточку «есть места»
   со статусом «собрана» — два правила об одном факте расходились.

   Обе половины остаются: новое условие добавляется к прежнему, а не заменяет
   его. Только `(status <> 'full') OR ...` ослабило бы 4.1 и позволило бы
   `published` при `filled_count = capacity`.

2. **AC-05 (FR73, NFR20):** `ix_event_cards_started (starts_at) WHERE
   status IN ('published','full','closed')` — индекс воркера `card_windows`.
   Без него выборка «что уже началось, но не в архиве» идёт `Seq Scan` каждые
   60 секунд. Частичный: архивных `finished` много, переводить их повторно
   незачем.

**Обе декларации продублированы в модели** (`models/event_card.py`): без
этого `alembic check` предложил бы «лишний» индекс. Имя CHECK задаётся через
`op.f` (§18.7), иначе naming convention модели сложилась бы с префиксом
миграции и дала `ck_event_cards_ck_event_cards_full_capacity`.

**Откат безопасен:** данные не меняются, пересоздаётся ограничение и
удаляется индекс. Порядок `downgrade` обратный порядку `upgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# AC-07 (X-35): имена и тексты обязаны совпадать с моделью посимвольно,
# иначе `alembic check` покажет расхождение.
# КОРОТКОЕ имя: `op.drop_constraint`/`op.create_check_constraint` прогоняют
# его через naming convention модели (`ck_%(table_name)s_%(constraint_name)s`),
# и полное имя дало бы `ck_event_cards_ck_event_cards_full_capacity` —
# воспроизведено исполнением на ревизии `0011`. Полное имя передаётся только
# через `op.f(...)` в `create`, где конвенция уже применена.
_FULL_CAPACITY_CONSTRAINT = "full_capacity"
_STARTED_INDEX = "ix_event_cards_started"

# Прежний текст (4.1) — нужен в `downgrade` дословно: иначе откат вернёт
# ограничение, которое не совпадает с моделью на ревизии `0010`.
_OLD_FULL_CAPACITY = "filled_count < capacity OR status IN ('full','closed','finished','cancelled')"
_NEW_FULL_CAPACITY = (
    "(filled_count < capacity OR status IN ('full','closed','finished','cancelled'))"
    " AND (status <> 'full' OR filled_count = capacity)"
)


def upgrade() -> None:
    """Пересоздаёт CHECK полной вместимости и создаёт индекс воркера."""
    op.drop_constraint(_FULL_CAPACITY_CONSTRAINT, "event_cards", type_="check")
    op.create_check_constraint(
        op.f("ck_event_cards_full_capacity"), "event_cards", sa.text(_NEW_FULL_CAPACITY)
    )
    op.create_index(
        _STARTED_INDEX,
        "event_cards",
        ["starts_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('published','full','closed')"),
    )


def downgrade() -> None:
    """Возвращает прежний CHECK и удаляет индекс (обратный порядок)."""
    op.drop_index(_STARTED_INDEX, table_name="event_cards")
    op.drop_constraint(_FULL_CAPACITY_CONSTRAINT, "event_cards", type_="check")
    op.create_check_constraint(
        op.f("ck_event_cards_full_capacity"), "event_cards", sa.text(_OLD_FULL_CAPACITY)
    )
