"""Схемы состава компании (стори 6.3; AC-01, AC-04).

Наружу уходят только публичные идентификаторы и то, что нужно экрану
«с кем и куда я иду». Внутренних `user_id`/`profile_id` нет (D-2),
`storage_key` нет (D-13): изображение адресуется только подписанным URL.

`photo_expires_in` есть ровно тогда, когда есть `photo_url`
(`D-5a.4-6`): два независимых поля допускали бы противоречие «ссылки нет,
а срок жизни есть» (project-context §4: `null`, не `""`).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.event_card import EventCardRead
from app.services.profile_normalize import normalize_bio


class CardParticipant(BaseModel):
    """AC-01, AC-03: один участник компании в ответе.

    `is_author` — признак АВТОРА карточки, а не «первого элемента»: порядок
    и признак связаны, но вычисляются из разных источников (автор приходит
    из карточки, участники — из заявок), и выводить одно из другого значит
    связать два правила.

    `name`/`age`/`photo_url` — `None`, когда анкеты нет, поле не заполнено или
    фото не одобрено: элемент списка остаётся полезным («человек в компании
    есть»), а `PersonMini` нарисует инициалы.
    """

    uuid: uuid_lib.UUID
    name: str | None = None
    age: int | None = None
    is_author: bool
    # D-11: presigned URL, считается при каждом чтении и в БД не живёт.
    photo_url: str | None = None
    # D-5a.4-6: срок жизни `photo_url`, `None` вместе с ним.
    photo_expires_in: int | None = None


class CardParticipantsResponse(BaseModel):
    """AC-01: карточка события + состав компании.

    `card` — та же `EventCardRead`, что у `GET /cards/{uuid}` (D-6.3-4):
    отдельная «краткая карточка для состава» разошлась бы с лентой при
    первом добавлении поля, и участник увидел бы два разных объекта одного
    ресурса. Побочный эффект — в ответе есть `chat_uuid`; это контракт 6.2,
    и участнику компании он легален.

    `total` дублирует `len(items)`: клиент не пересчитывает длину сам, а
    число участников — продуктовый факт (компания 2–6, FR85), который
    экран показывает текстом (NFR57).
    """

    card: EventCardRead
    items: list[CardParticipant]
    total: int


# ——— стори 6.4: обмен сообщениями (AC-01, AC-02, AC-04) ———


class MessageCreate(BaseModel):
    """AC-01, AC-04: тело отправки сообщения.

    `client_message_id` ОБЯЗАТЕЛЕН, а не генерируется сервером (D-6.4-2):
    идемпотентность возможна только если идентификатор переживает обрыв
    сети. Сгенерированный сервером ключ для каждого ретрая был бы новым, и
    повтор создавал бы дубль — ровно тот дефект, который NFR18 запрещает.

    `extra="forbid"`: `sender_id`/`created_at`/`chat_id` — серверные поля, и
    попытка их передать обязана стать `422`, а не молчаливым выбросом (иначе
    клиент считал бы, что отправил сообщение от чужого имени).

    `body` нормализуется `normalize_bio`, а не `normalize_text`: сообщение —
    это абзацы, и склейка переводов строки в пробелы сделала бы обычный
    текст нечитаемым. Предел длины — `Field(max_length=...)`, а не проверка
    в сервисе: `422` должен приходить до обращения к БД.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)
    client_message_id: uuid_lib.UUID

    @field_validator("body", mode="before")
    @classmethod
    def _normalize_body(cls, value: object) -> object:
        """AC-01: обрезка краёв и `\u0000`; пустое после нормализации — `422`."""
        if not isinstance(value, str):
            return value
        normalized = normalize_bio(value)
        if normalized is None or not normalized.strip():
            raise ValueError("Сообщение не может быть пустым")
        return normalized


class MessageRead(BaseModel):
    """AC-01, AC-02: сообщение в ответе.

    Чего здесь НЕТ и почему (D-2, NFR31):
    - `chat_id`/`sender_id` — внутренние `bigint`;
    - `client_message_id` — идентификатор устройства клиента, квитанция
      нужна только отправителю и приходит в ответе на `POST`;
    - `body_ciphertext` — шифртекст не покидает сервер ни при каких условиях.
    """

    uuid: uuid_lib.UUID
    # Стори 6.7 (AC-04): у СИСТЕМНОГО сообщения автора нет — `null`.
    # Нулевой uuid вместо `null` заставил бы клиент угадывать, а
    # `sender_uuid: null` + `is_system: true` — один явный контракт.
    sender_uuid: uuid_lib.UUID | None
    body: str
    created_at: datetime
    #: Стори 6.7 (AC-04): системное сообщение — без автора и без удаления.
    is_system: bool = False


# ——— стори 6.6: список чатов и непрочитанные (AC-01, AC-02) ———


class ChatListLastMessage(BaseModel):
    """AC-01: последнее сообщение в строке списка.

    `body` — РАСШИФРОВАННЫЙ текст (NFR31): ключ не покидает сервер. Поля
    `client_message_id` нет — идентификатор устройства клиента нужен только
    отправителю и только в ответе на `POST`; в списке он был бы утечкой
    между участниками.
    """

    uuid: uuid_lib.UUID
    # Стори 6.7 (AC-04): у системного превью автора нет.
    sender_uuid: uuid_lib.UUID | None
    body: str
    created_at: datetime
    #: Стори 6.7 (AC-04): признак системного сообщения в превью.
    is_system: bool = False


class ChatListItem(BaseModel):
    """AC-01, AC-04: одна строка списка чатов.

    `last_message = null` — переписки ещё нет (чат только что открыт или в
    нём будут только системные сообщения 6.7). Такой чат ВСЁ РАВНО в списке:
    компания собрана, и «чат исчез, пока никто не написал» — неверно.

    `unread_count` не включает свои сообщения (AC-06). `last_activity_at` —
    ключ сортировки: время последнего сообщения либо время открытия чата,
    если сообщений нет. Клиенту он нужен, чтобы не пересчитывать порядок
    самостоятельно (NFR57: сервер — источник истины).
    """

    chat_uuid: uuid_lib.UUID
    card_uuid: uuid_lib.UUID
    card_title: str
    last_message: ChatListLastMessage | None = None
    unread_count: int
    last_activity_at: datetime


class ChatListPage(BaseModel):
    """AC-01: список чатов в конверте §4 — `{items, total, cursor}`.

    `total` — число ВСЕХ чатов зрителя (не только страницы): `COUNT(*) OVER ()`
    того же запроса, поэтому отдельного round-trip нет (AC-05).
    """

    items: list[ChatListItem]
    total: int
    cursor: str | None = None


class ChatReadResponse(BaseModel):
    """AC-02: ответ отметки прочтения.

    `unread_count` всегда `0`: либо только что обнулили, либо там и было
    пусто (идемпотентность). Поле есть, чтобы клиент обновил счётчик без
    второго `GET /chats`.
    """

    chat_uuid: uuid_lib.UUID
    unread_count: int


class MessagePage(BaseModel):
    """AC-02: страница истории. `next_cursor = null` — дальше пусто.

    `from_attributes` не ставится: элементы собираются из доменного
    `ChatMessage`, а не из ORM-объекта (приём `MyRequestsResponse`).
    """

    items: list[MessageRead]
    next_cursor: str | None = None
    # X-6.5-5 (chore P-7): курсор ПОСЛЕДНЕГО элемента страницы, всегда (когда
    # страница непустая). Нужен клиенту для догрузки после реконнекта WS:
    # `next_cursor` равен `null` на последней странице, и без этого поля клиент
    # не может попросить «всё, что появилось после того, что я вижу». Второй
    # источник истины формата курсора на клиенте запрещён — формат opaque.
    last_cursor: str | None = None
