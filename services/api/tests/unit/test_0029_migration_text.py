"""Текст миграции `0029`: `CONCURRENTLY` в upgrade, атомарный downgrade (AC-06).

**Почему проверяется текст, а не поведение** (§18.3 разрешает для таких
случаев). `indisvalid` истинен и для индекса, построенного обычным
`CREATE INDEX`: свойство «не блокирует таблицу» каталогом не наблюдаемо, и
мутация «убрать `CONCURRENTLY`» иначе осталась бы зелёной — проверено на
`0028` в DS 8.5 (D-75).

Обратная сторона — `downgrade` БЕЗ `CONCURRENTLY` (урок H-1 CR 8.2):
`autocommit_block` в откате коммитит снятие индекса вне транзакции ревизии, и
остановка guard'ом ниже оставляет схему без индекса при формально верном
`alembic_version`.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0029_access_logs.py"


def _function_body(source: str, name: str) -> str:
    """Тело функции верхнего уровня: от `def name(` до следующего `def `.

    Разбор по тексту, а не по AST: нужна ПОДСТРОКА оператора, а AST вернул бы
    уже разрезанную конструкцию, и `CONCURRENTLY` искался бы в отрыве от
    `op.execute`.
    """
    start = source.index(f"def {name}(")
    rest = source[start + 1 :]
    match = re.search(r"^def ", rest, re.MULTILINE)
    return rest if match is None else rest[: match.start()]


def test_upgrade_creates_indexes_concurrently() -> None:
    """D-4: оба индекса строятся `CONCURRENTLY`."""
    body = _function_body(MIGRATION.read_text(encoding="utf-8"), "upgrade")
    assert "CREATE INDEX CONCURRENTLY" in body, body
    assert "CREATED_INDEX" in body and "MODERATOR_INDEX" in body, body


def test_downgrade_drops_indexes_without_concurrently() -> None:
    """H-1 CR 8.2: откат атомарный — `DROP INDEX` без `CONCURRENTLY`.

    Ищется ОПЕРАТОР, а не слово: `CONCURRENTLY` упоминается в комментарии,
    объясняющем запрет, и проверка на подстроку краснела бы на собственном
    пояснении (дефект, найденный при первом прогоне в DS 8.5).
    """
    body = _function_body(MIGRATION.read_text(encoding="utf-8"), "downgrade")
    assert "DROP INDEX IF EXISTS" in body, body
    assert "DROP INDEX CONCURRENTLY" not in body, body
    assert "with op.get_context().autocommit_block()" not in body, body


def test_both_functions_are_offline_safe() -> None:
    """D-7: в `--sql` режиме обе функции падают явно."""
    source = MIGRATION.read_text(encoding="utf-8")
    for name in ("upgrade", "downgrade"):
        assert "_assert_offline_safe" in _function_body(source, name), name


def test_no_foreign_keys_declared() -> None:
    """D-92: в миграции НЕТ `ForeignKey` — аудит самодостаточен.

    Проверка текстом, потому что отсутствие FK — это отсутствие кода:
    поведенчески его видно только после удаления объекта, а тест на это
    (`test_upgrade_creates_table_and_indexes`) требует живой БД. Здесь —
    статический контроль, чтобы FK не «вернули для целостности».
    """
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ForeignKey" not in source, "FK в access_logs нарушит D-92"
    assert "foreign_key" not in source.split('"""')[2], "FK в upgrade"
