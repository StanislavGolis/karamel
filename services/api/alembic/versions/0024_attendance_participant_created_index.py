"""attendance: index for moderator attendance history (story 7.5, AC-07)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-24

**Зачем второй индекс при наличии `ix_attendance_participant_status`.**
Существующий индекс — `(participant_id, status)`. Он обслуживает
`WHERE participant_id = ? AND status = ?` (фильтр по статусу), но НЕ
сортировку: второго столбца `status` без равенства-фильтра в запросе истории
нет, а `created_at` в индексе отсутствует, поэтому `ORDER BY a.created_at DESC,
a.card_id DESC` уходил в отдельный узел `Sort` — то есть в чтение ВСЕХ строк
участника с сортировкой в памяти при каждой странице.

**X-7.1-4 проверен фактически** (PostgreSQL 18.6, 20 000 строк `attendance`,
200 участников):

- `ORDER BY c.starts_at DESC` — узел `Sort` есть в плане ДО и ПОСЛЕ:
  сортировка по колонке ДРУГОЙ таблицы индексом `attendance` не обслуживается;
- `ORDER BY a.created_at DESC, a.card_id DESC` — до индекса `Bitmap Index Scan`
  плюс `Sort`; после — `Index Scan` по `ix_attendance_participant_created`
  БЕЗ узла `Sort`.

Сортировка по `starts_at` индексом `attendance` не обслуживается в принципе:
это колонка ДРУГОЙ таблицы (`event_cards`). Поэтому ключом сортировки выбран
`attendance.created_at` (D-7.5-5): он ≈ дата события (воркер 7.3 пишет строку
при запросе подтверждения) и не требует денормализации `starts_at` в
`attendance` (класс P-8 — лишняя копия факта разошлась бы с оригиналом).

**`ix_attendance_participant_status` НЕ удаляется.** Он обслуживает другой
предикат (`WHERE participant_id = ? AND status = ?`), и удаление ради
«экономии» превратило бы фильтр по статусу в Seq Scan.

**Почему отдельная миграция `0024`, а не правка `0020`** (D-7.5-6): `0020`
закрыта и задеплоена поэтапно, правка задним числом разошлась бы со стендом
(урок 6.7-D3). Номер проверен: `alembic heads` → `0023`.

**`downgrade` с guard.** Индекс ускоряет чтение, его удаление безопасно для
данных, но откат на большой таблице блокирует запись. Guard проверяет размер
таблицы и останавливает откат с внятным сообщением, чтобы оператор принял
решение осознанно, а не узнал о блокировке из таймаута.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
_down_revision = "0023"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_attendance_participant_created"
KEPT_INDEX_NAME = "ix_attendance_participant_status"

#: AC-07: порог, после которого `DROP INDEX` блокирует запись на заметное
#: время. Значение — не производительность, а решение оператора: до 100 000
#: строк откат безопасен, выше — нужен осознанный выбор окна.
DOWNGRADE_GUARD_ROWS = 100_000


def upgrade() -> None:
    """AC-07: индекс под keyset-сортировку истории явок.

    Направления `DESC` заданы литералами в тексте индекса, а не через
    `sa.desc()` на колонках: миграция не импортирует модели приложения, а
    порядок сортировки — часть SQL-контракта запроса, а не деталь билдера.
    """
    op.create_index(
        INDEX_NAME,
        "attendance",
        ["participant_id", sa.text("created_at DESC"), sa.text("card_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Удаление индекса с guard по размеру таблицы.

    Guard НЕ обходится в offline-режиме (`--sql`): там нет соединения, и
    проверка размера невозможна — генерировать `DROP INDEX` без неё значило бы
    отдать оператору скрипт без защиты ровно там, где её не видно (урок CR 7.3).
    """
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "Откат 0024 в offline-режиме (`--sql`) запрещён: проверить размер "
            "attendance невозможно, а `DROP INDEX` на большой таблице блокирует "
            f"запись. Выполните откат онлайн (порог {DOWNGRADE_GUARD_ROWS} строк)."
        )
    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM attendance")).scalar_one()
    if rows > DOWNGRADE_GUARD_ROWS:
        raise RuntimeError(
            f"Откат 0024 остановлен: в attendance {rows} строк (порог "
            f"{DOWNGRADE_GUARD_ROWS}). `DROP INDEX` на такой таблице заблокирует "
            "запись. Выберите окно обслуживания и повторите откат осознанно."
        )
    op.drop_index(INDEX_NAME, table_name="attendance")
