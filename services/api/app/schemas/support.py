"""Схемы обращения в поддержку (стори 9.5; AC-01…AC-04).

**`body` ограничен СХЕМОЙ, а не только `CHECK` в БД** (D-9.5-4). В миграции
`0030` `body` — `Text` без верхней границы, а `CHECK body_not_blank` ловит
только пустоту. Без `max_length` десятимегабайтное тело дошло бы до `INSERT`
и упало бы от драйвера (`500`) вместо `422`.

**Нормализация — `mode="before"`** (D-9.5-12, приём `JoinRequestCreate`):
`'   '` — три символа — прошло бы `min_length=1`, а после нормализации стало
бы пустой строкой. Порядок «нормализация → длина» существенен, поэтому
`field_validator(mode="before")` идёт ДО валидации поля.

**`normalize_bio`, не `normalize_text`.** Обращение многострочно («шаги
воспроизведения»), и `normalize_text` слепил бы строки в одну. Бонусом
`normalize_bio` снимает NUL/ZWSP/BOM, на которых `INSERT` дал бы `500`.

**Ответ не раскрывает внутренние поля** (D-9.5-6): `id` (bigint PK) наружу не
выходит никогда (§7), `user_id` избыточен (список уже «свои»),
`answered_by_id` раскрыл бы пользователю внутренний id модератора.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.profile_normalize import normalize_bio

__all__ = [
    "BODY_MAX_LENGTH",
    "SupportTicketCreate",
    "SupportTicketRead",
    "SupportTicketsResponse",
]

#: AC-01, D-9.5-4: верхняя граница тела обращения. Одна константа на схему и
#: на счётчик в форме: литерал в двух местах разошёлся бы при первой правке
#: (класс дефекта 4.1) — форма разрешала бы 2500, а API отвечал `422`.
#: 2000 = «длинное письмо»: ×6.7 от `join_requests.message` (300) и ×2 от
#: `reports.reason` (1000).
BODY_MAX_LENGTH = 2000


def _normalize(value: object) -> object:
    """Нормализация многострочного значения до проверки длины.

    `object`, а не `str`: в `mode="before"` Pydantic отдаёт сюда что угодно
    (включая `None` и `int`), и подмена типа сломала бы собственные проверки
    Pydantic — он должен сам сообщить «ожидалась строка».
    """
    if not isinstance(value, str):
        return value
    return normalize_bio(value)


class SupportTicketCreate(BaseModel):
    """AC-01, AC-04: вход создания обращения.

    `extra="forbid"`: клиент не может передать серверные поля (`status`,
    `answer`, `answered_at`, `answered_by_id`, `user_id`) — попытка даёт
    `422`, а не молчаливый выброс: иначе клиент считал бы, что назначил
    обращению состояние или ответ.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=BODY_MAX_LENGTH)

    @field_validator("body", mode="before")
    @classmethod
    def _strip_body(cls, value: object) -> object:
        """AC-04, D-9.5-12: нормализация ДО проверки длины.

        Краснеет (M5), если снять `mode="before"`: `'   '` и `'\t\n'`
        пройдут `min_length=1` (это 3 символа), затем `CHECK body_not_blank`
        их пропустит только после `btrim`-подобной нормализации в сервисе —
        то есть отказ придёт из БД, а не из схемы, и с другим кодом.

        `normalize_bio`, не `normalize_text`: обращение — абзацы, а не одна
        строка (`join_requests.message`).
        """
        return _normalize(value)


class SupportTicketRead(BaseModel):
    """AC-01…AC-03, D-9.5-5, D-9.5-6: полный элемент списка и ответа `201`.

    Одна схема на оба маршрута (D-9.5-5): клиент вставляет строку из `201`
    в уже отрисованный список без второго запроса — отдельная «минимальная»
    схема созданного дала бы второе описание того же ресурса, и они разошлись
    бы при первом расширении.

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


class SupportTicketsResponse(BaseModel):
    """AC-02, D-9.5-7: конверт `{items, total, cursor}` (как §7.1).

    `total` — «сколько всего своих обращений», не «сколько на странице»:
    клиент показывает «всего N», и усечённый счётчик соврал бы.
    `cursor = null` — страница последняя (признак «есть ещё» выражается
    отсутствием курсора; второй флаг был бы вторым источником правды).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[SupportTicketRead]
    total: int
    #: D-9.5-7: `serialization_alias` БЕЗ `validation_alias` — та же пара
    #: алиасов ломала КАЖДЫЙ вызов очереди жалоб (DS 8.3, D-41): сервис
    #: собирает схему по имени поля (`cursor=`), а `validation_alias` делал
    #: `cursor` «лишним входом» и при `extra="forbid"` валил сборку в `500`.
    #: Наружу по-прежнему `next_cursor`.
    cursor: str | None = Field(default=None, serialization_alias="next_cursor")
