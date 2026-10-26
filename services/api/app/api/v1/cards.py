"""Карточки события: создание, правка черновика, публикация (стори 4.2).

AC-01…AC-08. Роут не содержит бизнес-логики: проверки живут
в `services/event_card_service.py`, запросы — в репозитории
(project-context §6). Здесь только HTTP-форма: статусы, конверт ошибки,
`cache-control` и единица работы (`commit` — в сервисе).

**Границы:** `GET /cards` (лента) и `GET /cards/{uuid}` — 4.5; модерация
(`/moderation/*`) — 4.3; лимит активных карточек (`429 CARD_LIMIT_REACHED`) —
4.4; закрытие/отмена — 4.6. Их отсутствие проверяется тестом поверхности API.

**Маппинг ошибок — здесь, а не в `main.py`:** второй глобальный
`exception_handler` создал бы два источника правды для одной доменной ошибки
(тот же приём, что в `api/v1/places.py`).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ConsentUser, CurrentUser
from app.core.config import settings
from app.core.db import get_session
from app.models.enums import CardVerificationStatus, EventCardStatus, PlaceCategory
from app.schemas.attendance import AttendanceAnswer, AttendanceState
from app.schemas.chat import CardParticipant, CardParticipantsResponse
from app.schemas.event_card import (
    EventCardAuthor,
    EventCardCreate,
    EventCardFeedResponse,
    EventCardJoinResult,
    EventCardPlace,
    EventCardRead,
    EventCardRef,
    EventCardUpdate,
)
from app.schemas.report import NoShowReportRead, NoShowReportRequest
from app.services.attendance_service import (
    AttendanceNoPairError,
    AttendanceNotFoundError,
    AttendanceNotOpenedError,
    AttendanceSelfConfirmError,
    confirm_attendance,
    get_attendance,
)
from app.services.card_feed_cursor import InvalidCursorError
from app.services.card_join_service import (
    AlreadyJoinedError,
    CardIsFullError,
    CardJoinNotFoundError,
    ParticipationNotFoundError,
    join_open_card,
    leave_open_card,
)
from app.services.chat_participants_service import (
    ParticipantsNotFoundError,
    list_card_participants,
)
from app.services.event_card_place import PlaceNotAvailableError
from app.services.event_card_service import (
    CardAlreadyJoinedError,
    CardDecisionWindowRequiredError,
    CardEditRequiresReviewError,
    CardFeedItem,
    CardLimitReachedError,
    CardNoSeatsError,
    CardNotCancellableError,
    CardNotDraftError,
    CardNotEditableError,
    CardNotFoundError,
    CardNotPublishedError,
    CardRejectedError,
    CardStartsAtInPastError,
    CardVerificationPendingError,
    cancel_card,
    close_card,
    create_card,
    get_card,
    list_cards,
    publish_card,
    update_card,
)
from app.services.join_request_service import (
    CapacityOneUsesRequestsError,
    CardNotAcceptingError,
    JoinRequestOwnCardError,
)
from app.services.report_service import (
    AttendanceConfirmedError,
    AttendancePendingError,
    ReportAlreadyExistsError,
    ReportSubjectNotFoundError,
    report_no_show,
)

router = APIRouter(prefix="/cards", tags=["cards"])

# AC-08, project-context §4: единственный конверт ошибки `{"code": ...}`.
# Заголовок ставится на КАЖДЫЙ ответ, включая ошибки: `404`/`409` раскрывают
# существование карточки, а `201` — факт создания.
_NO_STORE = {"cache-control": "no-store"}


def _error(code: str, status_code: int, details: dict[str, Any] | None = None) -> JSONResponse:
    """Конверт `{"code": ...}` с необязательным `details`.

    `code` — машинный идентификатор; `details` несёт данные для действия
    (`place_uuid`, `reason`), а не текст для человека. Клиент ветвится по `code`.
    """
    body: dict[str, Any] = {"code": code}
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


def _card_to_read(item: CardFeedItem) -> EventCardRead:
    """AC-06: доменный `CardFeedItem` → схема ответа (§7.2).

    Одна функция на ленту и детальный экран: две копии сборки вложенных
    объектов разошлись бы при первом добавлении поля в §7.2, и карточка
    в ленте перестала бы совпадать с карточкой на своём экране.

    `chat_uuid=None` — D-4.5-7: таблицы чатов нет (эпик 6). Поле присутствует
    в контракте СЕЙЧАС, иначе клиент переписывался бы дважды — сначала без
    поля, потом с ним.

    `verification_status`/`status` берутся из ORM-объекта как есть: лента
    показывает только `published`/`full`, но детальный экран отдаёт и
    `draft` автору — пересчитывать статус в сборке ответа значило бы
    расходиться с БД.
    """
    card = item.card
    # В БД `status`/`verification_status`/`category` — `String` + CHECK (D-3),
    # в Python модель держит `str`. Схема объявляет `StrEnum`, поэтому значение
    # приводится здесь, а не `type: ignore`: приведение падает на неизвестном
    # значении, и рассинхрон CHECK со `StrEnum` станет видимым (`500` на
    # собственном дефекте), а не молча уйдёт клиенту строкой.
    return EventCardRead(
        uuid=card.uuid,
        status=EventCardStatus(card.status),
        verification_status=CardVerificationStatus(card.verification_status),
        created_at=card.created_at,
        title=card.title,
        description=card.description,
        category=PlaceCategory(card.category),
        starts_at=card.starts_at,
        expires_at=card.expires_at,
        # AC-04 стори 5б.1 (NFR14): якорь отсчёта — серверное время, взятое
        # в сервисе из того же `moment`, что и ленивые переходы. `datetime.now()`
        # здесь дал бы дрейф между `expires_at` и опорной точкой.
        server_time=item.server_time,
        capacity=card.capacity,
        filled_count=card.filled_count,
        # AC-08 стори 5а.4 (X-36): скрытое место не раскрывается — `null`.
        # Карточка при этом отдаётся: заявки и участники живы, и действие
        # модератора не отнимает у автора данные (FR68 «скрыть ≠ удалить»).
        place=(
            EventCardPlace(
                uuid=item.place.uuid,
                name=item.place.name,
                address_text=item.place.address_text,
            )
            if item.place is not None
            else None
        ),
        author=EventCardAuthor(uuid=item.author_uuid, name=item.author_name, age=item.author_age),
        # AC-01 стори 5б.2 (D-5b.2-1): идентификатор чата компании появляется
        # в момент выбора автора. До этого `None` — компания не собрана.
        chat_uuid=card.chat_uuid,
        # AC-09 стори 5б.2 (D-5b.2-7, FR104): признак зрителя считает сервис
        # (`get_card` точечно, `list_cards` — одним пакетным запросом). Схема
        # получает готовое значение: арифметика «занято vs занял я» на клиенте
        # потребовала бы списка участников, которого в ленте нет.
        viewer_participates=item.viewer_participates,
    )


def _domain_error(error: Exception) -> JSONResponse:
    """AC-02…AC-07: единое отображение доменных ошибок карточки в HTTP.

    Одна функция на все три маршрута, чтобы один и тот же ввод давал один
    и тот же код: две копии ветвления разошлись бы, и клиент получал бы
    на `PATCH` другой ответ, чем на `POST` (урок 3.2).

    Коды:
    - `422 PLACE_NOT_AVAILABLE` + `details.place_uuid` — место недоступно;
    - `422 STARTS_AT_IN_PAST` — время в прошлом;
    - `422 decision_window_required` — режим заявок без окна;
    - `422 VERIFICATION_PENDING` — текст ещё не проверен;
    - `409 CARD_NOT_DRAFT` — карточка не черновик (правка/повторная публикация);
    - `409 CARD_REJECTED` + `details.reason` — отклонена модератором (FR113);
    - `429 CARD_LIMIT_REACHED` + `details.{limit, active_count}` — лимит
      активных карточек автора (FR71, стори 4.4, AC-07);
    - `404 card_not_found` — нет карточки ИЛИ она чужая (ADR-13).
    """
    if isinstance(error, CardNotFoundError):
        return _error(error.code, 404)
    if isinstance(error, CardLimitReachedError):
        # AC-07 (FR71, §7.2): `429`, не `409` — `409` закреплён за конфликтом
        # состояния карточки. `details` — только числа: `uuid` активных
        # карточек раскрыли бы состав ленты и позволили бы перебор (OQ-8).
        return _error(error.code, 429, error.details)
    if isinstance(error, CardNotDraftError):
        return _error(error.code, 409)
    # Стори 4.6 (AC-01…AC-04, AC-09): конфликты состояния карточки — все `409`
    # (§7.2: `409` закреплён за конфликтом состояния). Отдельные классы, а не
    # один с текстом: клиент ветвится по `code`, и «уже есть участник» требует
    # другого действия, чем «карточка закрыта» (урок 4.3).
    if isinstance(
        error,
        (
            CardAlreadyJoinedError,
            CardNotEditableError,
            CardEditRequiresReviewError,
            CardNotPublishedError,
            CardNotCancellableError,
            CardNoSeatsError,
        ),
    ):
        return _error(error.code, 409)
    if isinstance(error, CardRejectedError):
        # FR113: без причины автор не поймёт, что исправлять.
        details = {"reason": error.reason} if error.reason is not None else None
        return _error(error.code, 409, details)
    # AC-06…AC-11 стори 5а.5: конфликты мгновенного присоединения. Все `409`,
    # кроме запрета домена (`403`, см. ниже): §7.2 закрепляет `409` за
    # конфликтом состояния. Ветки отдельные, а не одна с текстом: клиент
    # ветвится по `code`, и «мест нет» требует иного действия, чем «набор
    # закрыт» (D-5а.5-4).
    if isinstance(
        error,
        (CardIsFullError, AlreadyJoinedError, CardNotAcceptingError, CapacityOneUsesRequestsError),
    ):
        return _error(error.code, 409)
    # AC-10 (FR76): своя карточка — `403`, тот же код, что у заявок (5а.2).
    # Действие запрещено доменом независимо от состояния карточки.
    if isinstance(error, JoinRequestOwnCardError):
        return _error(error.code, 403)
    # AC-11 (ADR-13): нет карточки ИЛИ она чужого сообщества — один ответ.
    if isinstance(error, CardJoinNotFoundError):
        return _error(error.code, 404)
    # AC-02 стори 6.3 (ADR-13, NFR33): состав компании — «нет карточки»,
    # «чужая община» и «не участник» дают ОДИН ответ `404`. Ветка `403`
    # здесь отсутствует намеренно: она подтвердила бы существование карточки
    # и приватность её состава (утечка того же класса, что скрытый чат v1).
    if isinstance(error, ParticipantsNotFoundError):
        return _error(error.code, 404)
    # AC-05 стори 5а.6: отменять нечего — участия нет либо есть только
    # `pending`-заявка (домены разделены, D-5a.6-5). `404`, а не `409`:
    # конфликта состояния нет, ресурса «участие» не существует. Тело
    # одинаково во всех трёх случаях (нет строки / заявка / `capacity=1`)
    # и не раскрывает, что именно было у пользователя.
    if isinstance(error, ParticipationNotFoundError):
        return _error(error.code, 404)
    if isinstance(error, PlaceNotAvailableError):
        # AC-02: `place_uuid` — адрес того, что клиент сам прислал, не раскрытие.
        return _error(error.code, 422, error.details)
    if isinstance(
        error,
        (
            CardStartsAtInPastError,
            CardVerificationPendingError,
            CardDecisionWindowRequiredError,
        ),
    ):
        return _error(error.code, 422)
    # AC-08 (стори 4.5): битый курсор ленты. Класс ПЕРЕИСПОЛЬЗОВАН из
    # `place_cursor` (`card_feed_cursor` его реэкспортирует), поэтому ветка
    # одна на обе выдачи: клиент ветвится по `invalid_cursor` независимо от
    # того, листал он места или карточки. `500` здесь был бы дефектом:
    # значение пришло от клиента, а не от собственного кода.
    if isinstance(error, InvalidCursorError):
        return _error(error.code, 422)
    # Стори 7.2 (AC-08, D-7.2-8). «Нет карточки», «не участник» и «нет строки
    # явки» дают ОДИН ответ, ровно как у состава компании (6.3): отдельный
    # код здесь раскрыл бы существование карточки постороннему (ADR-13, NFR33).
    if isinstance(error, AttendanceNotFoundError):
        return _error(error.code, 404)
    # AC-07 (D-7.2-7): подтверждение до `starts_at` — `422`: значение пришло
    # от клиента, а `409` закреплён за конфликтом состояния сервера (§7.2).
    if isinstance(error, AttendanceNotOpenedError):
        return _error(error.code, 422)
    # AC-10 (X-7.1-7): «участник» факта — сам автор карточки. `403`, а не
    # `422`: тело запроса корректно, запрещено ДЕЙСТВИЕ (подделка факта явки).
    if isinstance(error, AttendanceSelfConfirmError):
        return _error(error.code, 403)
    # CR 7.2 H-3: пары «автор ↔ зритель» нет — подтверждать нечего. `409`,
    # а не тихий `200`: иначе клиент считал бы факт записанным, и он терялся
    # бы молча. Отдельный код допустим: зритель уже прошёл проверку участия.
    if isinstance(error, AttendanceNoPairError):
        return _error(error.code, 409)
    # Стори 7.4 (AC-12, AC-06, D-7.4-9). «Нет карточки», «не участник»,
    # «пары нет», «жалоба на себя» — ОДИН ответ: тот же код, что у
    # `AttendanceNotFoundError`. Отдельный код разделил бы случаи и через
    # перебор `accused_uuid` выдал бы состав компании (ADR-13, NFR33).
    if isinstance(error, ReportSubjectNotFoundError):
        return _error(error.code, 404)
    # AC-04 (D-7.4-7): подтверждение не завершено — `422`, как у
    # `ATTENDANCE_NOT_OPENED` в 7.2: значение пришло от клиента (подал рано).
    if isinstance(error, AttendancePendingError):
        return _error(error.code, 422)
    # CR 7.4 H-1 (решение пользователя 2026-10-24): пара `confirmed` — оба
    # подтвердили, что пришли. Жалоба на неявку ложна по построению, но это
    # НЕ `ATTENDANCE_PENDING` (подтверждение завершено успешно), НЕ `409`
    # (состояние не конфликтует с параллельной записью) и НЕ `404` (карточка
    # и пара существуют; `confirmed` раскрывает лишь «встреча состоялась»,
    # что обе стороны уже видят). `422`: клиент подал в неподходящем состоянии.
    if isinstance(error, AttendanceConfirmedError):
        return _error(error.code, 422)
    # AC-07 (D-7.4-3): повторная жалоба — `409`, не тихий `200`: клиент обязан
    # отличать «принята сейчас» от «я уже жаловался».
    if isinstance(error, ReportAlreadyExistsError):
        return _error(error.code, 409)
    # Неизвестная доменная ошибка — не маскируем доменным кодом: честный `500`
    # с диагностикой лучше ложного `422` (тот же принцип, что в `places`).
    raise error


@router.get("", response_model=EventCardFeedResponse)
async def list_cards_endpoint(
    reader: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    category: PlaceCategory | None = None,
    has_seats: bool = False,
    cursor: Annotated[str | None, Query()] = None,
) -> EventCardFeedResponse | JSONResponse:
    """AC-01, AC-04, AC-05, AC-07, AC-08, AC-06: публичная лента сообщества.

    Зависимость — `ConsentUser`, а не `CurrentUser`: лента это показ данных
    других людей (имя и возраст автора), и без согласия на обработку ПД
    она не показывается (FR9, тот же выбор, что у анкеты 2.3).

    `category` типизирован `PlaceCategory`: мусор отбивает Pydantic, и
    ветка `validation_error_handler` отдаёт `422 category_invalid` +
    `details.allowed` (AC-05). Второго кода не вводится — тот же, что
    у справочника мест, где белый список общий (ADR-12).

    `has_seats` — `bool`, а не строка: `?has_seats=true`/`false` разбирает
    FastAPI, и `has_seats=1` тоже даёт `true` (обычное поведение bool-парсера).

    `cursor` — непрозрачная строка; разбор в сервисе, битое значение даёт
    `422 invalid_cursor`, а не `500` (AC-08).

    `cache-control: no-store` ставит `NoStoreMiddleware` (QD A6): ручная
    установка создала бы второй, независимо поддерживаемый путь заголовка.

    `commit()` не вызывается: чтение (D-4 стори 2.2).
    """
    try:
        page = await list_cards(
            session,
            category=category,
            has_seats=has_seats,
            cursor=cursor,
            settings=settings,
            # AC-05 стори 10.1, ADR-01/ADR-13: срез сообщества — колонка
            # пользователя, не конфиг. Клиент слаг не присылает.
            viewer_community_slug=reader.community_slug,
            # AC-09 стори 5б.2 (D-5b.2-7): зритель нужен для `viewer_participates`
            # — одним пакетным запросом на страницу, не N+1.
            viewer_id=reader.id,
        )
    except InvalidCursorError as error:
        return _domain_error(error)

    return EventCardFeedResponse(
        items=[_card_to_read(item) for item in page.items],
        total=page.total,
        cursor=page.next_cursor,
    )


@router.get("/{card_uuid}", response_model=EventCardRead)
async def get_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRead | JSONResponse:
    """AC-02, AC-03, AC-06: карточка по `uuid` с ленивым закрытием окна.

    `card_uuid: UUID` в пути, не `str`: битый UUID отбивает Pydantic (`422`),
    а не драйвер (`500`). Числовой путь `/cards/1` не резолвится в карточку.

    Чужая карточка, карточка другого сообщества и `draft` чужого автора дают
    ОДИН ответ `404 card_not_found` (ADR-13): `403` раскрыл бы существование.

    `ConsentUser`: детальный экран показывает имя и возраст автора — то же
    правило, что у ленты.
    """
    try:
        item = await get_card(
            session,
            card_uuid=card_uuid,
            viewer_id=user.id,
            # AC-05 стори 10.1, ADR-13: чужое сообщество даёт 404 по колонке
            # пользователя, а не по конфигу.
            viewer_community_slug=user.community_slug,
            settings=settings,
        )
    except CardNotFoundError as error:
        return _domain_error(error)

    return _card_to_read(item)


@router.post("", status_code=201, response_model=EventCardRef)
async def create_card_endpoint(
    payload: EventCardCreate,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-01…AC-04, AC-08: создание черновика карточки.

    Тело — `EventCardRef` (X-13 вариант a): `uuid` для навигации и состояние
    проверки (NFR51). Полная форма §7.2 (`place`, `author`, `filled_count`,
    `chat_uuid`) — 4.5.

    `community_slug` клиентом не принимается (ADR-01): значение из конфига.
    Попытка передать — `422` через `extra="forbid"` в `EventCardCreate` (4.1).

    `cache-control: no-store` ставит `NoStoreMiddleware`; ручная установка
    в успешном ответе создала бы второй путь заголовка (QD A6).
    """
    try:
        card = await create_card(session, author_id=user.id, payload=payload, settings=settings)
    except (
        PlaceNotAvailableError,
        CardStartsAtInPastError,
        CardDecisionWindowRequiredError,
    ) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.patch("/{card_uuid}", response_model=EventCardRef)
async def update_card_endpoint(
    card_uuid: uuid_lib.UUID,
    payload: EventCardUpdate,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-05, AC-02…AC-04, AC-07: правка черновика.

    `card_uuid: UUID` в пути, не `str`: битый UUID отбивает Pydantic (`422`),
    а не драйвер (`500`).

    Чужая карточка → `404 card_not_found`, не `403`: существование чужой
    карточки не раскрывается (ADR-13).
    """
    try:
        card = await update_card(
            session,
            author_id=user.id,
            card_uuid=card_uuid,
            payload=payload,
            settings=settings,
        )
    except (
        PlaceNotAvailableError,
        CardStartsAtInPastError,
        CardDecisionWindowRequiredError,
    ) as error:
        return _domain_error(error)
    except (
        CardNotFoundError,
        # Стори 4.6 (AC-01, AC-02): правка опубликованной после присоединения,
        # правка терминальной и провал автопроверки — три разных `409`.
        CardAlreadyJoinedError,
        CardNotEditableError,
        CardEditRequiresReviewError,
    ) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.post("/{card_uuid}/publish", response_model=EventCardRef)
async def publish_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-06, AC-04, AC-07: `draft → published`.

    Публикация разрешена только при `verification_status='approved'` (FR101):
    `pending` → `422 VERIFICATION_PENDING`, `rejected` → `409 CARD_REJECTED`
    с причиной (FR113). Разделение кодов обязательно: «ещё проверяем» и
    «отклонено» требуют разных действий от автора.

    `expires_at` пересчитывается от момента публикации (D-4.2-3, OQ-2 a):
    окно означает «сколько времени на решение ПОСЛЕ публикации».

    AC-07 (4.4): при исчерпанном лимите активных карточек — `429
    CARD_LIMIT_REACHED` с `details.{limit, active_count}` и `no-store`.
    """
    try:
        card = await publish_card(
            session, author_id=user.id, card_uuid=card_uuid, settings=settings
        )
    except (
        CardNotFoundError,
        CardNotDraftError,
        CardRejectedError,
        CardVerificationPendingError,
        # CR 4.2, патч 3: публикация черновика с прошедшим `starts_at`.
        CardStartsAtInPastError,
        # AC-07 стори 4.4: лимит активных карточек автора → `429`.
        CardLimitReachedError,
        # AC-09 стори 4.6 (X-22): заполненная вместимость → `409`, не `500`.
        CardNoSeatsError,
    ) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.post("/{card_uuid}/close", response_model=EventCardRef)
async def close_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-03 (FR74): автор закрывает набор вручную.

    `card_uuid: UUID` в пути, не `str`: битый UUID отбивает Pydantic (`422`),
    а не драйвер (`500`).

    Чужая карточка → `404 card_not_found`, не `403`: существование чужой
    карточки не раскрывается (ADR-13). Модератор тоже получает `404`
    (D-4.6-5): снятие с ленты — предмет жалоб (6.x), а не ручного закрытия.

    Повторный вызов и `close` на `full`/`draft`/архиве → `409
    CARD_NOT_PUBLISHED`: действие «закрыть» неприменимо, клиент перечитывает
    карточку.
    """
    try:
        card = await close_card(session, author_id=user.id, card_uuid=card_uuid)
    except (CardNotFoundError, CardNotPublishedError) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.post("/{card_uuid}/cancel", response_model=EventCardRef)
async def cancel_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-04 (FR100, R-04): мягкая отмена карточки автором.

    Физического удаления нет (R-04): `cancelled` — состояние, а не `DELETE`
    строки. Тот же переход доступен через `DELETE /cards/{uuid}` — две формы
    одного действия (D-4.6-2); отдельный обработчик здесь, потому что `POST`
    не требует тела и удобен для кнопки «Отменить».

    `finished` (история, FR73) и повторная отмена → `409 CARD_NOT_CANCELLABLE`.
    """
    try:
        card = await cancel_card(session, author_id=user.id, card_uuid=card_uuid)
    except (CardNotFoundError, CardNotCancellableError) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.delete("/{card_uuid}", response_model=EventCardRef)
async def delete_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardRef | JSONResponse:
    """AC-04 (FR100, R-04): `DELETE` — мягкая отмена, НЕ удаление строки.

    Семантика совпадает с `POST /cards/{uuid}/cancel` (D-4.6-2): физическое
    удаление сломало бы историю заявок и чатов эпика 5, а `ON DELETE RESTRICT`
    у места всё равно не дал бы каскада. Ответ — `200` + `EventCardRef`, а не
    `204`: клиенту нужен актуальный статус, чтобы перерисовать карточку без
    второго запроса.
    """
    try:
        card = await cancel_card(session, author_id=user.id, card_uuid=card_uuid)
    except (CardNotFoundError, CardNotCancellableError) as error:
        return _domain_error(error)

    return EventCardRef.model_validate(card)


@router.post("/{card_uuid}/join", response_model=EventCardJoinResult)
async def join_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventCardJoinResult | JSONResponse:
    """AC-01, AC-02, AC-14 (FR83, FR84): мгновенное присоединение к открытому набору.

    `ConsentUser`, а не `CurrentUser`: участие связывает человека с другими
    людьми (имя и возраст автора видны в карточке), и обработка ПД без
    согласия недопустима — то же правило, что у ленты и детального экрана.

    `card_uuid: UUID` в пути, не `str`: битый UUID отбивает Pydantic (`422`),
    а не драйвер (`500`); `/cards/1/join` не резолвится (ADR-13).

    Тело ответа — `EventCardJoinResult` (`EventCardRef` + места): `status`
    и `filled_count` приходят из `RETURNING` того же `UPDATE`, поэтому клиент
    видит ровно то, что зафиксировано, а не результат второго чтения
    (D-5а.5-7). `chat_uuid` — `null` до эпика 6 (AC-14).

    `cache-control: no-store` ставит существующее middleware на каждый ответ,
    включая ошибки: `404`/`409` раскрывают существование карточки, а `200` —
    факт участия.

    WS-события `card.full` НЕТ (AC-13, D-5а.5-5): в `app/` нет socket.io/
    pub-sub, real-time — эпик 6, долг `X-43`. Заглушка не пишется.
    """
    try:
        result = await join_open_card(
            session,
            actor_id=user.id,
            card_uuid=card_uuid,
            settings=settings,
        )
    except (
        CardJoinNotFoundError,
        JoinRequestOwnCardError,
        CardNotAcceptingError,
        CapacityOneUsesRequestsError,
        CardIsFullError,
        AlreadyJoinedError,
    ) as error:
        return _domain_error(error)

    return EventCardJoinResult(
        uuid=result.card_uuid,
        status=result.status,
        verification_status=CardVerificationStatus(result.verification_status),
        created_at=result.created_at,
        filled_count=result.filled_count,
        capacity=result.capacity,
        # AC-02 стори 6.2 (D-6.2-5, CR-2 HIGH): фактически записанный `chat_uuid`,
        # а не `None`. Прежний хардкод отдавал `null` человеку, занявшему
        # ПОСЛЕДНЕЕ место: чат уже создан в этой же транзакции, но клиент
        # получал «чата нет» и не мог открыть переписку без перезагрузки
        # карточки. `None` теперь означает ровно одно — компания не собрана.
        chat_uuid=result.chat_uuid,
    )


@router.delete("/{card_uuid}/join", status_code=204, response_class=Response)
async def leave_card_endpoint(
    card_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """AC-01…AC-08 (FR104): отмена участия в открытом наборе.

    `ConsentUser`, не `CurrentUser`: участие связывает человека с другими
    людьми, и обработка ПД без согласия недопустима — тот же гард, что
    у `POST /join` (5а.5) и у ленты.

    `204` без тела (D-5a.6-9): дельта счётчика клиенту не нужна — он
    перечитывает карточку при возврате в ленту, а `EventCardJoinResult`
    обещал бы «состояние после отмены», которого нет у закрытой карточки
    (AC-11: `filled_count` там не меняется).

    `card_uuid: UUID` в пути, не `str`: битый UUID отбивает Pydantic (`422`),
    а не драйвер (`500`); `/cards/1/join` не резолвится (ADR-13).

    Идемпотентность (AC-06): повторный `DELETE` после успешной отмены — тоже
    `204`. `404` только когда строки участия нет вовсе.

    WS-события `card.seat_released` НЕТ (AC-14, долг `X-43`): в `app/` нет
    socket.io/pub-sub, real-time — эпик 6. Заглушка не пишется.
    """
    try:
        await leave_open_card(
            session,
            actor_id=user.id,
            card_uuid=card_uuid,
            settings=settings,
        )
    except (CardJoinNotFoundError, ParticipationNotFoundError) as error:
        return _domain_error(error)

    return Response(status_code=204)


@router.get("/{card_uuid}/participants", response_model=CardParticipantsResponse)
async def card_participants_endpoint(
    card_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardParticipantsResponse | JSONResponse:
    """AC-01…AC-03 стори 6.3 (FR85): состав компании — карточка и участники.

    `ConsentUser`, а не `CurrentUser`: ответ содержит имена, возраст и фото
    других людей — показ ПД без согласия запрещён (FR9), тот же выбор, что
    у ленты и детального экрана карточки.

    `card_uuid: UUID` в пути: битый UUID отбивает Pydantic (`422`), а не
    драйвер (`500`); `/cards/1/participants` не резолвится (ADR-13).

    Посторонний получает `404 card_not_found` — ТО ЖЕ тело, что на
    несуществующую карточку (ADR-13, NFR33). Ветки `403` нет намеренно:
    она подтвердила бы существование карточки и приватность состава.

    `commit()` вызывается внутри сервиса только при фактической мутации
    карточки (`get_card`, ленивые переходы): состав — чтение.
    """
    try:
        participants = await list_card_participants(
            session,
            viewer_id=user.id,
            card_uuid=card_uuid,
            # AC-05 стори 10.1: срез сообщества — колонка пользователя.
            viewer_community_slug=user.community_slug,
            settings=settings,
        )
    except ParticipantsNotFoundError as error:
        return _domain_error(error)

    items = [
        CardParticipant(
            uuid=member.user_uuid,
            name=member.name,
            age=member.age,
            is_author=member.is_author,
            photo_url=member.photo_url,
            photo_expires_in=member.photo_expires_in,
        )
        for member in participants.items
    ]
    return CardParticipantsResponse(
        card=_card_to_read(participants.card),
        items=items,
        total=len(items),
    )


@router.get("/{card_uuid}/attendance", response_model=AttendanceState)
async def get_attendance_endpoint(
    card_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttendanceState | JSONResponse:
    """AC-08 (FR87, NFR39): собственное состояние подтверждения явки.

    **Почему до фиксации виден только свой флаг.** Экран подтверждения задаёт
    один вопрос — «что я ответил». Чужой ответ не нужен для действия и не
    должен быть доступен до того, как обе стороны ответили: зная его, вторая
    сторона могла бы подстроиться (ответить так же при `true`, или наоборот
    создать `disputed`), и фиксация факта перестала бы быть независимой.

    **Почему `pending` не раскрывает чужое.** `pending` — это ОТСУТСТВИЕ
    информации, а не значение: он одинаков и когда вторая сторона не ответила
    вовсе, и когда ответила (но не терминально). Различить эти случаи по
    ответу нельзя: тело содержит ровно `my_answer` и `status` (D-7.2-4).

    `ConsentUser`, а не `CurrentUser`: ответ касается встречи с другим
    человеком, и без согласия на обработку ПД он не показывается (FR9) —
    тот же выбор, что у карточки и состава компании.

    `card_uuid: UUID` в пути: битый UUID отбивает Pydantic (`422`), а не
    драйвер (`500`); `/cards/1/attendance` не резолвится (ADR-13).

    Посторонний получает `404 card_not_found` — ТО ЖЕ тело, что на
    несуществующую карточку (ADR-13, NFR33). `commit()` здесь не вызывается,
    кроме ленивых переходов самой карточки внутри `get_card`.
    """
    try:
        return await get_attendance(
            session,
            card_uuid=card_uuid,
            viewer_id=user.id,
            # AC-05 стори 10.1: срез сообщества — колонка пользователя.
            viewer_community_slug=user.community_slug,
            settings=settings,
        )
    except AttendanceNotFoundError as error:
        return _domain_error(error)


@router.post("/{card_uuid}/attendance", status_code=200, response_model=AttendanceState)
async def confirm_attendance_endpoint(
    card_uuid: uuid_lib.UUID,
    payload: AttendanceAnswer,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttendanceState | JSONResponse:
    """AC-01…AC-07, AC-10: подтверждение «пришёл / не пришёл».

    `status_code=200`, а не `201`: ресурс (факт явки) уже существует с 7.1,
    подтверждение меняет его поле. `201` обещал бы создание, и клиент искал
    бы `Location`.

    Повторный `POST` с тем же ответом — `200` и НИЧЕГО не меняет (AC-05,
    D-1): настоящий no-op в `WHERE`, `updated_at` не двигается, второго
    события `attendance.done` нет.

    `AttendanceAnswer` с `StrictBool` (D-7.2-9): `1`/`0` из JSON дают `422`,
    а не тихое `true`/`false`. Иначе `0` стал бы ответом «не пришёл» и дал бы
    ложный `no_show` без второго человека (FR109).

    Порядок проверок — контракт анти-оракула (TD, D-4): `404` (видимость и
    участие) → `422` (время) → guard самоподтверждения → CAS → publish.
    Время проверяется ПОСЛЕ участия: иначе посторонний по разнице кодов узнал
    бы о существовании карточки (R7, `A-SELF-ORDER-01`).
    """
    try:
        return await confirm_attendance(
            session,
            card_uuid=card_uuid,
            viewer_id=user.id,
            arrived=payload.arrived,
            # AC-05 стори 10.1: срез сообщества — колонка пользователя.
            viewer_community_slug=user.community_slug,
            settings=settings,
        )
    except (
        AttendanceNoPairError,
        AttendanceNotFoundError,
        AttendanceNotOpenedError,
        AttendanceSelfConfirmError,
    ) as error:
        return _domain_error(error)


@router.post("/{card_uuid}/report-no-show", status_code=201, response_model=NoShowReportRead)
async def report_no_show_endpoint(
    card_uuid: uuid_lib.UUID,
    payload: NoShowReportRequest,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NoShowReportRead | JSONResponse:
    """AC-03…AC-12 (FR90, NFR39, NFR52): жалоба на неявку второй стороны.

    **Почему `201`, а не `200`.** В отличие от `POST /attendance` (там факт
    явки уже существует с 7.1 и подтверждение меняет его поле), жалоба
    СОЗДАЁТ объект: `uuid` присваивается впервые, и `Location` не отдаётся —
    читающего эндпоинта у отдельной жалобы нет (кабинет модератора 8.3 — другой
    ресурс, D-7.4-1).

    **Почему ответ — ровно четыре поля** (NFR39, D-7.4-4). Нет `reporter_id`:
    обвиняемый не должен узнать, кто пожаловался. Нет `accused_uuid`:
    жалобщик не должен получить подтверждение, что обвиняемый — именно он,
    иначе при `disputed` он восстановил бы чужой флаг (класс D-7.2-5).
    Проверяется тестом байт-в-байт по набору ключей и по числу SQL.

    **Почему `409` на повтор** (AC-07, D-7.4-3). Тихий `200` создал бы ложное
    впечатление новой жалобы. Защита — `UNIQUE (attendance_id, reporter_id)`
    и `ON CONFLICT DO NOTHING RETURNING`; предпроверки нет (P-3).

    **Почему «нет пары» = `404 card_not_found`** (AC-12, D-7.4-9). `403`
    подтвердил бы существование карточки, а отдельный код — состав компании.
    Тот же ответ отдаётся на «карточки нет», «не участник» и «жалоба на себя».

    `ConsentUser`, а не `CurrentUser`: жалоба связывает человека с другим
    человеком и раскрывает существование пары — без согласия на обработку ПД
    она не принимается (FR9, тот же выбор, что у `/attendance`).

    `commit()` делает роут, как в остальных пишущих маршрутах карточки;
    публикации нет: WS-событие раскрыло бы факт жалобы третьим лицам
    (AC-13, D-7.4-12).
    """
    try:
        created = await report_no_show(
            session,
            card_uuid=card_uuid,
            viewer_id=user.id,
            accused_uuid=payload.accused_uuid,
            # AC-05 стори 10.1: срез сообщества — колонка пользователя.
            viewer_community_slug=user.community_slug,
            settings=settings,
        )
    except (
        ReportSubjectNotFoundError,
        AttendancePendingError,
        AttendanceConfirmedError,
        ReportAlreadyExistsError,
    ) as error:
        return _domain_error(error)
    await session.commit()
    return NoShowReportRead.from_created(created)
