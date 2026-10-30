"""Доменная валидация анкеты (Task 4.7–4.8; AC-01, AC-03, AC-05).

Проверяются чистые функции сервиса: нормализация + доменные коды ошибок +
пересчёт `status`. Это слой, где ввод пользователя превращается в решение
«сохранить» или «отказать с кодом»; HTTP-слой только переводит исключение
в статус.

§18.2: проверяется конкретный `code`, а не факт исключения. `ProfileFieldError`
с другим кодом — другой дефект для клиента и другой текст в UI.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas.profile import ProfileCreate, ProfileUpdate
from app.services.profile_service import (
    ProfileFieldError,
    _check_raw_length,
    _compute_status,
    _validate_and_normalize_create,
    _validate_and_normalize_update,
)

PILOT_CITY = "Томск"


def _create(**overrides: object) -> ProfileCreate:
    """Полное тело `POST` в обход Pydantic-валидации.

    `model_construct`, а не `ProfileCreate(**...)`: Pydantic отбивает длину
    и диапазон раньше сервиса (`422 invalid_request`), и через конструктор
    доменные коды `*_too_long` / `invalid_age` было бы не проверить вовсе.
    Проверять их нужно: сервис вызывается не только из HTTP — из воркера
    и миграции данные приходят без Pydantic, и там единственная защита — эта.
    """
    payload = {
        "name": "Аня",
        "city": "Томск",
        # Стори 12.2, AC-2: вместо числа лет — дата рождения.
        # 1994-03-14 на 2026-10-29 даёт 32 полных года.
        "birth_date": date(1994, 3, 14),
        "bio": "люблю горы",
    }
    payload.update(overrides)
    return ProfileCreate.model_construct(**payload)


# --- AC-01: город обязателен ----------------------------------------------------


@pytest.mark.parametrize("city", ["", "   ", "\u00a0", "\t\n "])
def test_blank_city_raises_city_required(city: str) -> None:
    """AC-01: пустой город после нормализации — `422 city_required`.

    Пробелы, NBSP и таб неотличимы от пустой строки для человека. Если
    их пропустить, в БД появится строка со `status='complete'` и городом,
    который не найдёт ни один запрос выборки.
    """
    with pytest.raises(ProfileFieldError) as exc:
        _validate_and_normalize_create(_create(city=city), PILOT_CITY)

    assert exc.value.code == "city_required"


@pytest.mark.parametrize("field", ["name", "bio"])
def test_blank_text_field_raises_field_required(field: str) -> None:
    """AC-03, AC-05: пустое имя или текст — `422 field_required`.

    `city` получает собственный код (`city_required`), потому что AC-01
    называет город отдельно — клиенту нужен точный текст ошибки у поля.
    """
    with pytest.raises(ProfileFieldError) as exc:
        _validate_and_normalize_create(_create(**{field: "  \u00a0 "}), PILOT_CITY)

    assert exc.value.code == "field_required"


# --- AC-04, AC-05: длины и диапазон ---------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("name", "я" * 51, "name_too_long"),
        ("city", "я" * 101, "city_too_long"),
        ("bio", "я" * 501, "bio_too_long"),
    ],
)
def test_too_long_value_raises_domain_code(field: str, value: str, code: str) -> None:
    """AC-04, AC-05: превышение длины — доменный код, а не `500` из БД.

    Pydantic отбивает это раньше (`422 invalid_request`), но доменная проверка
    обязана существовать самостоятельно: `POST` из воркера или миграции
    не проходит через Pydantic, и без неё `varchar(100)` дал бы `DBAPIError`.
    """
    with pytest.raises(ProfileFieldError) as exc:
        _validate_and_normalize_create(_create(**{field: value}), PILOT_CITY)

    assert exc.value.code == code


@pytest.mark.parametrize(
    ("birth_date", "code"),
    [
        # Стори 12.2, AC-4: 16 полных лет на 2026-10-29.
        (date(2010, 1, 1), "underage"),
        # Верхняя граница `AGE_MAX`: 126 лет — опечатка в годе.
        (date(1900, 1, 1), "invalid_birth_date"),
        # Дата в будущем.
        (date(2027, 1, 1), "invalid_birth_date"),
    ],
)
def test_birth_date_out_of_range_raises_domain_code(birth_date: date, code: str) -> None:
    """AC-4, AC-5: недопустимая дата — `422 underage` / `422 invalid_birth_date`.

    Доменный код нужен по той же причине, что и для длин: CHECK в БД вернул
    бы `IntegrityError` → `500`. Границы проверяются на фиксированной дате,
    иначе тест «работает» только в день написания (AC-6).
    """
    with pytest.raises(ProfileFieldError) as exc:
        _validate_and_normalize_create(
            _create(birth_date=birth_date), PILOT_CITY, today=date(2026, 10, 29)
        )

    assert exc.value.code == code


# --- AC-04: нормализация применяется -------------------------------------------------


def test_create_normalizes_city_and_bio() -> None:
    """AC-04: сервис отдаёт нормализованные значения, а не сырой ввод.

    `'  ТОМСК '` → `'Томск'` (эталон из конфига), `'люблю\u00a0\u00a0горы'`
    → `'люблю горы'`. Без этого CHECK `city = btrim(city)` дал бы
    `IntegrityError` на живом сохранении.
    """
    values = _validate_and_normalize_create(
        _create(city="  ТОМСК ", bio="люблю\u00a0\u00a0горы"), PILOT_CITY
    )

    assert values == {
        "name": "Аня",
        "city": "Томск",
        "birth_date": date(1994, 3, 14),
        "bio": "люблю горы",
    }


# --- AC-03: частичная правка ---------------------------------------------------------


def test_update_returns_only_provided_fields() -> None:
    """AC-03, D-11: `PATCH` меняет только переданные поля.

    Отсутствующий ключ не попадает в результат: иначе сервис обнулил бы
    непереданное поле и CHECK полноты дал бы `IntegrityError` → `500`.
    """
    values = _validate_and_normalize_update(ProfileUpdate(bio="новый текст"), PILOT_CITY)

    assert values == {"bio": "новый текст"}


def test_update_explicit_null_raises_field_required() -> None:
    """AC-03, D-11: `{"name": null}` — `422 field_required`, не обнуление.

    D-3 стори: обратного перехода `complete → draft` нет, стереть поле нельзя.
    Без этой проверки явный `null` тихо обнулил бы имя и оставил `status`
    прежним — расхождение с CHECK.
    """
    with pytest.raises(ProfileFieldError) as exc:
        _validate_and_normalize_update(ProfileUpdate(city=None), PILOT_CITY)

    assert exc.value.code == "field_required"


def test_update_blank_city_raises_city_required() -> None:
    """AC-01: правка городом из пробелов — тот же отказ, что при создании."""
    with pytest.raises(ProfileFieldError) as exc:
        _validate_and_normalize_update(ProfileUpdate(city="   "), PILOT_CITY)

    assert exc.value.code == "city_required"


# --- status -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "city", "age", "bio", "expected"),
    [
        ("Аня", "Томск", 32, "текст", "complete"),
        (None, "Томск", 32, "текст", "draft"),
        ("Аня", None, 32, "текст", "draft"),
        ("Аня", "Томск", None, "текст", "draft"),
        ("Аня", "Томск", 32, None, "draft"),
        (None, None, None, None, "draft"),
    ],
)
def test_compute_status(
    name: str | None, city: str | None, age: int | None, bio: str | None, expected: str
) -> None:
    """AC-02, AC-03, D-2: `complete` только при всех четырёх полях.

    Таблица, а не одна проверка: дефект «забыли одно поле в условии» иначе
    не виден. `None` и пустая строка считаются незаполненным одинаково —
    CHECK полноты в БД смотрит на `NULL`, а пустая строка до него не доходит
    (её отбивает валидация).
    """
    assert _compute_status(name=name, city=city, age=age, bio=bio) == expected


def test_compute_status_treats_empty_string_as_missing() -> None:
    """D-2: пустая строка — не заполненное поле.

    `''` не должно давать `complete`: строка в БД прошла бы CHECK полноты
    (`''` не `NULL`), но карточка отрендерилась бы пустой.
    """
    assert _compute_status(name="", city="Томск", age=32, bio="текст") == "draft"


# --- Фикс OOM (2026-10-22): ранний предел до нормализации ---------------------


def test_raw_length_allows_long_whitespace_padding() -> None:
    """Длинный пробельный паддинг — не повод для `bio_too_long`.

    Порог ×20 считается по ЗНАЧИМЫМ символам: `'а' + ' ' * 10_000` после
    схлопывания занимает один символ и валиден. Проверка сырой `len()`
    отвергала бы его, хотя `_check_text` пропустил бы.
    """
    _check_raw_length("bio", "а" + " " * 10_000)
    _check_raw_length("name", "а" + "\u00a0" * 900)


def test_raw_length_rejects_megabyte_value() -> None:
    """Мегабайты значимых символов отбиваются ДО посимвольной нормализации.

    Иначе `_strip_invisible` обходит каждый символ в event loop — ровно тот
    механизм, который ронял контейнер по OOM.
    """
    with pytest.raises(ProfileFieldError) as exc:
        _check_raw_length("bio", "я" * 5_000_000)
    assert exc.value.code == "bio_too_long"
