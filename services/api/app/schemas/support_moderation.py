"""Схемы кабинета модератора для обращений в поддержку (стори 9.6; AC-01, AC-02).

**Пара ответа в схеме есть и для очереди** (D-9.6-11): у открытого обращения
`answer`/`answered_at` — `null`, у отвеченного — значения. Одна схема на
очередь и на ответ `POST` (приём D-9.5-5): клиент вставляет строку из ответа
в уже отрисованный список без второго запроса, а отдельная «минимальная»
схема ответа разошлась бы с элементом списка при первом расширении.

**`author_uuid`, а не `user_id`** (§7, У-8, D-9.6-11): модератору нужен
публичный адрес автора, чтобы открыть его историю; `bigint` PK наружу не
выходит никогда. `answered_by_id` наружу не выходит тоже — интерфейсу не
нужно «кто ответил», а внутренний id модератора в ответе был бы утечкой (§7).

**`SupportReplyRequest.answer` нормализуется ДО проверки длины**
(D-9.6-4, приём `SupportTicketCreate`): `'   '` — три символа — прошло бы
`min_length=1`, а после нормализации стало бы пустой строкой, и `UPDATE`
записал бы пустой ответ (или упал на `ck_answer_pair`). Порядок
«нормализация → длина» держится `field_validator(mode="before")`.

**`BODY_MAX_LENGTH` ИМПОРТИРУЕТСЯ из `schemas/support.py`**, а не
переписывается числом: тот же класс текста (ответ модератора и тело
обращения), и второе число разошлось бы с первым (класс дефекта 4.1).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.support import BODY_MAX_LENGTH
from app.services.profile_normalize import normalize_bio

__all__ = [
    "ModerationSupportQueuePage",
    "ModerationTicketItem",
    "SupportReplyRequest",
]


def _normalize(value: object) -> object:
    """Нормализация многострочного ответа до проверки длины.

    `object`, а не `str`: в `mode="before"` Pydantic отдаёт сюда что угодно
    (включая `None` и `int`), и подмена типа сломала бы собственные проверки
    Pydantic — он должен сам сообщить «ожидалась строка».
    """
    if not isinstance(value, str):
        return value
    return normalize_bio(value)


class ModerationTicketItem(BaseModel):
    """AC-01, AC-02, D-9.6-11: элемент очереди и ответ `POST .../reply`.

    Одна схема на оба маршрута (D-9.6-11, приём D-9.5-5): после ответа
    модератор видит ту же строку, что была в очереди, с заполненной парой
    `answer`/`answered_at` — без перечитки страницы.

    `answer`/`answered_at` — пара: у `open` оба `null`, у `answered` оба
    заполнены (`CHECK ck_support_tickets_answer_pair` в БД).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: uuid_lib.UUID
    body: str
    status: str
    created_at: datetime
    answer: str | None
    answered_at: datetime | None
    #: §7: публичный адрес автора. `user_id`/`answered_by_id` наружу не идут.
    author_uuid: uuid_lib.UUID


class ModerationSupportQueuePage(BaseModel):
    """AC-01, AC-04, D-9.6-3, D-9.6-13: конверт `{items, total, cursor}`.

    `total` — «сколько всего ОТКРЫТЫХ обращений», не «сколько на странице»:
    модератор планирует работу по объёму, и усечённый счётчик занизил бы её.
    `cursor = null` — страница последняя (признак «есть ещё» выражается
    отсутствием курсора; второй флаг был бы вторым источником правды).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ModerationTicketItem]
    total: int
    #: D-41 (8.3): `serialization_alias` БЕЗ `validation_alias`. Пара алиасов
    #: ломала КАЖДЫЙ вызов очереди жалоб: сервис собирает схему по имени поля
    #: (`cursor=`), а `validation_alias="next_cursor"` делал `cursor` «лишним
    #: входом» и при `extra="forbid"` валил сборку в `ValidationError` → `500`
    #: на живом маршруте. Наружу по-прежнему `next_cursor`.
    cursor: str | None = Field(default=None, serialization_alias="next_cursor")


class SupportReplyRequest(BaseModel):
    """AC-02, D-9.6-4: тело `POST /moderation/support/tickets/{uuid}/reply`.

    Одно поле — текст ответа. `extra="forbid"`: клиент не может передать
    серверные поля (`status`, `answered_at`, `answered_by_id`, `user_id`) —
    попытка даёт `422`, а не молчаливый выброс. Иначе модератор считал бы,
    что назначил состояние обращения или автора ответа.

    `max_length=BODY_MAX_LENGTH` — импорт из `schemas/support.py`: без верхней
    границы в схеме десятимегабайтный ответ дошёл бы до `UPDATE` и упал бы от
    драйвера (`500`) вместо `422`.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=BODY_MAX_LENGTH)

    @field_validator("answer", mode="before")
    @classmethod
    def _strip_answer(cls, value: object) -> object:
        """AC-02, D-9.6-4: нормализация ДО проверки длины.

        Краснеет, если снять `mode="before"`: `'   '` и `'\t\n'` пройдут
        `min_length=1` (это 3 символа), и пустой ответ дошёл бы до `UPDATE`
        (отказ пришёл бы из БД другим кодом, а не `422 invalid_request`).

        `normalize_bio`, не `normalize_text`: ответ модератора — абзацы, а не
        одна строка; бонусом снимаются NUL/ZWSP/BOM, на которых `UPDATE`
        дал бы `500`.
        """
        return _normalize(value)
