"""Схемы заявки на участие (стори 5а.2; AC-01, AC-02).

AC-01 (FR76, D-5a.1-8): `message` — обязательное непустое сообщение до 300
символов. Нормализация выполняется `mode="before"`, то есть ДО проверки
длины: пустая строка `''` проходит `CHECK` в БД (`message = btrim(message)`
истинно для пустой строки), поэтому единственный рубеж против「пустой»
заявки — схема. Порядок шагов важен ровно так же, как у `EventCardCreate`
(4.1): проверка длины РАНЬШЕ нормализации пропустила бы `'   '` (три
символа), которая после нормализации стала бы пустой.

AC-02: ответ `201` минимален — `uuid`, `status`, `submitted_at` (D-5a.2-9).
`message` не эхо (OQ-5a.2-2): `JoinRequestSheet` после отправки показывает
«Заявка отправлена · автор ответит до …», текст уже известен клиенту, а
лишнее поле расширило бы контракт, который 5а.3 всё равно расширит вместе
с `GET /my/requests`.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    CardRequestsMode,
    EventCardStatus,
    JoinRequestStatus,
    PlaceCategory,
)
from app.services.profile_normalize import normalize_text

# AC-01: предел совпадает с шириной `join_requests.message` (`String(300)`).
# Проверка на схеме, а не только в БД: иначе 301 символ падал бы
# `DBAPIError`-ом (`500`) на валидном для формы вводе.
MESSAGE_MAX_LENGTH = 300


def _normalize(value: object) -> object:
    """Нормализация однострочного значения до проверки длины.

    `object`, а не `str`: в `mode="before"` Pydantic отдаёт сюда что угодно
    (включая `None`), и подмена типа сломала бы собственные проверки
    Pydantic — он должен сам сообщить «ожидалась строка» (приём 4.1).
    """
    if not isinstance(value, str):
        return value
    return normalize_text(value)


class JoinRequestCreate(BaseModel):
    """AC-01 (FR76): вход подачи заявки.

    `extra="forbid"`: клиент не может передать серверные поля
    (`status`, `submitted_at`, `expires_at`, `card_id`, `applicant_id`) —
    попытка даёт `422`, а не молчаливый выброс: иначе клиент считал бы,
    что назначил заявке состояние.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MESSAGE_MAX_LENGTH)

    @field_validator("message", mode="before")
    @classmethod
    def _strip_message(cls, value: object) -> object:
        """AC-01 (D-5a.1-8): нормализация ДО проверки длины.

        Краснеет (тест), если снять этот валидатор: `'   '` и `'\u00a0'`
        пройдут `min_length=1` (это 3 и 1 символ), затем CHECK в БД их
        пропустит (они `btrim`-нутся в пустую строку), и автор увидит в
        очереди заявителей пустую заявку.

        `normalize_text`, не `normalize_bio`: сообщение — одна строка
        (`String(300)`), перенос строки в нём шум, а не абзац.
        """
        return _normalize(value)


class JoinRequestRead(BaseModel):
    """AC-02 (FR76, D-5a.2-9): минимальный ответ на подачу заявки.

    Три поля — ровно то, что нужно клиенту: идентификатор для отмены (5а.3),
    состояние для ветвления и момент подачи для «отправлено только что».

    `message` НЕ отдаётся (OQ-5a.2-2): эхо не требуется, а лишнее поле
    пришлось бы поддерживать в контракте навсегда.

    `from_attributes=True` — ответ собирается из ORM-объекта: сервис
    возвращает `JoinRequest`, роутер не переписывает поля вручную.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    # AC-02: `pending` на момент создания; тип — `StrEnum`, как у статусов
    # карточки: мусор из БД стал бы `500` на собственном дефекте, а не
    # молча ушёл клиенту строкой.
    status: JoinRequestStatus
    submitted_at: datetime


# ——— 5а.3: «Мои заявки» (AC-06, AC-07) ———


class MyRequestPlace(BaseModel):
    """AC-07: место глазами экрана «Мои заявки» — ровно три поля.

    Копия формы `EventCardPlace` из ленты (`schemas/event_card.py`) намеренная:
    импорт оттуда связал бы экран заявителя с контрактом ленты, и правка ленты
    (например добавление координат) меняла бы ответ 5а.3 молча. Совпадение
    полей сегодня — не повод связывать два разных экрана.

    `address_text`, а не координаты: место — ссылка на проверенное место
    (NFR34), а `place_id` (внутренний bigint) не покидает сервер (D-2).
    """

    uuid: uuid_lib.UUID
    name: str
    address_text: str


class MyRequestCard(BaseModel):
    """AC-06: карточка внутри «Моей заявки».

    Состояние карточки отдаётся как есть (`status`, `filled_count`), а не
    вычисляется клиентом: проигравший должен увидеть «Компания собрана»
    без второго запроса, и второй источник правды (`has_seats`) разошёлся бы
    с сервером при первой правке.

    `chat_uuid` здесь НЕТ: чат появляется в эпике 6 и только для выбранных;
    поле в этом контракте расширило бы выдачу заявителя без пользы.
    """

    uuid: uuid_lib.UUID
    title: str
    category: PlaceCategory
    starts_at: datetime
    expires_at: datetime
    status: EventCardStatus
    capacity: int
    filled_count: int
    place: MyRequestPlace


class MyRequestAuthor(BaseModel):
    """AC-07 (NFR34): автор карточки в «Моих заявках» — с фото.

    Не `EventCardAuthor` из ленты: там фото нет, а добавление поля в контракт
    4.5 расширило бы ленту без нужды.

    Все поля, кроме `uuid`, необязательны: анкеты может не быть (участник не
    прошёл онбординг), фото — не загружено. Элемент списка при этом остаётся
    полезным: «есть встреча, есть место, есть время».
    """

    uuid: uuid_lib.UUID
    name: str | None = None
    age: int | None = None
    # AC-07, D-11: presigned URL, считается при каждом чтении и в БД не живёт.
    photo_url: str | None = None


class MyRequestItem(BaseModel):
    """AC-06, AC-07: одна заявка в списке заявителя.

    `message` здесь ОТДАЁТСЯ, хотя в ответе на подачу (`JoinRequestRead`)
    его нет (OQ-5a.2-2): список — экран «что я написал», и текст своего же
    сообщения нужен для строки заявки. Эхо в `201` не требовалось, здесь —
    требуется.

    `expires_at` — окно решения СВОЕЙ заявки (копия окна карточки на момент
    подачи, D-5a.1-5): по нему клиент рисует дословный текст «Автор ответит
    до …» без второго запроса.
    """

    uuid: uuid_lib.UUID
    status: JoinRequestStatus
    submitted_at: datetime
    expires_at: datetime
    # AC-13 (долг X-5a.5-3): `str | None`, не `str`. Участие открытого набора
    # пишется с `message=''` (D-5а.5-1), и пустая строка выглядела бы как
    # «заявка без текста». Отсутствие — `null` (project-context §4);
    # приведение делает роутер в единственной точке сборки ответа.
    message: str | None
    card: MyRequestCard
    author: MyRequestAuthor


class MyRequestsResponse(BaseModel):
    """AC-06, §7.1: конверт `{items, total, cursor}` — как у мест и ленты.

    `total` — «сколько всего своих заявок под фильтром», не «сколько
    осталось»: считается без курсора (D-5a.3-7), поэтому листание его не
    меняет.

    `cursor` — `null` в конце выдачи и непрозрачная строка иначе. Клиент не
    разбирает его содержимое: формат — деталь сервера.

    `from_attributes` НЕ ставится: элементы собираются из `Row`-строки через
    доменный `MyRequestEntry`, а не из ORM-объекта. Атрибут создал бы ложное
    впечатление, что `model_validate(row)` работает.
    """

    items: list[MyRequestItem]
    total: int
    cursor: str | None = None


# ——— 5а.4: список заявившихся для автора (AC-01, AC-05, AC-06) ———


class CardRequestsPerson(BaseModel):
    """AC-01, AC-06: заявившийся глазами автора карточки.

    `PersonMini` (UX §877) показывает аватар + имя + возраст и НИКОГДА не
    показывает рейтинг, число встреч или «надёжность» — этих полей здесь нет
    намеренно: то, чего нет в контракте, нельзя случайно отрисовать.

    Все поля, кроме `uuid`, необязательны: анкеты может не быть (участник не
    прошёл онбординг), фото — не загружено или не одобрено. Строка списка
    при этом остаётся полезной: «есть человек, есть сообщение».
    """

    uuid: uuid_lib.UUID
    name: str | None = None
    age: int | None = None
    # AC-06, D-11: presigned URL, считается при каждом чтении, в БД не живёт.
    photo_url: str | None = None
    # AC-06, D-5a.4-6: срок жизни `photo_url` в секундах. Поле есть, потому
    # что клиент, висящий на экране дольше TTL, иначе молча показывал бы
    # протухшую ссылку (`OQ-5a.3-1`). `None`, когда фото нет — одно поле
    # вместо двух флагов.
    photo_expires_in: int | None = None


class CardRequestsItem(BaseModel):
    """AC-01: одна заявка в списке автора — заявка + человек.

    `uuid` — идентификатор ЗАЯВКИ (не человека): по нему в 5б.2 автор выберет
    заявившегося. `message` отдаётся: автор выбирает осознанно, и текст —
    половина решения (UX §865).

    `submitted_at` в ответе, хотя порядок уже задан сервером: клиент рисует
    «подал 2 часа назад» без второго запроса, а сноска «Порядок — по времени
    подачи» (UX §865) становится проверяемой глазами.
    """

    uuid: uuid_lib.UUID
    submitted_at: datetime
    # OQ-5а.5-4 (стори 5а.5): участие открытого набора подаётся без сообщения
    # (`message=''`, D-5а.5-1), и пустая строка в списке заявившихся выглядела
    # бы как «заявка без текста» вместо «текста нет» (project-context §4:
    # отсутствие — `null`, не `""`). Автор при этом продолжает получать
    # настоящий текст `pending`-заявки.
    message: str | None = None
    person: CardRequestsPerson


class CardRequestsResponse(BaseModel):
    """AC-03, AC-05: конверт `{items, total, cursor}` + режим и признак выбора.

    `total` — «сколько всего под фильтром» (D-4.5-9), не «сколько осталось»:
    считается без курсора, поэтому листание его не меняет.

    `mode` и `selection_available` — серверные признаки, а не вычисляемые
    клиентом из `capacity` (D-5a.4-1): правило FR70 живёт в одном месте, и
    при добавлении промежуточной вместимости клиент не разойдётся с сервером.
    `selection_available=false` означает «открытый набор, выбирать нечего»,
    а не «выбор временно недоступен»: кнопка «Выбрать» не рисуется вовсе.

    `from_attributes` НЕ ставится: элементы собираются из `Row`-строки через
    доменный `CardRequestsEntry`, а не из ORM-объекта.
    """

    items: list[CardRequestsItem]
    total: int
    cursor: str | None = None
    mode: CardRequestsMode
    selection_available: bool


class SelectRequestResponse(BaseModel):
    """AC-01 стори 5б.2: ответ выбора автора — `200 {card_uuid, chat_uuid}`.

    `card_uuid` нужен клиенту для `router.refresh()` без второго чтения
    карточки: автор и так на её экране, а повторный `GET` дал бы гонку
    с собственным `commit`. `chat_uuid` — идентификатор чата компании;
    таблицы `chats` пока нет (эпик 6), но контракт значения уже действует:
    `chats.uuid = event_cards.chat_uuid` (D-5b.2-1), НЕ генерировать новое.

    Ни `selected_request_uuid`, ни числа отклонённых здесь НЕТ и не будет
    (FR81): ответ не должен позволять отличить, сколько заявок отклонено.
    """

    card_uuid: uuid_lib.UUID
    chat_uuid: uuid_lib.UUID
