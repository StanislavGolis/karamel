"""QD A2: разбор имени ограничения из `IntegrityError` под asyncpg.

`deferred-work.md` (CR 3.2): `photo_service._is_position_race` читал
`error.orig.constraint_name`, а asyncpg кладёт имя на `error.orig.orig`.
Поэтому проверка возвращала `False` ВСЕГДА, и гонка за позицию давала `500`
вместо `409 photo_limit_reached`.

Форма ошибки здесь повторяет ЖИВОЙ драйвер (asyncpg 0.30 / PostgreSQL 18),
а не придумана: у обёртки SQLAlchemy атрибута `constraint_name` нет
намеренно, иначе тест снова «соглашался» бы с дефектом (§18.2).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.repositories.errors import constraint_name_of
from app.services.photo_service import _is_position_race


def _asyncpg_error(constraint: str | None) -> IntegrityError:
    """`IntegrityError` в форме живого asyncpg: имя уходит на `orig.orig`."""

    class _InnerDriverError(Exception):
        def __init__(self, name: str | None) -> None:
            super().__init__("driver error")
            if name is not None:
                self.constraint_name = name
            self.sqlstate = "23505"
            self.pgcode = "23505"

    class _SQLAlchemyWrapper(Exception):
        """У обёртки `constraint_name` НЕТ — так ведёт себя реальный asyncpg."""

        def __init__(self, inner: BaseException) -> None:
            super().__init__("wrapped")
            self.orig = inner
            self.sqlstate = "23505"
            self.pgcode = "23505"
            self.detail = "Key (profile_id, position) already exists."

    wrapper = _SQLAlchemyWrapper(_InnerDriverError(constraint))
    return IntegrityError("INSERT INTO profile_photos ...", {}, wrapper)


@pytest.mark.parametrize(
    "constraint",
    ["uq_profile_photos_profile_id_position", "uq_profile_photos_storage_key"],
)
def test_position_race_detected_on_real_asyncpg_shape(constraint: str) -> None:
    """QD A2: гонка за позицию распознаётся на форме ЖИВОГО драйвера.

    §18.1: КРАСНЕЕТ от отката патча A2. До него копия в `photo_service`
    читала `error.orig.constraint_name` — атрибута там нет, поэтому функция
    возвращала `False`, и вместо `409 photo_limit_reached` пользователь
    получал `500` на обычной конкурентной загрузке четвёртого фото.
    """
    assert _is_position_race(_asyncpg_error(constraint)) is True


def test_foreign_constraint_is_not_position_race() -> None:
    """QD A2, §18.2: чужое ограничение не маскируется под гонку.

    Краснеет, если `_is_position_race` начнёт возвращать `True` на любую
    ошибку целостности: нарушение CHECK отдавалось бы как «лимит фото»,
    и клиент шёл бы перезагружать файл вместо исправления данных.
    """
    assert _is_position_race(_asyncpg_error("ck_profile_photos_status")) is False


def test_unreadable_constraint_gives_none_not_crash() -> None:
    """QD A2: имя не прочитано — `None`, а не исключение и не ложный `True`.

    Так ведёт себя ошибка без `constraint_name` (другой драйвер, иной тип
    нарушения): вызывающий обязан получить «не тот случай», а не падение
    внутри обработчика ошибки.
    """
    assert constraint_name_of(_asyncpg_error(None)) is None
    assert _is_position_race(_asyncpg_error(None)) is False


def test_constraint_name_read_from_nested_orig() -> None:
    """QD A2: имя читается с `orig.orig`, а не с `orig`.

    §18.1: краснеет от отката общего хелпера на старую форму
    `getattr(error.orig, "constraint_name")` — она возвращает `None` всегда.
    """
    error: Any = _asyncpg_error("uq_places_community_name")

    assert constraint_name_of(error) == "uq_places_community_name"
    # Доказательство, что старая форма действительно не работает:
    assert getattr(error.orig, "constraint_name", None) is None
