"""Репозиторий мест (Task 6.5; AC-04).

§18.2: проверяется текст запроса, а не факт вызова. `exists_in_city`
существует ровно ради понятной ошибки на дешёвом пути (D-4), и его
единственная ценность — оба условия в `WHERE`. Мутация «убрать
`city_slug`» превращает проверку в «место с таким именем есть
в любом сообществе» и делает её красной.

`§18.3`: гонку двух вставок в одной сессии доказать нельзя, поэтому
корректность держит `uq_places_city_name` (проверяется в
интеграционных тестах), а здесь проверяется только запрос.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

from app.repositories.places import PlaceRepository


def test_exists_in_city_filters_by_both_columns() -> None:
    """AC-04: запрос проверки дубля содержит оба условия.

    Краснеет, если убрать `city_slug`: места с одинаковым названием
    в разных сообществах (AC-04 явно разрешает) считались бы дублями,
    и админка 3.2 отказывала бы на легальном вводе.
    """
    statement = PlaceRepository.build_exists_statement("tomsk", "Кафе „Прага“")
    sql = str(statement)

    assert "places.city_slug" in sql
    assert "places.name" in sql
    assert " AND " in sql


def test_exists_statement_compiles_with_bound_values() -> None:
    """AC-04: значения передаются параметрами, а не склейкой в текст.

    Краснеет, если собрать запрос через f-string: название места с кавычкой
    (`Кафе „Прага“`, `O'Brien's`) сломало бы SQL или дало ложное совпадение.
    """
    statement = PlaceRepository.build_exists_statement("tomsk", "O'Brien's")
    compiled = statement.compile()

    assert "O'Brien's" not in str(compiled)
    assert "O'Brien's" in compiled.params.values()


def test_get_by_uuid_statement_filters_by_uuid() -> None:
    """AC-01, D-2: адресация снаружи — только по `uuid`.

    Краснеет, если заменить условие на `id`: снаружи пришёл бы внутренний
    bigint, а `uuid` перестал бы быть единственным публичным адресом.
    """
    statement = PlaceRepository.build_get_by_uuid_statement(uuid_lib.uuid4())
    sql = str(statement)
    # Проверяется условие адресации (`WHERE`), а не список колонок: `SELECT`
    # тянет все колонки строки, включая `id`, — это нормально и наружу не уходит.
    where = sql.split("WHERE", 1)[1]

    assert "places.uuid" in where
    assert "places.id" not in where


def test_exists_excluding_statement_excludes_own_uuid() -> None:
    """Task 3.3 (AC-05): поиск дубля при правке исключает САМО место.

    §18.1: краснеет, если убрать `Place.uuid != exclude_uuid`. Правка одного
    поля (например, категории) ищет место по его же имени, находит себя и
    отказывает `409 PLACE_DUPLICATE` на легальном вводе — админ не смог бы
    отредактировать ни одно существующее место.
    """
    own_uuid = uuid_lib.uuid4()
    statement = PlaceRepository.build_exists_excluding_statement("tomsk", "Кафе „Прага“", own_uuid)
    compiled = statement.compile()
    sql = str(compiled)
    where = sql.split("WHERE", 1)[1]

    assert "places.city_slug" in where
    assert "places.name" in where
    assert "places.uuid" in where
    assert "!=" in where
    # Значения — параметрами: название с кавычкой не должно попадать в текст.
    assert "Кафе „Прага“" not in sql
    assert own_uuid in compiled.params.values()


# --- QD B1: детерминированный выбор дубля ----------------------------------------


class _CapturingSession:
    """Сессия-перехватчик: запоминает statement, который реально ушёл в execute."""

    def __init__(self, row: Any = None) -> None:
        self.statements: list[Any] = []
        self._row = row

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return _RowResult(self._row)


class _RowResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row

    def first(self) -> Any:
        return self._row


async def test_duplicate_lookup_sends_order_by_to_database() -> None:
    """QD B1 (CR 3.2): РЕАЛЬНЫЙ метод поиска дубля шлёт `ORDER BY` в БД.

    §18.1: КРАСНЕЕТ от отката `ORDER BY` в `find_by_city_and_name` —
    на двух совпадениях `limit(1)` возвращает произвольную строку, и
    `details.existing_uuid` в `PLACE_ARCHIVED` указывает на случайное место:
    админ «восстанавливает» не то, а имя остаётся занятым.

    §18.2: проверяется statement, который метод ДЕЙСТВИТЕЛЬНО отправил
    (через сессию-перехватчик), а не строка, собранная в самом тесте. Прошлая
    версия добавляла `.order_by(...)` руками и оставалась зелёной без
    сортировки в коде — тавтология.
    """
    session = _CapturingSession(row=None)
    repository = PlaceRepository(session)  # type: ignore[arg-type]

    await repository.find_by_city_and_name("tomsk", "Кафе „Прага“")

    assert session.statements, "метод не обратился к БД — тест ничего не проверил"
    sql = str(session.statements[0])
    assert "ORDER BY places.created_at, places.id" in sql, (
        f"порядок не детерминирован в реальном запросе: {sql}"
    )


async def test_excluding_lookup_sends_same_order_by_to_database() -> None:
    """QD B1: путь `PATCH` шлёт ТОТ ЖЕ порядок, что и путь `POST`.

    §18.1: краснеет, если сортировку добавить только в один метод — тогда
    `PLACE_ARCHIVED.existing_uuid` на создании и на правке указывал бы на
    РАЗНЫЕ места для одной и той же ситуации (админ видит два разных адреса).
    """
    session = _CapturingSession(row=None)
    repository = PlaceRepository(session)  # type: ignore[arg-type]

    await repository.exists_in_city_excluding("tomsk", "Кафе „Прага“", uuid_lib.uuid4())

    assert session.statements, "метод не обратился к БД — тест ничего не проверил"
    sql = str(session.statements[0])
    assert "ORDER BY places.created_at, places.id" in sql, (
        f"порядок не детерминирован в реальном запросе: {sql}"
    )
    assert "places.uuid !=" in sql, "исключение себя потеряно (ложный 409 на правке)"
