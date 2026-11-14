"""Схемы адресного прокси и автосоздания места (стори 13.1, Task 3; AC-9).

Границы доверия (`NFR34`): `AddressSuggestion` живёт в `services/address_geocoder`
рядом с кодом, который его собирает, — здесь только вход и выход второго
маршрута. Разделение намеренное: подсказка адреса (координаты, `display_name`)
и запись места (только `name`/`address_text`) — два разных контракта с разными
границами, и держать их в одном модуле значило бы позволить импортировать
координаты туда, где они запрещены.
"""

from __future__ import annotations

import uuid as uuid_lib

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import PlaceCategory
from app.schemas.place import ADDRESS_MAX_LENGTH, NAME_MAX_LENGTH
from app.services.profile_normalize import normalize_text


class AddressPlaceCreate(BaseModel):
    """AC-9: вход `POST /address/place` — только три поля.

    `city_slug` здесь НЕТ и не появится: слаг берётся из профиля автора
    (`user.city_slug`). Поле в теле позволило бы создать место в чужом городе,
    и оно было бы невидимо в ленте, но заняло бы имя в `uq_places_city_name`.
    `extra="forbid"` превращает такую попытку в `422`, а не в молчаливый
    отброс (приём `PlaceCreate`, CR 3.1 патч 6).

    `category_hint`, а не `category`: значение пришло из OSM и является
    ПОДСКАЗКОЙ. Имя поля напоминает об этом и не даёт клиенту думать, что
    он выбрал категорию окончательно.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    address_text: str = Field(min_length=1, max_length=ADDRESS_MAX_LENGTH)
    # AC-9: `PlaceCategory` — уже проверенный enum; `str` дал бы значение,
    # которого нет в `ck_places_category`, то есть `500` на вводе.
    category_hint: PlaceCategory = PlaceCategory.OTHER

    @field_validator("name", "address_text", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        """AC-9: нормализация ДО проверки длины — та же, что в `PlaceCreate`.

        Одна функция на две схемы: расхождение дало бы сохранение строки,
        которую `PlaceCreate` отвергает, и `CHECK` в БД вернул бы `500`.
        """
        if not isinstance(value, str):
            return value
        return normalize_text(value)


class AddressPlaceRead(BaseModel):
    """AC-9: результат автосоздания — `place_uuid` и признак создания.

    `created: false` означает «такое активное место уже было»: для автора это
    успех, а не конфликт — он хотел указать именно это место. Отдавать `409`
    на повторный выбор значило бы требовать от пользователя помнить, что он
    уже создавал, и искать место в справочнике вручную.

    Координат здесь нет и не должно появиться: `places` их не хранит (`NFR34`).
    """

    model_config = ConfigDict(extra="forbid")

    place_uuid: uuid_lib.UUID
    created: bool
