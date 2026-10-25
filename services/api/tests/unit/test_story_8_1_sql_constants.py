"""SQL-константы модели и миграции совпадают (стори 8.1; AC-02, AC-03, AC-06).

Миграция НЕ импортирует приложение осознанно (иначе `alembic` тянул бы
FastAPI-настройки и падал бы на голом окружении DBA). Цена — дублирование
SQL. Этот тест — единственное место, где дублирование проверяется: без него
правка модели без правки миграции дала бы `alembic check` зелёным (он
сравнивает СХЕМУ, а не текст `CHECK`).

Сравнение с нормализацией пробелов: PostgreSQL хранит `CHECK` в
переформатированном виде, поэтому побайтовое равенство литералов — это
контракт между ДВУМЯ файлами проекта, а не с БД.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models import report as report_model
from app.models import sanction as sanction_model


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
    """Модуль миграции `0025`."""
    return _load_migration()


def _norm(sql: str) -> str:
    """Схлопывает пробелы: переносы строк — форматирование, не смысл."""
    return re.sub(r"\s+", " ", sql).strip()


def test_target_scope_sql_matches(migration: object) -> None:
    """AC-03, D-15: одна и та же формула в модели и в миграции."""
    model_sql = _norm(report_model.TARGET_SCOPE_SQL)
    migration_sql = _norm(migration._TARGET_SCOPE_SQL)  # type: ignore[attr-defined]
    assert model_sql == migration_sql


def test_status_resolution_consistency_matches(migration: object) -> None:
    """AC-06: бикондиционал «не разобрана ⟺ решения нет»."""
    assert _norm(report_model.STATUS_RESOLUTION_CONSISTENCY_SQL) == _norm(
        migration._STATUS_RESOLUTION_CONSISTENCY_SQL  # type: ignore[attr-defined]
    )


def test_status_resolution_mapping_matches(migration: object) -> None:
    """AC-06: соответствие состояния и вида решения."""
    assert _norm(report_model.STATUS_RESOLUTION_MAPPING_SQL) == _norm(
        migration._STATUS_RESOLUTION_MAPPING_SQL  # type: ignore[attr-defined]
    )


def test_target_type_domain_has_both_values() -> None:
    """AC-02, P-8: предикат проверяется в ОБОИХ значениях.

    Домен из одного значения не отличил бы «расширили» от «не расширяли»:
    `IN ('no_show')` и `IN ('message','no_show')` — разные домены, и жалоба
    на переписку возможна только во втором.
    """
    assert set(report_model.REPORT_TARGET_TYPES) == {"message", "no_show"}


def test_resolution_domain_matches_sanction_type_plus_dismissed() -> None:
    """AC-06/AC-07: `resolution` = санкции + `dismissed` (закрытие без санкции)."""
    assert set(report_model.REPORT_RESOLUTIONS) == set(sanction_model.SANCTION_TYPES) | {
        "dismissed"
    }


def test_in_clause_quotes_every_value() -> None:
    """AC-02: каждое значение в кавычках — иначе `CHECK` не собрался бы."""
    sql = report_model._in_clause("target_type", report_model.REPORT_TARGET_TYPES)
    assert sql == "target_type IN ('message', 'no_show')"
