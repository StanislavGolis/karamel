"""Сервис справочника мест: создание места (стори 3.1, Task 7.1–7.6).

project-context §6: цепочка «роутер → сервис → репозиторий». Правило
«дубль запрещён» и сверка сообщества живут здесь, а не в роутере: второй
потребитель (импорт мест, админский скрипт) не начнёт их дублировать
с расхождениями.

D-1: HTTP-поверхности в 3.1 нет. `409 PLACE_DUPLICATE` реализован как
доменная ошибка `PlaceDuplicateError` с константой кода `PLACE_DUPLICATE`;
роутер 3.2 обязан только отобразить её в `409 {"code": "PLACE_DUPLICATE"}`,
не добавляя второй проверки дубля.

D-4 стори 2.2: `commit()` здесь НЕ вызывается — единица работы у роутера.
Сервис делает `flush` и возвращает объект; откат при ошибке — на вызывающем.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PlaceCategory
from app.models.place import Place
from app.repositories.places import PlaceRepository
from app.schemas.place import (
    ADDRESS_MAX_LENGTH,
    MAX_PLACES_PAGE_SIZE,
    NAME_MAX_LENGTH,
    PLACES_PAGE_SIZE,
)
from app.services.place_cursor import (
    INVALID_CURSOR_CODE,
    InvalidCursorError,
    decode_place_cursor,
    encode_place_cursor,
)
from app.services.profile_normalize import normalize_text

logger = structlog.get_logger(__name__)

# AC-04, D-1: код ошибки для роутера 3.2. Константа, а не строка по месту —
# клиент переключает отображение по коду, и опечатка в одном из двух мест
# дала бы неотличимую ошибку «что-то пошло не так».
PLACE_DUPLICATE_CODE = "PLACE_DUPLICATE"
# Стори 3.2, Task 4.2 (AC-03, D-6): «имя занято СКРЫТЫМ местом». Отдельный код,
# потому что действие у админа другое: не «придумай другое имя», а «восстанови
# вот это место». `details.existing_uuid` даёт ровно эту возможность.
PLACE_ARCHIVED_CODE = "PLACE_ARCHIVED"
# AC-04: код «места нет» для `PATCH`/`DELETE` по неизвестному `uuid`.
PLACE_NOT_FOUND_CODE = "PLACE_NOT_FOUND"
# AC-05, RISK-2: пустой `PATCH` — явная ошибка, а не молчаливый `200`.
# Клиент, отправивший пустое тело, получил бы «сохранено» без единой правки.
PLACE_NOTHING_TO_UPDATE_CODE = "place_nothing_to_update"
# QD A4 (R-05): попытка перенести место в другое сообщество. Поле убрано из
# `PlaceUpdate`, но редизайн формы или старый клиент могут его прислать;
# ответ обязан говорить «перенос запрещён», а не «сломанная форма».
PLACE_CITY_IMMUTABLE_CODE = "place_city_immutable"
PLACE_UNKNOWN_COMMUNITY_CODE = "PLACE_UNKNOWN_COMMUNITY"
# CR 3.1, патч 2: код категории — машинный идентификатор (snake_case, как все
# прочие коды проекта). Перечень допустимых значений едет отдельным полем
# `details`: клиент ветвится по константному `code`, а неконстантная строка со
# списком категорий не совпадёт ни с одной веткой `switch`.
PLACE_CATEGORY_INVALID_CODE = "category_invalid"


class PlaceDuplicateError(Exception):
    """AC-04: место с таким названием уже есть в этом сообществе.

    `409`, а не `422`: запрос синтаксически верен, конфликт в состоянии
    ресурса. Два пути ведут к этой ошибке — дешёвая проверка
    `exists_in_city` и `IntegrityError` от `uq_places_city_name`
    (гонка двух вставок). Обе дают один и тот же код.
    """

    code = PLACE_DUPLICATE_CODE


class PlaceArchivedError(Exception):
    """AC-03, D-6: имя занято СКРЫТЫМ местом этого же сообщества.

    `uq_places_city_name` не включает `is_active` (R-02: миграция `0007`
    не меняется), поэтому скрытое место продолжает держать своё имя. Отдавать
    на это `PLACE_DUPLICATE` нельзя: админ получил бы «такое место уже есть»
    про место, которого нет в выдаче, и не понял бы, что делать. `PLACE_ARCHIVED`
    с `existing_uuid` даёт единственное осмысленное действие — «восстановить».

    `existing_uuid` — доменный атрибут, а не часть кода: роутер кладёт его
    в `details`, а код остаётся константным (`PLACE_ARCHIVED`).
    """

    code = PLACE_ARCHIVED_CODE

    def __init__(self, existing_uuid: Any) -> None:
        super().__init__(PLACE_ARCHIVED_CODE)
        self.existing_uuid = existing_uuid


class PlaceUnknownCommunityError(Exception):
    """AC-03, ADR-01 «отвергнуто (б)»: слаг есть, но такого сообщества нет в конфиге.

    Без этой ошибки опечатка (`tomsk1`) создаёт место в «невидимом
    сообществе»: строка есть, `WHERE city_slug = 'tomsk'` её не находит,
    и лента пуста без единой ошибки в логах.
    """

    code = PLACE_UNKNOWN_COMMUNITY_CODE


class PlaceFieldError(Exception):
    """AC-01: доменная ошибка поля — `422 <code>` в роутере 3.2.

    Код лежит в атрибуте, а не в тексте: HTTP-слой отдаёт `{"code": ...}`,
    и клиент выбирает текст ошибки по коду. Форма `422`, а не `400` —
    project-context §4: это семантика домена, а не синтаксис запроса.
    """

    def __init__(self, code: str, details: Any | None = None) -> None:
        super().__init__(code)
        self.code = code
        # CR 3.1, патч 3: человекочитаемый перечень (например, допустимые
        # категории) не подменяет машинный код. Роутер 3.2 кладёт его
        # в `details`, клиент ветвится по константному `code`.
        self.details = details


def _validate_field_length(code: str, value: str, limit: int) -> None:
    """CR 3.1, патч 4 (AC-01, AC-07): предел длины ПОСЛЕ нормализации.

    Переполнение `varchar` даёт не `IntegrityError`, а `DBAPIError` (проверено
    на живом asyncpg/PostgreSQL 18), поэтому `except IntegrityError` его не
    видит, и воркер/импорт получали `500` на пользовательском вводе. Предел
    проверяется после NFC-канонизации: она удлиняет строку (один символ → до
    трёх, проверено на `\ufb2c`).
    """
    if len(value) > limit:
        raise PlaceFieldError(
            code,
            {"max_length": limit, "actual_length": len(value)},
        )


def _normalize_and_validate_fields(
    *, name: str | None, address_text: str | None, category: PlaceCategory | str | None
) -> tuple[str, str, str]:
    """Task 3.2 (AC-01, AC-07): единые правила полей для создания и правки.

    Вынесено, чтобы у `create_place` и `update_place` не появилось двух копий
    правил: нормализация, пустота, предел длины ПОСЛЕ нормализации, белый
    список категорий с перечнем в `details`. Вторая копия неизбежно разошлась
    бы — и админ получал бы на правке другой код ошибки, чем на создании,
    при одинаковом по сути вводе.

    Порядок шагов намеренный: нормализация ДО проверки пустоты и длины.
    Строка из одного ZWSP имеет длину 1 и прошла бы `min_length`, а `CHECK
    ck_places_name` отверг её в БД как `500`.

    Возвращает уже нормализованные `(name, address_text, category)`.

    CR 3.2, патч 2: `None` разбирается ЗДЕСЬ, до `normalize_text`. Поля схемы
    объявлены `str | None` (частичная правка), поэтому явный `null` доходит до
    хелпера; `normalize_text(None)` падал `TypeError` → `500` без конверта
    `{code}` (проверено исполнением: `PATCH {"name": null}`). Доменный ответ
    на попытку стереть поле — `*_required`, как у пустой строки.
    """
    if name is None:
        raise PlaceFieldError("name_required")
    if address_text is None:
        raise PlaceFieldError("address_text_required")
    if category is None:
        raise PlaceFieldError(
            PLACE_CATEGORY_INVALID_CODE,
            {"allowed": [str(item) for item in PlaceCategory]},
        )

    normalized_name = normalize_text(name)
    normalized_address = normalize_text(address_text)
    if not normalized_name:
        raise PlaceFieldError("name_required")
    if not normalized_address:
        raise PlaceFieldError("address_text_required")
    _validate_field_length("name_too_long", normalized_name, NAME_MAX_LENGTH)
    _validate_field_length("address_text_too_long", normalized_address, ADDRESS_MAX_LENGTH)

    # AC-02: категория сверяется здесь же. Pydantic делает то же для HTTP-входа,
    # но воркер и импорт идут мимо схем — без этой проверки `IntegrityError`
    # от `ck_places_category` дал бы `500`.
    try:
        normalized_category = str(PlaceCategory(category))
    except ValueError as error:
        # CR 3.1, патч 3: `code` константен, перечень — в `details`.
        raise PlaceFieldError(
            PLACE_CATEGORY_INVALID_CODE,
            {"allowed": [str(item) for item in PlaceCategory]},
        ) from error

    return normalized_name, normalized_address, normalized_category


class PlaceCursorError(Exception):
    """AC-04, AC-07: курсор не разбирается — `422 invalid_cursor`.

    Доменная ошибка, а не `InvalidCursorError` из кодека: HTTP-слой отображает
    ТОЛЬКО доменные ошибки (`_domain_error`), и пропущенный наверх
    `InvalidCursorError` дал бы `500` на пользовательском вводе. Код
    совпадает с кодеком (`invalid_cursor`) — клиент видит одну форму.
    """

    code = INVALID_CURSOR_CODE

    def __init__(self) -> None:
        super().__init__(INVALID_CURSOR_CODE)


class PlacePage(NamedTuple):
    """Task 4.1 (AC-01, AC-04): страница выдачи места.

    `next_cursor is None` — признак последней страницы (лишней строки не было).
    Отдельный тип, а не кортеж: перепутанные `items`/`total` в кортеже
    из трёх позиций дали бы `total` в теле ответа вместо списка.
    """

    items: list[Place]
    total: int
    next_cursor: str | None


class CommunitySettings(Protocol):
    """CR 3.1, патч 5: минимум настроек, нужный сервису.

    `Any` в сигнатуре отключал `mypy` на аргументе и пропускал строку:
    при `settings.city_slugs = "tomsk"` проверка `slug in ...`
    превращалась в поиск ПОДСТРОКИ (проверено: `"toms" in "tomsk"` → `True`),
    и место создавалось в несуществующем сообществе `toms`. Протокол ловит
    и это, и опечатку в имени атрибута.
    """

    city_slugs: tuple[str, ...]
    # Task 4.2 стори 3.3 (AC-01): слаг выдачи берётся из конфига, параметра
    # в запросе нет (D-3). Поле объявлено в протоколе, а не прочитано
    # из глобальных настроек внутри функции: иначе unit-тест зависел бы
    # от `.env` разработчика, а сервис получил бы второй источник среза.
    default_city_slug: str


async def create_place(
    session: AsyncSession,
    *,
    name: str,
    category: PlaceCategory | str,
    address_text: str,
    city_slug: str,
    settings: CommunitySettings | None = None,
) -> Place:
    """AC-01…AC-04: создаёт место, соблюдая белый список и запрет дубля.

    Порядок шагов намеренный:

    1. Нормализация `name`/`address_text` ДО любых проверок. Иначе дубль
       искался бы по сырому значению: `'Кафе „Прага“ '` не совпал бы
       с сохранённым `'Кафе „Прага“'`, проверка пропустила бы вставку,
       и `uq_places_city_name` отбил бы её уже как `IntegrityError`.
    2. Сверка сообщества с конфигом (AC-03). Неизвестный слаг — ошибка,
       а не «создалось, но не видно».
    3. Проверка пустоты: строка из ZWSP имеет длину 1 и прошла бы
       `min_length`, а `ck_places_name` отверг её как `500` на вводе.
    4. Дешёвый путь проверки дубля, затем вставка внутри SAVEPOINT.
       `begin_nested()` обязателен: после `IntegrityError` сессия непригодна
       до отката (`current transaction is aborted`), и следующий запрос
       в той же транзакции упал бы. Тестовый `conftest` использует
       `join_transaction_mode="create_savepoint"` — это спасает изоляцию
       теста, но не отменяет требования к сервису.

    `settings` передаётся аргументом (по умолчанию — глобальные настройки):
    так unit-тест не зависит от `.env` разработчика, а вызывающий слой
    может передать тестовый набор сообществ.
    """
    if settings is None:
        from app.core.config import settings as global_settings

        settings = global_settings

    # CR 3.1, патч 5b: рантайм-гард к типовому. `Protocol` ловит строку на
    # mypy, но вызывающий код может прийти из нетипизированного места, а `in`
    # по строке — это поиск ПОДСТРОКИ (проверено: `"toms" in "tomsk"` → True),
    # то есть опечатка создала бы место в несуществующем сообществе `toms`.
    if not isinstance(settings.city_slugs, (tuple, list)):
        raise TypeError(
            "settings.city_slugs должен быть последовательностью слагов, "
            f"получено {type(settings.city_slugs).__name__}"
        )

    # Task 3.2 стори 3.2: правила полей — один хелпер на создание и правку.
    # Вторая копия разошлась бы, и админ получал бы на `PATCH` другой код
    # ошибки, чем на `POST`, при одинаковом по сути вводе.
    normalized_name, normalized_address, normalized_category = _normalize_and_validate_fields(
        name=name, address_text=address_text, category=category
    )

    # AC-03: сверка с конфигом в момент вызова, а не при импорте: список
    # сообществ меняется деплоем `.env` без пересборки кода (ADR-01).
    if city_slug not in settings.city_slugs:
        raise PlaceUnknownCommunityError(city_slug)

    repository = PlaceRepository(session)

    # D-4: дешёвый путь ради понятной ошибки. Гарантию даёт уникальное
    # ограничение ниже: между этим `SELECT` и `INSERT` параллельная вставка
    # успевает создать свою строку (§18.3).
    #
    # Стори 3.2 (AC-03, D-6): возвращается СТРОКА, а не факт существования.
    # `uq_places_city_name` не включает `is_active` (R-02), поэтому скрытое
    # место держит имя. Отдавать на это `PLACE_DUPLICATE` нельзя: админ увидел бы
    # «такое место уже есть» про место, которого нет в выдаче, и не нашёл бы
    # единственного действия — «восстановить».
    conflicting = await repository.find_by_city_and_name(city_slug, normalized_name)
    if conflicting is not None:
        if conflicting.is_active:
            raise PlaceDuplicateError(normalized_name)
        raise PlaceArchivedError(conflicting.uuid)

    place = Place(
        name=normalized_name,
        category=normalized_category,
        address_text=normalized_address,
        city_slug=city_slug,
    )

    try:
        # Task 7.5: SAVEPOINT вокруг вставки. Без него провал уникальности
        # переводит транзакцию в состояние aborted, и вызывающий слой обязан
        # откатывать всю работу — даже если хотел продолжить (например,
        # создать место и карточку одной транзакцией).
        async with session.begin_nested():
            repository.add(place)
            await session.flush()
    except IntegrityError as error:
        if repository.is_duplicate_name_error(error):
            logger.info(
                "place_duplicate_race",
                city_slug=city_slug,
                name=normalized_name,
            )
            raise PlaceDuplicateError(normalized_name) from error
        # Чужое нарушение целостности (CHECK категории, шаблон слага) не
        # маскируется под дубль: админ искал бы несуществующее место вместо
        # кривого значения.
        raise

    return place


async def update_place(
    session: AsyncSession,
    *,
    place: Place,
    changes: dict[str, Any],
    settings: CommunitySettings | None = None,
) -> Place:
    """Task 3 (AC-05, AC-07): частичная правка места. `commit()` не вызывает.

    Порядок шагов намеренный:

    1. Пустой набор изменений — выход без записи. Пустой `PATCH` отбивает
       роутер кодом `place_nothing_to_update` (RISK-2); сервис не должен ни
       падать на нём, ни писать в БД «ничего».
    2. Нормализация и проверки переиспользуют `_normalize_and_validate_fields`
       от значений, которые ОСТАНУТСЯ в строке: неприсланное поле берётся
       из самого места. Иначе правка категории нормализовала бы `None`
       и падала бы на пустом имени.
    3. Поиск дубля — с исключением себя (`exists_in_city_excluding`).
       Без исключения правка любого поля нашла бы САМО место по его же имени
       и отказывала бы `409` на легальном вводе.
    4. Запись внутри SAVEPOINT: `IntegrityError` от `uq_places_city_name`
       в гонке переводит транзакцию в aborted, и вызывающий слой не смог бы
       продолжить работу в той же транзакции.

    Скрытое чужое место даёт `PlaceArchivedError` с `existing_uuid` (D-6):
    `PLACE_DUPLICATE` про место, которого админ не видит в выдаче, бесполезен.

    `city_slug` НЕ меняется (R-05): перенос между сообществами запрещён,
    поле отсутствует в `PlaceUpdate` и в наборе изменяемых полей.
    """
    mutable_fields = ("name", "category", "address_text", "is_active")
    provided = {field: value for field, value in changes.items() if field in mutable_fields}

    # CR 3.2, патч 2 (AC-05, AC-06, AC-07): `None` в текстовом поле разбирает
    # `_normalize_and_validate_fields` — единственная копия правила. Здесь
    # проверяется только то, чего хелпер не знает: `{"is_active": null}` —
    # тот же пустой PATCH (RISK-2).

    # `is_active` — единственное нестроковое поле: его не нормализуют и не
    # сверяют с белым списком, но и отбрасывать нельзя (D-7: восстановление
    # скрытого места идёт этим полем).
    new_is_active = provided.get("is_active")
    field_values = {field: value for field, value in provided.items() if field != "is_active"}
    # CR 3.2, патч 2 (RISK-2): `{"is_active": null}` — тот же «пустой PATCH»,
    # что и `{}`. До патча такой запрос давал `200 «сохранено»` без единой
    # правки, то есть обходил гард `place_nothing_to_update`. Пустой словарь
    # остаётся noop: что считать пустым запросом, решает роутер (его гард
    # отдаёт `place_nothing_to_update` ДО сервиса), а сервис не падает на `{}`.
    if "is_active" in provided and new_is_active is None and not field_values:
        raise PlaceFieldError(PLACE_NOTHING_TO_UPDATE_CODE)

    if not provided:
        # AC-05: правка без полей ничего не пишет и не ищет дубль по имени.
        return place

    if settings is None:
        from app.core.config import settings as global_settings

        settings = global_settings

    normalized_name, normalized_address, normalized_category = _normalize_and_validate_fields(
        name=field_values.get("name", place.name),
        address_text=field_values.get("address_text", place.address_text),
        category=field_values.get("category", place.category),
    )

    repository = PlaceRepository(session)

    # AC-05: дубль ищется только если имя реально меняется, и всегда без себя.
    conflicting = await repository.exists_in_city_excluding(
        place.city_slug, normalized_name, place.uuid
    )
    if conflicting is not None:
        if conflicting.is_active:
            raise PlaceDuplicateError(normalized_name)
        # D-6: скрытое место держит имя (R-02: `uq` без `WHERE is_active`),
        # поэтому админ получает не «придумай другое», а «восстанови это».
        raise PlaceArchivedError(conflicting.uuid)

    # Task 3.6 (AC-05): `updated_at` ставит `onupdate` миксина, а не код.
    # Ручная запись `datetime.now(UTC)` подавила бы механизм и разошлась бы
    # с остальными путями (долг DS 2.4: часы приложения против часов БД).

    # CR 3.2, патч 6 (AC-03): слаг захватывается ДО `begin_nested()`. После
    # отката SAVEPOINT объект экспирится, и обращение к `place.city_slug`
    # в `except`-ветке давало `MissingGreenlet` (ленивая подгрузка в async
    # вне greenlet-контекста) — то есть лог превращал `409 PLACE_DUPLICATE`
    # в `500`. Проверено исполнением: до патча гонка давала MissingGreenlet.
    place_city_slug = place.city_slug

    try:
        # CR 3.2, патч 3 (AC-03, AC-07): присваивание ВНУТРИ `begin_nested()`.
        # До патча `place.name = ...` выполнялось до входа в SAVEPOINT, и первый
        # же `flush` внутри `begin_nested()` (а также autoflush на SELECT
        # проверки дубля) отправлял `UPDATE places SET name=...` ВНЕ SAVEPOINT.
        # Тогда `IntegrityError` от `uq_places_city_name` в гонке откатывал
        # не SAVEPOINT, а всю транзакцию, и вместо `409 PLACE_DUPLICATE` сессия
        # отдавала `PendingRollbackError` → `500` (доказано исполнением на
        # живом PostgreSQL 18: `UPDATE` в логе до `SAVEPOINT sa_savepoint_N`).
        # `create_place` был защищён правильно — `repository.add()` уже внутри
        # SAVEPOINT; здесь собрана та же форма.
        async with session.begin_nested():
            place.name = normalized_name
            place.address_text = normalized_address
            place.category = normalized_category
            if new_is_active is not None:
                place.is_active = bool(new_is_active)
            await session.flush()
    except IntegrityError as error:
        if repository.is_duplicate_name_error(error):
            logger.info(
                "place_update_duplicate_race",
                city_slug=place_city_slug,
                name=normalized_name,
            )
            raise PlaceDuplicateError(normalized_name) from error
        # Чужое нарушение целостности (CHECK категории, переполнение) не
        # маскируется под дубль: админ искал бы несуществующее место.
        raise

    return place


async def deactivate_place(session: AsyncSession, *, place: Place) -> Place:
    """Task 4 (AC-04, R-04): скрытие места — всегда мягкое, идемпотентно.

    `DELETE /places/{uuid}` не удаляет строку физически и НЕ проверяет, где
    место используется. Причины (D-5, R-04):

    - FR68 говорит «скрыть», а не «удалить»; карточки эпика 4 ссылаются на
      `places.id` (`event_cards.place_id` появится в `0008`), и физическое
      удаление оставило бы висячий FK или потребовало бы `SET NULL`, что
      обнулило бы место у уже опубликованных карточек.
    - Проверка использования потребовала бы таблицы, которой ещё нет, и дала
      бы разное поведение до и после эпика 4 на одном и том же `DELETE`.
    - `session.delete` в коде нет ни на одном пути: `grep` по репозиторию
      пуст, и это зафиксировано тестом (`test_deactivate_sets_flag_and_keeps_row`),
      который читает строку из БД, а не только код ответа.

    Идемпотентность обязательна: клиент ретраит `DELETE` после обрыва сети,
    уже получив успешную запись. Ошибка на «уже скрыто» превратила бы
    безобидный ретрай в `409`/`404`, и админка показала бы сбой операции,
    которая на самом деле прошла.

    `commit()` не вызывается — единица работы у роутера (D-4 стори 2.2).
    """
    if place.is_active:
        place.is_active = False
        await session.flush()
    # Уже скрытое место: без `flush` — но и без ошибки (ретрай клиента).
    return place


async def list_places(
    session: AsyncSession,
    *,
    category: PlaceCategory | str | None = None,
    include_inactive: bool = False,
    cursor: str | None = None,
    limit: int = PLACES_PAGE_SIZE,
    settings: CommunitySettings | None = None,
) -> PlacePage:
    """AC-01…AC-05: страница активных мест сообщества пилота.

    Порядок шагов намеренный:

    1. Слаг берётся из `settings.default_city_slug` (D-3): параметра
       в контракте нет, и клиент не может запросить чужое сообщество. Значение
       сверяется с `city_slugs` — опечатка в `.env` иначе дала бы
       молча пустую ленту вместо ошибки конфигурации.
    2. Категория проходит белый список ДО запроса: `PlaceCategory` в схеме
       отбивает HTTP-вход, но воркер/импорт идут мимо Pydantic, и мусор
       в `category` дал бы пустую выдачу вместо внятного `422`.
    3. Курсор декодируется здесь и `InvalidCursorError` превращается
       в доменную `PlaceCursorError`: HTTP-слой отображает только доменные
       ошибки, и пропущенный наверх `InvalidCursorError` дал бы `500`.
    4. `next_cursor` собирается ТОЛЬКО при наличии лишней строки
       (`len(items) > limit`) и от последнего элемента УЖЕ УСЕЧЁННОЙ
       страницы: курсор от выброшенной строки сдвинул бы границу и потерял
       бы её.

    `commit()` не вызывается — чтение (D-4 стори 2.2).
    """
    if settings is None:
        from app.core.config import settings as global_settings

        settings = global_settings

    # CR 3.1, патч 5b (повтор для выдачи): `in` по строке — поиск ПОДСТРОКИ,
    # и `city_slugs = "tomsk"` пропустил бы слаг `toms`. Протокол ловит
    # это на mypy, рантайм-гард — на нетипизированном вызове.
    if not isinstance(settings.city_slugs, (tuple, list)):
        raise TypeError(
            "settings.city_slugs должен быть последовательностью слагов, "
            f"получено {type(settings.city_slugs).__name__}"
        )

    city_slug = settings.default_city_slug
    if city_slug not in settings.city_slugs:
        # Конфиг обязан падать на старте (`_validate_city`), но сервис
        # не полагается на инвариант другой подсистемы: иначе выдача молча
        # пуста, и «в сообществе нет мест» неотличимо от «слаг не тот».
        raise PlaceUnknownCommunityError(city_slug)

    normalized_category: str | None = None
    if category is not None:
        try:
            normalized_category = str(PlaceCategory(category))
        except ValueError as error:
            # AC-02: тот же код и тот же перечень, что в 3.2 — клиент ветвится
            # по константному `code`, а `details.allowed` даёт варианты.
            raise PlaceFieldError(
                PLACE_CATEGORY_INVALID_CODE,
                {"allowed": [str(item) for item in PlaceCategory]},
            ) from error

    after: tuple[Any, Any] | None = None
    if cursor is not None:
        try:
            after = decode_place_cursor(cursor)
        except InvalidCursorError as error:
            # AC-04, AC-07: битый курсор — `422`, не `500` и не тихая первая
            # страница. Доменная ошибка вместо ошибки кодека: HTTP-слой
            # отображает только доменные (см. `_domain_error`).
            raise PlaceCursorError() from error

    items, total = await PlaceRepository(session).list_page(
        city_slug=city_slug,
        category=normalized_category,
        include_inactive=include_inactive,
        after=after,
        limit=limit,
    )

    # AC-04: лишняя строка — признак «есть дальше». Она отбрасывается ДО
    # сборки курсора: курсор от неё указывал бы на строку, которой клиент
    # не видел, и следующая страница начиналась бы после неё (потеря строки).
    #
    # CR 3.3, патч 4: сравнивать надо с ЭФФЕКТИВНЫМ пределом, который применил
    # репозиторий (`min(limit, MAX_PLACES_PAGE_SIZE)`), а не с исходным `limit`.
    # Иначе `limit=150` из воркера/импорта: репозиторий берёт `LIMIT 101`,
    # а `len(items)=101 < 150` — лишняя строка НЕ отбрасывается, `next_cursor`
    # остаётся `None`, и клиент молча теряет остаток выдачи при `total > 101`.
    effective_limit = min(limit, MAX_PLACES_PAGE_SIZE)
    next_cursor: str | None = None
    if len(items) > effective_limit:
        bounded = items[:effective_limit]
        last = bounded[-1]
        next_cursor = encode_place_cursor(last.created_at, last.uuid)
        items = bounded

    return PlacePage(items=items, total=total, next_cursor=next_cursor)
