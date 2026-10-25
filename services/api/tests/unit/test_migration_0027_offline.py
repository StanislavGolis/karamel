"""Миграция `0027`: ревизионная привязка и offline-защита (стори 8.3; Task 7.3).

Регресс на два класса дефектов, уже случавшихся в проекте:

1. **`print` в миграции** (D-7) ломает `upgrade --sql` и грязнит stdout.
2. **`CONCURRENTLY` в `downgrade`** (CR 8.2, H-1) — `autocommit_block` в откате
   коммитит снятие индекса ВНЕ транзакции ревизии; остановка guard'ом ниже
   оставляет схему без индекса при формально верном `alembic_version`.

Проверяется ИСХОДНИК, а не поведение: запуск `alembic --sql` требует
конфигурации и потому живёт в integration-тестах цикла миграций. Исходник
здесь — носитель контракта, потому что вся защита выражена явными вызовами.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

MIGRATION_NAME = "0027_reports_queue_index"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{MIGRATION_NAME}.py"
)


def _load_migration() -> object:
    """Загружает `0027` по ПУТИ: `alembic/versions` — не пакет, имя с цифры."""
    spec = importlib.util.spec_from_file_location("migration_0027", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> object:
    return _load_migration()


def test_revision_chain(migration: object) -> None:
    """AC-01: `0027` продолжает `0026` — единственная миграция 8.3.

    Сверено `alembic heads` 2026-10-25: `0026` был головой, `0027` — новая.
    Разрыв цепочки (`down_revision` не `0026`) дал бы две головы и упавший
    `alembic upgrade head` на стенде.
    """
    assert migration.revision == "0027"  # type: ignore[attr-defined]
    assert migration.down_revision == "0026"  # type: ignore[attr-defined]


def test_no_print_in_migration() -> None:
    """AC-01, D-7: `print` ломает `upgrade --sql` и грязнит stdout."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if "print(" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, f"print в миграции: {offenders}"


def test_upgrade_creates_index_concurrently() -> None:
    """AC-01, D-4: `CREATE INDEX CONCURRENTLY` — иначе блокировка `INSERT`.

    `reports` на стенде может быть непустой; обычный `CREATE INDEX` взял бы
    `ACCESS EXCLUSIVE` и остановил подачу жалоб на всё время построения.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "ix_reports_open_reported_at_uuid" in source
    assert "WHERE status = 'open'" in source


def test_upgrade_is_offline_safe() -> None:
    """D-7: в `--sql` режиме `upgrade` падает явно.

    Проверить `indisvalid` без соединения нельзя; генерация скрипта без
    guard'а выдала бы оператору «успешный» скрипт, который мог оставить
    невалидный индекс.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert source.count("op.get_context().as_sql") == 2
    assert "raise RuntimeError(" in source


def test_upgrade_has_indisvalid_guard() -> None:
    """AC-01: невалидный индекс от прерванного построения не переживёт миграцию.

    `CONCURRENTLY` может оставить индекс с `indisvalid=false`: планировщик его
    не использует, и молчаливое «миграция применена» дало бы очередь без
    индекса на объёме. Тот же guard, что в `0025`.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "SELECT indisvalid FROM pg_index" in source
    assert "indisvalid={valid!r}" in source


def test_guard_uses_to_regclass_not_cast() -> None:
    """CR 8.3 H-3: guard ищет индекс через `to_regclass`, а не `::regclass`.

    **Дефект, который это ловит.** `'name'::regclass` на НЕСУЩЕСТВУЮЩЕМ
    индексе бросает `UndefinedTableError` («relation … does not exist»), а не
    возвращает `NULL`. Тогда ветка «индекс вообще не создан» проходила бы мимо
    задуманного `RuntimeError` сырой ошибкой драйвера, и оператор увидел бы
    трейсбек asyncpg вместо инструкции «пересоздайте индекс».

    Краснит при возврате приведения `::regclass`.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    # Комментарии отбрасываются: в пояснении к guard'у написано «`'name'::regclass`
    # бросает …» — это проза о дефекте, а не сам дефект. Проверяется КОД.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "to_regclass(:name)" in code
    assert "::regclass" not in code, code


def test_downgrade_drops_index_atomically() -> None:
    """AC-01, CR 8.2 H-1, мутация M8: откат БЕЗ `CONCURRENTLY`.

    Проверяется именно блок `downgrade`, а не файл целиком: `CONCURRENTLY`
    в `upgrade` обязателен, и поиск по всему тексту дал бы ложное срабатывание.
    Краснеет, если в откат вернут `CONCURRENTLY`/`autocommit_block`.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    downgrade_body = source.split("def downgrade()")[1]
    # Комментарии отбрасываются: в блоке отката есть пояснение «Без
    # `CONCURRENTLY`…», и поиск по всему тексту ловил бы слово в прозе.
    # Проверяется КОД, а не упоминание.
    code = "\n".join(
        line for line in downgrade_body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "CONCURRENTLY" not in code, code
    assert "autocommit_block" not in code, code
    assert "DROP INDEX IF EXISTS" in code


def test_upgrade_body_uses_autocommit_block() -> None:
    """D-4: `CONCURRENTLY` не работает внутри транзакции — нужен `autocommit_block`.

    Без него PostgreSQL отвечает `CREATE INDEX CONCURRENTLY cannot run inside
    a transaction block`, и миграция падает на стенде, а не в тестах.

    **CR 8.3, H-1: проверка по AST, а не по подстроке.** Первая версия искала
    слово `autocommit_block` в тексте блока `upgrade` — и оставалась ЗЕЛЁНОЙ
    после удаления реального вызова, потому что слово есть в docstring и в
    комментариях. Тавтология (§18.2): тест не краснел от снятия защиты.
    Теперь из разобранного дерева берутся только УЗЛЫ, то есть исполняемый
    код: проза в строковых литералах в дерево как вызов не попадает.
    """
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    upgrade_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    calls = {
        node.func.attr
        for node in ast.walk(upgrade_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "autocommit_block" in calls, sorted(calls)
