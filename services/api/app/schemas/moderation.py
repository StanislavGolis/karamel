"""Схемы очереди модерации карточек (стори 4.3, Task 5; AC-03, AC-05).

Конверт выдачи — тот же `{items, total, cursor}`, что у справочника мест
(§7.1 архитектуры): один клиентский разбор списка на два ресурса.

**Внутренние `id` наружу не выходят** (§7.2: «`place_id`, `author_id`,
`verified_by` не отдаются — только `uuid`»): модератор видит публичные
адреса и текст, а не ключи БД. Это не косметика — числовой `id` дал бы
перебор `/1..N` в обход проверки существования.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import CardVerificationStatus, EventCardStatus, PlaceCategory
from app.services.profile_normalize import normalize_bio

# AC-03: размер страницы очереди. Модератор разбирает очередь руками, поэтому
# страница меньше ленты: 20 карточек — это один экран кабинета и одна порция
# работы «за раз». Верхняя граница защищает от `limit=10000` из скрипта.
MODERATION_PAGE_SIZE = 20
MAX_MODERATION_PAGE_SIZE = 100

# AC-05, FR113: причина отклонения — обязательный короткий текст. Нижняя
# граница отделяет «причину» от случайного символа, верхняя — от простыни,
# которую автор не прочитает. 500 символов ≈ 5 строк на телефоне.
REJECTION_REASON_MIN_LENGTH = 1
REJECTION_REASON_MAX_LENGTH = 500

# AC-03, D-4.3-4: фильтр очереди — измерение очереди, а НЕ доменный статус.
# `all` в `CardVerificationStatus` не существует и не должен туда попасть:
# это параметр выборки, а не значение проверки. Literal отбивает мусор
# в Pydantic (`422`) без второй ветки маппинга ошибок.
ModerationStatusFilter = Literal["pending", "approved", "rejected", "all"]


class ModerationAuthor(BaseModel):
    """AC-03: автор карточки в очереди — без внутреннего `id`.

    `name`/`age` берутся из анкеты (`profiles`). Анкеты может НЕ быть: модератор
    её не заполняет, а участник мог не пройти онбординг — поэтому оба поля
    необязательны, а `user.uuid` есть всегда (он и есть адрес автора).

    NFR53: критерии модерации — адекватность контента, никогда внешность.
    Поэтому ни фото, ни города, ни пола здесь нет: модератор оценивает текст
    карточки, а не человека.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str | None = None
    age: int | None = None


class ModerationPlace(BaseModel):
    """AC-03: место карточки — только то, что нужно для решения."""

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    name: str


class ContentCheckRead(BaseModel):
    """AC-03, D-4.3-6: результат автопроверки, пересчитанный на ЧТЕНИИ.

    Колонки под `failed_rules` нет: результат чистой функции воспроизводим,
    а кэш разошёлся бы с текстом после `PATCH` и потребовал бы инвалидации.

    Модератору это нужно, чтобы видеть ПРИЧИНУ попадания в очередь: «правило
    `stop_word`» отвечает на вопрос «за что», а голый `pending` — нет.
    """

    passed: bool
    failed_rules: tuple[str, ...]


class ModerationCardItem(BaseModel):
    """AC-03: элемент очереди — карточка, автор, место и итог автопроверки."""

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    title: str
    description: str | None = None
    category: PlaceCategory
    starts_at: datetime
    capacity: int
    created_at: datetime
    # AC-03, ADR-08: состояние публикации и проверки — два разных измерения.
    # Модератор видит только `draft` (проверка идёт до публикации), но поле
    # остаётся в контракте: `status=all` покажет и историю.
    status: EventCardStatus
    verification_status: CardVerificationStatus
    # AC-05, FR113: причина предыдущего отклонения — контекст для повторного
    # решения («уже отклонял за это»). У `pending`/`approved` — `None`.
    rejection_reason: str | None = None
    author: ModerationAuthor
    place: ModerationPlace
    auto_check: ContentCheckRead


class ModerationCardListResponse(BaseModel):
    """AC-03: конверт очереди `{items, total, cursor}` (как §7.1).

    `total` считается БЕЗ курсора (D-4.3-7): курсор — позиция чтения, не
    фильтр; иначе «всего» прыгало бы от страницы к странице, и клиент не смог
    бы показать счётчик работы.

    `cursor` — `null` в конце выдачи, непрозрачная строка иначе (AC-08).
    """

    items: list[ModerationCardItem]
    total: int
    cursor: str | None = None


class ModerationRejectRequest(BaseModel):
    """AC-05, FR113: причина отклонения обязательна и непуста.

    Нормализация — `normalize_bio`, а не `normalize_text`: причина может быть
    многострочной (модератор перечисляет нарушения), и склеивать абзацы
    в одну строку значило бы менять смысл написанного.

    `extra="forbid"`: опечатка в имени поля (`reason` вместо `rejection_reason`)
    должна давать `422`, а не молча создавать отклонение без причины.
    """

    model_config = ConfigDict(extra="forbid")

    rejection_reason: str = Field(
        min_length=REJECTION_REASON_MIN_LENGTH,
        max_length=REJECTION_REASON_MAX_LENGTH,
    )

    @field_validator("rejection_reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: object) -> object:
        """AC-05: строка из пробелов — не причина.

        Без нормализации `'   '` прошла бы `min_length=1` (три символа),
        и автор получил бы отклонение с пустой причиной — то есть без причины,
        что прямо запрещено FR113 и CHECK `ck_event_cards_rejection_reason`.
        """
        if isinstance(value, str):
            return normalize_bio(value)
        return value
