"""Обращения в поддержку: создание и список своих (стори 9.5; AC-01…AC-04).

**Два маршрута, и только они** (D-9.5-2): `POST /support/tickets` и
`GET /support/tickets`. Маршрута `GET /support/tickets/{uuid}` НЕТ: AC эпика 9
не содержит чтения одного обращения, потребителя у него нет (своих обращений
единицы, экран показывает список целиком), а роут «на будущее» — ложный
контракт (прецедент `D-9.3-10`).

**Гард — `CurrentUser`, не `ConsentUser`** (D-9.5-3): обращение не раскрывает
ПД третьих лиц и не является анкетой (прецедент `D-9.3-9`). Следствие:
забаненный получает `403 user_blocked`, а публичный путь «написать» остаётся
`/privacy#contact` (D-9.5-14).

**Уведомления здесь НЕ создаются** (D-9.5-13): ответ модератора — 9.6, там и
`support.replied`. Тип уже в `NOTIFICATION_TYPES` (11) и в `CHECK` миграции
`0030`; «добавить тип» означало бы правку схемы без потребителя.

**Маппинг доменных ошибок — здесь, а не в `main.py`** (приём `join_requests.py`
и `notifications.py`): глобальный обработчик создал бы второй источник правды
для одной доменной ошибки.

**Границы 9.5/9.6.** Ответ модератора, очередь `/moderation/support/*` — 9.6,
отдельный роутер под `ModeratorUser`. Переходов статуса в 9.5 нет: создание
всегда `open`, `closed` не реализуется нигде (У-4).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.db import get_session
from app.schemas.support import (
    SupportTicketCreate,
    SupportTicketRead,
    SupportTicketsResponse,
)
from app.services.place_cursor import INVALID_CURSOR_CODE
from app.services.support_ticket_rate import RateLimiter, SupportTicketRateLimiter
from app.services.support_ticket_service import (
    InvalidCursorError,
    SupportTicketEntry,
    TicketBodyRequiredError,
    create_ticket,
    list_tickets,
)

router = APIRouter(prefix="/support", tags=["support"])

#: AC-01, AC-02: единственный конверт ошибки `{"code": ...}` (project-context §4).
#: Заголовок ставится на КАЖДЫЙ ответ: тело обращения — ПД, и прокси не должен
#: кешировать ни `201`, ни `429`, ни `422`.
_NO_STORE = {"cache-control": "no-store"}

#: AC-02: дефолт и границы страницы (D-9.5-8). 20 — размер экрана: своих
#: обращений единицы, и 50 пришлось бы листать без нужды; `le=50` не даёт
#: одному запросу вытянуть весь список (скрытый `SELECT *` на каждый рендер).
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": ...}` (приём `notifications._error`).

    Свободный `message` не отдаётся: клиент разбирает одно поле `code` и
    выбирает текст по нему (NFR60 — неизвестное игнорируется).
    """
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


def _rate_limited(retry_after: int) -> JSONResponse:
    """AC-01: ТОТ ЖЕ конверт `rate_limited`, что у заявок и SMS.

    `Retry-After` обязателен: без него клиент не знает, через сколько
    повторять, и будет долбить `POST` в цикле — то есть лимит не защитит.
    `retry_after <= 0` означает «окно неизвестно»: `Retry-After: 0` заставил бы
    клиента повторить немедленно (CR 1.4, патч 16), поэтому отдаём окно.
    """
    if retry_after <= 0:
        retry_after = settings.support_ticket_rate_window_seconds
    return JSONResponse(
        {"code": "rate_limited", "retry_after": retry_after},
        status_code=429,
        headers={**_NO_STORE, "Retry-After": str(retry_after)},
    )


def _cursor_error() -> JSONResponse:
    """AC-02: битый курсор — `422 invalid_cursor` (тот же код, что `/places`).

    Второй код на тот же дефект клиента заставил бы его различать источники
    выдачи вместо того, чтобы сбросить курсор и запросить первую страницу.
    """
    return _error(INVALID_CURSOR_CODE, 422)


def _read(entry: SupportTicketEntry) -> SupportTicketRead:
    """Доменный элемент → схема ответа. Одна точка конверсии на оба маршрута.

    Без неё `POST` и `GET` собирали бы элемент независимо, и `201` мог бы
    вернуть набор полей, отличный от списка: клиент вставлял бы строку из
    `201` в отрисованный список и получал бы разную форму одного ресурса.
    """
    return SupportTicketRead(
        uuid=entry.uuid,
        body=entry.body,
        status=entry.status,
        created_at=entry.created_at,
        answer=entry.answer,
        answered_at=entry.answered_at,
    )


def get_support_ticket_rate_limiter() -> RateLimiter:
    """AC-01: лимитер как зависимость — тесты подставляют фейк без Redis.

    Фабрика, а не модульная константа: значения берутся из настроек на момент
    запроса, что позволяет менять `.env` без пересборки (R-1, FR55).
    """
    return SupportTicketRateLimiter(
        limit=settings.support_ticket_rate_limit,
        window_seconds=settings.support_ticket_rate_window_seconds,
    )


@router.post("/tickets", status_code=201, response_model=SupportTicketRead)
async def create_support_ticket(
    payload: SupportTicketCreate,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[RateLimiter, Depends(get_support_ticket_rate_limiter)],
) -> SupportTicketRead | JSONResponse:
    """AC-01, AC-04: создаёт обращение в статусе `open`.

    Порядок: технический rate limit → запись. Лимит ПЕРВЫМ намеренно: он
    дешевле (один `INCR`) и защищает от потока запросов, которые всё равно
    не прошли бы.

    `201` с ПОЛНЫМ `SupportTicketRead` (D-9.5-5): клиент вставляет строку в
    уже отрисованный список без второго запроса — нет мигания. `id`,
    `user_id`, `answered_by_id` в ответе НЕТ (D-9.5-6).

    AC-04 (пустое тело) обеспечивается схемой (`min_length=1` + `mode="before"`),
    то есть `422` приходит из общего обработчика валидации ДО этого кода.
    Сервисный `TicketBodyRequiredError` — вторая линия (вызов мимо схемы).
    """
    retry_after = await limiter.hit(user.id)
    if retry_after is not None:
        return _rate_limited(retry_after)

    try:
        entry = await create_ticket(session, user_id=user.id, body=payload.body)
    except TicketBodyRequiredError:
        return _error("body_required", 422)

    # AC-01, §9: `commit` здесь, а не в сервисе — граница транзакции есть
    # свойство HTTP-запроса, а сервис обязан оставаться переиспользуемым.
    await session.commit()
    return _read(entry)


@router.get("/tickets", response_model=SupportTicketsResponse)
async def list_support_tickets(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> SupportTicketsResponse | JSONResponse:
    """AC-02, AC-03: страница СВОИХ обращений, новые сверху.

    Сортировка ФИКСИРОВАНА `created_at DESC, id DESC` и не является
    параметром (D-9.5-11): AC не требует выбора порядка, а `order` открыл бы
    путь по неиндексированному полю — тихая деградация p95 вместо ошибки.

    Ключ сортировки `(created_at, id)` — ровно индекс
    `ix_support_tickets_user_created`; `id` вторым ключом потому, что
    `created_at` может совпасть, и без него keyset терял бы строки из
    tie-группы между страницами.

    `total` — «сколько всего своих», не «сколько на странице» (D-9.5-7).
    AC-03 (ответ и дата видны) — это поля `answer`/`answered_at` в элементе:
    запись делает 9.6, чтение здесь.
    """
    try:
        page = await list_tickets(session, user_id=user.id, limit=limit, cursor=cursor)
    except InvalidCursorError:
        return _cursor_error()

    # AC-01: явно, хотя `NoStoreMiddleware` ставит заголовок на каждый ответ:
    # список — ПД, и полагаться на middleware значило бы, что его отключение
    # (или порядок слоёв) молча откроет кеширование.
    response.headers["cache-control"] = "no-store"
    return SupportTicketsResponse(
        items=[_read(item) for item in page.items],
        total=page.total,
        cursor=page.next_cursor,
    )


__all__: list[Any] = ["router"]
