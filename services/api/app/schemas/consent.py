"""Схемы подтверждения 18+ и согласия на обработку ПД (Task 2.1; AC-01 … AC-04).

`gender` объявлен `str | None`, а не `Literal["female", "male"]`: недопустимый
пол — это доменная ошибка (`422 gender_required`, AC-03), а не ошибка формы.
`Literal` отдал бы `422 invalid_request` из обработчика валидации, и клиент
не смог бы отличить «пол не выбран» от «сломан JSON».

Патч 19 код-ревью 1.6: `field_validator(mode="before")` для `gender`. Раньше
нестроковое значение (`gender: 123`, `gender: {"a": 1}`) падало на типизации
Pydantic ДО сервиса → `422 invalid_request`, и клиент не отличал «пол не
выбран» от «сломан JSON». Теперь всё, что не строка, приводится к `None`,
и домен отвечает своим `422 gender_required`.

Регистр полей — snake_case (project-context §4): TS-типы генерируются из
OpenAPI, конвертация в camelCase стала бы вторым источником правды.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from app.core.datetime_format import iso_utc


class ConsentRequest(BaseModel):
    """Тело `POST /api/v1/auth/consent` (Task 2.1).

    Дефолты `False`/`None` намеренно не «удобные»: запрос без явного `true`
    не подтверждает ничего, и сервис отвечает `422 consent_required` (AC-03).
    """

    age_confirmed: bool = False
    consent: bool = False
    gender: str | None = None

    @field_validator("gender", mode="before")
    @classmethod
    def _coerce_gender(cls, value: Any) -> Any:
        """Патч 19: нестроковый `gender` — это «пол не выбран», а не битый JSON.

        `mode="before"` — до типизации Pydantic, иначе `123` дал бы
        `422 invalid_request` из общего обработчика. Строка проходит как есть:
        нормализация регистра и `strip` — забота сервиса (AC-03), чтобы
        `Female` и `female` вели себя одинаково и там, и здесь.
        """
        return value if isinstance(value, str) or value is None else None


class ConsentResponse(BaseModel):
    """Ответ `POST /api/v1/auth/consent` (Task 3.3; AC-01, AC-02, AC-04).

    AR21/AR22: ни `id`, ни `phone`, ни `provider_user_id`. Наружу — только
    публичный `uuid`, статус, пол и факт согласия.

    Патч 10 код-ревью 1.6: модель подключена как `response_model` — без этого
    `ConsentResponse` не попадала в OpenAPI и TS-типы генерировать было не из чего.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    status: str
    gender: str | None
    age_confirmed: bool
    consent_given_at: datetime | None
    is_new: bool

    @field_serializer("consent_given_at")
    def _serialize_consent_given_at(self, value: datetime | None) -> str | None:
        """project-context §4: единый формат даты (`iso_utc`), не вторая копия."""
        return iso_utc(value)


class SessionUserResponse(BaseModel):
    """Ответ `GET /api/v1/auth/me` (Task 1.1; AC-05, AC-06).

    Патч 10 код-ревью 1.6: эндпоинт отдавал `JSONResponse` без `response_model`,
    поэтому `/auth/me` не попадал в OpenAPI вовсе — фронт держал `SessionUser`
    руками, а расхождение контракта ловилось только в рантайме.

    Решение 3 код-ревью 1.6: `awaiting_applications` — серверный признак
    «мужчина ждёт открытия заявок». До этого «мужчина вне выборки» жил только
    в UI: прямой вызов API давал тот же ответ, что и UI, то есть правило
    продукта не было выражено в контракте.
    """

    uuid: uuid_lib.UUID
    role: str
    status: str
    gender: str | None
    age_confirmed: bool
    consent_given_at: datetime | None
    awaiting_applications: bool

    @field_serializer("consent_given_at")
    def _serialize_consent_given_at(self, value: datetime | None) -> str | None:
        """project-context §4: единый формат даты (`iso_utc`)."""
        return iso_utc(value)
