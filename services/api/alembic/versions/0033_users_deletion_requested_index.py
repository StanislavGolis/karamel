"""add partial index on users.deletion_requested_at (story 10.2)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-26

Стори 10.2 (AC-02; FR10; NFR37, NFR40, NFR41; ADR-07; решение D-10.2-2).

**Зачем индекс.** Воркер `app.workers.retention` сканирует `users` каждые 60 с
по предикату `deletion_requested_at <= now() - interval '7 days'`. Без индекса
это полный скан таблицы участников на каждом прогоне — при пилоте в десятки
тысяч строк и цикле в минуту цена неприемлема.

**Почему частичный.** `NULL` — состояние подавляющего большинства строк
(никто не запросил удаление), и в индексе ему места нет. Тот же приём, что
в `0009` (очередь фото и модерации карточек) и `0012`.

**Зеркало в модели.** `User.__table_args__` объявляет тот же
`Index(..., postgresql_where=...)`: иначе `alembic check` предложит удалить
индекс, созданный миграцией (P-13, тот же класс, что в 10.1).

**`CONCURRENTLY` (P-11, CR 10.2).** Обычный `op.create_index` берёт
`SHARE`-блокировку на `users` на всё время построения: на живом стенде
регистрация и выставление метки встанут. У `0025`, `0027`, `0028`, `0029`
есть `autocommit_block`, у `0033` его не было — единственная миграция серии
без него. `CREATE INDEX CONCURRENTLY` не берёт `SHARE`, но не может идти
внутри транзакции ревизии, поэтому выполняется через `autocommit_block`.

**`downgrade()` реально откатывает** — `DROP INDEX`, БЕЗ `CONCURRENTLY`
(как в `0029`): откат обязан быть атомарным, а `autocommit_block` коммитит
оператор вне транзакции ревизии и при падении следующего шага оставил бы
индекс снесённым навсегда при формально верном `alembic_version`.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

#: Полное имя: индексы в naming convention не участвуют (прецедент 0009).
_DELETION_INDEX = "idx_users_deletion_requested_at"


def upgrade() -> None:
    # AC-02, D-10.2-2: частичный индекс под предикат воркера ретенции.
    # P-11: `CONCURRENTLY` — иначе `SHARE`-блокировка на `users` на всё
    # время построения останавливает регистрацию и выставление метки.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_DELETION_INDEX} "
            "ON users (deletion_requested_at) "
            "WHERE deletion_requested_at IS NOT NULL"
        )


def downgrade() -> None:
    # AC-02: зеркало в обратном порядке.
    op.drop_index(_DELETION_INDEX, table_name="users")
