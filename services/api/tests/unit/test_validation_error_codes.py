"""Конверт ошибок Pydantic (Task 6.6 стори 2.3; AC-06).

§18.2: проверяется конкретный `code` в теле ответа, а не факт `422`. Без этой
проверки `lat=91` возвращал общий `invalid_request`, и клиент не мог отличить
ошибку формы от мусора в координатах.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.main import validation_error_handler

app = FastAPI()
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]


class Coordinates(BaseModel):
    """Минимальная копия `LocateCityRequest` для проверки обработчика."""

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float = Field(ge=0)


@app.post("/coords")
async def coords(payload: Coordinates) -> dict[str, float]:
    return {"lat": payload.lat}


client = TestClient(app, raise_server_exceptions=False)


def test_coordinates_out_of_range_return_domain_code() -> None:
    """AC-06: широта за -90..90 — `invalid_coordinates`, не `invalid_request`.

    Клиент по этому коду понимает, что проблема в геолокации, а не в форме:
    первый случай лечится повторным определением города, второй — правкой поля.
    """
    response = client.post("/coords", json={"lat": 91, "lng": 84.97, "accuracy": 10})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_coordinates"


@pytest.mark.parametrize(("lat", "lng"), [(56.49, 181), (56.49, -181), (-91, 84.97)])
def test_any_coordinate_violation_returns_domain_code(lat: float, lng: float) -> None:
    """AC-06: нарушение любой координаты даёт тот же доменный код.

    Параметризация по каждой оси: обработчик различает набор полей, и дефект
    «смотрит только на `lat`» иначе остался бы незамеченным.
    """
    response = client.post("/coords", json={"lat": lat, "lng": lng, "accuracy": 10})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_coordinates"


# --- CR 3.1, патч 13: категория места (AC-02) -----------------------------------


class PlacePayload(BaseModel):
    """Минимальная копия `PlaceCreate` для проверки обработчика."""

    name: str
    category: str = Field(pattern="^(cafe|cinema)$")


# QD A3: маршрут объявлен на РЕАЛЬНОМ префиксе справочника. До правки он жил
# на `/place`, и ветка `category_invalid` срабатывала на ЛЮБОМ пути — ровно тот
# дефект, который закрывает A3. Теперь префикс обязателен, и тест проверяет
# поведение, которое действительно работает в продукте.
#
# Путь — ПОДпуть префикса, а не сам `/api/v1/places`: на нём ниже объявлен
# `create_place_endpoint` для проверок длины, и второй роут на том же пути
# перехватил бы его запросы (FastAPI берёт первый совпавший).
@app.post("/api/v1/places/category-probe")
async def place(payload: PlacePayload) -> dict[str, str]:
    return {"name": payload.name}


@app.post("/api/v1/foreign")
async def foreign(payload: PlacePayload) -> dict[str, str]:
    """Тот же payload на ЧУЖОМ пути: код места сюда попадать не должен."""
    return {"name": payload.name}


def test_invalid_category_returns_code_with_allowed_list() -> None:
    """AC-02: отказ по категории несёт код И перечень допустимых значений.

    §18.1: краснеет без ветки `category` в обработчике — ответ был бы общим
    `invalid_request` без списка, и клиент 3.2 не смог бы отрисовать
    выпадающий список. Перечень берётся из `PlaceCategory`, поэтому
    проверяются ВСЕ значения, а не одно: захардкоженный список отстал бы
    от enum при добавлении категории.
    """
    from app.models.enums import PlaceCategory

    response = client.post(
        "/api/v1/places/category-probe", json={"name": "Прага", "category": "theatre"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "category_invalid"
    assert body["details"]["allowed"] == [str(item) for item in PlaceCategory]


def test_invalid_category_on_foreign_path_is_not_place_code() -> None:
    """QD A3: на чужом пути код категории МЕСТА не появляется.

    §18.1: КРАСНЕЕТ от отката условия `startswith(PLACES_PATH_PREFIX)` — ответ
    становится `category_invalid` с перечнем категорий справочника, и клиент
    анкеты показал бы админу выпадашку «кафе/кино/каток».
    """
    response = client.post("/api/v1/foreign", json={"name": "Прага", "category": "theatre"})

    assert response.status_code == 422
    assert response.json()["code"] != "category_invalid"


# --- Стори 3.2, AC-07: доменные коды полей места из Pydantic ----------------------


class PlaceSchema1(BaseModel):
    """Копия `PlaceCreate` по проверкам длины: те же пределы и типы."""

    name: str = Field(min_length=1, max_length=120)
    address_text: str = Field(min_length=1, max_length=255)


@app.post("/api/v1/places")
async def create_place_endpoint(payload: PlaceSchema1) -> dict[str, str]:
    return {"name": payload.name}


@app.post("/api/v1/profiles")
async def create_profile_endpoint(payload: PlaceSchema1) -> dict[str, str]:
    """Контрольный путь ВНЕ справочника мест: его коды меняться не должны."""
    return {"name": payload.name}


def test_place_short_name_returns_domain_code() -> None:
    """AC-07: пустое имя места — `422 name_required`, как и от сервиса.

    §18.1: краснеет без ветки `_place_field_error` в обработчике. Проверки
    `min_length` стоят в схеме, поэтому ввод отбивает Pydantic ДО сервиса,
    и админ получал бы `invalid_request` там, где воркер/импорт получает
    `name_required` — две формы ответа на одно нарушение (AC-07 прямо
    запрещает такое расхождение).
    """
    response = client.post("/api/v1/places", json={"name": "", "address_text": "пр. Ленина, 1"})

    assert response.status_code == 422
    assert response.json() == {"code": "name_required"}


def test_place_short_address_returns_domain_code() -> None:
    """AC-07: пустой адрес — `address_text_required`.

    Параметризация по полю: дефект «ветка смотрит только на `name`» иначе
    остался бы незамеченным.
    """
    response = client.post("/api/v1/places", json={"name": "Прага", "address_text": ""})

    assert response.status_code == 422
    assert response.json() == {"code": "address_text_required"}


def test_place_long_name_carries_max_length() -> None:
    """AC-07: переполнение имени — `name_too_long` + `details.max_length`.

    §18.1: краснеет без маппинга `string_too_long`. Клиент показывает
    «не длиннее 120» из `details`, а не из текста, который не парсится.
    """
    response = client.post(
        "/api/v1/places", json={"name": "Ж" * 121, "address_text": "пр. Ленина, 1"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "name_too_long"
    assert body["details"]["max_length"] == 120
    assert body["details"]["actual_length"] == 121


def test_place_long_address_carries_max_length() -> None:
    """AC-07: переполнение адреса — `address_text_too_long` + `details`.

    Второе поле проверяется отдельно: коды строятся по имени поля, и опечатка
    в карте полей дала бы `name_too_long` на длинный адрес.
    """
    response = client.post("/api/v1/places", json={"name": "Прага", "address_text": "Ж" * 256})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "address_text_too_long"
    assert body["details"]["max_length"] == 255


def test_profile_path_keeps_generic_code() -> None:
    """Стори 3.2: маппинг полей места НЕ затрагивает прочие пути.

    §18.1: краснеет, если убрать проверку префикса. У анкеты тоже есть `name`,
    но её коды — предмет стори 2.3 (FR9): `402`/`422` анкеты не должны
    подменяться кодами справочника мест при правке схемы места.
    """
    response = client.post("/api/v1/profiles", json={"name": "", "address_text": "x"})

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


def test_place_field_limits_match_schemas() -> None:
    """AC-07: карта полей в обработчике не расходится со схемами места.

    §18.1: краснеет, если поменять `NAME_MAX_LENGTH`/`ADDRESS_MAX_LENGTH`
    в `schemas/place.py`, забыв карту в `main.py`. Импортировать схему в
    `main.py` нельзя (цикл `main` → `schemas` → `models`), поэтому связь
    держится тестом: иначе `details.max_length` сообщал бы клиенту старое
    число, а `code` оставался бы верным — расхождение было бы незаметным.
    """
    from app.main import _PLACE_FIELD_LIMITS
    from app.schemas.place import ADDRESS_MAX_LENGTH, NAME_MAX_LENGTH

    assert _PLACE_FIELD_LIMITS == {"name": NAME_MAX_LENGTH, "address_text": ADDRESS_MAX_LENGTH}
