"""Подача жалобы на собеседника в диалоге (стори 8.2; AC-02, AC-04, AC-09).

**Почему отдельный роутер, а не ветка в `cards.py`** (D-23). Жалоба на
неявку (7.4) живёт на карточке: её объект — пара `attendance`, и адресация
идёт через `card_uuid`. Здесь объект — ДИАЛОГ, адресация через `chat_uuid`,
а карточки в запросе нет вовсе. Объединение сломало бы контракт 7.4,
который закрыт и покрыт 17 тестами.

**Порядок «сначала субъект, потом вход»** (AC-09, D-27, `W-1`): сервис
сначала разрешает `chat_uuid` (существование + участие), и только потом
разбирает `accused_uuid`. Следствие — контракт, не дефект: битый UUID
адресата при несуществующем чате даёт `404`, при существующем — `422`.
Клиент не должен ветвиться по этому различию; оно документировано, чтобы
не быть принятым за оракул.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ConsentUser
from app.core.db import get_session
from app.schemas.report import MessageReportRead, MessageReportRequest
from app.schemas.ws import WsEnvelope
from app.services.notification_delivery import mark_notifications_delivered
from app.services.report_service import (
    REPORT_CREATED_TYPE,
    MessageReportAlreadyExistsError,
    ReportSubjectNotFoundError,
    report_message,
)
from app.services.ws_publisher import publish_to_chat

__all__ = ["router"]

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

_NO_STORE = {"cache-control": "no-store"}

#: AC-05 (NFR19): явный код отказа при недоступной БД — как у остальных
#: приватных ответов (образец `chats.py`).
SERVICE_UNAVAILABLE_CODE = "service_unavailable"


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": …}` с `no-store`."""
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


def _domain_error(error: Exception) -> JSONResponse:
    """Доменные ошибки жалобы → HTTP.

    - `404 card_not_found` — чата нет, зритель не участник, обвиняемый не
      сторона диалога ИЛИ жалоба на себя. Один код на все случаи: отдельный
      код выдал бы существование диалога и состав компании (ADR-13);
    - `409 ALREADY_REPORTED` — этот участник уже жаловался на этот диалог
      (AC-05). Код отличается от `ATTENDANCE_ALREADY_REPORTED` (7.4):
      разные объекты, клиенту нужен разный текст (D-24).
    """
    if isinstance(error, ReportSubjectNotFoundError):
        return _error(error.code, 404)
    if isinstance(error, MessageReportAlreadyExistsError):
        return _error(error.code, 409)
    raise error


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MessageReportRead)
async def report_message_endpoint(
    payload: MessageReportRequest,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MessageReportRead | JSONResponse:
    """AC-02 стори 8.2: жалоба на собеседника в диалоге.

    `201` + РОВНО четыре поля ответа. Скрытие диалога происходит в ТОЙ ЖЕ
    транзакции (AC-03): жалоба без скрытия оставила бы стороны в переписке,
    которую они только что прекратили.

    **Порядок разрешения — контракт** (AC-09, `W-1`): `chat_uuid` (существование
    и участие) → `accused_uuid` (валидация адреса) → вставка. Поэтому битый
    UUID адресата даёт `422` ТОЛЬКО на существующем чате: на несуществующем
    сначала срабатывает `404`. Это документированное поведение, а не оракул:
    роль и участие зрителя уже известны из сессии.

    `ConsentUser`: объект жалобы — переписка с персональными данными (FR9),
    без согласия маршрут недоступен.
    """
    try:
        created = await report_message(
            session,
            chat_uuid=payload.chat_uuid,
            viewer_id=user.id,
            accused_uuid=payload.accused_uuid,
        )
    except ReportSubjectNotFoundError as error:
        return _domain_error(error)
    except MessageReportAlreadyExistsError as error:
        return _domain_error(error)
    except OperationalError:
        logger.error("report_message.db_unavailable", chat_uuid=str(payload.chat_uuid))
        return _error(SERVICE_UNAVAILABLE_CODE, 503)

    # Стори 8.2 (AC-08, D-28): скрытие уже в БД, `commit` — здесь: публикация
    # в сервисе была бы ДО фиксации, и подписчик увидел бы `chat.hidden` о
    # состоянии, которого ещё нет (ADR-11, R8).
    await session.commit()

    # AC-08 (D-28): событие в канал чата. Обе стороны уже подписаны.
    await publish_to_chat(
        chat_uuid=payload.chat_uuid,
        envelope=WsEnvelope.build(
            "chat.hidden",
            {"chat_uuid": str(payload.chat_uuid), "report_uuid": str(created.uuid)},
        ),
    )

    # AC-06 (D-26, D-30): `delivered_at` — ПОСЛЕ публикации и ОТДЕЛЬНОЙ
    # транзакцией: основная уже закоммичена, откатывать нечего. Ошибка
    # Redis не откатывает доставку и не рвёт HTTP (R8) — `publish_to_chat`
    # не бросает.
    await mark_notifications_delivered(
        session, chat_uuid=payload.chat_uuid, type_=REPORT_CREATED_TYPE
    )

    return MessageReportRead.from_created(created)
