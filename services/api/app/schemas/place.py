"""Схемы места (стори 3.1, Task 5.1–5.5; AC-01, AC-02, AC-03).

D-2: наружу уходят только `uuid`, `name`, `category`, `address_text`, `is_active`.
Поля `id` в схемах нет и не должно появиться: внутренний bigint (`places.id`)
стал бы публичным идентификатором, и перебор `/1..N` обошёл бы любые лимиты
выдачи. Публичный адрес места — `uuid`.

AC-02: `category` типизирован `PlaceCategory`, а не `str`. Строка приняла бы
значение, которого нет в БД (там `ck_places_category`), и клиент получил бы
категорию, для которой у него нет ветки отображения. Сообщение об ошибке при
этом перечисляет допустимые значения — требование AC-02 «перечень допустимых
категорий».

AC-01: `name` и `address_text` нормализуются ТЕМ ЖЕ `normalize_text`, что и поля
анкеты (2.3). Без этого НБСП/ZWSP/таб по краям не режутся ни `strip()`,
ни `btrim` в PostgreSQL: строка прошла бы схему и упала `CHECK`-ом в БД —
`500` на пользовательском вводе вместо `422`. Для однострочных полей нормализация
идёт в `mode="before"`, чтобы ограничение длины считалось уже по очищенному
значению.
"""

from __future__ import annotations

import uuid as uuid_lib

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import PlaceCategory
from app.services.profile_normalize import normalize_text

# AC-03: тот же шаблон стоит в `ck_places_city_slug` и в
# `Settings._COMMUNITY_SLUG_RE`. Расхождение дало бы `500` на сохранении:
# Pydantic пропустил бы слаг, который PostgreSQL отвергает.
CITY_SLUG_PATTERN = r"^[a-z0-9_-]{3,64}$"

# AC-01: пределы совпадают с шириной колонок `places`. Проверка на схеме,
# а не только в БД: иначе слишком длинное значение падало бы `DBAPIError`-ом
# (`500`) на валидном для формы вводе.
NAME_MAX_LENGTH = 120
ADDRESS_MAX_LENGTH = 255


def _normalize(value: object) -> object:
    """Нормализация однострочного значения до проверки длины.

    `object`, а не `str`: в `mode="before"` Pydantic отдаёт сюда что угодно
    (в том числе `None` в `PlaceUpdate`), и подмена типа сломала бы
    собственные проверки Pydantic — он должен сам сообщить «ожидалась строка».
    """
    if not isinstance(value, str):
        return value
    return normalize_text(value)


class PlaceCreate(BaseModel):
    """AC-01, AC-03: вход создания места (подключит роутер 3.2).

    `city_slug` обязателен: место без сообщества не видно ни в одной
    ленте (`WHERE city_slug = ...` не совпадёт), и понять это по ответу
    API невозможно. Значение сверяется сервисом с `Settings.city_slugs` —
    опечатка не создаёт «невидимое сообщество» (AC-03).
    """

    # CR 3.1, патч 6: лишние поля отклоняются, а не отбрасываются молча.
    # `PlaceCreate(..., is_active=False)` раньше давал «активное» место, а
    # `id=99` тихо выбрасывался — клиент считал, что сохранил присланное.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    # AC-02: enum-тип, не `str` с проверкой после — иначе мусор из БД
    # (воркер, сырой SQL) дошёл бы до выдачи 3.3.
    category: PlaceCategory
    address_text: str = Field(min_length=1, max_length=ADDRESS_MAX_LENGTH)
    city_slug: str = Field(pattern=CITY_SLUG_PATTERN)

    @field_validator("name", "address_text", mode="before")
    @classmethod
    def _normalize_text_fields(cls, v: object) -> object:
        """AC-01: нормализация ДО проверки длины и до вставки.

        Порядок важен: строка из одного ZWSP имеет длину 1 и прошла бы
        `min_length`, а в БД упала бы на `ck_places_name` — `500` вместо `422`.
        """
        return _normalize(v)


class PlaceUpdate(BaseModel):
    """Task 5.1, R-05 стори 3.2: частичное обновление места.

    Все поля необязательны: правка одного названия не должна перезаписывать
    адрес значением по умолчанию. Отсутствие поля (`None`) означает «не менять»,
    а не «очистить» — очистка запрещена `NOT NULL` в БД и `min_length=1`.

    R-05: `city_slug` здесь НЕТ и не появится. Место привязано к сообществу
    навсегда: перенос ломал бы `uq_places_city_name` (имя, свободное в одном
    сообществе, занято в другом) и все сохранённые ссылки — карточки эпика 4
    адресуют место по `uuid`, а оно бы оказалось в чужой ленте без единого
    действия администратора в интерфейсе. Слаг задаётся только при создании.

    `is_active` — обычное поле (D-7): «скрыть» — это `DELETE`, «показать» — этот
    `PATCH`. Отдельного `/restore` нет: два маршрута на одно поле дали бы две
    ветки прав с разными проверками.
    """

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    category: PlaceCategory | None = None
    address_text: str | None = Field(default=None, min_length=1, max_length=ADDRESS_MAX_LENGTH)
    is_active: bool | None = None

    # AC-06, R-05: без `extra="forbid"` `city_slug` в теле `PATCH` прошёл бы
    # незаметно (Pydantic отбрасывает лишние поля по умолчанию), и клиент считал бы,
    # что перенос выполнен. `uuid`/`id`/`created_at` отбиваются тем же правилом.
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "address_text", mode="before")
    @classmethod
    def _normalize_optional_text_fields(cls, v: object) -> object:
        """AC-01: та же нормализация, но `None` (поле не прислали) не трогается."""
        return _normalize(v)


# Task 1.3 (AC-04, D-7): размер страницы — константа сервера. Параметра `limit`
# в контракте §7.1 нет: он вводил бы класс дефектов `limit=0` (молча пустая
# выдача) и `limit=-1` (`500` из драйвера) — тот же долг, что `Query(ge/le)`
# в DS 2.6. Одно значение — один источник: сервис, репозиторий и тесты берут
# его отсюда, а не пишут `50` по месту.
PLACES_PAGE_SIZE = 50

# Task 3.5 (AC-04): верхняя граница защищает от вызова сервиса с `limit`
# больше контрактного (воркер, скрипт импорта): `LIMIT` без предела выгрузил бы
# таблицу целиком. Отдельная константа, а не `= PLACES_PAGE_SIZE`, чтобы
# уменьшение страницы не меняло молча предел для внутренних вызовов.
MAX_PLACES_PAGE_SIZE = 100


class PlaceRead(BaseModel):
    """AC-01, D-2: место глазами клиента (форма ответа 7.1).

    `uuid` вместо `id`: внутренний bigint не покидает сервер. `is_active`
    в ответе нужен админке 3.2 — форма редактирования показывает текущее
    состояние «скрыто/видимо» без второго запроса.
    """

    # CR 3.1, патч 7: `from_attributes` обязателен — 3.2/3.3 отдают ОРМ-объект.
    # Без него `PlaceRead.model_validate(place)` падает `ValidationError`
    # (проверено на живом объекте модели), и роутеру пришлось бы собирать ответ
    # вручную третьим источником истины по полям.
    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str
    # AC-02: `StrEnum` сериализуется строкой (`"cafe"`), а не именем члена.
    category: PlaceCategory
    address_text: str
    is_active: bool


class PlaceListResponse(BaseModel):
    """AC-01, AC-06, контракт §7.1: конверт выдачи `{items, total, cursor}`.

    `items` — `PlaceRead`, а не урезанная схема (D-8): тот же ресурс не должен
    иметь два несовместимых представления в одном API. Таблица §7.1 перечисляет
    четыре поля, но `PlaceRead` уже отдаёт `is_active` в 3.2; вторая схема с тем
    же содержимым разошлась бы с первой при первом же добавлении поля.
    В публичной выдаче `is_active` всегда `true`, скрытое видит только модератор.

    `total` — «сколько всего под фильтром», не «сколько осталось» (D-4):
    считается отдельным `COUNT` без курсора, поэтому листание его не меняет.

    `cursor` — `null` в конце выдачи и непрозрачная строка иначе (AC-04).
    Клиент не разбирает его содержимое: формат — деталь сервера.
    """

    items: list[PlaceRead]
    total: int
    cursor: str | None = None
