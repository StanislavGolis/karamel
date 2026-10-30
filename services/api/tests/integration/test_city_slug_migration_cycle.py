"""Цикл миграции `0035` — переименование `community_slug` → `city_slug` (стори 12.1).

AC-1: колонка `city_slug` есть, `community_slug` нет в `places`, `event_cards`, `users`.
AC-2: индексы и ограничения переименованы, объектов с `community` в имени — ноль.
AC-3: `downgrade` возвращает схему к `community_slug` и старые имена.

Проверки идут через каталог PostgreSQL, а не через модель: модель описывает
намерение, AC говорит о фактической схеме (P-13). Тест на модели прошёл бы и при
расхождении миграции `0035` с моделью.

**Двойной откат.** После `0035.downgrade()` имена обязаны вернуться ТОЧНО к
`ck_users_community_slug`: `0032.downgrade()` дропает ограничение по этому имени.
Тест доходит до `0033` и возвращается — это ловит «переименовали в `city_slug`,
а откат вернул `ck_users_city_slug`».

Краснеет при:
- удалении `RENAME COLUMN` из `upgrade` — колонка останется `community_slug`;
- переименовании только колонки без индексов — 4 имени с `community` останутся;
- возврате в `downgrade` не тех имён — двойной откат до `0031` упадёт.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]

#: AC-1: таблицы, несущие ось города.
_TABLES = ("places", "event_cards", "users")

_COLUMNS_SQL = text(
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' "
    "AND column_name IN ('community_slug', 'city_slug') "
    "AND table_name = ANY(:tables)"
)

#: AC-2: любое имя индекса или ограничения со словом `community`.
_COMMUNITY_OBJECTS_SQL = text(
    "SELECT 'index:' || indexname AS obj FROM pg_indexes "
    "WHERE schemaname = 'public' AND indexname LIKE '%community%' "
    "UNION ALL "
    "SELECT 'constraint:' || conname FROM pg_constraint "
    "WHERE connamespace = 'public'::regnamespace AND conname LIKE '%community%' "
    "ORDER BY 1"
)

#: AC-2: имена, которые обязаны существовать после `upgrade`.
_EXPECTED_OBJECTS = (
    "ix_event_cards_city_starts",
    "uq_places_city_name",
    "idx_places_city_active",
    "idx_places_city_cursor",
)


def _run_alembic(*args: str) -> None:
    """`python -m alembic <args>` отдельным процессом.

    Сабпроцесс, а не `alembic.command`: тот вызывает `asyncio.run()` внутри
    `env.py`, а тест работает в запущенном event loop. Сабпроцесс — тот же
    путь, каким пользуется человек и CI.

    `returncode == 0` обязателен: проглоченный ненулевой код вернул бы
    «миграция прошла», схема осталась бы прежней, и тест проверял бы пустоту.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n"
        f"stdout: {result.stdout.decode()[-2000:]}\n"
        f"stderr: {result.stderr.decode()[-2000:]}"
    )


async def _fetch(sql, **params) -> list[tuple]:
    """Читает в СВОЁМ соединении и закрывает его до возврата.

    Фикстура `session` держала бы `ACCESS SHARE` на таблицах, а `ALTER TABLE
    RENAME COLUMN` требует `ACCESS EXCLUSIVE` и ждёт её вечно — тот же приём
    и причина, что в `test_consent_version_schema.py`.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(sql, params)).all())
    finally:
        await engine.dispose()


async def _apply_migration() -> None:
    """Снимает `0035`, затем ставит её обратно — миграция РЕАЛЬНО исполняется.

    **Зачем.** Первая версия тестов звала `upgrade head` на БД, которая уже на
    голове: Alembic отвечал «уже применено», `upgrade()` не исполнялся ни разу,
    и тест проверял каталог, оставшийся от прошлого прогона. Мутации M1/M2/M3
    (убрать переименование колонки / индексов / NOT NULL) оставались ЗЕЛЁНЫМИ —
    тест доказывал состояние БД, а не поведение миграции (P-25, P-29).

    `downgrade 0034` здесь безопасен: `0035` обратима и проверена отдельным
    тестом отката.
    """
    _run_alembic("downgrade", "0034")
    # CR 12.1 (P-29, C10): проверяется и состояние ДО наката. Без этого неполный
    # список `_TABLES` в миграции проходил незамеченным: таблица, которую забыли
    # в `downgrade`, оставалась с `city_slug`, и `upgrade` её тоже не трогал —
    # тест видел целевое состояние и зеленел.
    _after_downgrade = await _fetch(_COLUMNS_SQL, tables=list(_TABLES))
    _found_before = {(str(t), str(c)) for t, c in _after_downgrade}
    for table in _TABLES:
        assert (table, "community_slug") in _found_before, (
            f"{table}.community_slug не вернулся после downgrade 0034: таблица "
            "отсутствует в `_TABLES` миграции"
        )
    _run_alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_upgrade_renames_column_everywhere() -> None:
    """AC-1: `city_slug` есть во всех трёх таблицах, `community_slug` — ни в одной."""
    await _apply_migration()

    rows = await _fetch(_COLUMNS_SQL, tables=list(_TABLES))
    found = {(str(table), str(column)) for table, column in rows}

    for table in _TABLES:
        assert (table, "city_slug") in found, f"{table}.city_slug не создан"
        assert (table, "community_slug") not in found, (
            f"{table}.community_slug остался: переименование не применено"
        )


@pytest.mark.asyncio
async def test_upgrade_leaves_no_community_objects() -> None:
    """AC-2: ноль индексов и ограничений со словом `community` в имени."""
    await _apply_migration()

    leftovers = [str(obj) for (obj,) in await _fetch(_COMMUNITY_OBJECTS_SQL)]
    assert leftovers == [], f"объекты с `community` в имени остались: {leftovers}"


@pytest.mark.asyncio
async def test_upgrade_creates_expected_city_names() -> None:
    """AC-2: четыре имени из ADR-23 существуют — проверка по содержимому каталога.

    Без этого «ноль объектов с `community`» проходил бы и при пустой схеме:
    `DROP INDEX` тоже убирает слово из имени.
    """
    await _apply_migration()

    names = {str(obj) for (obj,) in await _fetch(_COMMUNITY_OBJECTS_SQL)}
    # В каталоге имена без префикса типа — сверяем по голому имени.
    rows = await _fetch(
        text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "UNION SELECT conname FROM pg_constraint "
            "WHERE connamespace = 'public'::regnamespace"
        )
    )
    existing = {str(name) for (name,) in rows}

    for expected in _EXPECTED_OBJECTS:
        assert expected in existing, f"{expected} не создан миграцией 0035"
    # `names` — именно объекты с `community`; пустота проверена отдельным тестом,
    # здесь важно, что ожидаемые имена не совпали с ними случайно.
    assert names == set()


@pytest.mark.asyncio
async def test_downgrade_restores_community_names() -> None:
    """AC-3: откат возвращает `community_slug` и старые имена.

    Цель отката — `0034`, а не `-1`: после появления `0036` (стори 12.2)
    относительный шаг снял бы ЕЁ, а не тестируемую `0035`, и тест проверял
    бы не ту миграцию. Явная цель не ломается при каждой новой ревизии.
    """
    await _apply_migration()
    _run_alembic("downgrade", "0034")

    try:
        rows = await _fetch(_COLUMNS_SQL, tables=list(_TABLES))
        found = {(str(table), str(column)) for table, column in rows}
        for table in _TABLES:
            assert (table, "community_slug") in found, (
                f"{table}.community_slug не вернулся после downgrade"
            )
            assert (table, "city_slug") not in found, (
                f"{table}.city_slug остался после downgrade"
            )

        # Точные имена: `0032.downgrade()` дропает `ck_users_community_slug`.
        constraints = await _fetch(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace "
                "AND conname IN ('ck_users_community_slug', "
                "'uq_places_community_name', 'ix_event_cards_community_starts')"
            )
        )
        indexes = await _fetch(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname IN ('ix_event_cards_community_starts', "
                "'idx_places_community_active', 'idx_places_community_cursor')"
            )
        )
        assert {str(n) for (n,) in constraints} == {
            "ck_users_community_slug",
            "uq_places_community_name",
        }
        assert {str(n) for (n,) in indexes} == {
            "ix_event_cards_community_starts",
            "idx_places_community_active",
            "idx_places_community_cursor",
        }
    finally:
        # Схема обязана вернуться на голову: остальные тесты ждут `0035`.
        _run_alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_double_downgrade_survives_0032() -> None:
    """AC-3: откат ниже `0034` не падает на именах ограничений.

    `0032.downgrade()` дропает `ck_users_community_slug` по имени. Если
    `0035.downgrade()` вернёт другое имя, откат до `0033` упадёт на
    отсутствующем ограничении — это и проверяется.
    """
    await _apply_migration()
    _run_alembic("downgrade", "0033")
    try:
        # Схема на `0033`: колонка `community_slug` на месте (её создал `0032`).
        rows = await _fetch(_COLUMNS_SQL, tables=["users"])
        found = {(str(table), str(column)) for table, column in rows}
        assert ("users", "community_slug") in found
        assert ("users", "city_slug") not in found
    finally:
        _run_alembic("upgrade", "head")


#: Симуляция PG 16. Именованные NOT NULL (`pg_constraint.contype = 'n'`)
#: появились в PG 17; на PG 16 NOT NULL живёт только в `pg_attribute.attnotnull`.
#: Здесь имя переименовывается в `zz_pg16_sim_*` — состояние то же, что на PG 16
#: (ожидаемого имени в `pg_constraint` нет), но достижимо на PG 18.
_SIM_PREFIX = "zz_pg16_sim_"


def _load_migration_module():
    """Модуль `0035` по пути — константы берутся из него, а не копируются.

    Копия списка в тесте разошлась бы с миграцией молча: правка миграции не
    потребовала бы правки теста, и замок перестал бы замком.
    """
    import importlib.util

    path = API_ROOT / "alembic" / "versions" / "0035_rename_community_to_city.py"
    spec = importlib.util.spec_from_file_location("migration_0035", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pg16_hide_sql(table: str, old_name: str, sim_name: str) -> str:
    """Скрывает имя NOT NULL-ограничения, сохраняя сам NOT NULL.

    Литералы, а не bind-параметры: asyncpg передаёт `$1` внутрь `DO`-блока как
    параметр plpgsql, и подстановка ломается. Имена — константы миграции и
    литерал `_SIM_PREFIX`, инъекция невозможна.
    """
    return (
        "DO $$\n"
        "DECLARE r record;\n"
        "BEGIN\n"
        "    FOR r IN SELECT conname FROM pg_constraint\n"
        f"             WHERE conrelid = to_regclass('public.{table}')\n"
        f"               AND conname = '{old_name}'\n"
        "    LOOP\n"
        "        EXECUTE format('ALTER TABLE %I RENAME CONSTRAINT %I TO %I',\n"
        f"                       '{table}', r.conname, '{sim_name}');\n"
        "    END LOOP;\n"
        "END $$;"
    )


def _pg16_restore_sql(table: str, sim_name: str, canonical: str) -> str:
    """Возвращает каноническое имя PG 17+ после теста (на PG 16 — no-op)."""
    return (
        "DO $$\n"
        "DECLARE r record;\n"
        "BEGIN\n"
        "    FOR r IN SELECT conname FROM pg_constraint\n"
        f"             WHERE conrelid = to_regclass('public.{table}')\n"
        f"               AND conname = '{sim_name}'\n"
        "    LOOP\n"
        "        EXECUTE format('ALTER TABLE %I RENAME CONSTRAINT %I TO %I',\n"
        f"                       '{table}', r.conname, '{canonical}');\n"
        "    END LOOP;\n"
        "END $$;"
    )


#: NOT NULL/`is_nullable` по тестируемым колонкам — единственный источник
#: проверки «ограничение сохранилось».
_SLUG_NULLABILITY_SQL = text(
    "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'public' "
    "AND column_name IN ('community_slug', 'city_slug') "
    "AND table_name = ANY(:tables)"
)


async def _execute(sql) -> None:
    """DDL/DO отдельным соединением — тем же приёмом и причиной, что `_fetch`."""
    statement = sql if hasattr(sql, "_execute_on_connection") else text(sql)
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(statement)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_survives_pg16_without_named_not_null() -> None:
    """Портируемость NOT NULL: где имени нет, переименование молчит, схема цела.

    **Инцидент деплоя 2026-10-30.** На стенде (PG 16.15) `upgrade 0035` падал:
    `constraint "places_community_slug_not_null" for table "places" does not
    exist`. Локально (PG 18.6) падения не было: именованные NOT NULL
    (`pg_constraint.contype = 'n'`) появились только в PG 17.

    **Симуляция условия, а не версии (P-25).** Тест не спрашивает версию
    сервера — он создаёт **состояние PG 16**: NOT NULL остаётся в
    `information_schema.columns.is_nullable = 'NO'`, а ожидаемого имени в
    `pg_constraint` нет (переименовано в `zz_pg16_sim_*`). На настоящем PG 16
    тот же шаг был бы no-op, проверяемое требование идентично.

    **Что доказывается, в три стороны:**
    1. `upgrade` доходит до конца (без фикса — `ProgrammingError` и ненулевой код);
    2. `city_slug` остаётся `NOT NULL` — пропуск переименования не ослабляет
       ограничение (иначе «прошло» значило бы «сломало схему»);
    3. `downgrade` симметричен — на PG 16 ему тоже нечего возвращать.
    """
    migration = _load_migration_module()
    await _apply_migration()
    _run_alembic("downgrade", "0034")

    sims = [
        (table, old, f"{_SIM_PREFIX}{table}_slug")
        for table, old, _new in migration._NOT_NULL_RENAMES
    ]
    try:
        # Состояние PG 16: имена скрыты, сами NOT NULL на месте.
        for table, old_name, sim_name in sims:
            await _execute(_pg16_hide_sql(table, old_name, sim_name))

        hidden = await _fetch(
            text(
                "SELECT conname FROM pg_constraint WHERE connamespace = "
                "'public'::regnamespace AND conname LIKE :prefix"
            ),
            prefix=f"{_SIM_PREFIX}%",
        )
        _hidden = {str(n) for (n,) in hidden}
        # Предусловие: на PG 18 имена скрыты все три; на PG 16 — ноль.
        assert _hidden <= {sim for _t, _o, sim in sims}

        rows = await _fetch(_SLUG_NULLABILITY_SQL, tables=list(_TABLES))
        found = {(str(t), str(c), str(n)) for t, c, n in rows}
        for table in _TABLES:
            assert (table, "community_slug", "NO") in found, (
                f"{table}.community_slug не NOT NULL до upgrade — состояние теста неверно"
            )

        # САМА ПРОВЕРКА: без фикса `alembic upgrade` падает на RENAME CONSTRAINT.
        _run_alembic("upgrade", "0035")

        rows = await _fetch(_SLUG_NULLABILITY_SQL, tables=list(_TABLES))
        found = {(str(t), str(c), str(n)) for t, c, n in rows}
        for table in _TABLES:
            assert (table, "city_slug", "NO") in found, (
                f"{table}.city_slug потерял NOT NULL после upgrade без "
                "именованного NOT NULL"
            )
            assert (table, "community_slug", "NO") not in found

        # Симметрия отката: на PG 16 возвращать нечего, падать не на чем.
        _run_alembic("downgrade", "0034")
        rows = await _fetch(_SLUG_NULLABILITY_SQL, tables=list(_TABLES))
        found = {(str(t), str(c), str(n)) for t, c, n in rows}
        for table in _TABLES:
            assert (table, "community_slug", "NO") in found
    finally:
        _run_alembic("upgrade", "head")
        # Возврат канонических имён PG 17+ (на PG 16 — no-op).
        for table, _old_name, new_name in migration._NOT_NULL_RENAMES:
            await _execute(
                _pg16_restore_sql(table, f"{_SIM_PREFIX}{table}_slug", new_name)
            )


@pytest.mark.asyncio
async def test_offline_sql_generation_survives_not_null_block() -> None:
    """Offline-режим (`--sql`): условное переименование не требует соединения.

    `alembic/env.py` держит offline-режим рабочим. Проверка условия через
    `op.get_bind()` его ломает: в offline соединения нет, падает генерация всей
    цепочки (класс дефекта `0024`, H-1 CR 12.2). `DO`-блок остаётся статическим
    текстом — проверяется эффект: команда исполняется и печатает SQL.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0034:0035", "--sql"],
        cwd=API_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"offline-генерация 0035 сломана:\n{result.stderr.decode()[-2000:]}"
    )
    generated = result.stdout.decode()
    assert "RENAME CONSTRAINT" in generated, generated[-2000:]
    # Условие сохранено в тексте: без него offline-скрипт падал бы на PG 16.
    assert "pg_constraint" in generated, generated[-2000:]


@pytest.mark.asyncio
async def test_old_code_fails_on_new_schema() -> None:
    """AC-4: старый код (читающий `community_slug`) получает ошибку.

    Это доказательство того, что деплой обязан быть одновременным: после
    миграции любой запрос со старым именем колонки падает. Без этого AC
    остался бы прозой в рунбуке.
    """
    await _apply_migration()

    from sqlalchemy.exc import ProgrammingError

    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            with pytest.raises(ProgrammingError) as exc:
                await connection.execute(text("SELECT community_slug FROM places LIMIT 1"))
            assert "community_slug" in str(exc.value)
    finally:
        await engine.dispose()
