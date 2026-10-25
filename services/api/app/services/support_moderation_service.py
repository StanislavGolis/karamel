"""Очередь обращений и ответ модератора (стори 9.6; AC-01…AC-04).

**Порядок — КОНТРАКТ** (D-3, D-29, урок CR 8.2 H-1): гард (в роуте) → разбор
входа (курсор) → чтение. Битый курсор разбирается ПОСЛЕ гарда: иначе код
ответа на один и тот же битый вход зависел бы от того, кто спрашивает.

**`commit` НЕ здесь** (§9, ADR-11): единица работы закрывается в роуте, после
неё вызывается уведомление. Коммит в сервисе разорвал бы связку «ответ записан
→ уведомление отправлено» на две независимые транзакции и позволил бы
уведомлению уйти до того, как ответ стал виден в `GET /support/tickets`.

**Один ответ на обращение** (D-9.6-5, У-4): повтор — `409`, не перезапись.
Проверка идёт под `SELECT … FOR UPDATE` (D-47): без блокировки двое модераторов
оба увидели бы `open`, и «кто ответил» решал бы порядок планировщика. CAS в
`UPDATE ... WHERE status='open'` — вторая линия обороны: даже если блокировка
отпущена между проверкой и записью, перезаписи не произойдёт.

**Три класса ошибок, а не `ValueError`** (D-9.6-7): `404` и `409` — РАЗНЫЕ коды.
Слитый код заставил бы кабинет перечитывать очередь ради объяснения отказа, а
различие не раскрывает лишнего: модератор видит все обращения и так.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.support_tickets import SupportTicketsRepository
from app.repositories.users import UserRepository
from app.schemas.support_moderation import (
    ModerationSupportQueuePage,
    ModerationTicketItem,
    SupportReplyRequest,
)
from app.services.notification_service import notify_support_replied
from app.services.profile_normalize import normalize_bio
from app.services.reports_queue_cursor import decode_queue_cursor, encode_queue_cursor

__all__ = [
    "TICKET_ALREADY_ANSWERED_CODE",
    "TICKET_ANSWER_REQUIRED_CODE",
    "TICKET_NOT_FOUND_CODE",
    "TicketAlreadyAnsweredError",
    "TicketAnswerRequiredError",
    "TicketNotFoundError",
    "list_queue",
    "reply_to_ticket",
]

logger = structlog.get_logger(__name__)

#: AC-02: обращения нет — `404`.
TICKET_NOT_FOUND_CODE = "ticket_not_found"

#: AC-02, D-9.6-5: обращение уже отвечено — `409`, не перезапись.
TICKET_ALREADY_ANSWERED_CODE = "ticket_already_answered"

#: AC-02, D-9.6-4: пустой ответ после нормализации — `422 invalid_request`.
#: Тот же код, что у схемы: два разных кода на «пустой текст» дали бы клиенту
#: два вида одной ошибки в зависимости от того, где его поймали.
TICKET_ANSWER_REQUIRED_CODE = "invalid_request"


def _to_item(row: dict[str, Any]) -> ModerationTicketItem:
    """Строка репозитория → элемент ответа (одна точка сборки схемы).

    Отдельная функция, а не `ModerationTicketItem(**row)` в двух местах:
    состав полей элемента — контракт (`extra="forbid"`), и вторая сборка
    разошлась бы с первой при добавлении колонки (класс дефекта 4.1).
    """
    return ModerationTicketItem(
        uuid=row["uuid"],
        body=row["body"],
        status=row["status"],
        created_at=row["created_at"],
        answer=row["answer"],
        answered_at=row["answered_at"],
        author_uuid=row["author_uuid"],
    )


class TicketNotFoundError(Exception):
    """AC-02: обращения с таким `uuid` нет — `404 ticket_not_found`."""

    code = TICKET_NOT_FOUND_CODE


class TicketAlreadyAnsweredError(Exception):
    """AC-02, D-9.6-5: обращение уже отвечено — `409 ticket_already_answered`.

    Не «уже отвечено, вот ответ»: правка ответа продуктово не решена
    (долг `L-9.6-2`), и возврат чужого текста без запроса был бы раскрытием
    данных в ответ на действие, которое их не запрашивало.
    """

    code = TICKET_ALREADY_ANSWERED_CODE


class TicketAnswerRequiredError(Exception):
    """AC-02, D-9.6-4: пустой ответ после нормализации — `422 invalid_request`.

    Второй рубеж после схемы: `reply_to_ticket` может быть вызван мимо
    `SupportReplyRequest` (воркер, тест, будущий потребитель), и пустая строка
    в `UPDATE` записала бы `answered` с пустым ответом — бикондиционал
    `ck_answer_pair` такое пропускает (непустота не проверяется на `answer`).
    """

    code = TICKET_ANSWER_REQUIRED_CODE


async def list_queue(
    session: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
) -> ModerationSupportQueuePage:
    """AC-01, AC-04, D-9.6-3, D-9.6-13: страница очереди ОТКРЫТЫХ обращений.

    **`limit + 1` строк** (D-9.6-13): лишняя строка — признак «есть следующая
    страница» без второго запроса. Курсор собирается из последней ВИДИМОЙ
    строки, а не из отброшенной: курсор из скрытой строки сдвинул бы чтение
    вперёд, и первый элемент следующей страницы потерялся бы молча.

    Число SQL — константа: страница (1) + `total` (1). Запрос по строке дал бы
    зависимость от `limit` (ось NFR39).
    """
    position: tuple[Any, uuid_lib.UUID] | None = None
    if cursor is not None:
        # D-9.6-2: кодек курсора переиспользуется из 8.3/9.5 в ИСПРАВЛЕННОМ
        # CR 9.5 MEDIUM-1 виде. Битое значение — `InvalidCursorError` (`422`).
        position = decode_queue_cursor(cursor)

    repository = SupportTicketsRepository
    rows = await repository.list_open(session, limit=limit + 1, cursor=position)
    total = await repository.count_open(session)

    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor: str | None = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = encode_queue_cursor(last["created_at"], last["uuid"])

    return ModerationSupportQueuePage(
        items=[_to_item(row) for row in visible],
        total=total,
        cursor=next_cursor,
    )


async def reply_to_ticket(
    session: AsyncSession,
    *,
    ticket_uuid: uuid_lib.UUID,
    payload: SupportReplyRequest,
    moderator_id: int,
) -> tuple[ModerationTicketItem, int]:
    """AC-02, D-9.6-5, D-9.6-6: записывает ответ; возвращает элемент и автора.

    Порядок: нормализация ответа → блокировка строки → `404`/`409` → CAS.

    `commit` НЕ здесь (§9): роут коммитит домен, и только потом уходит
    уведомление (ADR-11). Возвращаемый `author_id` — адресат уведомления,
    прочитанный ПОД той же блокировкой, что и запись: между чтением и отправкой
    автор не может быть подменён (D-47).
    """
    answer = normalize_bio(payload.answer)
    if not answer.strip():
        # D-9.6-4: второй рубеж после схемы — см. `TicketAnswerRequiredError`.
        raise TicketAnswerRequiredError(TICKET_ANSWER_REQUIRED_CODE)

    repository = SupportTicketsRepository
    locked = await repository.select_for_update(session, ticket_uuid=ticket_uuid)
    if locked is None:
        raise TicketNotFoundError(TICKET_NOT_FOUND_CODE)
    if locked["status"] != "open":
        # D-9.6-5: повтор — `409`. Проверка под блокировкой: второй модератор
        # не увидит `open` у строки, которую только что ответил первый.
        raise TicketAlreadyAnsweredError(TICKET_ALREADY_ANSWERED_CODE)

    row = await repository.reply(
        session,
        ticket_uuid=ticket_uuid,
        answer=answer,
        moderator_id=moderator_id,
    )
    if row is None:
        # CAS не сработал: строку ответили между проверкой и записью.
        raise TicketAlreadyAnsweredError(TICKET_ALREADY_ANSWERED_CODE)

    return _to_item(row), int(locked["user_id"])


async def notify_author_of_reply(
    session: AsyncSession,
    *,
    ticket_uuid: uuid_lib.UUID,
    author_id: int,
) -> bool:
    """AC-02, D-9.6-8, D-9.2-12: уведомление автору ПОСЛЕ `commit`.

    Вызывается роутом после `session.commit()` (ADR-11): сетевой вызов внутри
    транзакции держал бы блокировку строки на время round-trip'а к Redis.

    **Автор отсутствует** (удалён между ответом и уведомлением) — `warning`
    и `False`, без исключения: домен уже зафиксирован, и падение уведомления
    не имеет права превратить успешный ответ в `500` (D-9.2-12).

    **Исключение конвейера** ловится здесь же: `_create_publish_mark` уже
    возвращает `False` вместо проброса, но чтение пользователя идёт ДО него и
    тоже может упасть на недоступной БД — `rollback` и `False`, не `raise`.
    """
    try:
        user = await UserRepository(session).get_by_id(author_id)
        if user is None:
            logger.warning("notification.recipient.missing")
            return False
        return await notify_support_replied(
            session,
            user_uuid=user.uuid,
            user_id=user.id,
            ticket_uuid=ticket_uuid,
        )
    except Exception as exc:  # noqa: BLE001 — домен уже закоммичен, ответ успешен
        await session.rollback()
        logger.error(
            "notification.create.failed",
            notification_type="support.replied",
            error=type(exc).__name__,
        )
        return False
