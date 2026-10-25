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
`community_slug`, `author_id`, `expires_at`, `uuid`) в схему НЕ входят:
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

from app.models.enums import CardVerificationStatus, EventCardStatus, PlaceCategory
from app.models.event_card import (
    CARD_MAX_CAPACITY,
    CARD_MAX_DECISION_WINDOW_MINUTES,
    CARD_MIN_CAPACITY,
    CARD_MIN_DECISION_WINDOW_MINUTES,
    CARD_REQUEST_MODE_CAPACITY,
)
from app.services.profile_normalize import normalize_bio, normalize_text

# AC-01: пределы совпадают с шириной колонок `event_cards`. Проверка на схеме,
# а не только в БД: иначе слишком длинное значение падало бы `DBAPIError`-ом
# (`500`) на валидном для формы вводе.
TITLE_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 2000


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

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
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
    # AC-01, FR70: 1..6.
    capacity: int = Field(ge=CARD_MIN_CAPACITY, le=CARD_MAX_CAPACITY)
    # AC-01, FR79: окно решения в минутах; для режима заявок обязательно.
    decision_window_minutes: int | None = Field(
        default=None,
        ge=CARD_MIN_DECISION_WINDOW_MINUTES,
        le=CARD_MAX_DECISION_WINDOW_MINUTES,
    )

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
    `community_slug`, `author_id`, `expires_at`, `uuid`): их задаёт сервер.
    `extra="forbid"` превращает попытку передать их в `422`, а не в молчаливый
    выброс — иначе клиент считал бы, что опубликовал карточку.

    `capacity` менять можно (AC-05): режим определяется вместимостью (FR70),
    и смена режима — легальное действие до публикации.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    category: PlaceCategory | None = None
    place_uuid: uuid_lib.UUID | None = None
    starts_at: datetime | None = None
    capacity: int | None = Field(default=None, ge=CARD_MIN_CAPACITY, le=CARD_MAX_CAPACITY)
    decision_window_minutes: int | None = Field(
        default=None,
        ge=CARD_MIN_DECISION_WINDOW_MINUTES,
        le=CARD_MAX_DECISION_WINDOW_MINUTES,
    )

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: object) -> object:
        """AC-05: заголовок нормализуется тем же кодом, что при создании.

        Две разные нормализации дали бы «одинаковый» заголовок, который
        проходит `POST` и падает `CHECK`-ом на `PATCH`.
        """
        return _normalize(value)

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
