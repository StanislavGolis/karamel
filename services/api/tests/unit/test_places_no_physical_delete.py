"""AST-гард: физического удаления мест нет ни на одном пути (QD B5, AC-04).

R-04 стори 3.2: `DELETE /api/v1/places/{uuid}` — ВСЕГДА мягкое удаление
(`is_active=false`). Причина не в стиле: FR68 говорит «скрыть», а не «удалить»,
и карточки эпика 4 ссылаются на `places.id` — физическое удаление оставило бы
висячий FK либо потребовало `SET NULL`, обнулив место у опубликованных карточек.

До этого теста запрет держался только unit-тестом с ФЕЙКОВОЙ сессией
(`session.deleted == []`). Фейк доказывает лишь то, что сервис не зовёт метод
у заглушки; он не увидит `session.execute(delete(Place)...)`, `session.delete`
в новом хелпере или в репозитории. Читаются исходники, а не импорт — как в
`test_visibility_rule_defined_once`.

§18.1: краснеет, если добавить `session.delete(place)` в любое из двух мест.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Файлы, через которые идёт ЛЮБАЯ запись/чтение справочника мест.
# Роутер (`app/api/v1/places.py`) в список не входит по замыслу: он работает
# с сессией только через сервис и коммит, а не через удаление.
WATCHED = (
    "app/services/place_service.py",
    "app/repositories/places.py",
)


def _deletion_calls(path: Path) -> list[tuple[int, str]]:
    """Все вызовы удаления в файле: `(строка, как выглядит вызов)`.

    Ловится три формы:
    - `session.delete(obj)` — ORM-удаление объекта;
    - `delete(Place)` / `delete(places_table)` — Core-удаление;
    - `session.execute(delete(...))` — то же через `execute`.

    Имена методов сравниваются по последнему атрибуту (`x.delete`), поэтому
    подмена переменной сессии (`self._session.delete`) тоже видна.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name == "delete":
            found.append((node.lineno, ast.unparse(node)))
    return found


@pytest.mark.parametrize("relative_path", WATCHED)
def test_no_physical_delete_in_places_code(relative_path: str) -> None:
    """AC-04, QD B5: ни одного вызова удаления в коде справочника.

    Краснеет от `session.delete(place)` в `deactivate_place` — то есть от
    возврата к физическому удалению, которое сломало бы карточки эпика 4.
    """
    project_root = Path(__file__).resolve().parents[2]
    path = project_root / relative_path
    assert path.exists(), f"файл под наблюдением исчез: {path}"

    offenders = _deletion_calls(path)
    assert offenders == [], f"в {relative_path} появилось физическое удаление: " + "; ".join(
        f"строка {line}: {code}" for line, code in offenders
    )


def test_guard_actually_scans_something() -> None:
    """§18.2: гард обязан доказать, что скан состоялся, а не «ноль файлов — ок».

    Без этой проверки переименование файла (или пустой список `WATCHED`)
    оставляло бы гард зелёным и бессмысленным.
    """
    project_root = Path(__file__).resolve().parents[2]
    scanned = 0
    total_lines = 0
    for relative_path in WATCHED:
        path = project_root / relative_path
        assert path.exists(), f"файл под наблюдением исчез: {path}"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"{relative_path} пуст"
        total_lines += len(text.splitlines())
        scanned += 1

    assert scanned == len(WATCHED), "часть файлов не просканирована"
    assert total_lines > 200, f"скан выглядит подозрительно малым: {total_lines} строк"
