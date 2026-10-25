"""Сервисный вход видимости по городу (Task 3.4; AC-03, AC-04).

§18.2: тест обязан краснеть от правки в коде. Здесь он краснеет, если сервис
начнёт нормализовать город, подставлять литерал, брать `get_settings()` внутри
или менять `limit` — то есть от любой попытки собрать правило в сервисе
вместо передачи аргумента в репозиторий.

Репозиторий подменяется фейком: тест проверяет РОВНО переданные значения,
а не результат запроса к БД (это делает интеграционный тест).
"""

from __future__ import annotations

import ast
from typing import Any

import pytest

from app.services import profile_visibility


class _FakeRepository:
    """Фейковый репозиторий: запоминает аргументы вызова."""

    calls: list[tuple[Any, str, int]] = []

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_visible_in_city(self, city: str, *, limit: int) -> list[Any]:
        _FakeRepository.calls.append((self._session, city, limit))
        return ["profile"]


@pytest.fixture(autouse=True)
def _patch_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRepository.calls = []
    monkeypatch.setattr(profile_visibility, "ProfileRepository", _FakeRepository)


async def test_service_passes_pilot_city_verbatim() -> None:
    """AC-03: сервис передаёт ровно полученный город — без нормализации.

    Нормализация в сервисе сделала бы аргумент бесполезным: тест AC-03
    («смена `PILOT_CITY` меняет выдачу») requiring правки кода, а не значения.
    """
    session = object()

    result = await profile_visibility.list_visible_profiles(
        session, pilot_city="  Томск  ", limit=7
    )

    assert _FakeRepository.calls == [(session, "  Томск  ", 7)]
    assert result == ["profile"]


def test_service_does_not_read_settings() -> None:
    """AC-03, AC-04: сервис не читает настройки и не собирает условие.

    Проверяется AST, а не текст файла: упоминание `get_settings` в докстринге
    (объяснение «почему нельзя») не должно красить тест — иначе он проверяет
    документацию, а не поведение (§18.2).

    Краснеет, если в сервисе появится реальный вызов `get_settings()` или
    сравнение `Profile.city == ...`.
    """
    source = profile_visibility.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    used_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "get_settings" not in used_names
    assert "city" not in {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
