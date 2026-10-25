"""moderation indexes: photo queue key and card queue key

Стори 4.3 (AC-09, D-4.3-8). Две правки индексов — обе для очередей модерации:

1. **Х-01 (ex-D-7 стори 3.1):** `idx_profile_photos_created_at_id`
   `(created_at, id) WHERE status = 'pending'`. Очередь фото читается в порядке
   поступления (`ORDER BY created_at, id`), а `idx_profile_photos_status_pending`
   покрывает ТОЛЬКО фильтр — сортировка шла отдельным `Sort`. Долг закрывается
   здесь, а не отдельным chore, по решению пользователя от 2026-10-23 (п.4):
   модерация карточки — тот же класс очереди, атомарность сохраняется
   (один `revision` — один откат).

2. **Очередь карточек:** `ix_event_cards_pending_verification`
   `(created_at)` → `(created_at, uuid)` `WHERE verification_status = 'pending'`.
   Ключ курсора и сортировки — `(created_at, uuid)`; без `uuid` в индексе
   при равных метках порядок не определён, и keyset-пагинация теряла бы
   или дублировала строки между страницами. `uuid`, а НЕ `id`: курсор
   публичен, внутренний bigint в него попадать не должен (§7.2).

**Почему `DROP` + `CREATE`, а не `ALTER` индекса:** PostgreSQL не умеет менять
набор колонок индекса на месте; «пересоздание» — это ровно два оператора.

**Обе декларации продублированы в моделях** (`models/profile_photo.py`,
`models/event_card.py`): без этого `alembic check` предложил бы удалить
созданный индекс как «лишний» (урок 4.1 с X-02).

**Почему важно `op.f` (§18.7) для новых имён:** индексы в naming convention
не участвуют, но имена передаются в `op.create_index`/`op.drop_index` как есть,
поэтому `op.f` здесь не нужен и не применяется — совпадение с моделью даёт
буквальное имя строкой.

**Откат безопасен:** старый индекс восстанавливается в прежнем виде, данные
не меняются (индексы — только структура доступа). Порядок `downgrade` обратный:
сначала карточки, затем фото — как в `upgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Х-01: очередь фото — порядок поступления. Условие в модели и здесь одно:
# `status = 'pending'` (обе строки обязаны совпадать посимвольно, иначе
# `alembic check` покажет расхождение).
_PHOTO_QUEUE_INDEX = "idx_profile_photos_created_at_id"
# Очередь карточек: имя не меняется, меняется только набор колонок.
_CARD_QUEUE_INDEX = "ix_event_cards_pending_verification"


def upgrade() -> None:
    """Создаёт индекс очереди фото и дополняет индекс очереди карточек `uuid`."""
    op.create_index(
        _PHOTO_QUEUE_INDEX,
        "profile_photos",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # PostgreSQL не меняет колонки индекса на месте: два оператора, имя то же.
    # `DROP` идёт первым, иначе `CREATE` упадёт на «relation already exists».
    op.drop_index(_CARD_QUEUE_INDEX, table_name="event_cards")
    op.create_index(
        _CARD_QUEUE_INDEX,
        "event_cards",
        ["created_at", "uuid"],
        unique=False,
        postgresql_where=sa.text("verification_status = 'pending'"),
    )


def downgrade() -> None:
    """Возвращает индексы в исходное состояние в обратном порядке."""
    op.drop_index(_CARD_QUEUE_INDEX, table_name="event_cards")
    op.create_index(
        _CARD_QUEUE_INDEX,
        "event_cards",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("verification_status = 'pending'"),
    )
    op.drop_index(_PHOTO_QUEUE_INDEX, table_name="profile_photos")
