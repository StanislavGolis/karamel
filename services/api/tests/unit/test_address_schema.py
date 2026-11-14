"""Схема `AddressPlaceCreate`: enum и нормализация (стори 13.1; CR C12, C13).

**Почему после CR.** Оба свойства — вход второго маршрута, и оба выжили в
мутациях: `category_hint` как `str` (C12) и отключённая нормализация (C13)
проходили все тесты, потому что ни один не подавал `"ATM"` в категорию и
`'  Прага  '` в имя.

**Почему нормализация критична.** `create_place` нормализует имя сам, но
поиск дубля идёт по нормализованному значению: без нормализации на входе
`'  Прага  '` не совпало бы с сохранённым `'Прага'`, `uq_places_city_name`
отбил бы вставку как `IntegrityError` — то есть `500` на пользовательском
вводе вместо `created:false`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.enums import PlaceCategory
from app.schemas.address import AddressPlaceCreate


def test_category_hint_outside_enum_is_rejected_by_schema() -> None:
    """C12 CR 2026-10-31: `category_hint` вне enum → `422`, а не значение как есть.

    Краснеет, если заменить тип на `str`: `"atm"` прошёл бы в
    `create_place(category="atm")`, а `ck_places_category` вернул бы `500`
    на вводе. `other` — ВНУТРИ геокодера (подсказка), а на входе маршрута
    допустимы только значения enum.

    Имя намеренно отличается от `test_category_hint_outside_enum_becomes_other`
    (`L-2`/`W-13.1-m`): тот тест про геокодер, этот — про схему маршрута.
    """
    with pytest.raises(ValidationError):
        AddressPlaceCreate(name="Банк", address_text="ул. Ленина, 1", category_hint="atm")


def test_category_hint_defaults_to_other() -> None:
    """C12: пара к отказу — без подсказки категория `other`.

    AC-9: `category_hint` — ПОДСКАЗКА; её отсутствие не ошибка.
    """
    payload = AddressPlaceCreate(name="Банк", address_text="ул. Ленина, 1")

    assert payload.category_hint is PlaceCategory.OTHER


def test_name_is_normalized_on_input() -> None:
    """C13 CR 2026-10-31: имя нормализуется ДО проверки длины.

    Краснеет, если убрать `normalize_text`: `'  Прага  '` сохранилось бы с
    пробелами, поиск дубля не нашёл бы `'Прага'`, и `uq_places_city_name`
    вернул бы `IntegrityError` — `500` вместо `created:false`.
    """
    payload = AddressPlaceCreate(name="  Кинотеатр   Прага  ", address_text="  ул.  Ленина, 12  ")

    assert payload.name == "Кинотеатр Прага"
    assert payload.address_text == "ул. Ленина, 12"


def test_city_slug_is_forbidden_in_body() -> None:
    """AC-9 (Task 3.4): `city_slug` в теле — `422`, а не молчаливый отброс.

    Краснеет при снятии `extra="forbid"`: клиент был бы уверен, что создал
    место в выбранном городе, а сервер взял бы город из профиля.
    """
    with pytest.raises(ValidationError):
        AddressPlaceCreate(name="Банк", address_text="ул. Ленина, 1", city_slug="kemerovo")
