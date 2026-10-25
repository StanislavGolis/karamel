"""Белый список категорий мест (Task 1.4; AC-02).

§18.1: каждый тест краснеет от конкретной правки. Здесь правка — любое
изменение состава `PlaceCategory`: список категорий живёт в одном месте,
и `CHECK` в БД собирается из него же. Тест на состав ловит «добавили
категорию и забыли миграцию» — расхождение модели и БД.
"""

from __future__ import annotations

from app.models.enums import PlaceCategory

# ADR-12: закрытый набор. Порядок — как в архитектуре, чтобы diff миграции
# читался построчно и не выглядел перестановкой при добавлении значения.
EXPECTED_CATEGORIES = (
    "cafe",
    "cinema",
    "quiz",
    "skating",
    "park",
    "sport",
    "walk",
    "other",
)


def test_place_category_members_match_adr12() -> None:
    """AC-02: состав и порядок значений совпадают с ADR-12.

    Краснеет при добавлении/удалении/переименовании любого значения.
    Это и нужно: новое значение категории требует миграции (AC-02),
    а не тихой правки роутера.
    """
    assert tuple(str(member) for member in PlaceCategory) == EXPECTED_CATEGORIES


def test_place_category_is_str_enum() -> None:
    """AC-02: `StrEnum`, а не нативный PG-enum.

    Краснеет, если базовый класс заменят на `Enum`: тогда `str(member)`
    вернёт `'PlaceCategory.CAFE'`, и `_in_clause(...)` соберёт в `CHECK`
    литералы, которых нет в БД. Заодно ломается сериализация в JSON.
    """
    assert PlaceCategory.CAFE == "cafe"
    assert isinstance(PlaceCategory.CAFE, str)


def test_place_category_check_clause_built_from_enum() -> None:
    """AC-02, D-2: `CHECK` собирается из значений класса, а не дублируется строкой.

    Краснеет, если `models/place.py` начнёт писать список категорий
    литералом: добавление `theatre` в enum перестанет менять ограничение
    в БД, и категория из Python будет отвергаться PostgreSQL.
    """
    from app.models.place import _in_clause  # noqa: PLC0415 — проверяем именно сборку

    clause = _in_clause("category", tuple(PlaceCategory))

    assert clause == (
        "category IN ('cafe', 'cinema', 'quiz', 'skating', 'park', 'sport', 'walk', 'other')"
    )
