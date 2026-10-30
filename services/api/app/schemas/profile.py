"""Публичная схема анкеты (Task 4.1–4.4; CR 2.1, Решение 3).

AC-04: наружу отдаётся только `uuid`. Полей `id` и `user_id` в схеме нет
и не должно появиться: `id` — внутренний bigint, перебор `/1..N` позволил бы
выгрузить город в обход выборки из 20 и модерации (D-2). Тест 8.2 проверяет
отсутствие ключей в реальном ответе, чтобы регрессия ловилась автоматически.

CR 2.1, Решение 3: схем две, как две аудитории. `ProfilePublic` — то, что
увидит другой пользователь в карточке (эпик 4): без `is_hidden` и `status`,
иначе каждый ответ раскрывал бы факт скрытия или недозаполненности анкеты.
`ProfileOwn` — свой профиль: владелец видит и флаг скрытия, и статус
заполнения (без него клиент не знает, черновик у него или готовая анкета).

`ProfileOwn` наследует `ProfilePublic`: общие поля описаны один раз, и
появление нового общего поля не требует правки в двух местах.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.datetime_format import iso_utc
from app.models.enums import Gender
from app.services.age_service import compute_age, today_or_now


class ProfilePublic(BaseModel):
    """AC-04: публичное представление анкеты для чужой карточки (эпик 4).

    Полей `id` и `user_id` здесь нет. `is_hidden` и `status` тоже нет
    (CR 2.1, Решение 3): чужой пользователь не должен узнавать, что анкета
    скрыта владельцем или ещё не заполнена — в выборку такие не попадают.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str | None
    age: int | None
    city: str | None
    bio: str | None
    # Типизировано enum-ом, а не str: строка приняла бы `other` и отдала бы
    # клиенту значение, которого не может быть в БД (там CHECK, AC-02).
    gender: Gender
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_dt(self, value: datetime) -> str | None:
        """project-context §4: ISO 8601 UTC с `Z`.

        Переиспользуется `iso_utc`, а не пишется третий сериализатор
        (Task 4.3): своя копия в `schemas/user.py` уже приводила к тому,
        что `/auth/me` и `/auth/consent` расходились по формату.
        """
        return iso_utc(value)

    @classmethod
    def from_profile(cls, profile: object, *, today: date | None = None) -> Self:
        """Собирает ответ из ORM-объекта, **вычисляя** `age` (AC-2, AC-3).

        `from_attributes` здесь не работает: в модели `age` — legacy-колонка,
        у новых учёток она `NULL`, а наружу должен уходить вычисленный возраст.
        Взять `profile.age` напрямую значило бы отдавать `null` всем, кто
        заполнил дату рождения, — то есть сломать контракт `age` для всех
        потребителей (лента, карточка, участники, модерация).

        `birth_date` в схему не попадает вообще: поля нет в `ProfilePublic`,
        и `model_validate` его не читает (ADR-18, NFR66). Гард на отсутствие
        ключа в JSON — стори 12.3.
        """
        values = {
            "uuid": profile.uuid,
            "name": profile.name,
            # AC-2, AC-5: `birth_date` приоритетнее legacy-`age`.
            "age": compute_age(profile.birth_date, profile.age, today or today_or_now()),
            "city": profile.city,
            "bio": profile.bio,
            "gender": profile.gender,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
        if cls is ProfileOwn:
            values["is_hidden"] = profile.is_hidden
            values["status"] = profile.status
        return cls(**values)


class ProfileOwn(ProfilePublic):
    """Свой профиль: публичные поля + служебные флаги владельца.

    CR 2.1, Решение 3: `is_hidden` только здесь. Если бы флаг жил в
    `ProfilePublic`, эпик 4 раскрывал бы факт скрытия каждому, кто получил
    карточку. `status` нужен клиенту, чтобы отличить черновик от готовой
    анкеты (патч 14).
    """

    is_hidden: bool
    status: Literal["draft", "complete"]


# --- Запросы (Task 2.1–2.6; AC-01, AC-02, AC-03, AC-05) ------------------------

# D-7: пределы полей — один источник для сервиса и для сообщения об ошибке.
# Значения совпадают с типами колонок (`varchar(50)`, `varchar(100)`) и с
# доменным пределом `bio` (500). Расхождение дало бы либо `500` из БД
# (схема мягче колонки), либо необъяснимый отказ (схема жёстче).
#
# CR 2.3, Решение 1: в Pydantic-схемах пределов НЕТ. `Field(max_length=...)`
# срабатывает раньше сервиса и возвращает общий `invalid_request`, из-за чего
# доменные коды `name_too_long`/`city_too_long`/`bio_too_long`/`invalid_age`
# были недостижимы по HTTP. Пределы держит сервис (`_TEXT_LIMITS`, `_check_age`),
# а тип колонки (`varchar`) остаётся последним рубежом от `500`.
NAME_MAX = 50
CITY_MAX = 100
BIO_MAX = 500
AGE_MIN = 18
AGE_MAX = 99


class ProfileCreate(BaseModel):
    """AC-02 (FR11): тело `POST /api/v1/profiles`.

    Все четыре поля обязательны (D-2): публичное создание всегда даёт полную
    анкету, `draft` — только служебное состояние онбординга 2.2.

    D-4: `gender` здесь нет и не появится. Пол берётся из `users.gender`;
    присланное значение либо отклонится `extra="forbid"`, либо (если бы
    поле добавили) дало бы `IntegrityError` из составного FK → `500`.
    `status`, `is_hidden`, `user_id`, `uuid` — тоже не поля запроса.

    D-7: лимиты — верхний слой защиты. `bio` в БД `text`, тип не ограничивает:
    тело на 5 МБ дошло бы до CHECK и вернуло `IntegrityError` → `500`.
    """

    model_config = ConfigDict(extra="forbid")

    # CR 2.3, Решение 1: только базовые типы. Пределы (50/100/500/18..99)
    # проверяет сервис — иначе `Field(max_length=...)` отбивал бы ввод раньше
    # и клиент получал бы `invalid_request` вместо `city_too_long`.
    name: str = Field(description="Имя, до 50 символов")
    city: str = Field(description="Город, до 100 символов")
    # Стори 12.2, AC-2: дата рождения вместо числа лет. Возраст больше не
    # принимается от клиента — он вычисляется сервером (`age_service`),
    # иначе человек мог бы прислать любое число и обойти 18+.
    birth_date: date = Field(description="Дата рождения (18..99 полных лет)")
    bio: str = Field(description="Текст о себе, до 500 символов")


class ProfileUpdate(BaseModel):
    """AC-03 (FR11): тело `PATCH /api/v1/profiles/me` — частичная правка.

    D-11: все поля `| None`, различие «не передано» и «передано `null`»
    делается через `model_fields_set`. Отсутствующий ключ — не трогаем;
    явный `null` — `422 field_required` (D-3: стереть поле нельзя).

    Без этого различия `{"name": null}` тихо обнулил бы имя и оставил
    `status='complete'`, а CHECK полноты дал бы `IntegrityError` → `500`.
    """

    model_config = ConfigDict(extra="forbid")

    # CR 2.3, Решение 1: `PATCH` не обходит доменные проверки — они в сервисе,
    # а не здесь. Пределы в схеме отбивали ввод раньше и подменяли точный код
    # (`city_too_long`, `invalid_age`) общим `invalid_request`.
    name: str | None = Field(default=None)
    city: str | None = Field(default=None)
    # Стори 12.2, AC-2: правка тоже принимает дату, а не число.
    # `age` здесь нет осознанно: два источника правды разойдутся на первом
    # дне рождения (ADR-17, «Отвергнуто» а). Старый клиент с `age` получит
    # `422 invalid_request` от `extra="forbid"` — волна 1 ломающая,
    # веб и API деплоятся одновременно.
    birth_date: date | None = Field(default=None)
    bio: str | None = Field(default=None)


class LocateCityRequest(BaseModel):
    """AC-06 (FR12, FR59): тело `POST /api/v1/profiles/locate-city`.

    Координаты приходят в теле, а не в query: query попадает в журналы
    прокси и историю браузера, а хранение координат запрещено (FR59).

    Диапазоны проверяются здесь: без них `lat=91` сравнилась бы
    с прямоугольником и вернула бы `outside_pilot_city` — «валидный» ответ
    на мусор вместо отказа.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90, description="Широта")
    lng: float = Field(ge=-180, le=180, description="Долгота")
    # CR 2.3, патч 16: верхняя граница обязательна. Без неё `accuracy=1e308`
    # проходил парсинг и уходил в ответ как «точность», хотя сервер её
    # не использует: поле должно быть правдоподобным или отвергнутым.
    # 100 км — заведомо больше любого разумного значения браузера.
    accuracy: float = Field(ge=0, le=100_000, description="Точность в метрах, от браузера")


class LocateCityResponse(BaseModel):
    """AC-06: ответ — только город пилота или причина отказа.

    Координат в ответе нет: клиент не должен получать назад то, что прислал
    (аналитика, кэш браузера, журналы). `reason` — закрытый набор значений,
    чтобы клиент не разбирал свободный текст.
    """

    city: str | None
    reason: Literal["outside_pilot_city"] | None = None
