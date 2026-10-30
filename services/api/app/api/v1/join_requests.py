"""Заявки на участие: подача (стори 5а.2; AC-02, AC-05, AC-07, AC-10).

Роут не содержит бизнес-логики: проверки — в `services/join_request_service.py`,
запросы — в репозитории (project-context §6). Здесь только HTTP-форма:
статусы, конверт ошибки, `cache-control`.

**Префикс `/cards` дублируется осознанно (Task 6.2):** путь ресурса принадлежит
карточке (`POST /cards/{uuid}/requests`), но набор гардов и кодов ошибок
другой, а `cards.py` уже 400+ строк. Тот же приём, что у `moderation.py`.

**Маппинг ошибок — здесь, а не в `main.py`:** глобальный обработчик создал бы
второй источник правды для одной доменной ошибки (приём `cards.py`).

**Стори 5а.3:** тот же модуль получил ВТОРОЙ роутер `requests_router` без
префикса — `DELETE /requests/{uuid}` и `GET /my/requests`. Отдельный файл
развёл бы `_error`/`_domain_error` и коды ошибок по двум местам, то есть дал
бы второй источник правды на один домен (D-5a.3-8).

**Границы:** список заявившихся для автора — 5а.4; отмена участия в открытом
наборе — 5а.6.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ConsentUser
from app.core.config import settings
from app.core.db import get_session
from app.models.enums import EventCardStatus, PlaceCategory
from app.schemas.join_request import (
    CardRequestsItem,
    CardRequestsPerson,
    CardRequestsResponse,
    JoinRequestCreate,
    JoinRequestRead,
    MyRequestAuthor,
    MyRequestCard,
    MyRequestItem,
    MyRequestPlace,
    MyRequestsResponse,
    SelectRequestResponse,
)
from app.services.join_request_rate import JoinRequestRateLimiter, RateLimiter
from app.services.join_request_service import (
    AlreadyRequestedError,
    CapacityOneUsesRequestsError,
    CardAlreadyFilledError,
    CardNotAcceptingError,
    CardRequestsEntry,
    JoinRequestCardNotFoundError,
    JoinRequestLimitReachedError,
    JoinRequestNotActiveError,
    JoinRequestNotFoundError,
    JoinRequestOwnCardError,
    JoinRequestSelectedError,
    MyRequestEntry,
    RequestNotPendingError,
    cancel_join_request,
    list_card_requests,
    list_my_requests,
    select_join_request,
    submit_join_request,
)
from app.services.place_cursor import INVALID_CURSOR_CODE, InvalidCursorError

router = APIRouter(prefix="/cards", tags=["join-requests"])

# AC-01, AC-03, D-5a.3-8: второй роутер БЕЗ префикса — `/requests/{uuid}` и
# `/my/requests` не принадлежат карточке. Отдельный модуль развёл бы
# `_error`/`_domain_error` и коды ошибок по двум файлам, то есть дал бы второй
# источник правды на один домен.
requests_router = APIRouter(tags=["join-requests"])

# AC-02, project-context §4: единственный конверт ошибки `{"code": ...}`.
# Заголовок ставится на КАЖДЫЙ ответ: `404`/`409` раскрывают существование
# карточки, а `201` — факт подачи.
_NO_STORE = {"cache-control": "no-store"}


def _error(code: str, status_code: int, details: dict[str, Any] | None = None) -> JSONResponse:
    """Конверт `{"code": ...}` с необязательным `details` (приём `cards.py`)."""
    body: dict[str, Any] = {"code": code}
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


def _rate_limited(retry_after: int) -> JSONResponse:
    """AC-10: ТОТ ЖЕ конверт, что `phone_auth._rate_limited` (`rate_limited`).

    Отличать технический лимит от доменного клиент обязан по коду:
    `rate_limited` — «слишком часто, подождите», `JOIN_REQUEST_LIMIT_REACHED`
    — «у вас уже две активные заявки, отмените одну». Один код на оба случая
    заставил бы клиента показывать неверное действие.

    `retry_after <= 0` означает «окно неизвестно»: `Retry-After: 0` заставил бы
    клиента повторить немедленно (CR 1.4, патч 16).
    """
    if retry_after <= 0:
        retry_after = settings.join_request_rate_window_seconds
    return JSONResponse(
        {"code": "rate_limited", "retry_after": retry_after},
        status_code=429,
        headers={**_NO_STORE, "Retry-After": str(retry_after)},
    )


def _domain_error(error: Exception) -> JSONResponse:
    """AC-02…AC-07: доменные ошибки подачи → HTTP.

    Одна функция на все ветки, чтобы один и тот же ввод давал один код:
    две копии ветвления разошлись бы при первой правке (урок 3.2).

    Коды:
    - `404 card_not_found` — нет карточки ИЛИ чужое сообщество (ADR-13);
    - `403 JOIN_REQUEST_OWN_CARD` — своя карточка (AC-05);
    - `409 CARD_NOT_ACCEPTING` — не `published` (AC-05);
    - `409 CAPACITY_ONE_USES_REQUESTS` — вместимость 2..6 (AC-04);
    - `429 JOIN_REQUEST_LIMIT_REACHED` + `details.{limit, active_count}` (AC-03);
    - `409 ALREADY_REQUESTED` — активная заявка пары уже есть (AC-06).
    """
    if isinstance(error, JoinRequestCardNotFoundError):
        return _error(error.code, 404)
    if isinstance(error, JoinRequestOwnCardError):
        # AC-05: `403`, а не `409`: действие запрещено доменом независимо от
        # состояния карточки. `404` здесь был бы ложью — карточка своя.
        return _error(error.code, 403)
    if isinstance(error, JoinRequestLimitReachedError):
        # AC-03 (§7.2): `429` закреплён за лимитами. `details` — только числа:
        # `uuid` активных заявок раскрыли бы состав чужих заявок.
        return _error(error.code, 429, error.details)
    if isinstance(
        error,
        (
            CardNotAcceptingError,
            CapacityOneUsesRequestsError,
            AlreadyRequestedError,
            # AC-04 стори 5б.2: карточка уже собрана и заявка уже не `pending` —
            # тоже конфликт состояния. Отдельные КОДЫ (не отдельные статусы):
            # клиент показывает разные тексты, но HTTP-класс один.
            CardAlreadyFilledError,
            RequestNotPendingError,
        ),
    ):
        # Все — конфликт состояния (`409`): карточка/заявка существует и валидна,
        # но действие сейчас невозможно.
        return _error(error.code, 409)
    # AC-04 (ADR-13): заявки нет ИЛИ она чужая — `404` с ОДНИМ телом. `403`
    # подтвердил бы существование чужой заявки, а различимое тело позволило
    # бы перебором `uuid` узнать, какие заявки существуют.
    if isinstance(error, JoinRequestNotFoundError):
        return _error(error.code, 404)
    # AC-02 (FR104): `409`, а не `403`: действие в принципе легально, но не для
    # этой заявки — выйти можно только отменой участия (5а.6).
    if isinstance(error, JoinRequestSelectedError):
        return _error(error.code, 409)
    # AC-05 (FR81): `expired` и `not_selected` — один код, чтобы `DELETE` не
    # стал оракулом «вас не выбрали».
    if isinstance(error, JoinRequestNotActiveError):
        return _error(error.code, 409)
    # Неизвестная доменная ошибка — ошибка программиста: `500` с диагностикой,
    # а не «на всякий случай 409», который скрыл бы дефект.
    raise error


def get_join_request_rate_limiter() -> RateLimiter:
    """AC-10: лимитер как зависимость — тесты подставляют фейк без Redis.

    Фабрика, а не модульная константа: значения берутся из настроек на момент
    запроса, что позволяет менять `.env` без пересборки (FR61).
    """
    return JoinRequestRateLimiter(
        limit=settings.join_request_rate_limit,
        window_seconds=settings.join_request_rate_window_seconds,
    )


@router.post("/{card_uuid}/requests", status_code=201, response_model=JoinRequestRead)
async def submit_join_request_endpoint(
    card_uuid: uuid_lib.UUID,
    payload: JoinRequestCreate,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[RateLimiter, Depends(get_join_request_rate_limiter)],
) -> JoinRequestRead | JSONResponse:
    """AC-02, AC-05, AC-07, AC-10: подача заявки на карточку режима «заявки».

    Порядок: технический rate limit → доменные гарды. Технический ПЕРВЫМ
    намеренно: он дешевле (один `INCR`) и защищает от потока запросов,
    которые всё равно не прошли бы домен.

    `ConsentUser`, не `CurrentUser` (OQ-5a.2-3): подача — публичное действие
    с сообщением, как создание карточки, и требует согласия на обработку ПД.

    `cache-control: no-store` — через `NoStoreMiddleware` на успешном ответе
    и явно в теле ошибок выше: `404`/`409` раскрывают существование карточки.
    """
    retry_after = await limiter.hit(user.id)
    if retry_after is not None:
        return _rate_limited(retry_after)

    try:
        request = await submit_join_request(
            session,
            applicant_id=user.id,
            card_uuid=card_uuid,
            message=payload.message,
            settings=settings,
        )
    except (
        JoinRequestCardNotFoundError,
        JoinRequestOwnCardError,
        JoinRequestLimitReachedError,
        CardNotAcceptingError,
        CapacityOneUsesRequestsError,
        AlreadyRequestedError,
    ) as error:
        return _domain_error(error)

    return JoinRequestRead.model_validate(request)


# ——— 5а.3: отмена заявки и «Мои заявки» ———


def _cursor_error() -> JSONResponse:
    """AC-06: битый курсор — `422 invalid_cursor` (тот же код, что `/places`).

    Второй код на тот же дефект клиента заставил бы его различать источники
    выдачи вместо того, чтобы сбросить курсор и запросить первую страницу
    (приём `cards.py`).
    """
    return JSONResponse({"code": INVALID_CURSOR_CODE}, status_code=422, headers=_NO_STORE)


@requests_router.delete("/requests/{request_uuid}", status_code=204)
async def cancel_join_request_endpoint(
    request_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """AC-01, AC-02, AC-04, AC-05: отмена своей заявки до решения автора.

    `request_uuid` типизирован `uuid.UUID`: мусор в пути даёт `422` от общего
    обработчика, а не `404` — клиент различает опечатку и отсутствие заявки
    (тот же довод, что у `/cards/{uuid}`).

    `204` без тела: отменять нечего показывать — клиент уже знает `uuid`,
    а состояние он перечитает списком. Повторная отмена — тоже `204`
    (AC-05, идемпотентность): обрыв сети после `commit` не должен выглядеть
    сбоем операции, которая прошла.

    `ConsentUser`, не `CurrentUser`: заявка — публичное действие с сообщением,
    как подача (5а.2), и гард должен совпадать, иначе подать без согласия
    нельзя, а отменить можно.
    """
    try:
        await cancel_join_request(
            session,
            applicant_id=user.id,
            request_uuid=request_uuid,
            settings=settings,
        )
    except (
        JoinRequestNotFoundError,
        JoinRequestSelectedError,
        JoinRequestNotActiveError,
    ) as error:
        return _domain_error(error)

    # `204` не имеет тела; `cache-control` ставит `NoStoreMiddleware`, поэтому
    # заголовок здесь не дублируется (долг X-46: дубли не краснеют тестом).
    return Response(status_code=204)


@requests_router.post(
    "/requests/{request_uuid}/select",
    status_code=200,
    response_model=SelectRequestResponse,
)
async def select_join_request_endpoint(
    request_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """AC-01, AC-04, AC-05 стори 5б.2: автор выбирает одного заявившегося.

    `POST`, а не `PATCH`: действие не «правит заявку», а принимает решение —
    и повтор его НЕ идемпотентен (D-5b.2-9: второй вызов → `409`).

    Путь `{request_uuid}`, а не `/cards/{uuid}/select`: решение принимается
    НАД ЗАЯВКОЙ, и автор видит её `uuid` в списке заявившихся. Карточка
    выводится из заявки — так нельзя выбрать заявку чужой карточки.

    `ConsentUser`, не `CurrentUser`: действие публичное и создаёт компанию,
    как подача заявки; гард обязан совпадать, иначе заявку подать без
    согласия нельзя, а выбрать — можно.

    `200`, а не `201`: ничего не создаётся — меняется состояние карточки и
    заявок. Тело `{card_uuid, chat_uuid}` (AC-01), без числа отклонённых
    (FR81).
    """
    try:
        result = await select_join_request(
            session,
            author_id=user.id,
            request_uuid=request_uuid,
            settings=settings,
        )
    except (
        JoinRequestCardNotFoundError,
        JoinRequestNotFoundError,
        CardNotAcceptingError,
        CardAlreadyFilledError,
        RequestNotPendingError,
    ) as error:
        return _domain_error(error)

    return JSONResponse(
        {
            "card_uuid": str(result.card_uuid),
            "chat_uuid": str(result.chat_uuid),
        },
        status_code=200,
        headers=_NO_STORE,
    )


def _to_item(entry: MyRequestEntry) -> MyRequestItem:
    """AC-06, AC-07: доменный `MyRequestEntry` → схема ответа.

    Сборка здесь, а не в сервисе (`§6`: сервис не знает про HTTP-форму),
    и не `model_validate`: строка страницы — это `Row`, а не ORM-объект,
    поэтому атрибутного маппинга нет и поля перечисляются явно.
    """
    return MyRequestItem(
        uuid=entry.request_uuid,
        status=entry.status,
        submitted_at=entry.submitted_at,
        expires_at=entry.request_expires_at,
        # AC-13 (долг X-5a.5-3): участие открытого набора приходит с `message=''`,
        # и пустая строка выглядела бы как «заявка без текста». Отсутствие —
        # `null` (project-context §4), приведение ровно здесь, в единственной
        # точке сборки ответа (тот же приём, что у `CardRequestsItem` в 5а.5).
        message=entry.message or None,
        card=MyRequestCard(
            uuid=entry.card_uuid,
            title=entry.card_title,
            category=PlaceCategory(entry.card_category),
            starts_at=entry.card_starts_at,
            expires_at=entry.card_expires_at,
            status=EventCardStatus(entry.card_status),
            capacity=entry.card_capacity,
            filled_count=entry.card_filled_count,
            place=MyRequestPlace(
                uuid=entry.place_uuid,
                name=entry.place_name,
                address_text=entry.place_address_text,
            ),
        ),
        author=MyRequestAuthor(
            uuid=entry.author_uuid,
            name=entry.author_name,
            age=entry.author_age,
            photo_url=entry.author_photo_url,
        ),
    )


@requests_router.get("/my/requests", response_model=MyRequestsResponse)
async def list_my_requests_endpoint(
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[str | None, Query()] = None,
) -> MyRequestsResponse | JSONResponse:
    """AC-03, AC-06, AC-07: список своих заявок.

    `not_selected` не отдаётся НИКОГДА (FR81): фильтр — в SQL репозитория
    (`JOIN_REQUEST_VISIBLE_STATUSES`), а не в питоне после выборки. Это
    проверяется тестом с заявкой `not_selected` в БД.

    Курсор — единственный query-параметр: размер страницы задан сервером,
    чтобы `limit=0` не давал молча пустую выдачу, а `limit=-1` — `500`
    из драйвера.
    """
    try:
        page = await list_my_requests(
            session,
            applicant_id=user.id,
            settings=settings,
            cursor=cursor,
        )
    except InvalidCursorError:
        return _cursor_error()

    return MyRequestsResponse(
        items=[_to_item(entry) for entry in page.items],
        total=page.total,
        cursor=page.cursor,
    )


# ——— 5а.4: список заявившихся для автора (AC-01…AC-09) ———


def _to_card_request_item(entry: CardRequestsEntry) -> CardRequestsItem:
    """AC-01, AC-06: доменный `CardRequestsEntry` → схема ответа.

    Сборка здесь, а не в сервисе (§6: сервис не знает про HTTP-форму),
    и не `model_validate`: строка страницы — это `Row`, а не ORM-объект,
    поэтому атрибутного маппинга нет и поля перечисляются явно.

    `photo_expires_in` заполняется ТОЛЬКО когда есть `photo_url`: TTL без
    ссылки — бессмысленное поле, и клиент не смог бы отличить «фото нет»
    от «фото есть, срок неизвестен».
    """
    return CardRequestsItem(
        uuid=entry.request_uuid,
        submitted_at=entry.submitted_at,
        # OQ-5а.5-4 (стори 5а.5): участие открытого набора приходит без
        # сообщения (`message=''`), и пустая строка выглядела бы как «заявка
        # без текста». Отсутствие — `null` (project-context §4), приведение
        # ровно здесь, в единственной точке сборки ответа.
        message=entry.message or None,
        person=CardRequestsPerson(
            uuid=entry.applicant_uuid,
            name=entry.applicant_name,
            age=entry.applicant_age,
            photo_url=entry.photo_url,
            # AC-06, D-5a.4-6: срок жизни ссылки рядом со ссылкой — клиент
            # не показывает протухший URL молча (OQ-5a.3-1).
            photo_expires_in=(settings.s3_signed_url_ttl if entry.photo_url is not None else None),
        ),
        # AC-3 стори 12.6 (FR119): попадание заявителя в рамки карточки.
        # Значение пришло из SQL-проекции — того же предиката, что фильтрует
        # автовыбор (AC-5a); второго вычисления здесь нет. `None` — у карточки
        # рамок нет; заявителю это поле не отдаётся никогда (AC-4, FR81).
        within_bounds=entry.within_bounds,
    )


@router.get("/{card_uuid}/requests", response_model=CardRequestsResponse)
async def list_card_requests_endpoint(
    card_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> CardRequestsResponse | JSONResponse:
    """AC-01…AC-09: список заявившихся на карточку — только для автора.

    Путь принадлежит карточке, поэтому роутер — существующий `router`
    (prefix `/cards`), а не `requests_router` (D-5a.4-1: адрес списка — это
    адрес ресурса карточки, а не заявки).

    `ConsentUser`, не `CurrentUser` (как подача 5а.2 и отмена 5а.3): экран
    показывает чужие анкеты с фото, то есть обработку ПД, и гард обязан
    совпадать с остальными действиями домена.

    `cursor` ограничен `max_length=512` (долг CR 5а.3 про `Query()` без
    предела — не повторять): без предела строка в 1 МБ тратила бы память
    на декодирование до первого отказа.

    Не автор → `404 card_not_found` через `_domain_error`: тот же код, что
    у несуществующей карточки (ADR-13). `403` подтвердил бы существование.
    """
    try:
        page = await list_card_requests(
            session,
            author_id=user.id,
            card_uuid=card_uuid,
            settings=settings,
            cursor=cursor,
        )
    except InvalidCursorError:
        return _cursor_error()
    except JoinRequestCardNotFoundError as error:
        return _domain_error(error)

    return CardRequestsResponse(
        items=[_to_card_request_item(entry) for entry in page.items],
        total=page.total,
        cursor=page.cursor,
        mode=page.mode,
        selection_available=page.selection_available,
    )
