"""Очередь модерации карточек (стори 4.3, Task 8; AC-03…AC-08).

Роут не содержит бизнес-логики: проверки живут в
`services/moderation_service.py`, запросы — в репозитории (project-context §6).
Здесь только HTTP-форма: статусы, конверт ошибки `{"code": ...}` и маппинг
доменных ошибок.

**Гард — `ModeratorUser` на ВСЕХ трёх маршрутах** (R-01, R-06): очередь — не
публичные данные, и чтение её обычным `user` запрещено тем же `403
{"code": "forbidden"}`, что и запись в справочник. Разные формы отказа
на чтении и записи дали бы клиенту два вида одной ошибки.

**Маппинг ошибок — здесь, а не в `main.py`:** второй глобальный
`exception_handler` создал бы два источника правды для одной доменной ошибки
(тот же приём, что в `api/v1/cards.py` и `api/v1/places.py`).

**Границы:** лента (`GET /cards`) — 4.5, лимит активных карточек (4.4),
закрытие/отмена (4.6). `/moderation/photos` здесь НЕ появится: пайплайн фото
остаётся в своих маршрутах (ADR-08, долг X-23).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ModeratorUser
from app.core.config import settings
from app.core.db import get_session
from app.schemas.event_card import EventCardRef
from app.schemas.moderation import (
    ContentCheckRead,
    ModerationAuthor,
    ModerationCardItem,
    ModerationCardListResponse,
    ModerationPlace,
    ModerationRejectRequest,
    ModerationStatusFilter,
)
from app.services.moderation_service import (
    CardAlreadyPublishedError,
    CardAlreadyVerifiedError,
    ModerationCardNotFoundError,
    ModerationCursorError,
    RejectionReasonRequiredError,
    RejectionReasonTooLongError,
    approve_card,
    list_moderation_queue,
    reject_card,
)

router = APIRouter(prefix="/moderation/cards", tags=["moderation"])

# AC-08, project-context §4: единственный конверт ошибки `{"code": ...}`.
# `no-store` ставит `NoStoreMiddleware` на каждый ответ (QD A6): ручная
# установка создала бы второй путь заголовка.
_NO_STORE = {"cache-control": "no-store"}


def _error(code: str, status_code: int, details: dict[str, Any] | None = None) -> JSONResponse:
    """Конверт `{"code": ...}` с необязательным `details`.

    `code` — машинный идентификатор; клиент ветвится по нему, а не по тексту.
    """
    body: dict[str, Any] = {"code": code}
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


def _domain_error(error: Exception) -> JSONResponse:
    """AC-03…AC-08: единое отображение доменных ошибок модерации в HTTP.

    Коды:
    - `404 card_not_found` — карточки с таким `uuid` нет;
    - `409 CARD_ALREADY_VERIFIED` — уже одобрена (OQ-3);
    - `409 CARD_ALREADY_PUBLISHED` — карточка не черновик (OQ-5);
    - `422 rejection_reason_required` — отклонение без причины (FR113);
    - `422 invalid_cursor` — битый курсор (AC-08).
    """
    if isinstance(error, ModerationCardNotFoundError):
        return _error(error.code, 404)
    if isinstance(error, (CardAlreadyVerifiedError, CardAlreadyPublishedError)):
        return _error(error.code, 409)
    if isinstance(
        error,
        (
            RejectionReasonRequiredError,
            # CR 4.3, патч 4: `422`, не `500` — причина длиннее предела схемы.
            RejectionReasonTooLongError,
            ModerationCursorError,
        ),
    ):
        return _error(error.code, 422)
    # Неизвестная доменная ошибка — не маскируем доменным кодом: честный `500`
    # с диагностикой лучше ложного `422` (тот же принцип, что в `cards.py`).
    raise error


def _to_item(entry: Any) -> ModerationCardItem:
    """AC-03: перекладывает доменную строку очереди в Pydantic-схему.

    Сервис возвращает `ModerationQueueEntry` (не схему ответа): HTTP-слой
    владеет формой ответа, сервис — доменом. `auto_check.passed` выводится
    из пустого `failed_rules` — второго источника правды нет.
    """
    card = entry.card
    return ModerationCardItem(
        uuid=card.uuid,
        title=card.title,
        description=card.description,
        category=card.category,
        starts_at=card.starts_at,
        capacity=card.capacity,
        created_at=card.created_at,
        status=card.status,
        verification_status=card.verification_status,
        rejection_reason=card.rejection_reason,
        author=ModerationAuthor(
            uuid=entry.author_uuid,
            name=entry.author_name,
            age=entry.author_age,
        ),
        place=ModerationPlace(uuid=entry.place_uuid, name=entry.place_name),
        auto_check=ContentCheckRead(
            passed=not entry.failed_rules,
            failed_rules=entry.failed_rules,
        ),
    )


@router.get("", response_model=ModerationCardListResponse)
async def list_moderation_cards_endpoint(
    _moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    status: ModerationStatusFilter = "pending",
    cursor: Annotated[str | None, Query()] = None,
) -> ModerationCardListResponse | JSONResponse:
    """AC-03, AC-07, AC-08: очередь карточек с автором, местом и автопроверкой.

    `status` типизирован `Literal` (D-4.3-4): мусор отбивает Pydantic → `422`
    без новой ветки маппинга. `all` — без фильтра, остальные значения —
    конкретный `verification_status`.

    `cursor` — непрозрачная строка; битое значение даёт `422 invalid_cursor`,
    а не `500` и не тихую первую страницу.

    `commit()` не вызывается: чтение.
    """
    try:
        page = await list_moderation_queue(
            session,
            status=status,
            cursor=cursor,
            settings=settings,
        )
    except ModerationCursorError as error:
        return _domain_error(error)

    return ModerationCardListResponse(
        items=[_to_item(entry) for entry in page.items],
        total=page.total,
        cursor=page.next_cursor,
    )


@router.post("/{card_uuid}/approve", response_model=EventCardRef)
async def approve_card_endpoint(
    card_uuid: uuid_lib.UUID,
    moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-04, AC-07: ручное одобрение черновика модератором.

    `card_uuid` типизирован `UUID`: числовой id из адреса даёт `422` схемы,
    и перебор `/1..N` невозможен (§7.2: внутренние `bigint` наружу не выходят).

    `verified_by` берётся из `moderator` (сессия), а не из тела: клиент
    не выбирает, кто проверил.
    """
    try:
        card = await approve_card(
            session,
            card_uuid=card_uuid,
            moderator_id=moderator.id,
            # CR 4.3, патч 1 (ADR-01): срез сообщества берётся из настроек.
            # Без него модератор мог одобрить карточку ЧУЖОГО города, зная uuid.
            settings=settings,
        )
    except (
        ModerationCardNotFoundError,
        CardAlreadyVerifiedError,
        CardAlreadyPublishedError,
    ) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.post("/{card_uuid}/reject", response_model=EventCardRef)
async def reject_card_endpoint(
    card_uuid: uuid_lib.UUID,
    payload: ModerationRejectRequest,
    moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-05, AC-07: отклонение карточки с причиной (FR113).

    Причина обязательна и нормализуется схемой: строка из пробелов даёт `422
    rejection_reason_required` (`min_length` после нормализации). Схема
    отбивает это ДО сервиса, но сервис проверяет ещё раз — админский скрипт
    идёт мимо Pydantic.

    Повторное отклонение — `200` с заменой причины (OQ-4): модератор уточняет
    формулировку, а `409` заставлял бы его уходить в SQL.
    """
    try:
        card = await reject_card(
            session,
            card_uuid=card_uuid,
            moderator_id=moderator.id,
            reason=payload.rejection_reason,
            # CR 4.3, патч 1 (ADR-01): срез сообщества берётся из настроек.
            # Без него модератор мог отклонить карточку ЧУЖОГО города, зная uuid.
            settings=settings,
        )
    except (
        ModerationCardNotFoundError,
        CardAlreadyPublishedError,
        RejectionReasonRequiredError,
        # CR 4.3, патч 4: причина длиннее 500 символов — `422`, не `500`.
        RejectionReasonTooLongError,
    ) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)
