"""Схема `places` (Task 9.1–9.6; AC-01, AC-02, AC-04, AC-05).

Проверки идут через `information_schema` и `pg_constraint`, а не через модель:
модель описывает намерение, а AC говорит о фактической схеме. Тест на модели
прошёл бы и при расхождении миграции `0007` с моделью — ровно тот дефект,
что был с `ProfilePhoto` (модель есть, таблицы нет).

§18.1: каждое ограничение проверяется НАРУШЕНИЕМ, а не чтением каталога:
наличие `CHECK` в `pg_constraint` не доказывает, что он что-то запрещает.
Каталог используется только для проверки ИМЁН — они часть контракта
(`op.f`, §18.7), и расхождение ломает `alembic check` и `downgrade`.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PlaceCategory
from app.schemas.place import PlaceRead

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01: точный состав колонок с типами и nullability. Равенство, а не
# подмножество: тест падает и при лишней колонке, и при недостающей.
# ACHTUNG: ключевое свойство AC-01/NFR34 — здесь НЕТ ни одной колонки
# координат. Появление `lat`/`lng`/`geo`/`coords` ломает тест.
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "name": ("character varying", "NO"),
    "category": ("character varying", "NO"),
    "address_text": ("character varying", "NO"),
    "city_slug": ("character varying", "NO"),
    "is_active": ("boolean", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}

FORBIDDEN_COORDINATE_COLUMNS = ("lat", "lng", "latitude", "longitude", "coords", "geo")

EXPECTED_CONSTRAINTS = (
    "pk_places",
    "uq_places_uuid",
    "uq_places_city_name",
    "ck_places_category",
    "ck_places_name",
    "ck_places_address_text",
    "ck_places_city_slug",
)


def _place(**overrides: Any) -> dict[str, Any]:
    """Строка места для прямой вставки SQL: тест идёт мимо Pydantic намеренно.

    Проверяется, что белый список и запрет пустоты живут в БД, а не только
    в схемах: воркер и админский скрипт пишут сырым SQL.
    """
    base: dict[str, Any] = {
        "name": "Кафе „Прага“",
        "category": "cafe",
        "address_text": "пр. Ленина, 1",
        "city_slug": "tomsk",
    }
    base.update(overrides)
    return base


async def _insert_place(session: AsyncSession, **overrides: Any) -> None:
    """Вставка места сырым SQL — без ORM-нормализации и схем."""
    values = _place(**overrides)
    await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug) "
            "VALUES (:name, :category, :address_text, :city_slug)"
        ),
        values,
    )


# --- AC-01: колонки -------------------------------------------------------------


async def test_places_columns_match_declared_schema(session: AsyncSession) -> None:
    """AC-01: состав колонок, типы и `NOT NULL` совпадают с объявленными.

    Краснеет при лишней колонке (например, добавленной «на будущее») и при
    снятом `NOT NULL` с обязательного поля: место без адреса сломало бы
    карточку события (эпик 4).
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'places'"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}

    assert actual == EXPECTED_COLUMNS


async def test_places_has_no_coordinate_columns(session: AsyncSession) -> None:
    """AC-01, NFR34: координат нет ни в каком виде.

    Краснеет, если добавить `lat`, `lng`, `latitude`, `longitude`, `coords`
    или `geo`. Причина не стилистическая: точные координаты — трансграничная
    передача и лишние ПД, которых PRD не требует; геокодер не вызывается
    вовсе.
    """
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'places'"
        )
    )
    columns = {row[0] for row in result}

    assert not (columns & set(FORBIDDEN_COORDINATE_COLUMNS))


# --- AC-01, AC-04: имена ограничений -------------------------------------------


async def test_places_constraint_names_match_model(session: AsyncSession) -> None:
    """§18.7: имена ограничений совпадают с моделью.

    Краснеет при двойном префиксе (`ck_places_ck_places_category`) — то есть
    когда в миграции забыт `op.f`. Такой diff не ловится `CHECK`-ом, но валит
    `alembic check` и `downgrade` (падает на несуществующем имени).
    """
    # CR 3.1, патч 12b: `contype IN ('p','u','c')` — только PK/UNIQUE/CHECK.
    # PostgreSQL 18 показывает `NOT NULL` отдельными записями `pg_constraint`
    # (`contype='n'`, имена вида `places_name_not_null`), и без фильтра
    # множество «лишних» ограничений состояло бы из них, а не из дефектов.
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.places'::regclass AND contype IN ('p','u','c')"
        )
    )
    names = {row[0] for row in result}

    # CR 3.1, патч 12 (AC-03): РАВЕНСТВО, а не подмножество. С `<=` тест был
    # зелёным при появлении лишнего ограничения — например FK
    # `fk_places_city_slug_communities` на таблицу, которой по ADR-01
    # не существует. AC-03 требует «таблица `communities` не создаётся
    # и FK на неё нет», и это должно проверяться, а не подразумеваться.
    assert names == set(EXPECTED_CONSTRAINTS), (
        f"лишние ограничения: {sorted(names - set(EXPECTED_CONSTRAINTS))}, "
        f"отсутствуют: {sorted(set(EXPECTED_CONSTRAINTS) - names)}"
    )


async def test_no_foreign_keys_on_places(session: AsyncSession) -> None:
    """AC-03 (ADR-01): на `places` нет ни одного FK.

    Краснеет, если появится FK на `communities`: таблицы сообществ в пилоте
    нет (NFR46), и FK потребовал бы её создать, то есть сменить архитектуру
    одним «аккуратным» ограничением. Ссылочную целостность `city_slug`
    держат regex-CHECK и сверка с `Settings.city_slugs`.
    """
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.places'::regclass AND contype = 'f'"
        )
    )

    assert result.scalars().all() == []


async def test_active_index_is_partial_on_is_active(session: AsyncSession) -> None:
    """AC-05, FR98: `idx_places_city_active` — частичный по `is_active`.

    Краснеет, если предикат убрать: индекс начнёт индексировать скрытые места,
    и выпадающий список выбора (FR98) потянет их в план запроса. Проверяется
    и состав колонок: `(city_slug, category)` — порядок под фильтр
    «активные места сообщества, категория».
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = 'idx_places_city_active'"
        )
    )
    indexdef = result.scalar_one()

    assert "(city_slug, category)" in indexdef
    assert "WHERE is_active" in indexdef


# --- AC-02: белый список в БД ---------------------------------------------------


async def test_unknown_category_rejected_by_database(session: AsyncSession) -> None:
    """AC-02, §18.1: категория вне списка отбивается `ck_places_category`.

    Краснеет, если список живёт только в Pydantic: воркер и админский скрипт
    пишут сырым SQL, и категория `'bar'` попала бы в БД, а фильтр 3.3
    получил бы значение, для которого у клиента нет ветки отображения.
    """
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_place(session, category="bar")

    assert "ck_places_category" in str(excinfo.value)


async def test_every_enum_category_accepted_by_database(session: AsyncSession) -> None:
    """AC-02, D-2: ВСЕ значения `PlaceCategory` проходят `CHECK`.

    Краснеет, если `CHECK` собирается из захардкоженного списка, отставшего
    от enum: категория, добавленная в `PlaceCategory` (например `theatre`),
    отвергалась бы PostgreSQL при вставке — то есть `500` на валидном вводе.
    """
    for index, category in enumerate(PlaceCategory):
        await _insert_place(session, name=f"Место {index}", category=str(category))

    result = await session.execute(text("SELECT count(*) FROM places"))

    assert result.scalar_one() == len(PlaceCategory)


# --- AC-01: пустота -------------------------------------------------------------


@pytest.mark.parametrize("field", ["name", "address_text"])
@pytest.mark.parametrize(
    "value",
    [
        "   ",
        "\t",
        # CR 3.1, патч 8: краевые пробельные и NBSP. До патча CHECK проверял
        # только пустоту (`<> ''`), поэтому `' Прага'` и `'Прага'`
        # сосуществовали: `uq_places_city_name` сравнивает байты, а сервис
        # ищет дубль по нормализованному значению.
        " Прага",
        "Прага ",
        "\u00a0Прага",
        "Прага\u00a0",
        "\u202fПрага",
        "\u00a0",
    ],
)
async def test_blank_text_rejected_by_database(
    session: AsyncSession, field: str, value: str
) -> None:
    """AC-01: пустое и «пробельное» значение отбивается CHECK-ом.

    Параметр `\t` принципиален: `btrim(x)` без второго аргумента режет только
    U+0020, и название из таба прошло бы ограничение, навсегда выпав из
    поиска. Краснеет, если в миграции `btrim` без набора `E' \\t\\r\\n\\f\\v'`.
    """
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_place(session, **{field: value})

    assert f"ck_places_{field}" in str(excinfo.value)


@pytest.mark.parametrize("value", ["Tomsk", "to", "tomsk!", "томск"])
async def test_bad_city_slug_rejected_by_database(session: AsyncSession, value: str) -> None:
    """AC-03: слаг сообщества подчиняется `^[a-z0-9_-]{3,64}$` в самой БД.

    Краснеет при снятии `ck_places_city_slug`: место легло бы
    в сообщество, которого нет в конфиге, и не находилось бы ни одним запросом
    ленты — «невидимое сообщество» без ошибки в логах.
    """
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_place(session, city_slug=value)

    assert "ck_places_city_slug" in str(excinfo.value)


# --- AC-05: активность по умолчанию ---------------------------------------------


async def test_is_active_defaults_to_true(session: AsyncSession) -> None:
    """AC-05: место создаётся активным без явного значения.

    Краснеет, если убрать `server_default true`: вставка без колонки падала бы
    `NOT NULL`, а вставка с явным `true` показала бы, что дефолт в БД
    отсутствует. Следствие: админка 3.2 обязана была бы присылать `is_active`
    при каждом создании, и забытое поле давало бы `500`.
    """
    await _insert_place(session)

    result = await session.execute(text("SELECT is_active FROM places"))

    assert result.scalar_one() is True


# --- AC-04: запрет дубля --------------------------------------------------------


async def test_duplicate_name_in_same_city_rejected(session: AsyncSession) -> None:
    """AC-04, D-4: дубль отбивается `uq_places_city_name`.

    Краснеет, если уникальность держится проверкой в сервисе, а ограничения
    нет: две параллельные вставки проходят обе (§18.3), и в выдаче появляются
    два «Кафе „Прага“» с разными `uuid`.
    """
    await _insert_place(session)

    with pytest.raises(IntegrityError) as excinfo:
        await _insert_place(session)

    assert "uq_places_city_name" in str(excinfo.value)


async def test_same_name_in_other_city_allowed(session: AsyncSession) -> None:
    """AC-04: одинаковое название в РАЗНЫХ сообществах разрешено.

    Краснеет, если уникальность объявить только по `name`: при расширении
    пилота второе сообщество не смогло бы завести «Кафе „Прага“», хотя это
    разные заведения в разных городах.
    """
    await _insert_place(session, city_slug="tomsk")
    await _insert_place(session, city_slug="kemerovo")

    result = await session.execute(text("SELECT count(*) FROM places"))

    assert result.scalar_one() == 2


# --- AC-01: `id` наружу не выходит -----------------------------------------------


def test_read_schema_never_exposes_internal_id() -> None:
    """AC-01, D-2: `id` не появляется в сериализованном ответе.

    Краснеет, если `id` добавят в `PlaceRead` или в `model_dump` попадёт
    колонка модели: внутренний bigint стал бы публичным, и перебор `/1..N`
    обошёл бы любые будущие лимиты выдачи.
    """
    from app.models.enums import PlaceCategory as _Category

    read = PlaceRead(
        uuid=__import__("uuid").uuid4(),
        name="Кафе „Прага“",
        category=_Category.CAFE,
        address_text="пр. Ленина, 1",
        is_active=True,
    )

    assert "id" not in read.model_dump()
