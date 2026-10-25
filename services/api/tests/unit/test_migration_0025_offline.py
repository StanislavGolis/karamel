"""Миграция `0025` в offline-режиме и её ревизионная привязка (AC-09, AC-12).

**Регресс на класс дефекта D-7.** В эпике 7 `print` в миграции ломал
`alembic upgrade --sql`, а guard в `downgrade` обходился в offline-режиме.
Оба дефекта — про одно: скрипт, отданный оператору, не должен ни печатать
мусор в stdout, ни молча удалять данные.

Здесь проверяется ИСХОДНИК, а не поведение: запуск `alembic --sql` требует
конфигурации и потому живёт в integration-тестах миграционного цикла.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATION_NAME = "0025_extend_reports_and_create_sanctions"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{MIGRATION_NAME}.py"
)


def _load_migration() -> object:
    """Загружает `0025` по ПУТИ, а не по имени.

    `alembic/versions` — не пакет (нет `__init__.py`), а имя модуля
    начинается с цифры: обычный `import` невозможен. Тот же приём, что в
    `alembic/env.py` при перечислении ревизий.
    """
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0025_extend_reports_and_create_sanctions.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0025", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> object:
    return _load_migration()


def test_revision_chain(migration: object) -> None:
    """AC-12: `0025` продолжает `0024` — сверено `alembic heads` 2026-10-25."""
    assert migration.revision == "0025"  # type: ignore[attr-defined]
    assert migration.down_revision == "0024"  # type: ignore[attr-defined]


def test_no_print_in_migration() -> None:
    """AC-09, D-7: `print` в миграции ломает `upgrade --sql` и грязнит stdout."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    # Только вызовы: `print` внутри docstring/комментария не считается.
    code_lines = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    offenders = [line for line in code_lines if "print(" in line]
    assert not offenders, f"print в миграции: {offenders}"


def test_offline_guards_present() -> None:
    """AC-09: `upgrade` и `downgrade` падают явно при `as_sql`.

    Guard, обходимый в offline-режиме, отсутствует ровно там, где скрипт
    отдают оператору (урок CR 7.3). Проверяются ОБА направления.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert source.count("op.get_context().as_sql") == 2, (
        "ожидаются два guard-а: в upgrade и в downgrade"
    )


def test_downgrade_guards_sanctions_before_drop() -> None:
    """AC-10: guard по `sanctions` стоит ДО `DROP TABLE`.

    Порядок проверяется по позициям в исходнике: если guard окажется после
    удаления, он не защитит ничего.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    guard_at = source.index("SELECT count(*) FROM sanctions")
    drop_at = source.index('op.drop_table("sanctions")')
    assert guard_at < drop_at


def test_concurrent_indexes_use_autocommit_block() -> None:
    """AC-08: `CONCURRENTLY` вне транзакции — иначе `CREATE INDEX` упадёт."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CONCURRENTLY" in source
    assert "autocommit_block()" in source


def test_backfill_guard_present_before_check() -> None:
    """AC-05, D-18: guard-backfill стоит ДО включения `target_scope`.

    **Почему проверяется исходник, а не поведение.** На легальных данных
    нарушитель недостижим: в `0024` `attendance_id` был `NOT NULL` с
    `ON DELETE CASCADE`, а `chat_id` ещё нет — любой `no_show` имеет пару, и
    backfill заполняет `event_card_id` из неё. Guard защищает от состояния,
    которое возникает только при ручной правке БД или при будущем ослаблении
    FK. Тест фиксирует наличие защиты и её МЕСТО: если `CHECK` окажется
    раньше backfill, миграция упадёт на объёме (мутация M7).
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    backfill_at = source.index("UPDATE reports r SET event_card_id")
    guard_at = source.index("WHERE target_type = 'no_show'")
    scope_at = source.index('op.create_check_constraint("target_scope"')
    assert backfill_at < guard_at < scope_at
