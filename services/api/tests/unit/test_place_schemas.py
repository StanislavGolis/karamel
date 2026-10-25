"""Схемы места (Task 5.5, 8.2; AC-02).

§18.1: каждый тест краснеет от конкретной правки. Проверяется не «схема
собралась», а «мусор не дошёл до БД»: категория вне белого списка, имя из
таба и слаг чужого формата отвергаются здесь, а не `IntegrityError` из
PostgreSQL (иначе пользователь получил бы `500`).
"""

from __future__ import annotations

import uuid as uuid_lib

import pytest
from pydantic import ValidationError

from app.models.enums import PlaceCategory
from app.schemas.place import PlaceCreate, PlaceRead, PlaceUpdate


def _payload(**overrides: object) -> dict[str, object]:
    """Минимальный валидный набор: тест меняет ровно одно поле."""
    base: dict[str, object] = {
        "name": "Кафе „Прага“",
        "category": "cafe",
        "address_text": "пр. Ленина, 1",
        "community_slug": "tomsk",
    }
    base.update(overrides)
    return base


# --- AC-02: категория -------------------------------------------------------------


def test_known_category_accepted_as_enum() -> None:
    """AC-02: строка категории превращается в `PlaceCategory`, а не остаётся `str`.

    Краснеет, если поле объявить `str`: тогда фильтр 3.3 примет значение,
    которого нет в БД, и выдача упадёт на `CHECK`-е при вставке — то есть
    мусор из БД (воркер, сырой SQL) попадёт в ответ API.
    """
    place = PlaceCreate(**_payload())

    assert place.category is PlaceCategory.CAFE
    assert isinstance(place.category, PlaceCategory)


def test_unknown_category_message_lists_all_categories() -> None:
    """AC-02: отказ перечисляет допустимые категории.

    Сообщение строится из `PlaceCategory` (единственный источник, D-2),
    поэтому проверяются ВСЕ значения, а не одна подсказка. Краснеет, если
    поле станет `str` с проверкой после: тогда текст ошибки не содержит
    перечня, и клиент 3.2 не сможет показать админу список из выпадашки.
    """
    with pytest.raises(ValidationError) as excinfo:
        PlaceCreate(**_payload(category="bar"))

    message = str(excinfo.value)
    for category in PlaceCategory:
        assert str(category) in message


# --- AC-01, AC-03: поля и нормализация --------------------------------------------


@pytest.mark.parametrize("raw", ["  Кафе „Прага“\u00a0", "Кафе\u200b „Прага“", "Кафе\t„Прага“"])
def test_name_is_normalized_before_insert(raw: str) -> None:
    """AC-01, §18.1: имя нормализуется до валидации длины и до вставки.

    Краснеет, если убрать `mode="before"`-валидатор: НБСП и ZWSP не режутся
    ни `strip()`, ни `btrim` в PostgreSQL, и место с таким именем навсегда
    выпадает из точного поиска по названию (D-5).
    """
    assert PlaceCreate(**_payload(name=raw)).name == "Кафе „Прага“"


def test_address_is_normalized_before_insert() -> None:
    """AC-01: адрес нормализуется тем же `normalize_text`, что и имя.

    Краснеет при отказе от нормализации адреса: `CHECK ck_places_address_text`
    отверг бы строку из таба, и пользователь получил бы `500`.
    """
    place = PlaceCreate(**_payload(address_text="  пр. Ленина,\u00a0 1 "))

    assert place.address_text == "пр. Ленина, 1"


@pytest.mark.parametrize("field", ["name", "address_text"])
@pytest.mark.parametrize("value", ["", "   ", "\u200b", "\t"])
def test_blank_required_text_rejected(field: str, value: str) -> None:
    """AC-01: пустое или невидимое значение — отказ на схеме.

    Краснеет, если выставить `min_length=1` до нормализации: строка из одного
    ZWSP проходит `len()` и отвергается `CHECK`-ом только в БД — `500`
    вместо `422` на форме админки.
    """
    with pytest.raises(ValidationError):
        PlaceCreate(**_payload(**{field: value}))


@pytest.mark.parametrize(("field", "limit"), [("name", 120), ("address_text", 255)])
def test_length_limits_follow_column(field: str, limit: int) -> None:
    """AC-01: предел символов совпадает с шириной колонки.

    Краснеет, если предел схемы больше колонки: строка прошла бы Pydantic
    и упала `DBAPIError`-ом на вставке — `500` на валидном для формы вводе.
    """
    PlaceCreate(**_payload(**{field: "я" * limit}))
    with pytest.raises(ValidationError):
        PlaceCreate(**_payload(**{field: "я" * (limit + 1)}))


@pytest.mark.parametrize("value", ["Tomsk", "to", "tomsk!", "томск", ""])
def test_community_slug_pattern_rejects_bad_values(value: str) -> None:
    """AC-03: слаг сообщества подчиняется `^[a-z0-9_-]{3,64}$`.

    Тот же шаблон стоит в `ck_places_community_slug`. Краснеет при удалении
    `pattern`: `Tomsk` прошёл бы схему и упал `CHECK`-ом в БД, а место
    легло бы в сообщество, которого нет в конфиге.
    """
    with pytest.raises(ValidationError):
        PlaceCreate(**_payload(community_slug=value))


def test_community_slug_missing_is_rejected() -> None:
    """AC-03: `community_slug` обязателен.

    Краснеет, если дать полю значение по умолчанию: место без сообщества
    не видно ни в одной ленте, и понять это по ответу API невозможно.
    """
    payload = _payload()
    payload.pop("community_slug")

    with pytest.raises(ValidationError):
        PlaceCreate(**payload)


# --- AC-01: `id` наружу не выходит -------------------------------------------------


def test_read_schema_has_no_internal_id() -> None:
    """AC-01, D-2: в ответе нет `id`, только `uuid`.

    Краснеет, если добавить `id` в `PlaceRead`: внутренний bigint становится
    публичным, и перебор `/1..N` обходит любые будущие лимиты выдачи.
    """
    keys = set(PlaceRead.model_fields)

    assert "id" not in keys
    assert keys == {"uuid", "name", "category", "address_text", "is_active"}


def test_read_schema_serializes_enum_as_string() -> None:
    """AC-02: `category` уходит в JSON строкой, а не именем члена.

    Краснеет, если заменить `StrEnum` на обычный `Enum`: клиент получил бы
    `"PlaceCategory.CAFE"` и не смог бы сравнить его со списком из FR66.
    """
    read = PlaceRead(
        uuid=uuid_lib.uuid4(),
        name="Кафе „Прага“",
        category=PlaceCategory.CAFE,
        address_text="пр. Ленина, 1",
        is_active=True,
    )

    assert read.model_dump(mode="json")["category"] == "cafe"


# --- Task 5.1: частичное обновление -----------------------------------------------


def test_update_schema_allows_partial_payload() -> None:
    """Task 5.1: `PlaceUpdate` допускает отсутствующие поля.

    Краснеет, если сделать поля обязательными: `PATCH /places/{uuid}` в 3.2
    требовал бы присылать все четыре поля, и правка одного названия
    перезаписывала бы адрес значением по умолчанию.
    """
    update = PlaceUpdate(name="Кафе „Прага“")

    assert update.name == "Кафе „Прага“"
    assert update.category is None
    assert update.address_text is None


# --- CR 3.1, патч 14: чтение ОРМ-объекта -----------------------------------------


def test_read_schema_accepts_orm_object() -> None:
    """AC-01, готовность 3.2/3.3: `PlaceRead` читает ОРМ-объект напрямую.

    §18.1: краснеет без `model_config = ConfigDict(from_attributes=True)` —
    `PlaceRead.model_validate(place)` падал `ValidationError` (проверено на
    живом объекте модели). Роутеру 3.2 пришлось бы собирать ответ вручную,
    то есть заводить третий источник истины по составу полей.
    """
    from app.models.place import Place

    place = Place(
        name="Кафе „Прага“",
        category="cafe",
        address_text="пр. Ленина, 1",
        community_slug="tomsk",
    )
    place.uuid = uuid_lib.uuid4()
    place.is_active = True

    read = PlaceRead.model_validate(place)

    assert read.name == "Кафе „Прага“"
    assert read.category is PlaceCategory.CAFE
    assert read.is_active is True
    assert "id" not in read.model_dump()


# --- R-05 стори 3.2: перенос между сообществами запрещён --------------------------


@pytest.mark.parametrize("extra", ["community_slug", "uuid", "id", "created_at", "updated_at"])
def test_update_schema_rejects_immutable_fields(extra: str) -> None:
    """AC-06, R-05: `PlaceUpdate` не принимает перенос и смену публичного id.

    §18.1: краснеет, если вернуть `community_slug` в схему (или снять
    `extra="forbid"`). Перенос места между сообществами ломает `uq`-пару
    и все сохранённые ссылки: карточки эпика 4 ссылаются на `places.uuid`,
    а место внезапно оказывается в чужой ленте.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        PlaceUpdate(name="Кафе „Прага“", **{extra: "tomsk"})

    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


def test_update_schema_has_no_community_field() -> None:
    """AC-06, R-05: поля `community_slug` в `PlaceUpdate` нет вовсе.

    §18.1: краснеет, если поле вернуть. `extra="forbid"` и отсутствие поля
    работают вместе: клиент обязан получить `422`, а не «тихо проигнорировано».
    """
    assert "community_slug" not in PlaceUpdate.model_fields


def test_update_schema_allows_is_active() -> None:
    """AC-05, D-7: `is_active` — обычное поле правки (скрыть/восстановить).

    Краснеет, если запретить `is_active` вместе с непубличными полями:
    восстановление скрытого места (`PATCH is_active=true`) осталось бы без
    единственного маршрута (D-7: `DELETE` — частный случай `is_active=false`).
    """
    update = PlaceUpdate(is_active=True)

    assert update.is_active is True
    assert update.name is None


def test_create_schema_rejects_own_uuid() -> None:
    """AC-02, AC-06: `uuid` при создании не принимается — его выдаёт сервер.

    §18.1: краснеет, если снять `extra="forbid"` у `PlaceCreate`: клиент
    прислал бы `uuid` и решил, что адрес места ему подконтролен.
    """
    with pytest.raises(ValidationError):
        PlaceCreate(**_payload(uuid=str(uuid_lib.uuid4())))
