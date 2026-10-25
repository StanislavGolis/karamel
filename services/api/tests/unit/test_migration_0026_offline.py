"""Миграция `0026` в offline-режиме и ревизионная привязка (AC-11, AC-12).

Регресс на класс дефекта D-7: `print` в миграции ломает `upgrade --sql`, а
guard в `downgrade` не должен обходиться в offline-режиме — иначе скрипт,
отданный оператору, удалит колонку без единой проверки (урок CR 7.3).

Проверяется ИСХОДНИК, а не поведение: запуск `alembic --sql` требует
конфигурации и потому живёт в integration-тестах цикла миграций.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MIGRATION_NAME = "0026_add_chats_status"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{MIGRATION_NAME}.py"
)


def _load_migration() -> object:
    """Загружает `0026` по ПУТИ: `alembic/versions` — не пакет, имя с цифры."""
    spec = importlib.util.spec_from_file_location("migration_0026", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> object:
    return _load_migration()


def test_revision_chain(migration: object) -> None:
    """AC-12: `0026` продолжает `0025` — сверено `alembic heads` 2026-10-25."""
    assert migration.revision == "0026"  # type: ignore[attr-defined]
    assert migration.down_revision == "0025"  # type: ignore[attr-defined]


def test_no_print_in_migration() -> None:
    """AC-11, D-7: `print` ломает `upgrade --sql` и грязнит stdout."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if "print(" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, f"print в миграции: {offenders}"


def test_downgrade_has_offline_guard() -> None:
    """AC-11: откат падает явно при `--sql`, а не удаляет колонку молча."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert source.count("op.get_context().as_sql") == 1


def test_upgrade_adds_status_with_server_default() -> None:
    """AC-01: `server_default` обязателен — иначе `NOT NULL` упал бы на данных."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "server_default=sa.text(\"'active'\")" in source


def test_downgrade_drops_check_before_column() -> None:
    """AC-11: снять `CHECK` до колонки — иначе PostgreSQL откажет.

    Порядок проверяется по позициям в исходнике: `DROP COLUMN` раньше
    `DROP CONSTRAINT` упал бы на зависимом ограничении.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    drop_check = source.index('op.drop_constraint(op.f("ck_chats_status")')
    drop_column = source.index('op.drop_column("chats", "status")')
    assert drop_check < drop_column
