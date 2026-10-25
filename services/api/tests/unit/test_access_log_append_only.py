"""Append-only по исходникам: нет `UPDATE`/`DELETE` по `access_logs` (AC-07, D-84).

**Почему unit-на-текст.** Триггера нет осознанно (D-84): он молча блокировал бы
легитимную очистку по ретенции в эпике 10. Единственный автор запросов в MVP —
этот сервис, поэтому проверяется, что в его SQL-константах нет операторов,
изменяющих историю.

**Что тест НЕ доказывает (честно):** он не мешает прямому `psql`-запросу
дежурного. Защита от дежурного — права БД (`REVOKE UPDATE, DELETE`), и это
предмет деплоя, а не миграции. Утверждать обратное значило бы обещать больше,
чем даёт код.
"""

from __future__ import annotations

import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
REPO = API_ROOT / "app" / "repositories" / "access_logs.py"

#: Операторы, которых быть не должно. Регистр не важен: SQL пишется капсом,
#: но правка «для читаемости» не должна обходить тест.
FORBIDDEN = ("UPDATE ACCESS_LOGS", "DELETE FROM ACCESS_LOGS")


def test_repository_has_no_mutating_statements() -> None:
    """AC-07: в репозитории аудита нет `UPDATE`/`DELETE` по таблице."""
    source = REPO.read_text(encoding="utf-8").upper()
    found = [operator for operator in FORBIDDEN if operator in source]
    assert found == [], f"найдены изменяющие операторы: {found}"


def test_repository_has_both_inserts() -> None:
    """Контроль: файл действительно содержит `INSERT` (иначе тест выше пуст).

    Без этой проверки удаление всего файла оставило бы AC-07 «зелёным»:
    запрещённых операторов нет в пустоте.
    """
    source = REPO.read_text(encoding="utf-8").upper()
    assert source.count("INSERT INTO ACCESS_LOGS") == 2, source.count("INSERT INTO ACCESS_LOGS")


def test_no_delete_anywhere_in_app() -> None:
    """AC-07: ни один модуль `app/` не удаляет строки аудита.

    Репозиторий — не единственное место, где мог бы появиться `DELETE`:
    его могли бы добавить в сервис или в скрипт очистки «по-быстрому».
    Поиск по всему `app/`, а не по одному файлу.
    """
    offenders = []
    for path in (API_ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8").upper()
        if "DELETE FROM ACCESS_LOGS" in source or re.search(r"UPDATE\s+ACCESS_LOGS", source):
            offenders.append(str(path.relative_to(API_ROOT)))
    assert offenders == [], offenders
