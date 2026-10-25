"""Очередь жалоб и фрагмент переписки для модератора (стори 8.3).

**Порядок — КОНТРАКТ** (D-3, D-29): гард `ModeratorUser` → резолв субъекта →
разбор входа (курсор) → чтение. Битый курсор при несуществующей жалобе даёт
`404`, при существующей — `422`: это документированное поведение, а не оракул.
Роль модератора уже известна из сессии, поэтому код ответа не раскрывает
ничего, чего клиент не знает.

**Только чтение.** `commit()` не вызывается ни на одном маршруте: 8.3 ничего
не пишет. Единственный побочный эффект — строка аудита в логе (AC-03).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditedModeratorUser, ModeratorUser
from app.core.db import get_session
from app.schemas.report import (
    ReportFragmentResponse,
    ReportQueuePage,
    Resolution,
    ResolvedStatus,
    ResolveReportRead,
    ResolveReportRequest,
)
from app.schemas.ws import WsEnvelope
from app.services.moderation_reports_service import (
    ReportNotFoundError,
    list_reports,
    read_fragment,
)
from app.services.moderation_resolve_service import (
    SANCTION_ISSUED_TYPE,
    ReportAlreadyResolvedError,
    SanctionNotApplicableError,
    resolve_report,
)
from app.services.notification_delivery import mark_notifications_delivered
from app.services.place_cursor import InvalidCursorError
from app.services.ws_publisher import publish_to_chat

logger = structlog.get_logger(__name__)

#: NFR19: явный код отказа при недоступной БД — как у остальных приватных
#: ответов (образец `reports.py` и `chats.py`).
SERVICE_UNAVAILABLE_CODE = "service_unavailable"

__all__ = ["router"]

router = APIRouter(prefix="/moderation/reports", tags=["moderation"])

_NO_STORE = {"cache-control": "no-store"}

#: §7.1: конверт пагинации — те же границы, что у остальных списков.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": …}` с `no-store`."""
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


@router.get("", response_model=ReportQueuePage)
async def list_reports_endpoint(
    _moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    target_type: Annotated[Literal["no_show", "message"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> ReportQueuePage | JSONResponse:
    """AC-01, AC-04, AC-05: очередь ОТКРЫТЫХ жалоб по возрасту.

    **`target_type` типизирован `Literal`** (D-4.3-4): мусор отбивает Pydantic
    → `422` без новой ветки маппинга. Отсутствие параметра — все открытые
    жалобы обоих типов (D-31: разобранные в очередь не попадают).

    **`overdue`** — по `reported_at` и порогу `REPORTS_OVERDUE_HOURS` (NFR52,
    NFR63): модератор видит, что жалоба вышла за 20 часов, не сверяясь с
    часами вручную.

    **Число SQL не зависит от `limit`** (ось NFR39, мутация M7): страница,
    `total` и факты явки — по одному запросу.

    Битый `cursor` — `422 invalid_cursor`, не `500`: значение пришло от
    клиента, и обвинять сервер не в чем.
    """
    try:
        return await list_reports(session, target_type=target_type, limit=limit, cursor=cursor)
    except InvalidCursorError:
        return _error(InvalidCursorError.code, 422)


@router.get("/{report_uuid}/fragment", response_model=ReportFragmentResponse)
async def read_fragment_endpoint(
    report_uuid: uuid_lib.UUID,
    request: Request,
    moderator: AuditedModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReportFragmentResponse | JSONResponse:
    """AC-02, AC-03: не более 10 последних сообщений диалога + аудит доступа.

    **`limit` здесь НЕТ намеренно** (D-32, FR40, NFR27): ограничение объёма
    раскрываемой переписки не может зависеть от клиента, поэтому
    `FRAGMENT_LIMIT = 10` живёт константой в сервисе. `?limit=1000` не
    принимается — параметр неизвестен маршруту.

    `404 report_not_found` — жалобы нет ИЛИ она не про переписку. Один код на
    оба случая: различие не даёт модератору ничего, а лишняя ветка маппинга
    стала бы вторым источником правды.
    """
    try:
        result = await read_fragment(
            session,
            report_uuid=report_uuid,
            moderator_id=moderator.id,
            moderator_uuid=moderator.uuid,
            route=request.url.path,
        )
    except ReportNotFoundError as error:
        return _error(error.code, 404)

    # Стори 8.6 (AC-01, D-90): `commit` — здесь, в роуте. 8.3 не коммитил
    # вовсе (только читал); запись аудита обязана зафиксироваться, и при
    # ошибке записи ответ не уходит, а строки не остаётся.
    await session.commit()
    return result


@router.post("/{report_uuid}/resolve", response_model=ResolveReportRead)
async def resolve_report_endpoint(
    report_uuid: uuid_lib.UUID,
    payload: ResolveReportRequest,
    moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResolveReportRead | JSONResponse:
    """AC-01, AC-05, AC-10: решение по жалобе — предупреждение, бан или закрытие.

    **`ModeratorUser`, а не `ConsentUser`** (D-3 стори 3.2): у модератора может
    не быть анкеты и согласия на ПД, а решение выносить он обязан. Бан и
    отзыв сессии уже проверены `get_current_user`.

    **`resolution` типизирован `Literal`** (D-45): мусор отбивает Pydantic →
    `422` без новой ветки маппинга.

    Коды (D-48): `200` — решение принято; `404 report_not_found` — жалобы нет;
    `409 REPORT_ALREADY_RESOLVED` — уже решена (в том числе вторым модератором);
    `422 SANCTION_NOT_APPLICABLE` — FR89: санкция за `disputed`/`confirmed` пару.

    **`reason` в лог не пишется** (NFR32): логируются адрес жалобы, исход и
    модератор — не текст причины.
    """
    try:
        result = await resolve_report(
            session,
            report_uuid=report_uuid,
            moderator_id=moderator.id,
            resolution=payload.resolution,
            reason=payload.reason,
        )
    except ReportNotFoundError as error:
        return _error(error.code, 404)
    except ReportAlreadyResolvedError as error:
        return _error(error.code, 409)
    except SanctionNotApplicableError as error:
        return _error(error.code, 422)
    except OperationalError:
        logger.error("moderation.resolve.db_unavailable", report_uuid=str(report_uuid))
        return _error(SERVICE_UNAVAILABLE_CODE, 503)

    # ADR-11, R8: `commit` — в роуте, публикация — ПОСЛЕ него. Иначе подписчик
    # увидел бы событие о решении, которого ещё нет в БД.
    await session.commit()

    logger.info(
        "moderation.report.resolved",
        report_uuid=str(report_uuid),
        resolution=result.resolution,
        moderator_uuid=str(moderator.uuid),
    )

    # D-50: канал `chat:{uuid}` существует только у жалобы на переписку.
    # Для `no_show` публикации нет — пользовательского канала в системе нет,
    # и уведомление живёт строкой в `notifications` (эпик 9 его покажет).
    if result.chat_uuid is not None:
        await publish_to_chat(
            chat_uuid=result.chat_uuid,
            envelope=WsEnvelope.build(
                SANCTION_ISSUED_TYPE,
                {
                    "report_uuid": str(report_uuid),
                    "resolution": result.resolution,
                    "sanction_uuid": (str(result.sanction_uuid) if result.sanction_uuid else None),
                },
            ),
        )
        await mark_notifications_delivered(
            session, report_uuid=report_uuid, type_=SANCTION_ISSUED_TYPE
        )

    # `cast` — не ослабление схемы: `ResolveResult` приходит из сервиса, где
    # `resolution` уже отбит `Literal`-ом тела запроса, а `status` выведен из
    # `_RESOLUTION_TO_STATUS` (три значения, все в домене схемы). Аннотация
    # датакласса — `str`, потому что он же несёт `report_uuid`/`chat_uuid` без
    # доменных алиасов; дублировать `Literal` в двух местах значило бы
    # поддерживать два источника правды.
    return ResolveReportRead(
        report_uuid=result.report_uuid,
        status=cast("ResolvedStatus", result.status),
        resolution=cast("Resolution", result.resolution),
        sanction_uuid=result.sanction_uuid,
        resolved_at=result.resolved_at,
    )
