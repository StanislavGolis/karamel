"""Схемы запросов анкеты (Task 2.1–2.6; AC-01, AC-02, AC-03, AC-05).

Проверяется граница «Pydantic — верхний слой защиты»: тело в 5 МБ, лишнее
поле, `null` и `gender` в теле должны отбиваться ДО сервиса и БД. Без этого
предел `varchar(100)` даёт `DBAPIError` → `500` на пользовательском вводе.

§18.2: проверяется исход (`ValidationError` / конкретное значение), а не
внутренности — тест не должен проходить при снятом ограничении.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.profile import (
    LocateCityRequest,
    LocateCityResponse,
    ProfileCreate,
    ProfileUpdate,
)

# Стори 12.2, AC-2: в запросе дата рождения, а не число лет.
# 1994-03-14 — 32 полных года на 2026-10-29.
VALID = {"name": "Аня", "city": "Томск", "birth_date": date(1994, 3, 14), "bio": "люблю горы"}


# --- ProfileCreate ---------------------------------------------------------------


def test_create_accepts_valid_payload() -> None:
    """AC-02: минимально корректный набор принимается без изменений.

    Схема не нормализует значения (это делает сервис, D-5): иначе
    нормализация жила бы в двух местах и расходилась.
    """
    payload = ProfileCreate(**VALID)

    assert payload.city == "Томск"
    assert payload.birth_date == date(1994, 3, 14)


@pytest.mark.parametrize("field", ["name", "city", "birth_date", "bio"])
def test_create_requires_all_four_fields(field: str) -> None:
    """AC-02, D-2: `POST` создаёт полную анкету — все четыре поля обязательны.

    Имя и возраст не «опциональны на будущее»: клиент обязан прислать их,
    иначе анкета ушла бы в `draft`, а клиент считал бы её сохранённой.
    """
    payload = {k: v for k, v in VALID.items() if k != field}

    with pytest.raises(ValidationError):
        ProfileCreate(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "я" * 51),  # AC-05: предел varchar(50)
        ("city", "я" * 101),  # AC-04: предел varchar(100)
        ("bio", "я" * 501),  # AC-05: доменный предел 500
        # Стори 12.2: границы возраста проверяет сервис по `birth_date`
        # (`underage` / `invalid_birth_date`), а не схема — значений,
        # которые схема обязана пропустить, у даты нет.
    ],
)
def test_create_passes_out_of_range_values_to_service(field: str, value: object) -> None:
    """CR 2.3, Решение 1: пределы держит сервис, а не Pydantic.

    `Field(max_length=...)` в схеме срабатывал раньше сервиса и возвращал
    общий `invalid_request`, поэтому доменные коды `name_too_long`,
    `city_too_long`, `bio_too_long` и `invalid_age` были недостижимы по HTTP.

    Тест краснеет, если предел вернут в схему: значение перестанет доходить
    до сервиса, и клиент снова увидит безадресный `invalid_request`.
    Сверяется дословно: схема не нормализует и не обрезает ввод (это сервис).
    """
    payload = ProfileCreate(**{**VALID, field: value})

    assert getattr(payload, field) == value


@pytest.mark.parametrize("extra_field", ["gender", "status", "is_hidden", "user_id", "uuid"])
def test_create_forbids_extra_fields(extra_field: str) -> None:
    """AC-02, D-4: служебные поля из тела не принимаются.

    `gender` в теле дал бы клиенту возможность прислать `male` женской
    учётке: составной FK отбил бы `IntegrityError` → `500`. `status`
    и `is_hidden` — не то, что решает пользователь на этом экране.
    """
    with pytest.raises(ValidationError):
        ProfileCreate(**{**VALID, extra_field: "x"})


# --- ProfileUpdate ---------------------------------------------------------------


def test_update_all_fields_optional() -> None:
    """AC-03: частичная правка — любое подмножество полей допустимо.

    Пустая модель технически валидна: сервис решает, что «ничего не пришло»
    — не ошибка, а `200` без изменений.
    """
    assert ProfileUpdate().model_fields_set == set()
    assert ProfileUpdate(bio="новый текст").model_fields_set == {"bio"}


def test_update_keeps_explicit_null_in_fields_set() -> None:
    """AC-03, D-11: «не передано» и «передано `null`» различимы.

    Именно `model_fields_set` позволяет сервису отличить отсутствующий ключ
    (ничего не делать) от явного `null` (`422 field_required`). Без этого
    `{"name": null}` тихо обнулил бы имя и оставил анкету в `complete`,
    а CHECK дал бы `IntegrityError` → `500`.
    """
    update = ProfileUpdate(name=None)

    assert "name" in update.model_fields_set
    assert update.name is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "я" * 51), ("city", "я" * 101), ("bio", "я" * 501)],
)
def test_update_passes_out_of_range_values_to_service(field: str, value: object) -> None:
    """CR 2.3, Решение 1: `PATCH` тоже доходит до сервиса, а не отбивается схемой.

    `PATCH` вызывается чаще `POST` (правка после одобрения). Предел в схеме
    подменял бы доменный код (`city_too_long`, `invalid_age`) общим
    `invalid_request`, и клиент не знал бы, какое поле исправить.
    """
    payload = ProfileUpdate(**{field: value})

    assert getattr(payload, field) == value


@pytest.mark.parametrize("extra_field", ["gender", "status", "is_hidden", "user_id"])
def test_update_forbids_extra_fields(extra_field: str) -> None:
    """AC-03: через `PATCH` нельзя подменить пол или флаг скрытия."""
    with pytest.raises(ValidationError):
        ProfileUpdate(**{extra_field: "x"})


# --- LocateCityRequest / Response ------------------------------------------------


def test_locate_city_accepts_coordinates() -> None:
    """AC-06: `lat`, `lng`, `accuracy` обязательны и приходят в теле.

    `accuracy` нужна клиенту, чтобы решить, показывать ли подсказку;
    сервер её не использует в решении о городе, но принимает.
    """
    request = LocateCityRequest(lat=56.49, lng=84.97, accuracy=10.0)

    assert request.lat == 56.49


@pytest.mark.parametrize(
    ("lat", "lng"),
    [(91.0, 84.97), (-91.0, 84.97), (56.49, 181.0), (56.49, -181.0)],
)
def test_locate_city_rejects_out_of_globe_coordinates(lat: float, lng: float) -> None:
    """AC-06: широта за -90..90 и долгота за -180..180 — не координаты.

    Без этой проверки точка `lat=91` попала бы в сравнение с прямоугольником
    и вернула бы `outside_pilot_city` — то есть «валидный» ответ на мусор.
    """
    with pytest.raises(ValidationError):
        LocateCityRequest(lat=lat, lng=lng, accuracy=10.0)


def test_locate_city_response_shape() -> None:
    """AC-06: ответ — только город или `city=null` с причиной.

    Координат в ответе нет: клиент не должен получать назад то, что прислал,
    иначе они попадут в аналитику и кэш браузера.
    """
    inside = LocateCityResponse(city="Томск")
    outside = LocateCityResponse(city=None, reason="outside_pilot_city")

    assert inside.model_dump() == {"city": "Томск", "reason": None}
    assert outside.model_dump() == {"city": None, "reason": "outside_pilot_city"}


def test_locate_city_rejects_unbounded_accuracy() -> None:
    """CR 2.3, патч 16: `accuracy` имеет верхнюю границу.

    Прежнее `Field(ge=0)` без верхней границы принимало `1e308`: сервер это
    поле не использует, но контракт «браузерная точность в метрах» переставал
    быть контрактом — любое число объявлялось допустимым. 100 км заведомо
    больше разумного значения `navigator.geolocation`, поэтому всё выше — мусор.
    """
    with pytest.raises(ValidationError):
        LocateCityRequest(lat=56.49, lng=84.97, accuracy=100_001)

    # Граница включена: 100 км ещё допустимо, ровно как в геолокации.
    assert LocateCityRequest(lat=56.49, lng=84.97, accuracy=100_000).accuracy == 100_000
