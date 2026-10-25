"""Настройки канала уведомлений: чтение и переключение одного типа.

(стори 9.3; AC-01, AC-02; FR46)

**`GET` отдаёт ПОЛНЫЙ перечень, `PATCH` меняет ОДИН тип** (D-9.3-8).
Клиент не хранит список типов у себя: локальная копия тихо отстала бы от
серверной, а NFR60 велит игнорировать неизвестное — то есть новый тип не
появился бы в UI никогда. `PUT` (полная замена набора) отклонён: UI
переключает один тип, а замена заставила бы клиент присылать и неотключаемые,
дав ложный `409` на весь запрос из-за одного элемента.

**Права — `CurrentUser`, не `ConsentUser`** (D-9.3-9). Настройка не раскрывает
ПД и не является анкетой: требовать согласие на обработку ПД ради
переключателя «присылать ли уведомления» значило бы закрыть настройку от
пользователя, который ещё не заполнил анкету, но уже получает уведомления.

**Маппинг доменных ошибок — здесь, а не в `main.py`** (тот же приём, что в
`cards.py`/`places.py`/`moderation.py`): второй глобальный
`exception_handler` создал бы два источника правды для одной доменной ошибки.

**Роут НЕ отдаёт список уведомлений** (D-9.3-10, Y-8.4-2 вне 9.3): у списка
нет потребителя в AC эпика 9, и фиктивный роут дал бы ложное «долг закрыт».
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.db import get_session
from app.schemas.notification_settings import (
    NotificationSettingRead,
    NotificationSettingsListResponse,
    NotificationSettingsUpdate,
)
from app.services.notification_settings_service import (
    NotificationSettingItem,
    NotificationTypeRequiredError,
    UnknownNotificationTypeError,
    list_settings,
    set_enabled,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

#: AC-01: ответ привязан к сессии и меняется по действию пользователя.
#: Без `no-store` прокси или браузер мог бы показать настройки прежнего
#: пользователя на том же устройстве после смены аккаунта.
_NO_STORE = {"cache-control": "no-store"}


def _error(code: str, status_code: int) -> JSONResponse:
    """Единый конверт ошибки `{"code": ...}` — как в `auth.py`/`profiles.py`.

    Клиент разбирает одно поле `code` и выбирает текст по нему; свободный
    `message` не парсится (NFR60: неизвестное игнорируется).
    """
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


def _read(item: NotificationSettingItem) -> NotificationSettingRead:
    """Доменный элемент → схема ответа. Одна точка конверсии на оба маршрута.

    Без неё `GET` и `PATCH` собирали бы ответ независимо, и `PATCH` мог бы
    вернуть `required=false` для обязательного типа — UI показал бы
    переключатель доступным, а следующий запрос получил бы `409`.
    """
    return NotificationSettingRead(type=item.type, enabled=item.enabled, required=item.required)


@router.get("/settings", response_model=NotificationSettingsListResponse)
async def read_settings(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> NotificationSettingsListResponse:
    """AC-01, AC-02: полный перечень 11 типов с `enabled` и `required`.

    `CurrentUser` даёт `401` без сессии и `403 user_blocked` забаненному.
    Порядок — `NOTIFICATION_TYPES` (фиксированный), а не порядок строк в БД:
    иначе переключатели «прыгали» бы при каждом изменении настроек.
    """
    items = await list_settings(session, user_id=user.id)
    response.headers["cache-control"] = "no-store"
    return NotificationSettingsListResponse(items=[_read(item) for item in items])


@router.patch("/settings", response_model=NotificationSettingRead)
async def update_settings(
    payload: NotificationSettingsUpdate,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationSettingRead | JSONResponse:
    """AC-01: включает/отключает ОДИН тип. Идемпотентно (D-9.3-12).

    `200` с обновлённым элементом, а не `204`: клиент делает оптимистичное
    обновление и при ошибке откатывается (Task 5.2), поэтому ему нужно
    подтверждённое состояние, а не пустое тело.

    `422 invalid_notification_type` — значение вне перечня 11 или неверная
    форма тела; `409 notification_type_required` — попытка выключить
    `report.resolved`/`sanction.issued` (D-9.3-5). Разные коды дают клиенту
    разные действия: `422` — баг клиента, `409` — «переключатель недоступен».

    Повторный запрос с тем же `enabled` — `200` и одна строка: `INSERT ...
    ON CONFLICT DO NOTHING`/`DELETE` (§18.11). Ошибки «уже так» нет.
    """
    try:
        item = await set_enabled(
            session, user_id=user.id, notification_type=payload.type, enabled=payload.enabled
        )
    except UnknownNotificationTypeError:
        return _error("invalid_notification_type", 422)
    except NotificationTypeRequiredError:
        return _error("notification_type_required", 409)

    # AC-01: `commit` здесь, а не в сервисе: граница транзакции — свойство
    # HTTP-запроса (project-context §9), а сервис обязан оставаться
    # переиспользуемым из воркера, где транзакцией управляет вызывающий.
    await session.commit()
    return _read(item)
