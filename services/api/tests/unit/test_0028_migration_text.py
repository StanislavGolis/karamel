"""Текст миграции `0028`: `CONCURRENTLY` в upgrade и его отсутствие в downgrade.

**Почему проверяется текст, а не поведение** (§18.3 разрешает для таких
случаев). `indisvalid` истинен и для индекса, построенного обычным
`CREATE INDEX` — свойство «не блокирует таблицу» проверкой каталога не
наблюдается вообще: чтобы его увидеть, нужен параллельный `INSERT`,
упёршийся в `ACCESS EXCLUSIVE`, то есть тест на таймингах. Мутация «убрать
`CONCURRENTLY`» иначе осталась бы зелёной — проверено прогоном в DS 8.5.

Обратная сторона — `downgrade` БЕЗ `CONCURRENTLY` (урок H-1 CR 8.2):
`autocommit_block` в откате коммитит снятие индекса вне транзакции ревизии,
и остановка guard'ом ниже оставляет схему без индекса при формально верном
`alembic_version`. Это уже не «мягкое» свойство — это дрейф схемы, и он
проверяется тем же приёмом.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0028_reports_accused_id.py"
)


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
    """D-4: оба индекса строятся `CONCURRENTLY` — без блокировки таблицы."""
    source = MIGRATION.read_text(encoding="utf-8")
    body = _function_body(source, "upgrade")
    assert "CREATE INDEX CONCURRENTLY" in body, body
    # Оба индекса внутри цикла — проверяется, что цикл не сузился до одного.
    assert "ACCUSED_INDEX" in body and "REPORTER_INDEX" in body, body


def test_downgrade_drops_indexes_without_concurrently() -> None:
    """H-1 CR 8.2: откат атомарный — `DROP INDEX` без `CONCURRENTLY`.

    `CONCURRENTLY` в откате вынес бы снятие индекса за транзакцию ревизии, и
    остановка guard'ом ниже оставила бы схему без индексов навсегда.
    """
    source = MIGRATION.read_text(encoding="utf-8")
    body = _function_body(source, "downgrade")
    assert "DROP INDEX IF EXISTS" in body, body
    # Ищется ОПЕРАТОР, а не слово: `CONCURRENTLY` и `autocommit_block`
    # упоминаются в комментарии, объясняющем запрет, и проверка на подстроку
    # краснела бы на собственном пояснении (дефект этой проверки, найденный
    # прогоном в DS 8.5). Формы операторов ниже однозначны.
    assert "DROP INDEX CONCURRENTLY" not in body, body
    assert "with op.get_context().autocommit_block()" not in body, body


def test_upgrade_is_offline_safe() -> None:
    """D-7: в `--sql` режиме обе функции падают явно.

    Без guard'а `alembic upgrade --sql` выдал бы скрипт без проверки
    `indisvalid` и без guard'а backfill, то есть применил бы миграцию,
    которая на живой БД могла молча оставить жалобы без обвиняемого.
    """
    source = MIGRATION.read_text(encoding="utf-8")
    for name in ("upgrade", "downgrade"):
        body = _function_body(source, name)
        assert "_assert_offline_safe" in body, (name, body)
