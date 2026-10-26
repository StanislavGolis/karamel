"""Удаление аккаунта с 7-дневным окном (стори 10.2; AC-01, AC-03, AC-05).

**Три маршрута, и только они** (D-10.2-3): `POST /me/delete`,
`GET /me/delete`, `DELETE /me/delete`.

**Почему отдельный модуль, а не ветка в `profiles.py`** (Task 4.4). Другой
ресурс (`/me/delete` — операция над УЧЁТКОЙ, анкета — про человека) и другой
набор кодов (`deletion_already_requested`). Расширять `GET /profiles/me`
полем `deletion_*` отказано (D-10.2-6): один ответ не смешивает две оси
(project-context §6). Прецедент разделения — `notifications.py` (9.3).

**Гард — `CurrentUser`, не `ConsentUser`** (как в `support.py`, D-9.5-3):
удаление учётки не раскрывает ПД третьих лиц. Следствие: забаненный получает
`403 user_blocked` раньше, чем дойдёт до сервиса.

**Порядок «commit → S3»** (D-10 стори 2.4) здесь не нужен: физическое
удаление делает воркер (`workers/retention.py`), а роутер только ставит и
снимает пометку.

**Бизнес-логики здесь нет** (project-context §6): предикат окна, расчёт
`purge_after` и идемпотентность живут в `services/account_deletion_service.py`.
Здесь только HTTP-форма: статусы, конверт ошибки и `cache-control`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.db import get_session
from app.services.account_deletion_service import (
    DeletionAlreadyRequestedError,
    DeletionStatus,
    cancel_deletion,
    read_status,
    request_deletion,
)

router = APIRouter(prefix="/me", tags=["me"])

#: AC-01, AC-03: удаление — ПД-операция, прокси не должен кешировать ни один ответ.
_NO_STORE = {"cache-control": "no-store"}


def _iso_utc(value: datetime) -> str:
    """AC-01, AC-05: ISO 8601 UTC с `Z` (project-context §4).

    Тот же формат, что `schemas/user.py` отдаёт для `created_at`: naive
    трактуется как UTC, иначе `astimezone` взял бы зону процесса.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class DeletionStatusResponse(BaseModel):
    """AC-01, AC-05: единственный источник даты окна для клиента (D-10.2-6).

    **P-16 (CR 10.2): поля — `str`, а не `datetime`.** До правки модель
    объявляла `datetime`, а обработчик возвращал `JSONResponse` с уже
    отформатированной строкой `...Z`: FastAPI отдаёт `JSONResponse` как есть,
    поэтому `response_model` был декоративен, а OpenAPI обещал клиенту
    `date-time` вместо приходящей строки. Теперь тип в схеме совпадает
    с фактическим ответом, и `response_model` реально сериализует.
    """

    deletion_requested_at: str | None
    purge_after: str | None


class DeletionConflictResponse(BaseModel):
    """AC-01, D-10.2-7: тело `409` — код причины и неизменный `purge_after`.

    Объявлено моделью (P-16): без этого OpenAPI не описывал `409` вообще,
    и клиент узнавал о поле `purge_after` только из текста ошибки.
    """

    code: str
    purge_after: str


_STATUS_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": DeletionConflictResponse, "description": "Окно уже идёт (D-10.2-7)"},
}


def _to_response(status: DeletionStatus) -> DeletionStatusResponse:
    """Ответ статуса окна. Вынесен: `POST` и `GET` отдают один контракт.

    Даты форматируются ЗДЕСЬ, до модели (P-16): поля объявлены `str`, поэтому
    `model_validate` на объекте с `datetime` дал бы `ValidationError` —
    ровно это и случилось при первой правке. Форматирование в конструкторе
    оставляет единственный источник формата (`_iso_utc`) и честную схему OpenAPI.
    """
    return DeletionStatusResponse(
        deletion_requested_at=(
            None if status.deletion_requested_at is None else _iso_utc(status.deletion_requested_at)
        ),
        purge_after=None if status.purge_after is None else _iso_utc(status.purge_after),
    )


@router.post("/delete", response_model=DeletionStatusResponse, responses=_STATUS_RESPONSES)
async def request_my_deletion(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> DeletionStatusResponse | JSONResponse:
    """AC-01: пометить учётку на удаление; доступ сохраняется 7 дней.

    `409 deletion_already_requested` с `purge_after` (D-10.2-7): повторный
    `POST` окно НЕ продлевает, и клиент обязан видеть неизменный срок.
    Не `200`: клиент не должен решать, продлилось окно или нет.

    `403 user_blocked` после истечения окна отдаёт гард `CurrentUser`
    (AC-06) — до сервиса дело не доходит.
    """
    try:
        await request_deletion(session, user, now=datetime.now(UTC))
    except DeletionAlreadyRequestedError as exc:
        return JSONResponse(
            {"code": exc.code, "purge_after": _iso_utc(exc.purge_after)},
            status_code=409,
            headers=_NO_STORE,
        )
    await session.commit()
    # P-16: заголовок ставится на реальный `Response`, а не на обходной `JSONResponse`.
    response.headers.update(_NO_STORE)
    return _to_response(read_status(user))


@router.get("/delete", response_model=DeletionStatusResponse)
async def read_my_deletion(
    user: CurrentUser,
    response: Response,
) -> DeletionStatusResponse | JSONResponse:
    """AC-05: статус окна — `deletion_requested_at` и `purge_after`.

    Без пометки оба поля `null`: клиент различает «окна нет» по `null`,
    а не по отсутствию ключей. Другого источника даты у клиента нет.
    """
    response.headers.update(_NO_STORE)
    return _to_response(read_status(user))


@router.delete("/delete", status_code=204, response_class=Response, response_model=None)
async def cancel_my_deletion(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """AC-03: снять пометку. `204` на ЛЮБОЙ `DELETE`, в том числе повторный.

    Не `404` (D-10.2-7): пользователь мог нажать дважды, и второй ответ
    «не найдено» показал бы ошибку на успешной операции. После физического
    удаления `401` срабатывает раньше гарда — до этой ветки дело не дойдёт.
    """
    await cancel_deletion(session, user)
    await session.commit()
    return Response(status_code=204, headers=_NO_STORE)
