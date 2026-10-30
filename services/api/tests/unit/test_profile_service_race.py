"""Гонка и блокировка при создании анкеты (CR 2.3, патчи 1 и 15; §18.3).

§18.3: гонку двух `POST` в одной сессии доказать нельзя — второй запрос
в том же соединении не начнётся. Поэтому проверяется поведение сервиса
на тех входах, которые гонка создаёт: `IntegrityError` от `INSERT`
и вызов блокирующего чтения. Это не тавтология (§18.2): тест краснеет
ровно тогда, когда защита удалена.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.profile import Profile
from app.repositories.profiles import ProfileRepository
from app.schemas.profile import ProfileCreate
from app.services.profile_service import create_profile

PILOT_CITY = "Томск"
# Стори 12.2, AC-2: вместо числа лет — дата рождения.
PAYLOAD = ProfileCreate.model_construct(
    name="Аня", city="Томск", birth_date=date(1994, 3, 14), bio="люблю горы"
)


class _FakeSession:
    """Минимальная сессия: сервису нужны `flush`, `refresh` и `rollback`.

    Реального соединения здесь нет намеренно: тест про порядок вызовов,
    а не про PostgreSQL. Поведение БД на этих путях покрыто integration-тестами.
    """

    def __init__(self) -> None:
        self.rollbacks = 0
        self.refreshed: list[Any] = []

    async def flush(self) -> None:
        return None

    async def refresh(self, instance: Any) -> None:
        self.refreshed.append(instance)

    async def rollback(self) -> None:
        self.rollbacks += 1


def _user(gender: str = "female") -> Any:
    """Заменяет модель `User`: сервису нужны только `id` и `gender`."""
    return SimpleNamespace(id=1, gender=gender)


def _draft() -> Profile:
    """Строка-черновик, как её создаёт загрузка фото в 2.2."""
    profile = Profile(user_id=1, gender="female", status="draft")
    profile.uuid = uuid_lib.uuid4()
    return profile


async def test_integrity_error_reraises_when_row_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 2.3, патч 1: чужая ошибка целостности не глотается.

    Ветка `except IntegrityError` лечит гонку. Если после отката строки нет,
    ошибка означает нарушение другого ограничения (например, CHECK полноты),
    и её нельзя превращать в «черновик заполнен»: пользователь получил бы
    `200`, а анкета не сохранилась.
    """
    session = _FakeSession()

    async def _no_row(_self: Any, _user_id: int) -> None:
        return None

    async def _broken_create(_self: Any, **_kwargs: Any) -> None:
        raise IntegrityError("INSERT", {}, Exception("check violated"))

    monkeypatch.setattr(ProfileRepository, "get_by_user_id_for_update", _no_row)
    monkeypatch.setattr(ProfileRepository, "create", _broken_create)

    with pytest.raises(IntegrityError):
        await create_profile(session, _user(), PAYLOAD, PILOT_CITY)  # type: ignore[arg-type]

    assert session.rollbacks == 1


async def test_integrity_error_race_fills_the_winning_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 2.3, патч 1: проигранная гонка даёт `200` с заполненной строкой.

    Сценарий: два `POST` одновременно, второй упирается в `uq_profiles_user_id`.
    Прежний код отдавал необработанный `500`. Правильное поведение — откат,
    повторное чтение и решение по фактическому состоянию: `complete` → `409`,
    `draft` → заполнить и вернуть `200`.

    Тест краснеет, если убрать `try/except IntegrityError`: исключение
    выйдет наружу, и роутер отдаст `500`.
    """
    session = _FakeSession()
    raced = _draft()
    calls: list[int] = []

    async def _read(_self: Any, _user_id: int) -> Profile | None:
        calls.append(1)
        # Первое чтение — «строки нет», второе (после отката) — строка победителя.
        return None if len(calls) == 1 else raced

    async def _broken_create(_self: Any, **_kwargs: Any) -> None:
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(ProfileRepository, "get_by_user_id_for_update", _read)
    monkeypatch.setattr(ProfileRepository, "create", _broken_create)

    profile, created = await create_profile(session, _user(), PAYLOAD, PILOT_CITY)  # type: ignore[arg-type]

    assert created is False
    assert profile is raced
    assert profile.status == "complete"
    assert session.rollbacks == 1
    assert len(calls) == 2


async def test_create_reads_existing_row_with_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 2.3, патч 15: `POST` читает черновик под `FOR UPDATE`.

    Прежний `POST` читал через `get_by_user_id` без блокировки: параллельный
    `PATCH` фиксировал правку, `POST` перезаписывал её своим снимком —
    потерянное обновление. Тест краснеет, если вернуть неблокирующее чтение:
    подменённый `get_by_user_id` бросает, и вызов сервиса падает.
    """
    session = _FakeSession()
    draft = _draft()

    async def _for_update(_self: Any, _user_id: int) -> Profile:
        return draft

    async def _unlocked(_self: Any, _user_id: int) -> Profile:
        raise AssertionError("POST прочитал черновик без FOR UPDATE")

    monkeypatch.setattr(ProfileRepository, "get_by_user_id_for_update", _for_update)
    monkeypatch.setattr(ProfileRepository, "get_by_user_id", _unlocked)

    profile, created = await create_profile(session, _user(), PAYLOAD, PILOT_CITY)  # type: ignore[arg-type]

    assert created is False
    assert profile is draft
    assert profile.city == "Томск"
