"""Схемы ответов по фото анкеты (Task 2.1–2.4 стори 2.4; AC-01, AC-05, AC-11).

D-13 стори 2.2: наружу уходят только `uuid`, `position`, `status`, `url`,
`expires_in`, `content_type`, `created_at`, а с 2.4 — ещё `rejection_reason`
и `moderated_at`. Полей `id`, `storage_key` и `profile_id` в схемах нет
и не должно появиться:

  * `id` — внутренний bigint, перебор `/1..N` обошёл бы лимит выборки;
  * `storage_key` — ключ объекта в бакете; зная его, клиент мог бы пытаться
    подобрать путь к чужому фото, а сам ключ не нужен ни для показа, ни для
    удаления (удаление адресуется `uuid`) — только подписанный URL (FR58);
  * `profile_id` — раскрывает связь с анкетой, которая клиенту уже известна.

`url` — единственный путь к изображению (AC-04). Он не хранится в БД (D-11):
подпись считается при каждом чтении, поэтому схема собирается фабрикой
`from_photo`, а не `model_validate` модели.

D-2 стори 2.4: `url = None` означает «изображение не выдаётся», а не «ошибка».
Так выглядит `pending`: клиент рисует заглушку «На проверке». Для `pending`
`get_signed_url` не вызывается ВООБЩЕ (AC-03) — иначе валидная ссылка
на непроверенное фото жила бы в памяти 900 с и утекала бы в ответ при любой
будущей правке.

D-4 стори 2.4: `rejection_reason` и `moderated_at` есть только в `PhotoOwn`,
то есть в ответе владельцу. Публичная схема (эпик 4) их не получит: чужие
выборки идут через `list_approved_by_profile`, где `rejected` физически нет.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from pydantic import BaseModel, Field, field_serializer

from app.core.datetime_format import iso_utc
from app.models.enums import PhotoStatus
from app.models.profile_photo import ProfilePhoto


class PhotoOwn(BaseModel):
    """AC-01, AC-04, AC-11, D-13: фото глазами владельца.

    `status` типизирован `PhotoStatus`, а не `str`: строка приняла бы значение,
    которого нет в БД (там CHECK `ck_profile_photos_status`), и клиент получил бы
    статус, для которого у него нет ветки отображения.

    `expires_in` — секунды жизни ссылки (D-11). Без него клиент показывал бы
    битую картинку после истечения `S3_SIGNED_URL_TTL` и не знал бы, когда
    запросить новую. Остаётся числом всегда, даже когда `url = None`: это
    свойство конфигурации, а не выданного фото.

    Поля и их гарантии (AC-01):

      * `url` — `None` для `pending`, подписанная ссылка для `approved`
        и `rejected` (AC-03, AC-04);
      * `rejection_reason` — `None` для `pending` и `approved`, непустая
        строка для `rejected` (AC-05);
      * `moderated_at` — `None` для `pending`, ISO 8601 UTC с `Z` для
        решённых (project-context §4).

    Пол пользователя на состав полей не влияет (FR63, AC-02).
    """

    uuid: uuid_lib.UUID
    position: int
    status: PhotoStatus
    url: str | None = Field(
        default=None,
        description="Подписанная ссылка; None — изображение не выдаётся (pending)",
    )
    expires_in: int = Field(description="Срок жизни подписанной ссылки, секунды")
    content_type: str
    rejection_reason: str | None = Field(
        default=None, description="Причина отказа модератора; только для rejected"
    )
    created_at: datetime
    moderated_at: datetime | None = Field(
        default=None, description="Время решения модератора; None — ещё не проверено"
    )

    @field_serializer("created_at", "moderated_at")
    def _serialize_dt(self, value: datetime | None) -> str | None:
        """project-context §4: ISO 8601 UTC с `Z` — общий формат дат API.

        `iso_utc` уже None-safe: `None` остаётся `null`, а не превращается
        в строку `"None"`. Без этого `pending` отдавал бы мусор вместо `null`.
        """
        return iso_utc(value)

    @classmethod
    def from_photo(cls, photo: ProfilePhoto, *, url: str | None, expires_in: int) -> PhotoOwn:
        """Собирает схему из строки БД и уже посчитанной подписи.

        Фабрика, а не `model_validate`: `url` и `expires_in` в модели
        отсутствуют, и попытка валидировать объект напрямую дала бы
        `ValidationError` на отсутствующих полях.

        `url=None` — легальное значение (AC-03): для `pending` подпись
        не считается вовсе, и сервис передаёт сюда `None`.
        """
        return cls(
            uuid=photo.uuid,
            position=photo.position,
            status=PhotoStatus(photo.status),
            url=url,
            expires_in=expires_in,
            content_type=photo.content_type,
            rejection_reason=photo.rejection_reason,
            created_at=photo.created_at,
            moderated_at=photo.moderated_at,
        )


class PhotoListResponse(BaseModel):
    """project-context §4: список — `{items: [...], total: N}`.

    `total` обязателен контрактом проекта, даже когда он равен длине `items`:
    клиент рисует счётчик «2 из 3» и не должен считать сам.
    """

    items: list[PhotoOwn]
    total: int
