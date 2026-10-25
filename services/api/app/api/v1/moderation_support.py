"""Очередь обращений и ответ модератора (стори 9.6; AC-01…AC-04).

**Отдельный модуль, а не ветка `support.py`**: другой гард (`ModeratorUser`
против `CurrentUser`), другой ресурс (`/moderation/support`) и другой набор
кодов (`ticket_not_found`, `ticket_already_answered`). Ветка внутри
пользовательского роутера поставила бы модераторский гард в модуль, где
остальные маршруты его не требуют.

**Порядок `POST` — КОНТРАКТ** (D-9.6-8, ADR-11): гард → блокировка+`404`/`409`
→ CAS → `commit()` → уведомление → ответ. Сетевой вызов ПОСЛЕ коммита: внутри
транзакции он держал бы блокировку строки на время round-trip'а к Redis, а
падение Redis откатило бы уже принятый ответ.

**`no-store` на всех ответах ставит `NoStoreMiddleware`** (QD A6,
`core/no_store.py`), а не роутер. Ручная установка в успешных ветках была бы
ВТОРЫМ, независимо поддерживаемым путём заголовка (прецедент `cards.py`,
`places.py`, `moderation.py`): middleware видит готовый заголовок и второй не
добавляет, поэтому снятие ручной строки НЕ наблюдаемо ни одним ответом —
мутация получается эквивалентной (CR 9.6 MEDIUM-1, замерено). Тело обращения и
текст ответа — ПД, поэтому требование закрыто проверкой заголовка в тестах
(`test_queue_item_shape`, `test_reply_sets_answered_status`) и мутациями M12/M13
на самом middleware. `_NO_STORE` остаётся для конвертов ошибок `_error` —
единственный способ установки на ошибке в пределах проекта (CR 7.5 L-2).

**Фронта нет** (D-9.6-9): зона `(mod)` — заглушка без компонентов, прецедент
8.3/7.5 (API-only). Долг `L-9.6-1`.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ModeratorUser
from app.core.db import get_session
from app.schemas.support_moderation import (
    ModerationSupportQueuePage,
    ModerationTicketItem,
    SupportReplyRequest,
)
from app.services.place_cursor import InvalidCursorError
from app.services.support_moderation_service import (
    TicketAlreadyAnsweredError,
    TicketAnswerRequiredError,
    TicketNotFoundError,
    list_queue,
    notify_author_of_reply,
    reply_to_ticket,
)

logger = structlog.get_logger(__name__)

__all__ = ["router"]

router = APIRouter(prefix="/moderation/support", tags=["moderation"])

_NO_STORE = {"cache-control": "no-store"}

#: §7.1: конверт пагинации — те же границы, что у остальных списков.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": …}` с `no-store`."""
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


@router.get("/tickets", response_model=ModerationSupportQueuePage)
async def list_tickets_endpoint(
    _moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> ModerationSupportQueuePage | JSONResponse:
    """AC-01, AC-03, AC-04: очередь ОТКРЫТЫХ обращений, старые сверху.

    **Фильтра статуса НЕТ** (D-9.6-3): параметр открыл бы путь
    по неиндексированному полю и позволил бы вернуть отвеченные обращения в
    очередь открытых — против AC-04.

    **Гард `ModeratorUser`, не `AuditedModeratorUser`** (D-9.6-12): аудит 8.6
    написан про переписку ТРЕТЬИХ лиц; обращение адресовано поддержке, и
    строка аудита на каждое чтение очереди стала бы вектором усиления записи
    (D-85). `403` отдаёт зависимость — роутер его не дублирует.

    Битый `cursor` — `422 invalid_cursor`, не `500`: значение пришло от
    клиента. Курсор разбирается ПОСЛЕ гарда (порядок D-3/D-29).
    """
    try:
        result = await list_queue(session, limit=limit, cursor=cursor)
    except InvalidCursorError:
        return _error(InvalidCursorError.code, 422)
    return result


@router.post("/tickets/{ticket_uuid}/reply", response_model=ModerationTicketItem)
async def reply_ticket_endpoint(
    ticket_uuid: uuid_lib.UUID,
    payload: SupportReplyRequest,
    moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ModerationTicketItem | JSONResponse:
    """AC-02, AC-03: ответ модератора — `200` с обновлённым обращением.

    Коды (D-9.6-7): `200` — ответ записан; `404 ticket_not_found` — обращения
    нет; `409 ticket_already_answered` — уже отвечено (повтор, D-9.6-5);
    `422 invalid_request` — пустой `answer` после нормализации.

    `moderator.id` уходит ТОЛЬКО в `UPDATE` (D-9.6-11): «кто ответил» в ответе
    и в payload'е уведомления не раскрывается.
    """
    try:
        item, author_id = await reply_to_ticket(
            session,
            ticket_uuid=ticket_uuid,
            payload=payload,
            moderator_id=moderator.id,
        )
    except TicketNotFoundError as error:
        await session.rollback()
        return _error(error.code, 404)
    except TicketAlreadyAnsweredError as error:
        await session.rollback()
        return _error(error.code, 409)
    except TicketAnswerRequiredError as error:
        await session.rollback()
        return _error(error.code, 422)

    # ADR-11: `commit` — в роуте, до уведомления. Если коммит упадёт, уходит
    # `500` и уведомления не будет: автор не получит «ответили» про ответ,
    # которого нет в БД.
    await session.commit()

    logger.info(
        "moderation.support.replied",
        ticket_uuid=str(ticket_uuid),
        moderator_uuid=str(moderator.uuid),
    )

    # D-9.6-8, ADR-11: уведомление ПОСЛЕ `commit` и вне транзакции. Ошибка
    # уведомления не влияет на ответ (D-9.2-12): домен уже зафиксирован.
    await notify_author_of_reply(session, ticket_uuid=ticket_uuid, author_id=author_id)

    return item
