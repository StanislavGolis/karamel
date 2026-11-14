"""Схемы карточки события (стори 4.1, Task 4.1–4.6; AC-02, AC-03).

AC-02 (FR69, ADR-16): `starts_at` принимается ТОЛЬКО с зоной. «Наивная»
строка (`2026-11-01T19:00:00`) отклоняется `422`, а не трактуется как
локальное время сервера: сервер живёт в UTC, автор — в Томске (+07:00),
и молчаливая подстановка сдвинула бы событие на семь часов.
Конвертация в UTC выполняется ЗДЕСЬ и ровно один раз: в БД лежит
`timestamptz`, и повторное приведение в сервисе или воркере дало бы
второй источник правды для одного значения.

AC-03 (FR70): при `capacity = 1` окно решения ОБЯЗАТЕЛЬНО — это режим
заявок, где окно и есть смысл карточки («решу до такого-то часа»).
При 2–6 окно не запрашивается: 4.2 подставит дефолт из конфига
(`Settings.card_default_decision_window_minutes`, OQ-1: 2880 = 48 ч).

Серверные поля (`status`, `verification_status`, `filled_count`,
`city_slug`, `author_id`, `expires_at`, `uuid`) в схему НЕ входят:
их задаёт сервер, а не клиент. `extra="forbid"` превращает попытку
их передать в `422`, а не в молчаливый выброс — иначе клиент считал бы,
что опубликовал карточку, а она осталась черновиком.

`EventCardRead` здесь НЕ создаётся: схема ответа принадлежит 4.5 (лента),
а частичная схема стала бы четвёртым представлением одной сущности.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    ACTIVITY_INPUT_VALUES,
    Activity,
    CardKind,
    CardVerificationStatus,
    EventCardStatus,
    Gender,
    PlaceCategory,
)
from app.models.event_card import (
    CARD_MAX_CAPACITY,
    CARD_MAX_DECISION_WINDOW_MINUTES,
    CARD_MIN_CAPACITY,
    CARD_MIN_DECISION_WINDOW_MINUTES,
    CARD_REQUEST_MODE_CAPACITY,
)
from app.services.age_service import AGE_MAX, AGE_MIN
from app.services.profile_normalize import normalize_bio, normalize_text

# AC-01: пределы совпадают с шириной колонок `event_cards`. Проверка на схеме,
# а не только в БД: иначе слишком длинное значение падало бы `DBAPIError`-ом
# (`500`) на валидном для формы вводе.
TITLE_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 2000


def reject_legacy_activity(value: object) -> object:
    """AC-3 стори 12.4 (FR118): легаси `other` на вход не принимается.

    `Activity.OTHER` существует в БД (бэкфилл миграции `0037`) и в CHECK
    модели, но автор не может его выбрать: `ACTIVITY_INPUT_VALUES` содержит
    ровно шесть значений FR118. Без этой проверки `other` стал бы седьмой
    активностью через API, и «неизвестная активность» превратилась бы
    в выбираемый смысл встречи.

    Живёт функцией, а не двумя копиями валидаторов: `EventCardCreate` и
    `EventCardUpdate` — два входа в одно поле, и разошедшиеся копии дали бы
    дыру через `PATCH` (класс `W-11.5-c`).

    `object`, а не `Activity`: в `mode="before"` Pydantic отдаёт сырое
    значение (в том числе `None` при «не передан»), и подмена типа сломала
    бы собственные проверки Pydantic.
    """
    if value is None:
        return value
    if str(value) not in {str(item) for item in ACTIVITY_INPUT_VALUES}:
        raise ValueError("activity_other_reserved")
    return value


def _normalize(value: object) -> object:
    """Нормализация однострочного значения до проверки длины.

    `object`, а не `str`: в `mode="before"` Pydantic отдаёт сюда что угодно
    (включая `None`), и подмена типа сломала бы собственные проверки
    Pydantic — он должен сам сообщить «ожидалась строка».
    """
    if not isinstance(value, str):
        return value
    return normalize_text(value)


class EventCardCreate(BaseModel):
    """AC-01, AC-02, AC-03: вход создания карточки (подключит роутер 4.2)."""

    # CR 3.1, патч 6 (перенесено на карточку): лишние поля отклоняются,
    # а не отбрасываются молча.
    model_config = ConfigDict(extra="forbid")

    # AC-7 стори 13.2 (FR130, ADR-26): заголовок не вводится автором — его
    # собирает сервер из активности и места. Поле ОСТАЛОСЬ в модели, потому
    # что легаси-карточки с `activity='other'` приходят с заголовком от
    # клиента (AC-6), но для остальных активностей передача `title` — ошибка.
    # Отвергает её валидатор `_reject_author_title` ниже, а не `extra="forbid"`:
    # `forbid` запретил бы и легаси-путь.
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_LENGTH)
    # AC-01: описание необязательно. `None`, а не `""` — project-context §4:
    # пустое значение передаётся как `null`.
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    # AC-06, ADR-12: enum-тип, не `str` с проверкой после — иначе мусор
    # из формы дошёл бы до CHECK в БД и стал `500` вместо `422`.
    category: PlaceCategory
    # AC-04, FR98: место адресуется публичным `uuid`, не внутренним `id` (D-2).
    place_uuid: uuid_lib.UUID
    # AC-02, ADR-16: значение с зоной, конвертация ниже.
    starts_at: datetime
    # AC-03 стори 12.4 (FR117, ADR-20): тип встречи выбирается явно и
    # обязательно. `CardKind`, а не `str`: мусор отбивает Pydantic (`422`),
    # а не CHECK в БД (`500`).
    kind: CardKind
    # AC-03 стори 12.4 (FR118): активность из белого списка FR118.
    # Валидатор `_reject_legacy_activity` ниже запрещает легаси `other`.
    activity: Activity
    # AC-2 стори 12.6 (FR119, ADR-19): рамки аудитории — МЯГКИЙ фильтр.
    # Все три необязательны: карточка без рамок законна («ищу любого»).
    #
    # `Gender`, а не `str`: мусор отбивает Pydantic (`422`), а не CHECK в БД
    # (`500`). Границы 18..99 — те же `AGE_MIN`/`AGE_MAX`, что у профиля
    # (`age_service`): рамка «ищу от 17» отсекла бы всех, кто вообще может
    # зарегистрироваться, и выглядела бы как рабочая настройка.
    gender_filter: Gender | None = None
    age_min: int | None = Field(default=None, ge=AGE_MIN, le=AGE_MAX)
    age_max: int | None = Field(default=None, ge=AGE_MIN, le=AGE_MAX)
    # AC-01, FR70: 1..6. Обязательное: `W-11.5-c` — умолчание `2` уводило бы
    # забытое поле в открытый набор, и пользователь `capacity=1` не смог бы
    # подать заявку. Тест-замок: `test_card_kind_activity_schemas.py`.
    capacity: int = Field(ge=CARD_MIN_CAPACITY, le=CARD_MAX_CAPACITY)
    # AC-01, FR79: окно решения в минутах; для режима заявок обязательно.
    decision_window_minutes: int | None = Field(
        default=None,
        ge=CARD_MIN_DECISION_WINDOW_MINUTES,
        le=CARD_MAX_DECISION_WINDOW_MINUTES,
    )

    @field_validator("activity", mode="before")
    @classmethod
    def _no_legacy_activity(cls, value: object) -> object:
        """AC-3 стори 12.4: `other` — легаси БД, не выбор автора."""
        return reject_legacy_activity(value)

    @model_validator(mode="after")
    def _reject_author_title(self) -> EventCardCreate:
        """AC-7 стори 13.2 (FR130, ADR-26): автор не присылает заголовок.

        **Почему валидатор, а не `extra="forbid"`.** `extra="forbid"` не
        сработает: поле `title` объявлено в модели, значит оно «известное».
        Убрать объявление нельзя — легаси-карточки с `activity='other'`
        приходят с заголовком (AC-6), и сервис читает `payload.title`
        единообразно. Поэтому запрет выражен явно: значение, отличное от
        `None`, — ошибка `422`, а не тихий выброс.

        **Почему это не ломает легаси.** `_no_legacy_activity` выше отвергает
        `Activity.OTHER` на этом же входе, значит легаси-ветка через HTTP
        недостижима вовсе. Легаси-строки попадают в БД мимо Pydantic (импорт,
        сырой `INSERT` воркера) и через этот валидатор не проходят.
        """
        # Отвергается ФАКТ передачи поля, а не значение: `title: null` — тоже
        # попытка управлять полем, и клиент должен узнать о запрете, а не
        # получить молчаливый выброс с пустым заголовком на сервере.
        if "title" in self.model_fields_set:
            raise ValueError(
                "title_not_accepted: заголовок генерируется сервером из активности "
                "и места (FR130). Автор его не вводит."
            )
        return self

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: object) -> object:
        """AC-01: нормализация тем же `normalize_text`, что у анкеты и места.

        Без неё `'   '` прошло бы `min_length=1` (три символа), затем
        нормализовалось в сервисе до пустой строки, и карточка с пустым
        заголовком попала бы в ленту. NBSP и ZWSP из копипаста `strip()`
        не режет — тот же класс дефекта, что в 2.3 и 3.1.

        Только `title`: заголовок однострочный, и перенос строки в нём — шум.
        """
        return _normalize(value)

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: object) -> object:
        """CR 4.1, патч 1 (AC-01): описание многострочное — абзацы сохраняются.

        Дефект, найденный код-ревью на исполнении: `normalize_text`
        схлопывает ВСЕ пробельные последовательности в один пробел, включая
        `\\n`. Описание `'Первая строка\\n\\nВторой абзац'` превращалось
        в `'Первая строка Второй абзац'` — автор видел в ленте слипшийся
        текст, а в БД лежала одна строка. Воспроизведено: `repr()` после
        валидации не содержал `\\n`.

        Для многострочного текста в проекте уже есть правильная функция —
        `normalize_bio` (2.3, поле `profiles.bio`): она схлопывает только
        ГОРИЗОНТАЛЬНЫЕ пробелы, а переводы строк оставляет смыслом.
        `description` — тот же класс поля (свободный текст в `Text`), поэтому
        нормализатор берётся тот же, а не копируется новый.

        Пустая строка после нормализации → `None` (project-context §4:
        «пусто передаётся как `null`»). Иначе `''` попал бы в `Text`
        колонку, и клиент не смог бы отличить «описания нет» от «описание
        пустое» — при том что в ответе 4.5 оба случая обязаны выглядеть
        одинаково (`null`).
        """
        if not isinstance(value, str):
            return value
        normalized = normalize_bio(value)
        return normalized or None

    @field_validator("starts_at", mode="after")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """AC-02, ADR-16: только aware-значение, конвертация в UTC один раз.

        `tzinfo is None` — «наивная» строка без зоны: отклонить, а не
        подставлять UTC. Молчаливая подстановка сдвинула бы событие на
        разницу поясов автора, и это выяснилось бы только на встрече.

        `utcoffset() is None` проверяется отдельно: `tzinfo` может быть
        объектом, чей `utcoffset()` возвращает `None` (вырожденная зона),
        и такой `datetime` для PostgreSQL неотличим от наивного.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "starts_at требует часовой пояс: передайте ISO-8601 с зоной, "
                "например 2026-11-01T19:00:00+07:00"
            )
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _reject_inverted_age_range(self) -> EventCardCreate:
        """AC-6.4 стори 12.6: `age_min > age_max` — `422`, а не `500` из БД.

        Без этой проверки перевёрнутая пара доходила бы до CHECK
        `ck_event_cards_age_range` и превращалась в `IntegrityError` → `500`
        (класс дефекта, найденный в 12.5: `CardStartsAtTooFarError` ловился
        в роутере, но ветки не было). Pydantic отдаёт `422` с машинным кодом
        `age_range_invalid` — клиент показывает текст у поля, а не «ошибка
        сервера».

        Проверка только когда заданы ОБЕ границы: полуоткрытые пары легальны
        (AC-1a), и `age_min = 30, age_max = None` — не ошибка.
        """
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            raise ValueError("age_range_invalid")
        return self

    @model_validator(mode="after")
    def _require_decision_window_for_request_mode(self) -> EventCardCreate:
        """AC-03, FR70/FR79: у режима заявок окно решения обязательно.

        При `capacity = 1` карточка спрашивает «возьму одного»: без окна
        заявка висела бы бессрочно, и автор не знал бы, когда выбирать.

        При 2–6 окно не запрашивается (FR70) — дефолт подставит 4.2 из
        `Settings.card_default_decision_window_minutes`. Проверка «обязательно»
        живёт здесь, а не в модели БД: это правило формы, а не инвариант
        строки — карточка открытого набора с явно заданным окном легальна.
        """
        if self.capacity == CARD_REQUEST_MODE_CAPACITY and self.decision_window_minutes is None:
            raise ValueError("decision_window_minutes обязателен при capacity=1 (режим заявок)")
        return self


class EventCardRef(BaseModel):
    """AC-08 (X-13 вариант a): минимальный ответ на создание, правку, публикацию.

    Почему не полный `EventCardRead`: форма ответа §7.2 архитектуры требует
    `place` и `author` — два дополнительных запроса (`places`, `profiles`)
    на создание карточки, которую автор ещё не видит в ленте. Лента (4.5)
    всё равно расширит форму (`filled_count`, `chat_uuid`), то есть контракт
    менялся бы дважды. Поэтому здесь только то, что нужно клиенту сразу:
    идентификатор для навигации и состояние проверки (NFR51).

    **Для 4.5:** `EventCardRef` переиспользуется как вложенный объект,
    расширение — добавлением полей, существующие не меняются.

    `from_attributes=True` — ответ собирается прямо из ORM-объекта: сервис
    возвращает `EventCard`, роутер не переписывает поля вручную.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    # AC-01: состояние публикации. Клиент по нему ветвится (draft/published).
    status: EventCardStatus
    # AC-06, NFR51: исход проверки контента — «проверяем текст» или «опубликована».
    verification_status: CardVerificationStatus
    # AC-01: момент создания. Нужен, чтобы клиент мог показать «создано только что»
    # без дополнительного запроса, которого в 4.2 нет (лента — 4.5).
    created_at: datetime


class EventCardJoinResult(EventCardRef):
    """AC-01, AC-02 (стори 5а.5): ответ на мгновенное присоединение.

    **X-13, D-5а.5-1: НАСЛЕДОВАНИЕ `EventCardRef`, не копия.** Стори 4.1
    зафиксировала «расширяется только добавлением полей, существующие не
    меняются». Копия четырёх полей разошлась бы при первой правке, а
    расширение самого `EventCardRef` изменило бы контракты 4.2/4.3/4.6
    (`POST`/`PATCH`/`publish`/`close`/`cancel` отдают тот же класс) — те
    маршруты не считают места и не должны обещать их в ответе.

    Стори 5а.5 предполагала, что `EventCardRef` уже несёт `filled_count`,
    `capacity`, `chat_uuid`; фактически их там нет (4.1/4.2 оставили класс
    минимальным). Отклонение зафиксировано в Dev Agent Record.

    `chat_uuid` присутствует как `null` (D-4.5-7): чат — эпик 6, но клиент
    не должен переписываться дважды.
    """

    # AC-01: занятые места после присоединения — из `RETURNING` (D-5а.5-7).
    filled_count: int
    # AC-02: `capacity` рядом с `filled_count`: клиент не вычитает сам
    # (второй источник правды разошёлся бы с сервером, урок 4.5).
    capacity: int
    # AC-01, AC-02: `published` или `full` — статус после того же `UPDATE`.
    # В `EventCardRef.status` поле уже объявлено, здесь только пояснение.
    # AC-14: `null` до эпика 6 — поле есть в контракте с 4.5.
    chat_uuid: uuid_lib.UUID | None = None


class EventCardUpdate(BaseModel):
    """AC-05: правка черновика — только переданные поля.

    Все поля `| None` и по умолчанию `None` — это и есть «меняются только
    переданные»: `title=None` означает «не трогать», а не «очистить». Очистка
    `description` выражается явным `description=""` → нормализуется в `None`
    (валидатор `_strip_description` выше).

    Серверных полей здесь НЕТ (`status`, `verification_status`, `filled_count`,
    `city_slug`, `author_id`, `expires_at`, `uuid`): их задаёт сервер.
    `extra="forbid"` превращает попытку передать их в `422`, а не в молчаливый
    выброс — иначе клиент считал бы, что опубликовал карточку.

    `capacity` менять можно (AC-05): режим определяется вместимостью (FR70),
    и смена режима — легальное действие до публикации.
    """

    model_config = ConfigDict(extra="forbid")

    # AC-7 стори 13.2 (FR130, ADR-26): поля `title` здесь НЕТ — заголовок не
    # редактируется автором, он следует за активностью и местом. Поле удалено
    # из модели, а не «принимается и игнорируется»: `extra="forbid"` даёт
    # `422` на `PATCH {"title": ...}`, и клиент узнаёт, что действие
    # запрещено, вместо тихой потери правки.
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    category: PlaceCategory | None = None
    place_uuid: uuid_lib.UUID | None = None
    starts_at: datetime | None = None
    # AC-3 стори 12.4: тип и активность меняются до публикации.
    # `| None` = «не передан, не трогать» (общая семантика PATCH).
    kind: CardKind | None = None
    activity: Activity | None = None
    # AC-2 стори 12.6: рамки аудитории. `| None = None` — «не передано, не
    # трогать»: снять рамку = передать `null` явно, и PATCH-семантика это
    # различает через `model_fields_set` в сервисе.
    gender_filter: Gender | None = None
    age_min: int | None = Field(default=None, ge=AGE_MIN, le=AGE_MAX)
    age_max: int | None = Field(default=None, ge=AGE_MIN, le=AGE_MAX)
    capacity: int | None = Field(default=None, ge=CARD_MIN_CAPACITY, le=CARD_MAX_CAPACITY)
    decision_window_minutes: int | None = Field(
        default=None,
        ge=CARD_MIN_DECISION_WINDOW_MINUTES,
        le=CARD_MAX_DECISION_WINDOW_MINUTES,
    )

    @field_validator("activity", mode="before")
    @classmethod
    def _no_legacy_activity(cls, value: object) -> object:
        """AC-3 стори 12.4: `PATCH` — второй вход, запрет `other` тот же."""
        return reject_legacy_activity(value)

    @model_validator(mode="after")
    def _reject_inverted_age_range(self) -> EventCardUpdate:
        """AC-6.4 стори 12.6: перевёрнутая пара — `422`, а не `500` из CHECK.

        Проверяется только когда ОБЕ границы переданы явно: `PATCH {"age_min":
        40}` без `age_max` может оказаться перевёрнутым относительно текущего
        значения в БД, но это решает сервис на смеси payload и строки —
        схеме строка недоступна. Здесь отбивается самоочевидный случай, когда
        оба значения пришли одним запросом.
        """
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            raise ValueError("age_range_invalid")
        return self

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: object) -> object:
        """AC-08 (X-19, стори 4.6): пустая строка — команда стереть, `null` — «не трогать».

        Отличие от `EventCardCreate` намеренное: там пустой текст означает
        «описания нет» и приводится к `None`, потому что другого состояния
        у новой карточки быть не может.

        Здесь `None` уже занят смыслом «поле не передано» (все поля `Update`
        необязательны). Если бы пустая строка тоже становилась `None`, стереть
        описание стало бы невозможно в принципе: `PATCH {"description": ""}`
        и `PATCH {}` давали бы одинаковый результат (дефект X-19).

        Поэтому `""` сохраняется как `""`, а `None` — как `None`; различие
        доводит сервис по `model_fields_set` (`"description" in ...`), то есть
        по ФАКТУ передачи поля, а не по значению. Проверка на `None` в сервисе
        отсекает явный `null`: он означает «не трогать» (AC-08).
        """
        if not isinstance(value, str):
            return value
        # `normalize_bio`, не `normalize_text`: абзацы сохраняются (AC-05).
        return normalize_bio(value)

    @field_validator("starts_at", mode="after")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        """AC-03, ADR-16: `None` допустим («не менять»), значение — только с зоной."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "starts_at требует часовой пояс: передайте ISO-8601 с зоной, "
                "например 2026-11-01T19:00:00+07:00"
            )
        return value.astimezone(UTC)


class EventCardPlace(BaseModel):
    """AC-06, §7.2: место глазами ленты — ровно три поля.

    `address_text`, а не координаты: местоположение карточки — ссылка на
    проверенное место (NFR34), а `place_id` (внутренний bigint) не покидает
    сервер (D-2).
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str
    address_text: str


class EventCardAuthor(BaseModel):
    """AC-06, §7.2: автор карточки в ленте — `uuid`, имя, возраст.

    `name`/`age` необязательны: анкеты может не быть (участник не прошёл
    онбординг; модератор её не заполняет) — тот же `LEFT JOIN profiles`,
    что в очереди модерации 4.3. Пустая анкета не должна ломать ленту: без
    автора карточка всё равно полезна («есть встреча, есть место, есть время»).
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str | None = None
    age: int | None = None


class EventCardAuthorProfile(BaseModel):
    """AC-2, AC-4, AC-5 стори 12.7 (FR120, П11): витрина анкеты автора карточки.

    ЕДИНСТВЕННАЯ точка чтения чужой анкеты (D-12.7-1): постоянной витрины
    `/profiles/{uuid}` нет намеренно (ADR-22), а `GET /cards/{uuid}` на
    архиве отдаёт `200` (FR73, замок `test_cards_state_api.py:492-512`).
    Анкета живёт на под-маршруте карточки, поэтому доступ к ней можно
    закрыть вместе с ней.

    **`birth_date` здесь отсутствует и появиться не может** (AC-2, замок
    12.3 `SENSITIVE_FIELDS`): наружу уходит только `age` — вычисленный
    `compute_age` в сервисе. Комментарий не декоративный: поле добавляется
    в схему, а не в SQL, поэтому гард 12.3 его не поймает — ловит замок
    AC-2 в `test_card_author_profile_api.py`.

    Все поля, кроме `uuid`, допускают `null`: анкеты может не быть вовсе
    (участник не прошёл онбординг), и пустая анкета не должна ломать экран
    карточки — тот же довод, что у `EventCardAuthor`.

    `photo_url`/`photo_expires_in` — пара: ссылка есть ровно тогда, когда
    есть срок (D-5a.4-6, Task 3.3). Фото живёт ТОЛЬКО здесь, не в
    `EventCardAuthor` (D-12.7-5): иначе лента подписывала бы URL на каждый
    элемент страницы (N+1 подписей).
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str | None = None
    # AC-2: вычисленный возраст. `birth_date` не отдаётся ни при каких
    # значениях (12.3, NFR38).
    age: int | None = None
    city: str | None = None
    bio: str | None = None
    # AC-5 (NFR23, NFR34): подписанный URL, в БД ссылки нет.
    photo_url: str | None = None
    # AC-5, D-12.7-4: ОДНО число — и `ExpiresIn` подписи, и срок для клиента.
    photo_expires_in: int | None = None


class EventCardRead(EventCardRef):
    """AC-06 (§7.2, NFR51): полная карточка для ленты и детального экрана.

    **X-13, D-4.5-1: НАСЛЕДОВАНИЕ `EventCardRef`, не копия.** Стори 4.1
    зафиксировала «расширяется только добавлением полей, существующие не
    меняются»; копия четырёх полей разошлась бы при первой же правке
    (одно представление начало бы отдавать `status` строкой одного набора,
    другое — другого). Наследование делает переопределение невозможным
    физически.

    Следствие: `created_at` присутствует в ленте. Это осознанно (см. 4.1):
    клиент показывает «создано только что» без второго запроса.

    Чего здесь НЕТ и почему:
    - `place_id`, `author_id`, `verified_by` — внутренние bigint (D-2);
    - `rejection_reason` — это сообщение автору на его экране (4.3), а не
      поле публичной ленты: чужим людям причина отклонения не показывается;
    - `decision_window_minutes` — окно уже выражено в `expires_at` (NFR14:
      источник истины — серверное время), второе представление того же
      факта разошлось бы при правке формулы ADR-02.
    """

    title: str
    # AC-01 стори 4.1: `null`, не `""` — project-context §4.
    description: str | None = None
    category: PlaceCategory
    # AC-7 стори 12.4 (FR117, FR118): тип и активность видны в ленте до
    # открытия карточки — «смысл встречи» перестаёт быть скрытым.
    # Обязательные, потому что в БД они `NOT NULL`.
    kind: CardKind
    activity: Activity
    # AC-2a стори 12.6 (FR119, ADR-19): рамки аудитории видны ДО подачи —
    # автор указал, кого ищет, и участник вправе это знать, прежде чем
    # подавать заявку. Поля обязательные (без умолчаний): NULL — законное
    # значение («рамка не задана»), и умолчание маскировало бы забытую
    # передачу в сборке ответа.
    #
    # Пометки «ты вне рамок» в карточке НЕТ намеренно (ADR-19 «Отвергнуто
    # (в)»): рамки — констатация автора, а не оценка участника. Тон —
    # констатация (UX §1052), не приглашение и не отказ.
    gender_filter: Gender | None = None
    age_min: int | None = None
    age_max: int | None = None
    # AC-01: `timestamptz` из БД уже в UTC; сериализуется с `Z`.
    starts_at: datetime
    # AC-01, NFR14: момент закрытия окна решения — источник для `CountdownBadge`.
    expires_at: datetime
    # AC-04 стори 5б.1 (NFR14): серверное «сейчас» на момент формирования ответа.
    # Клиент считает остаток как `expires_at - server_time`, а не по локальным
    # часам: часы устройства могут врать на минуты, и «осталось 0 минут» у
    # одного участника против «12 минут» у другого разрушали бы доверие.
    # Второе представление `expires_at` не вводится: `decision_window_minutes`
    # остаётся скрытым (см. docstring класса), а `server_time` — якорь, не окно.
    server_time: datetime
    capacity: int
    # AC-04: занятые места для `SeatsIndicator`; `has_seats` считается
    # на сервере, клиент не вычитает сам (второй источник правды).
    filled_count: int
    # AC-06: вложенные объекты вместо плоских `place_name`/`author_name`:
    # §7.2 задаёт форму, и один объект дешевле шести полей с префиксами.
    # AC-08 стори 5а.4 (X-36, FR68, R-04): место СКРЫТО модератором
    # (`places.is_active=false`) → `null`, а не «место с пустым именем».
    # Карточка при этом остаётся доступной по прямой ссылке: заявки и
    # участники живы, и действие модератора не отнимает данные у автора.
    # `None`, не `""` — project-context §4. В ленту такая карточка не
    # попадает вовсе (фильтр в билдере): витрина не показывает встречу,
    # на которую нельзя прийти.
    place: EventCardPlace | None
    author: EventCardAuthor
    # AC-06, D-4.5-7: поле есть в контракте с 4.5, значение `null` до эпика 6.
    # Иначе клиент переписывался бы дважды (сначала без поля, потом с ним).
    # AC-01 стори 5б.2 (D-5b.2-1): заполняется в момент выбора автора.
    chat_uuid: uuid_lib.UUID | None = None
    # AC-09 стори 5б.2 (D-5b.2-7, FR104): признак ПРО ЗРИТЕЛЯ, не про карточку.
    # `true` — этот человек в составе компании (`selected`), и клиент рисует
    # «Отменить участие» вместо «Присоединиться». Поле обязано быть в контракте:
    # без него UI не отличит участника от наблюдателя, и FR104 недостижим.
    #
    # Почему НЕ выводится из других полей: `filled_count` говорит «сколько
    # мест занято», а не «занял ли одно из них я». Вычисление на клиенте
    # потребовало бы списка участников, которого в ленте нет и не будет
    # (FR81: состав компании виден только её членам).
    #
    # Значение по умолчанию `False`, а не `None`: отсутствие участия —
    # легальное состояние наблюдателя, а не «неизвестно». Третье значение
    # заставило бы клиент ветвиться на неопределённости.
    viewer_participates: bool = False
    # AC-2, AC-5 стори 11.5 (FR82): признак «МОЯ ЗАЯВКА» — на карточке режима
    # заявок (`capacity = 1`) зритель подал заявку, и она ещё `pending`.
    #
    # Почему не выводится из других полей: `viewer_participates` отвечает на
    # вопрос «я в компании», а при `capacity = 1` до выбора автора это ВСЕГДА
    # `false` — заявитель не участник. `filled_count` говорит «сколько мест
    # занято», `status` — состояние карточки: у `published` бывает и живая, и
    # истёкшая заявка. Список заявившихся участнику недоступен (FR81).
    #
    # `False`/`None` — легальные состояния наблюдателя и автора, а не
    # «неизвестно»: третье значение заставило бы клиент ветвиться на
    # неопределённости там, где ответ известен.
    viewer_requested: bool = False
    # AC-5 (FR82): `uuid` нужен, потому что отмена идёт по `DELETE
    # /requests/{uuid}`, а не по `card_uuid`. Второе представление одного
    # факта не вводится: `viewer_requested` — булев итог, `uuid` — адрес
    # действия; оба приходят из одной строки `join_requests`.
    viewer_request_uuid: uuid_lib.UUID | None = None


class EventCardFeedResponse(BaseModel):
    """AC-06, AC-08, project-context §4: конверт выдачи `{items, total, cursor}`.

    `total` — «сколько всего под фильтром», не «сколько осталось» (D-4.5-9):
    считается отдельным `COUNT` без курсора, поэтому листание его не меняет.

    `cursor` — `null` в конце выдачи и непрозрачная строка иначе. Клиент не
    разбирает его содержимое: формат — деталь сервера (AC-08).

    Расхождение `total` со страницей в `READ COMMITTED` допустимо (AC-07,
    X-07): параллельная публикация между двумя `execute` меняет счётчик, но
    не границы страниц — курсор строится от `items`.
    """

    items: list[EventCardRead]
    total: int
    cursor: str | None = None
