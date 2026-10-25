"""Зависимость чтения справочника мест (Task 7.4; AC-03, R-06).

§18.2: разница между `user` и `moderator` — ровно `role` у объекта
пользователя, и это единственное отличие фикстур. Поэтому тест `403`
краснеет при снятии проверки роли, а не «при любом изменении функции».

Проверяется наблюдаемый результат: объект `PlacesReader` с правильным
`include_inactive` или `HTTPException` с телом `{"code": "forbidden"}`.
Проверка «функция вызвана» дала бы зелёный тест на заглушке внутри.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.deps import PlacesReader, get_places_reader
from app.models.enums import UserRole


def _user(role: UserRole) -> Any:
    """Минимальный объект пользователя: зависимости нужен только `role`.

    `SimpleNamespace`, а не модель: `get_current_user` уже проверен отдельно,
    и поднимать ради роли целую ORM-строку значило бы зависеть от БД
    в unit-тесте зависимости.
    """
    return SimpleNamespace(role=role, uuid=None, status="active")


async def test_default_reader_does_not_check_role() -> None:
    """AC-01: обычный участник читает активные места без проверки роли.

    §18.1: краснеет, если вызвать `require_moderator` безусловно — тогда
    `GET /places` перестал бы работать для всех, кроме модератора, то есть
    выпадающий список выбора места (FR98) не открылся бы ни у кого.
    """
    user = _user(UserRole.USER)

    reader = await get_places_reader(user=user, include_inactive=False)

    assert isinstance(reader, PlacesReader)
    assert reader.user is user
    assert reader.include_inactive is False


async def test_user_with_include_inactive_is_forbidden() -> None:
    """AC-03: `include_inactive=true` под `user` — `403 forbidden`.

    §18.1: краснеет, если убрать вызов `require_moderator`. Тогда обычный
    участник увидел бы скрытые места, а `total` разошёлся бы с публичной
    выдачей — админский счётчик стал бы публичным.

    Тело проверяется целиком: `details` быть не должно (D-2, ADR-13 — отказ
    не превращается в карту прав).
    """
    user = _user(UserRole.USER)

    with pytest.raises(HTTPException) as exc_info:
        await get_places_reader(user=user, include_inactive=True)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "forbidden"}


async def test_moderator_with_include_inactive_is_allowed() -> None:
    """AC-03: модератор со флагом получает `include_inactive=True`.

    §18.1: краснеет, если зависимость ВСЕГДА возвращает `False` (флаг теряется):
    админ не увидел бы скрытое место и не смог бы его восстановить.
    """
    user = _user(UserRole.MODERATOR)

    reader = await get_places_reader(user=user, include_inactive=True)

    assert reader.include_inactive is True
    assert reader.user is user


async def test_moderator_without_flag_sees_public_view() -> None:
    """AC-03: `include_inactive=false` под модератором = выдача участника.

    §18.1: краснеет, если модератору подставлять `True` по роли: админская
    выдача перестала бы совпадать с публичной, и «что видит участник»
    нельзя было бы проверить глазами.
    """
    moderator = _user(UserRole.MODERATOR)
    participant = _user(UserRole.USER)

    moderator_reader = await get_places_reader(user=moderator, include_inactive=False)
    user_reader = await get_places_reader(user=participant, include_inactive=False)

    assert moderator_reader.include_inactive is False
    assert user_reader.include_inactive is False
