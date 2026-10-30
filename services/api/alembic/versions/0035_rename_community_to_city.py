"""rename community_slug to city_slug (story 12.1)

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-28

Стори 12.1 (AC-1, AC-2, AC-3; ADR-23; NFR48).

**Ломающее переименование.** Ось продукта — город (`FR16` = а), а колонка
называлась `community_slug`. Имя, значащее город, — тот же класс лжи, что 13
фактических ошибок в брифе. ADR-23 отвергает и «оставить имя, поменять
семантику» (расхождение невидимо), и «ввести рядом» (два поля на одну
сущность — гарантированный дрейф).

**Почему `RENAME`, а не `ADD` + `DROP` + бэкфилл.** Переименование сохраняет
данные, порядок колонок и определение частичных индексов. `ADD`+`DROP`
переписал бы 40 000 строк таблиц и потребовал бы копирования значений, при
этом результат тот же — только дольше и с окном, где данные разъехались.

**Почему индексы переименовываются, а не пересоздаются.** `idx_places_city_active`
и `idx_places_city_cursor` — частичные (`WHERE is_active`). `ALTER INDEX ...
RENAME TO` сохраняет предикат и определение. Пересоздание через
`CREATE INDEX CONCURRENTLY` в транзакции запрещено (`CREATE INDEX CONCURRENTLY
cannot run inside a transaction block`), а обычное `CREATE INDEX` взяло бы
`ACCESS EXCLUSIVE` на перестройку 40 000 строк без выигрыша: ключи не менялись.

**`uq_places_community_name` — ограничение, не индекс.** Замер `pg_constraint`:
`contype = 'u'`. `ALTER INDEX` по нему вернёт «is not an index», поэтому имя
меняется через `ALTER TABLE ... RENAME CONSTRAINT`.

**NOT NULL-ограничения переименовываются условно (PG 16 vs 17+).** Именованные
NOT NULL (`contype = 'n'`) появились в `pg_constraint` только в **PG 17**.
Замер на PG 18.6: в `places`, `event_cards`, `users` есть
`*_community_slug_not_null`, и `RENAME COLUMN` их не переименовывает. На **PG 16**
такого объекта в `pg_constraint` нет физически — NOT NULL хранится только в
`pg_attribute.attnotnull`, имени не существует, и `RENAME CONSTRAINT` падает
(`constraint "..._not_null" for table "places" does not exist` — живой инцидент
деплоя 2026-10-30). AC-2 требует **ноль** объектов с `community` в имени, а на
PG 16 это условие выполняется само собой: имени нет.

**Стенд работает на PG 16.15, локальная разработка — на PG 18.6; миграция
обязана проходить на обеих.** Отсюда условное переименование
(`_rename_not_null_if_exists`) и расхождение окружений в `W-12.1-f`.

**Откат обязан вернуть точные имена.** `0032.downgrade()` вызывает
`op.drop_constraint(op.f("ck_users_community_slug"), ...)`. Если `downgrade()`
этой ревизии вернёт другое имя, откат до `0031` упадёт на отсутствующем
ограничении. Тест цикла проверяет двойной откат `0035 → 0033 → 0035`.

**Транзакционность.** PostgreSQL исполняет DDL транзакционно, поэтому
`upgrade()` целиком атомарен: сбой на любом шаге оставляет схему в `community`.
Алембик заворачивает ревизию в транзакцию (`transactional_ddl`), отдельный
`BEGIN` не нужен.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


#: Таблицы, несущие ось города. Порядок не важен для RENAME COLUMN, но
#: зафиксирован, чтобы откат шёл тем же списком.
_TABLES = ("places", "event_cards", "users")

#: Пары «старое имя → новое имя» для индексов и ограничений.
#: Источник — замер `pg_indexes` + `pg_constraint` на голове `0034`.
_INDEX_RENAMES = (
    ("ix_event_cards_community_starts", "ix_event_cards_city_starts"),
    ("idx_places_community_active", "idx_places_city_active"),
    ("idx_places_community_cursor", "idx_places_city_cursor"),
)

#: `uq_places_community_name` — ограничение (`contype = 'u'`), не индекс.
_UNIQUE_CONSTRAINT_RENAMES = (
    ("uq_places_community_name", "uq_places_city_name"),
)

#: CHECK-ограничения. Имя без префикса: naming convention даёт `ck_<table>_<name>`.
_CHECK_RENAMES = (
    ("places", "ck_places_community_slug", "ck_places_city_slug"),
    ("event_cards", "ck_event_cards_community_slug", "ck_event_cards_city_slug"),
    ("users", "ck_users_community_slug", "ck_users_city_slug"),
)

#: NOT NULL-ограничения PG 17+ (`contype = 'n'`). Замер: `RENAME COLUMN` их
#: не переименовывает, а AC-2 требует ноль объектов с `community`.
#: На PG 16 этих имён не существует — переименование условное, см.
#: `_rename_not_null_if_exists`.
_NOT_NULL_RENAMES = (
    ("places", "places_community_slug_not_null", "places_city_slug_not_null"),
    (
        "event_cards",
        "event_cards_community_slug_not_null",
        "event_cards_city_slug_not_null",
    ),
    ("users", "users_community_slug_not_null", "users_city_slug_not_null"),
)


def _rename_constraint(table: str, old: str, new: str) -> None:
    """AC-2: имя CHECK/UNIQUE-ограничения без `community`.

    Проверка существования не нужна **только для этих двух видов**: CHECK и
    UNIQUE существуют в `pg_constraint` на всех поддерживаемых версиях
    PostgreSQL (замер 2026-10-28 на голове `0034`). Для NOT NULL так делать
    нельзя — там имя есть лишь с PG 17, см. `_rename_not_null_if_exists`.
    """
    op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"')


def _rename_not_null_if_exists(table: str, old: str, new: str) -> None:
    """AC-2: переименовать NOT NULL-ограничение, если оно вообще названо.

    Именованные NOT NULL (`pg_constraint.contype = 'n'`) появились в **PG 17**.
    На **PG 16** NOT NULL живёт только в `pg_attribute.attnotnull`: имени нет,
    и `ALTER TABLE ... RENAME CONSTRAINT` падает
    (`constraint "..._not_null" for table "places" does not exist`). Требование
    AC-2 («ноль объектов с `community` в имени») на PG 16 выполняется без
    переименования — объекта с таким именем не существует.

    **Почему `DO`-блок, а не запрос из Python.** В offline-режиме
    (`alembic upgrade head --sql`, включён в `alembic/env.py`) соединения нет,
    и `op.get_bind()` уронил бы генерацию SQL для всей цепочки. `DO`-блок
    остаётся статическим текстом и работает в обоих режимах.

    **Почему не `try/except`.** Ошибка в PostgreSQL травит транзакцию: после неё
    нужен `SAVEPOINT`, иначе не выполнится ни один следующий оператор. Проверка
    условия дешевле и не оставляет отравленной транзакции.

    `to_regclass` вместо `'table'::regclass`: при отсутствии таблицы вернётся
    NULL (условие ложно) вместо ошибки приведения типа.
    """
    op.execute(
        f"DO $$\n"
        f"BEGIN\n"
        f"    IF EXISTS (\n"
        f"        SELECT 1 FROM pg_constraint\n"
        f"        WHERE conrelid = to_regclass('public.{table}')\n"
        f"          AND conname = '{old}'\n"
        f"    ) THEN\n"
        f'        ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}";\n'
        f"    END IF;\n"
        f"END $$;"
    )



def upgrade() -> None:
    # AC-1: колонка переименовывается во всех трёх таблицах. Данные на месте,
    # тип `varchar(64)` и `NOT NULL` сохраняются автоматически.
    for table in _TABLES:
        op.alter_column(table, "community_slug", new_column_name="city_slug")

    # AC-2: NOT NULL-ограничения PG 17+ не переименовались вместе с колонкой
    # (замер на 18.6) — правятся явно. На PG 16 их имён не существует, вызов
    # молча пропускается. До CHECK и индексов, чтобы падение здесь не
    # оставляло половинчатого набора имён.
    for table, old, new in _NOT_NULL_RENAMES:
        _rename_not_null_if_exists(table, old, new)

    # AC-2: CHECK-шаблон `^[a-z0-9_-]{3,64}$` остаётся, меняется только имя.
    for table, old, new in _CHECK_RENAMES:
        _rename_constraint(table, old, new)

    # AC-2: уникальность `(city_slug, name)` — ограничение, не индекс.
    for old, new in _UNIQUE_CONSTRAINT_RENAMES:
        _rename_constraint("places", old, new)

    # AC-2: индексы. `ALTER INDEX ... RENAME TO` сохраняет предикат
    # частичных индексов (`WHERE is_active`).
    for old, new in _INDEX_RENAMES:
        op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')


def downgrade() -> None:
    # AC-3: зеркало `upgrade()` в обратном порядке. Имена возвращаются
    # ТОЧНО такими, какими их ждёт `0032.downgrade()`
    # (`ck_users_community_slug`) — иначе откат до `0031` упадёт.
    for old, new in reversed(_INDEX_RENAMES):
        op.execute(f'ALTER INDEX "{new}" RENAME TO "{old}"')

    for old, new in reversed(_UNIQUE_CONSTRAINT_RENAMES):
        _rename_constraint("places", new, old)

    for table, old, new in reversed(_CHECK_RENAMES):
        _rename_constraint(table, new, old)

    # Симметрично `upgrade`: на PG 16 возвращать нечего — имени не было.
    for table, old, new in reversed(_NOT_NULL_RENAMES):
        _rename_not_null_if_exists(table, new, old)

    for table in reversed(_TABLES):
        op.alter_column(table, "city_slug", new_column_name="community_slug")
