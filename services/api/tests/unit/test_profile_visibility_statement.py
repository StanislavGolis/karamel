"""Предикат видимости анкеты по городу пилота (Task 2.5, 6.1; AC-01, AC-03, AC-04, AC-05).

§18.3: план запроса в одной сессии не доказать как «правило», поэтому
проверяется текст условия — тот же приём, что `test_profile_lock_statement.py`.

§18.2: каждый тест обязан краснеть от конкретной правки в коде:
- `test_visibility_has_city_status_hidden` — удаление любого из трёх условий;
- `test_visibility_has_no_city_literal` — подстановка литерала вместо параметра;
- `test_visibility_ignores_gender` — добавление `gender` в условие (FR63);
- `test_visibility_city_value_is_parameter` — жёсткая константа города;
- `test_visibility_rule_defined_once` — второй текст условия в приложении.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.profiles import ProfileRepository


def _statement(city: str = "Томск") -> Any:
    """Скомпилированный `Select` предиката видимости без обращения к БД.

    Билдер статический, поэтому текст запроса доступен без сессии
    (образец — `_build_for_update_statement`).

    CR 2.6, патч 3: `.limit()` здесь больше не добавляется руками — производственный
    путь (`_build_visible_in_city_page_statement`) проверяется отдельно, иначе
    тест проверял бы собственную строку, а не код.
    """
    return ProfileRepository._build_visible_in_city_statement(city)


def _page_statement(city: str = "Томск", *, limit: int = 20) -> Any:
    return ProfileRepository._build_visible_in_city_page_statement(city, limit=limit)


def _sql(city: str = "Томск") -> str:
    return str(_statement(city).compile(dialect=postgresql.dialect()))


def test_visibility_has_city_status_hidden() -> None:
    """AC-01, AC-05: все три условия обязательны.

    Без `city` в выдачу попадут чужие города; без `status='complete'` —
    черновики (CHECK `completeness` разрешает `draft` с заполненным городом,
    но пустыми `name`/`age`/`bio`); без `is_hidden` — анкеты, скрытые
    владельцем и мужские анкеты до одобрения заявки (2.1).

    CR 2.6, патч 4 (§18.2): проверяется значение статуса, а не только имя
    колонки. Раньше `Profile.status == "complete"` → `== "draft"` оставляло
    тест зелёным: подстрока `profiles.status = ` не менялась.
    """
    sql = _sql()

    assert "profiles.city = " in sql
    assert "profiles.status = " in sql
    assert "profiles.is_hidden IS false" in sql

    params = _statement().compile(dialect=postgresql.dialect()).params
    assert params["status_1"] == "complete", "черновики не должны попадать в выдачу"


def test_visibility_has_no_extra_conditions() -> None:
    """AC-01, AC-05 (§18.2): в условии ровно три предиката — ни больше, ни меньше.

    CR 2.6, патч 4: раньше добавление `Profile.age >= 21` или
    `Profile.bio.isnot(None)` оставляло весь набор зелёным — все фикстуры
    однородны (`age=30`, `bio="текст"`). Здесь фиксируется точное число
    сравнений, поэтому лишнее условие обязано упасть.
    """
    sql = _sql()
    where = sql.split("WHERE", 1)[1]

    assert where.count(" AND ") == 2, f"ожидались ровно 3 условия, получено: {where!r}"


def _where_clause(city: str = "Томск") -> str:
    """Только `WHERE`-часть запроса.

    Без этого срезa проверка «нет `gender`» ложно-красная: `gender` есть
    в списке выбираемых колонок (анкета читается целиком) и его отсутствие
    в условии — то, что требует FR63.
    """
    sql = _sql(city)
    return sql.split("WHERE", 1)[1]


def test_visibility_has_no_city_literal() -> None:
    """AC-03: литерала города в SQL нет — значение приходит параметром.

    Иначе смена `PILOT_CITY` требовала бы правки кода (FR16).
    """
    sql = _sql("Томск")

    assert "Томск" not in sql
    assert "%(city_1)s" in sql


def test_visibility_city_value_is_parameter() -> None:
    """AC-03: разные города дают одинаковый текст условий и разные параметры."""
    first = _statement("Томск")
    second = _statement("Москва")

    assert str(first.compile(dialect=postgresql.dialect())) == str(
        second.compile(dialect=postgresql.dialect())
    )
    assert first.compile().params["city_1"] == "Томск"
    assert second.compile().params["city_1"] == "Москва"


def test_visibility_ignores_gender() -> None:
    """AC-05, FR63: пол не влияет на попадание в выдачу.

    Видимость зависит от статуса фото (`PhotoPlaceholder`), а не от `gender`.
    """
    where = _where_clause()

    assert "gender" not in where


def _city_comparisons(path: Path) -> list[tuple[str, int]]:
    """Пары `(имя_функции, строка)` для каждого сравнения по атрибуту `city`.

    Имя функции нужно, чтобы разрешить сравнение ровно в билдере предиката
    и не привязываться к номеру строки (он поедет от любого рефакторинга).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Compare):
                operands = [child.left, *child.comparators]
                if any(
                    isinstance(item, ast.Attribute) and item.attr == "city" for item in operands
                ):
                    found.append((owner, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_page_statement_orders_by_uuid_and_limits() -> None:
    """Task 2.4, AC-01 (§18.2): производственный путь содержит `ORDER BY` и `LIMIT`.

    CR 2.6, патч 3: удаление `.order_by(Profile.uuid)` или `.limit(limit)`
    из `list_visible_in_city` больше не остаётся зелёным. Раньше unit-тест
    собирал свой запрос и видел только билдер.
    """
    sql = str(_page_statement("Томск", limit=20).compile(dialect=postgresql.dialect()))

    assert "ORDER BY profiles.uuid" in sql
    assert "LIMIT" in sql


def test_page_statement_uses_given_limit() -> None:
    """AC-01: `limit` доезжает до SQL значением, а не литералом."""
    first = _page_statement("Томск", limit=5).compile(dialect=postgresql.dialect())
    second = _page_statement("Томск", limit=50).compile(dialect=postgresql.dialect())

    assert first.params["param_1"] == 5
    assert second.params["param_1"] == 50


async def test_list_visible_in_city_rejects_non_positive_limit() -> None:
    """AC-01, §18.2: `limit` валидируется до обращения к БД.

    CR 2.6, патч 2: `LIMIT -1` в PostgreSQL — ошибка выполнения (`500` на
    пользовательском вводе), `LIMIT 0` — молча пустая выдача. Проверка стоит
    до `session.execute`, поэтому сессию можно не создавать.
    """
    repository = ProfileRepository(session=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="limit"):
        await repository.list_visible_in_city("Томск", limit=0)
    with pytest.raises(ValueError, match="limit"):
        await repository.list_visible_in_city("Томск", limit=-1)


# CR 2.6, патч 5: единственная функция, которой разрешено сравнивать `city`.
_ALLOWED_CITY_COMPARISON_FUNCTION = "_build_visible_in_city_statement"


def test_visibility_rule_defined_once() -> None:
    """AC-04: правило города живёт в единственном месте приложения.

    Читаются исходники, не импорт: второй потребитель (воркер, модерация)
    не должен получить повод написать `WHERE city = ...` заново.

    CR 2.6, патч 5: `repositories/profiles.py` больше не пропускается целиком
    (прошлая версия не видела второе условие внутри него же). Разрешённые
    места — ровно `_build_visible_in_city_statement`: там сравнение обязано быть.
    """
    app_root = Path(__file__).resolve().parents[2] / "app"

    offenders: list[str] = []
    scanned = 0
    allowed = 0
    for path in app_root.rglob("*.py"):
        scanned += 1
        for function_name, lineno in _city_comparisons(path):
            if function_name == _ALLOWED_CITY_COMPARISON_FUNCTION:
                allowed += 1
                continue
            offenders.append(f"{path.relative_to(app_root)}:{lineno} в {function_name}()")

    assert scanned > 20, f"скан не состоялся: найдено {scanned} файлов"
    assert allowed >= 1, "билдер предиката не найден — скан не доказал ничего"
    assert offenders == []
