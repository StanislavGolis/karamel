"""Схема БД: координат нет НИ В ОДНОЙ таблице (Task 1; AC-01, AC-02).

Почему отдельный файл, а не правка существующих гардов. В проекте уже пять
гардов на координаты, но они покрывают 3 таблицы из 16 и держат наборы
разной длины (6/6/6/3/4 имени), скопированные вручную (Н-2 стори 10.3).
Тринадцать таблиц не покрыты вовсе: новая таблица с колонкой `lat` прошла бы
молча. Здесь проверяется КАТАЛОГ ЦЕЛИКОМ — без фильтра по `table_name`
(D-10.3-1), поэтому новая таблица попадает в проверку автоматически и гард
не ломается от её появления.

Проверка идёт через `information_schema`, а не через модель: модель описывает
намерение, AC говорит о фактической схеме. Тест на модели прошёл бы и при
расхождении миграции с моделью — ровно тот дефект, что был с `ProfilePhoto`
(модель есть, таблицы нет). Гард на модели — `tests/unit/test_no_coordinates_guard.py`.

FR59 (PRD v2:686), NFR34 (:797): точные координаты не хранятся ни для
пользователя, ни для места встречи. FR67 (:592): место — название, категория
и адрес текстом.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Импорт, а не копия: состав `places` — часть контракта AC-02, и второй список
# тех же девяти колонок разошёлся бы при первой правке миграции.
from tests.integration.test_places_schema import EXPECTED_COLUMNS

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01, D-10.3-2: двенадцать имён, а не шесть, как в существующих гардах.
# Обоснование каждого расширения против набора `test_places_schema.py`:
#   `lon`        — вторая общепринятая запись долготы (Leaflet/GeoJSON); набор
#                  с `lng` без `lon` пропускал колонку из GeoJSON-экспорта;
#   `geom`/`geometry`/`geography` — типы PostGIS с той же точностью, что
#                  `numeric`-пара: колонка `location geometry` не содержит
#                  слова `lat` и прежним набором не ловилась;
#   `point`      — и тип PostGIS, и самое вероятное имя колонки «место»;
#   `coords`/`coordinates`/`geo` — без изменений, совместимость с пятью
#                  существующими гардами (D-10.3-9: их наборы не унифицируем).
FORBIDDEN_COLUMN_NAMES = frozenset(
    {
        "lat",
        "lng",
        "lon",
        "latitude",
        "longitude",
        "coords",
        "coordinates",
        "geo",
        "geom",
        "geography",
        "geometry",
        "point",
    }
)

# Служебная таблица самого Alembic: её колонка `version_num` — не наша схема.
_SERVICE_TABLES = frozenset({"alembic_version"})


# P-1 CR 10.3: типы PostGIS хранят ту же точность, что пара `numeric`, и приходят
# безобидным именем колонки (`location geometry`, `location point`). Проверка по
# имени их не видит — нужна проверка `udt_name` из `information_schema.columns`.
# `point` — встроенный тип PostgreSQL (доступен без расширения), `geometry` и
# `geography` — PostGIS; набор покрывает все три, потому что точность у них одна.
_POSTGIS_UDT_NAMES = frozenset({"geometry", "geography", "point"})


async def _catalog_rows(session: AsyncSession) -> list[tuple[str, str, str]]:
    """Каталог колонок схемы `public`: `(таблица, колонка, udt_name)`.

    Один запрос на две проверки (имя и тип): второй запрос по тому же каталогу
    дал бы расхождение при правке `WHERE` в одном месте и забытом другом.

    `assert rows` (P-5 CR 10.3) — пустой каталог означает «схема не поднята»,
    и без него обе проверки были бы зелены и слепы.
    """
    result = await session.execute(
        text(
            "SELECT c.table_name, c.column_name, c.udt_name "
            "FROM information_schema.columns AS c "
            "JOIN information_schema.tables AS t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
        )
    )
    rows = [(row[0], row[1], row[2]) for row in result]
    assert rows, "каталог пуст: схема не поднята миграциями, проверка слепа"
    return rows


async def test_no_coordinate_columns_anywhere(session: AsyncSession) -> None:
    """AC-01, NFR34: ни в одной таблице схемы `public` нет колонки координат.

    §18.1: краснеет при появлении колонки. Штатный сценарий проверки —
    `ALTER TABLE places ADD COLUMN geom text` (мутация M-5): строка появляется
    в `information_schema.columns` без пересборки схемы, и тест падает,
    называя `таблица.колонка`.

    Фильтра по `table_name` НЕТ намеренно (D-10.3-1): с ним покрытие привязано
    к литералу, и семнадцатая таблица с координатами прошла бы зелёной.
    `table_type = 'BASE TABLE'` отсекает представления — их состав производный
    от таблиц и проверяется теми же строками каталога.

    Проверка ТОЛЬКО по имени (P-1 CR 10.3): колонка `location point` здесь
    зелёная намеренно — её ловит `test_no_postgis_typed_columns_anywhere`.
    Два теста, а не один, чтобы откат показывал, какое правило сработало.
    """
    rows = await _catalog_rows(session)

    violations = sorted(
        f"{table}.{column}"
        for table, column, _ in rows
        if column.lower() in FORBIDDEN_COLUMN_NAMES and table not in _SERVICE_TABLES
    )

    assert not violations, (
        "NFR34/FR59: координаты в схеме — "
        f"{', '.join(violations)}. Точные координаты не хранятся ни для "
        "пользователя, ни для места встречи; адрес хранится текстом (FR67)."
    )


async def test_no_postgis_typed_columns_anywhere(session: AsyncSession) -> None:
    """AC-01, NFR34, P-1 CR 10.3: колонок типов-координат нет ни в одной таблице.

    Почему отдельный тест, а не строка в предыдущем. Гард по имени слеп к
    `location geometry` / `location point`: имя безобидно, точность хранится
    в типе. Ревью 10.3 доказало дыру фактом — `ALTER TABLE places ADD COLUMN
    location text` давал **1 passed**.

    Набор имён при этом НЕ расширяется на `location` (решение D-1 CR): `location`
    может быть легитимным текстовым адресом (FR67), и гард по имени краснел бы
    на законной правке — то есть был бы отключён. Тип различает случаи точно:
    `location text` — адрес, `location point` — координата.

    §18.1, мутация: `ALTER TABLE places ADD COLUMN location point` → красный
    с `places.location (point)`. `geometry`/`geography` требуют PostGIS
    (в проекте расширение не устанавливается), `point` — встроен, поэтому
    красный тест воспроизводится без внешних зависимостей.
    """
    rows = await _catalog_rows(session)

    violations = sorted(
        f"{table}.{column} ({udt_name})"
        for table, column, udt_name in rows
        if udt_name.lower() in _POSTGIS_UDT_NAMES and table not in _SERVICE_TABLES
    )

    assert not violations, (
        "NFR34/FR59: координаты в схеме по типу колонки — "
        f"{', '.join(violations)}. Тип `geometry`/`geography`/`point` хранит "
        "точность координат независимо от имени колонки; адрес хранится текстом (FR67)."
    )


async def test_places_columns_are_exactly_name_category_address(
    session: AsyncSession,
) -> None:
    """AC-02, FR67: состав `places` — равенство контракту, не подмножество.

    §18.1: краснеет и при лишней колонке, и при недостающей. Место встречи
    хранит название, категорию и адрес текстом; появление `lat` рядом с
    `address_text` — попытка хранить и то и другое.

    `EXPECTED_COLUMNS` импортируется из `test_places_schema.py`, где перечислены
    типы и nullability всех девяти колонок: копия разошлась бы с оригиналом
    при первой правке миграции `0007` (Н-2, D-10.3-6).
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'places'"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}

    # Равенство, а не `EXPECTED_COLUMNS.items() <= actual.items()`: подмножество
    # пропустило бы лишнюю колонку, то есть ровно то, что запрещает AC-02.
    assert actual == dict(EXPECTED_COLUMNS)
    # P-7 CR 10.3: `.lower()` обязателен — проверка выше (`:98`) нормализацию
    # регистра делает, а без неё здесь `places.Lat` проходил эту строку зелёным.
    assert not {name.lower() for name in actual} & FORBIDDEN_COLUMN_NAMES
