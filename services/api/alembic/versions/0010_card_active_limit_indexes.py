"""card active limit indexes: author publishable and place reference

Стори 4.4 (AC-01, AC-04, X-16). Две правки индексов — обе для лимита
активных карточек автора:

1. **AC-01, AC-04 (FR71, NFR30):** `ix_event_cards_author_publishable
   (author_id) WHERE status IN ('published','full','closed')` — индекс под
   подсчёт лимита. Существующий `ix_event_cards_author_active
   WHERE status IN ('draft','published')` для этого предиката НЕ подходит:
   `full`/`closed` в него не входят, и планировщик читал бы таблицу
   вместо индекса. Подсчёт идёт ПОД `FOR UPDATE users` (ADR-09), но индекс
   всё равно нужен: блокировка сериализует транзакции, а не ускоряет COUNT.

2. **X-16:** `ix_event_cards_place_id (place_id)` — FK `places` с
   `ON DELETE RESTRICT` проверяется при каждом удалении места, а без индекса
   PostgreSQL делает `Seq Scan` по `event_cards`. Долг закрывается здесь,
   а не отдельным chore (OQ-5): миграция уже создаётся этой стори, отдельная
   ревизия стоила бы ещё один цикл `upgrade/downgrade` на проде.

**Обе декларации продублированы в модели** (`models/event_card.py`): без
этого `alembic check` предложил бы создать «лишний» индекс или удалить
существующий (урок 4.1 с X-02). Имена индексов передаются в
`op.create_index`/`op.drop_index` буквальными строками, поэтому `op.f`
здесь не применяется (§18.7 касается имён ограничений): совпадение
с моделью даёт буквальное имя.

**Откат безопасен:** данные не меняются, удаляются только структуры
доступа. Порядок `downgrade` обратный порядку `upgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# AC-01, AC-04: имя и предикат обязаны совпадать с моделью посимвольно,
# иначе `alembic check` покажет расхождение.
_AUTHOR_PUBLISHABLE_INDEX = "ix_event_cards_author_publishable"
# X-16: индекс ссылки на место — FK `RESTRICT` без него проверяется Seq Scan.
_PLACE_ID_INDEX = "ix_event_cards_place_id"


def upgrade() -> None:
    """Создаёт индекс подсчёта лимита автора и индекс ссылки на место."""
    op.create_index(
        _AUTHOR_PUBLISHABLE_INDEX,
        "event_cards",
        ["author_id"],
        unique=False,
        postgresql_where=sa.text("status IN ('published','full','closed')"),
    )
    op.create_index(
        _PLACE_ID_INDEX,
        "event_cards",
        ["place_id"],
        unique=False,
    )


def downgrade() -> None:
    """Удаляет оба индекса в обратном порядке."""
    op.drop_index(_PLACE_ID_INDEX, table_name="event_cards")
    op.drop_index(_AUTHOR_PUBLISHABLE_INDEX, table_name="event_cards")
