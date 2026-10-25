"""reports queue index for moderator cabinet (story 8.3, AC-01, AC-04)

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-25

**Зачем индекс.** Очередь модератора читает ОТКРЫТЫЕ жалобы в порядке
возраста (`ORDER BY reported_at ASC, uuid ASC`, `LIMIT` на каждой странице).
Существующий `ix_reports_status_reported_at (status, reported_at)` покрывает
фильтр по статусу, но не второй ключ сортировки: при tie-группе (две жалобы
с одинаковым `reported_at` — обычное дело, `now()` в одной транзакции
совпадает) планировщик добавляет `Sort`, и `LIMIT` перестаёт быть дешёвым.

**Почему ЧАСТИЧНЫЙ (`WHERE status='open'`).** Закрытые жалобы в очередь не
попадают (D-31): `resolved_warning`/`resolved_ban`/`dismissed` — архив.
Индекс без предиката рос бы вместе с историей, а планировщик всё равно не
смог бы его использовать для запроса без `status='open'` в `WHERE`.

**Почему `CONCURRENTLY`** (D-4). `reports` на стенде может быть непустой, а
обычный `CREATE INDEX` взял бы `ACCESS EXCLUSIVE` и заблокировал подачу
жалоб (`INSERT`) на всё время построения. `CONCURRENTLY` не работает внутри
транзакции — отсюда `autocommit_block` в `upgrade`.

**Почему `downgrade` БЕЗ `CONCURRENTLY`** (CR 8.2, H-1). `autocommit_block`
коммитит оператор вне транзакции ревизии: когда откат ниже `0027`
останавливает guard `0024`/`0023`, транзакция возвращается к `0027`, а
индекс остаётся снесённым навсегда — схема дрейфует при формально верном
`alembic_version`. Откат обязан быть атомарным. `reports` при откате
`0027` пуста от новых строк не бывает, но `DROP INDEX IF EXISTS` внутри
транзакции берёт короткую блокировку на метаданные, а не на данные.

**Offline-safe** (D-7). В `--sql` режиме проверить `indisvalid` невозможно,
поэтому `upgrade` падает явно, а не генерирует скрипт без guard'а.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027"

down_revision: str | None = "0026"

branch_labels: str | Sequence[str] | None = None

depends_on: str | Sequence[str] | None = None

#: AC-01, AC-04: очередь читает открытые жалобы по возрасту.
QUEUE_INDEX = "ix_reports_open_reported_at_uuid"


def upgrade() -> None:
    """Частичный `CONCURRENTLY`-индекс очереди (AC-01, AC-04)."""
    if op.get_context().as_sql:
        raise RuntimeError(
            "`upgrade --sql` для 0027 запрещён: `CREATE INDEX CONCURRENTLY` не "
            "выражается в offline-скрипте, а guard по `indisvalid` требует "
            "соединения с БД. Выполните upgrade онлайн."
        )

    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {QUEUE_INDEX} "
            "ON reports (reported_at, uuid) WHERE status = 'open'"
        )

    # Guard: `CONCURRENTLY` может оставить НЕВАЛИДНЫЙ индекс (прерванное
    # построение). Невалидный индекс не используется планировщиком, но и не
    # удаляется сам — миграция считалась бы применённой без работающего
    # индекса. Тот же guard, что в `0025` для составного FK.
    #
    # CR 8.3, H-3: `to_regclass`, а НЕ `'name'::regclass`. Приведение
    # `::regclass` на несуществующем имени БРОСАЕТ `UndefinedTableError`
    # (`relation "…" does not exist`), а не возвращает NULL. Тогда две ветки
    # различались бы не по смыслу: «индекс невалиден» давала бы понятный
    # `RuntimeError`, а «индекс вообще не создан» — сырую ошибку драйвера
    # мимо guard'а. `to_regclass` возвращает NULL вместо исключения, и обе
    # ветки сходятся в одно сообщение.
    valid = (
        op.get_bind()
        .execute(
            sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": QUEUE_INDEX},
        )
        .scalar_one_or_none()
    )
    if valid is not True:
        raise RuntimeError(
            f"Индекс {QUEUE_INDEX} не построен или невалиден "
            f"(indisvalid={valid!r}). Пересоздайте индекс вручную и повторите "
            "`upgrade`."
        )


def downgrade() -> None:
    """Снимает индекс АТОМАРНО (CR 8.2, H-1); в offline-режиме падает явно."""
    if op.get_context().as_sql:
        raise RuntimeError(
            "Откат 0027 в offline-режиме (`--sql`) запрещён: проверить состояние "
            "индекса невозможно, а генерация скрипта без guard'а повторила бы "
            "дефект CR 7.3. Выполните откат онлайн (без `--sql`)."
        )

    # Без `CONCURRENTLY` и без `autocommit_block`: откат атомарен. См. docstring
    # модуля — это закрытие H-1 из CR 8.2.
    op.execute(f"DROP INDEX IF EXISTS {QUEUE_INDEX}")
