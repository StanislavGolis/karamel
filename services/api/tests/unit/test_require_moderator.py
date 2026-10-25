"""Гард администратора справочника (Task 1, Task 8.1; AC-01).

§18.1: каждый тест краснеет от снятия конкретной защиты. Проверяется не
«функция вернула объект», а «доступ к записи справочника не получил тот,
кто не является модератором».

R-01: администратор справочника — `UserRole.MODERATOR`. Отдельной роли нет,
поэтому разница между `user` и `moderator` — ровно значение `role` в БД.

§18.2: вызов идёт напрямую, без БД и без HTTP — проверяется решение
зависимости. Конверт ошибки проверяется отдельно на реальном обработчике
`main.py`: `{"code": "forbidden"}` ровно с одним ключом и `no-store`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.api import deps
from app.main import http_error_handler
from app.models.enums import UserRole


def _user(role: str) -> Any:
    """Минимальный пользователь: гарду нужна ТОЛЬКО роль.

    Настоящий `User` не создаётся: остальные поля к решению отношения не имеют,
    а их заполнение маскировало бы, что гард смотрит именно на `role`.
    """
    from types import SimpleNamespace

    return SimpleNamespace(role=role, status="active", deletion_requested_at=None)


class _Request:
    """Пустой запрос-заглушка для вызова обработчика ошибок."""


async def test_moderator_granted() -> None:
    """AC-01: модератор проходит — гард не блокирует легальную работу.

    Краснеет, если сравнивать роль с `UserRole.USER` или забыть вернуть
    пользователя: админ не смог бы вести справочник вовсе.
    """
    moderator = _user(UserRole.MODERATOR)

    assert await deps.require_moderator(moderator) is moderator


async def test_regular_user_forbidden_envelope() -> None:
    """AC-01: `user` получает `403` с конвертом `{"code": "forbidden"}`.

    §18.2: проверяется И статус, И форма тела. Один только статус прошёл бы
    и при `detail={"code": "user_blocked"}` — клиент показал бы не ту ошибку.
    Краснеет, если убрать проверку роли или подменить код.
    """
    with pytest.raises(HTTPException) as excinfo:
        await deps.require_moderator(_user(UserRole.USER))

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == {"code": "forbidden"}


async def test_forbidden_carries_no_details() -> None:
    """AC-01, D-2: в отказе нет `details` и подсказок.

    Краснеет, если добавить в конверт `details`/`allowed`/`message`: тело
    административного отказа не должно объяснять, чего не хватает, — иначе
    оно превращается в карту прав.
    """
    with pytest.raises(HTTPException) as excinfo:
        await deps.require_moderator(_user(UserRole.USER))

    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert set(detail) == {"code"}


@pytest.mark.parametrize("role", [UserRole.MODERATOR, "moderator"])
async def test_role_compared_with_enum_member(role: Any) -> None:
    """AC-01, Task 1.2: сравнение с `UserRole.MODERATOR`, а не со строкой по месту.

    Из БД роль приходит строкой, из фикстур — членом `StrEnum`: `StrEnum`
    сравнивается со своим значением, поэтому обе формы обязаны проходить.
    Краснеет, если появится вторая копия строки с опечаткой или сравнение
    через `is` (тогда строка из БД не прошла бы и модератор получал `403`).
    """
    moderator = _user(role)

    assert await deps.require_moderator(moderator) is moderator


async def test_unknown_role_forbidden() -> None:
    """AC-01: незнакомая роль не даёт доступа по умолчанию.

    Краснеет, если писать `if user.role != UserRole.USER: return user`:
    любое новое или испорченное значение роли открывало бы запись
    в справочник (безопасная сторона отказа — только явное совпадение).
    """
    with pytest.raises(HTTPException):
        await deps.require_moderator(_user("role-that-does-not-exist"))


async def test_forbidden_envelope_through_main_handler() -> None:
    """AC-01, OQ-1: зависимость бросает `HTTPException`, конверт даёт `main.py`.

    §18.2: проверяется РЕАЛЬНЫЙ обработчик, а не повторение литерала в тесте.
    Краснеет, если `_STATUS_CODES[403]` изменить или обработчик перестанет
    пропускать `detail`-словарь: клиент получил бы другую форму тела.
    """
    response = await http_error_handler(
        _Request(),  # type: ignore[arg-type]
        HTTPException(status_code=403, detail={"code": "forbidden"}),
    )

    assert response.status_code == 403
    assert response.body == b'{"code":"forbidden"}'
    assert response.headers["cache-control"] == "no-store"
