"""Сервис модерации карточек (стори 4.3, Task 7; AC-01, AC-03…AC-06).

ADR-08: модерация карточек живёт ОТДЕЛЬНО от пайплайна фото (`PhotoStatus`
не переиспользуется). Методы названы по объекту (`approve_card`/`reject_card`,
а не `approve`), чтобы при объединении с `moderate_photo` (долг X-23) не
пришлось переименовывать вызовы в роутере и тестах.

**Порядок проверок в `approve`/`reject`** (тесты фиксируют именно его):

1. карточка существует? → `404 card_not_found`;
2. карточка в `draft`? → иначе `409 CARD_ALREADY_PUBLISHED`;
3. текущий `verification_status`? → `approve` на `approved` даёт `409
   CARD_ALREADY_VERIFIED` (OQ-3), `reject` на `rejected` — `200` с заменой
   причины (OQ-4);
4. запись → один `commit`.

Проверка «опубликована» идёт ДО проверки статуса проверки: на опубликованной
карточке любое решение проверки запрещено, и «уже одобрено» было бы менее
точным ответом, чем «уже опубликовано» (OQ-5: снятие с ленты — предмет
жалоб 6.x, а не модерации).
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CardVerificationStatus, EventCardStatus
from app.models.event_card import EventCard
from app.repositories.event_cards import EventCardRepository
from app.schemas.moderation import (
    MAX_MODERATION_PAGE_SIZE,
    MODERATION_PAGE_SIZE,
    REJECTION_REASON_MAX_LENGTH,
)
from app.services.age_service import compute_age, today_or_now
from app.services.card_content_rules import CardContent, evaluate_card_content
from app.services.moderation_cursor import (
    InvalidCursorError,
    decode_moderation_cursor,
    encode_moderation_cursor,
)

# AC-04: карточки нет (или она недоступна модератору по uuid).
MODERATION_CARD_NOT_FOUND_CODE = "card_not_found"
# AC-04 (OQ-3): повторное одобрение — конфликт состояния, не успех.
CARD_ALREADY_VERIFIED_CODE = "CARD_ALREADY_VERIFIED"
# AC-04/AC-05 (OQ-5): решение по опубликованной карточке запрещено.
CARD_ALREADY_PUBLISHED_CODE = "CARD_ALREADY_PUBLISHED"
# AC-05 (FR113): отклонение без причины.
REJECTION_REASON_REQUIRED_CODE = "rejection_reason_required"
# CR 4.3, патч 4 (AC-05, FR113): причина длиннее предела схемы (500 символов).
REJECTION_REASON_TOO_LONG_CODE = "rejection_reason_too_long"
# AC-08: битый курсор — ошибка ввода, не 500.
INVALID_CURSOR_CODE = "invalid_cursor"


class ModerationSettings(Protocol):
    """Минимум настроек для очереди (не весь `Settings`).

    `default_city_slug` — срез сообщества (CR 4.3, патч 1, ADR-01):
    без него очередь отдавала карточки чужих городов.
    """

    card_content_stop_words: tuple[str, ...]
    default_city_slug: str


class ModerationCardNotFoundError(Exception):
    """AC-04: карточки с таким `uuid` нет."""

    code = MODERATION_CARD_NOT_FOUND_CODE


class CardAlreadyVerifiedError(Exception):
    """AC-04 (OQ-3): карточка уже `approved` человеком или автопроверкой.

    `200` сделал бы `verified_at`/`verified_by` неотличимыми от «кто проверил
    первым», а история решений стала бы недостоверной.
    """

    code = CARD_ALREADY_VERIFIED_CODE


class CardAlreadyPublishedError(Exception):
    """AC-04/AC-05 (OQ-5): карточка уже не черновик.

    Проверка идёт ДО статуса проверки: на опубликованной карточке любое
    решение модерации запрещено, а снятие с ленты — предмет жалоб (6.x).
    """

    code = CARD_ALREADY_PUBLISHED_CODE


class RejectionReasonTooLongError(Exception):
    """AC-05 (FR113): причина длиннее 500 символов.

    Отдельный класс, а не `RejectionReasonRequiredError`: «пусто» и «слишком
    длинно» требуют от клиента разных действий, и общий код заставил бы его
    гадать (тот же принцип, что `CARD_REJECTED` vs `VERIFICATION_PENDING`).

    Найдено ревью исполнением: схема отбивает >500 в Pydantic, но админский
    скрипт идёт мимо неё, а `rejection_reason` — колонка `Text` без предела:
    100 КБ текста легли бы в карточку очереди без единой ошибки.
    """

    code = REJECTION_REASON_TOO_LONG_CODE


class RejectionReasonRequiredError(Exception):
    """AC-05 (FR113): причина отклонения обязательна.

    Схема отбивает пустую причину в Pydantic, но сервис не полагается на
    инвариант другого слоя: админский скрипт вызывает сервис напрямую.
    """

    code = REJECTION_REASON_REQUIRED_CODE


class ModerationCursorError(Exception):
    """AC-08: курсор не разбирается — `422 invalid_cursor`.

    Доменная ошибка, а не `InvalidCursorError` из кодека: HTTP-слой отображает
    только доменные, и пропущенный наверх кодек дал бы `500` на вводе клиента
    (тот же приём, что `PlaceCursorError` в `place_service`).
    """

    code = INVALID_CURSOR_CODE


@dataclass(frozen=True, slots=True)
class ModerationQueueEntry:
    """AC-03: строка очереди, готовая к сборке Pydantic-схемы.

    Не `ModerationCardItem` напрямую: сервис не импортирует ответ HTTP-слоя
    (иначе `schemas` → `services` → `schemas` и лишняя связность). Роутер
    перекладывает поля — это одна функция на страницу.

    `author_uuid` всегда есть (FK `users` NOT NULL); `author_name`/`author_age`
    могут быть `None` — анкеты может не быть (D-4.3-6).
    """

    card: EventCard
    author_uuid: uuid_lib.UUID
    author_name: str | None
    author_age: int | None
    place_uuid: uuid_lib.UUID
    place_name: str
    failed_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModerationQueuePage:
    """AC-03: страница очереди и `total` без курсора (D-4.3-7)."""

    items: list[ModerationQueueEntry] = field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None


async def list_moderation_queue(
    session: AsyncSession,
    *,
    status: str = "pending",
    cursor: str | None = None,
    limit: int = MODERATION_PAGE_SIZE,
    settings: ModerationSettings,
    repository: EventCardRepository | None = None,
) -> ModerationQueuePage:
    """AC-03, AC-08: очередь карточек с автопроверкой на чтении.

    `status` уже проверен `Literal`-ом схемы роутера; здесь — только значение
    для запроса: `'all'` означает «без фильтра» (D-4.3-4).

    `auto_check` пересчитывается ЗДЕСЬ (D-4.3-6): отдельной колонки нет,
    а текст карточки — вход чистой функции. Модератор видит ПРИЧИНУ попадания
    в очередь (`stop_word`, `link`), а не голый `pending`.

    Курсор: `InvalidCursorError` кодека превращается в доменную
    `ModerationCursorError` — иначе пользовательский query-параметр дал бы `500`.

    `commit()` не вызывается — чтение (D-4 стори 2.2).
    """
    repo = repository if repository is not None else EventCardRepository(session)
    # CR 4.3, патч 1 (ADR-01): срез сообщества. Слаг из настроек, не из запроса.
    city_slug = settings.default_city_slug

    after: tuple[datetime, uuid_lib.UUID] | None = None
    if cursor is not None:
        try:
            after = decode_moderation_cursor(cursor)
        except InvalidCursorError as error:
            raise ModerationCursorError() from error

    # Сравнение с ЭФФЕКТИВНЫМ пределом, который применит репозиторий
    # (CR 3.3, патч 4): иначе `limit=150` из скрипта дал бы `LIMIT 150`
    # и потерянный `next_cursor` при `total > 150`.
    effective_limit = min(limit, MAX_MODERATION_PAGE_SIZE)
    rows, total = await repo.list_queue_page(
        status_filter=status,
        city_slug=city_slug,
        after=after,
        limit=effective_limit + 1,
    )

    next_cursor: str | None = None
    if len(rows) > effective_limit:
        # Лишняя строка — признак «есть дальше». Курсор снимается с последней
        # строки УЖЕ УСЕЧЁННОЙ страницы, а не с выброшенной: иначе следующая
        # страница началась бы после невидимой клиенту строки (потеря).
        rows = rows[:effective_limit]
        last_row = rows[-1]
        last_card: EventCard = last_row[0]
        next_cursor = encode_moderation_cursor(last_card.created_at, last_card.uuid)

    entries: list[ModerationQueueEntry] = []
    for row in rows:
        (
            card,
            author_uuid,
            author_name,
            author_age,
            author_birth_date,
            place_uuid,
            place_name,
        ) = row
        check = evaluate_card_content(
            CardContent(
                title=card.title,
                description=card.description,
                category=card.category,
            ),
            stop_words=settings.card_content_stop_words,
        )
        entries.append(
            ModerationQueueEntry(
                card=card,
                author_uuid=author_uuid,
                author_name=author_name,
                # Стори 12.2, AC-3: возраст вычисляется из `birth_date`.
                author_age=compute_age(author_birth_date, author_age, today_or_now()),
                place_uuid=place_uuid,
                place_name=place_name,
                failed_rules=check.failed_rules,
            )
        )

    return ModerationQueuePage(items=entries, total=total, next_cursor=next_cursor)


async def approve_card(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    moderator_id: int,
    settings: ModerationSettings,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-04: ручное одобрение карточки модератором.

    `verified_by` = id модератора ИЗ СЕССИИ, а не из тела: клиент не выбирает,
    кто проверил. `verified_at` — серверный момент (`now` — параметр только
    для тестов, как в `create_card`).

    `rejection_reason` стирается: причина относилась к прошлому решению и после
    одобрения вводила бы автора в заблуждение (FR113: причина есть ТОЛЬКО
    у отклонённой карточки — это же держит CHECK `ck_event_cards_rejection_reason`).
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = repository if repository is not None else EventCardRepository(session)

    # CR 4.3, патч 2 (ADR-13): решение только в своём сообществе.
    card = await repo.get_for_moderation(card_uuid, city_slug=settings.default_city_slug)
    if card is None:
        raise ModerationCardNotFoundError()
    if card.status != EventCardStatus.DRAFT:
        raise CardAlreadyPublishedError()
    if card.verification_status == CardVerificationStatus.APPROVED:
        raise CardAlreadyVerifiedError()

    card.verification_status = CardVerificationStatus.APPROVED
    card.verified_by = moderator_id
    card.verified_at = moment
    card.rejection_reason = None
    await session.commit()
    return card


async def reject_card(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    moderator_id: int,
    reason: str,
    settings: ModerationSettings,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-05: отклонение карточки с причиной (FR113).

    Повторный `reject` на уже отклонённой карточке — `200` с ЗАМЕНОЙ причины
    (OQ-4): модератор уточняет формулировку, а `409` гнал бы его в SQL.
    Идемпотентность по результату — статус остаётся `rejected`.

    Причина проверяется здесь, а не только схемой: админский скрипт идёт
    мимо Pydantic, а CHECK в БД поймал бы пустую строку только как
    `IntegrityError` → `500` вместо `422`.
    """
    if not reason or not reason.strip():
        raise RejectionReasonRequiredError()
    # CR 4.3, патч 4 (FR113): предел длины — и в сервисе, не только в схеме.
    # Админский скрипт идёт мимо Pydantic; `rejection_reason` — `Text` без предела.
    if len(reason) > REJECTION_REASON_MAX_LENGTH:
        raise RejectionReasonTooLongError()

    moment = now if now is not None else datetime.now(UTC)
    repo = repository if repository is not None else EventCardRepository(session)

    # CR 4.3, патч 2 (ADR-13): решение только в своём сообществе.
    card = await repo.get_for_moderation(card_uuid, city_slug=settings.default_city_slug)
    if card is None:
        raise ModerationCardNotFoundError()
    if card.status != EventCardStatus.DRAFT:
        raise CardAlreadyPublishedError()

    card.verification_status = CardVerificationStatus.REJECTED
    card.verified_by = moderator_id
    card.verified_at = moment
    card.rejection_reason = reason
    await session.commit()
    return card
