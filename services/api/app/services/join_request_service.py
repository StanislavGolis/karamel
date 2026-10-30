"""Сервис заявок на участие: доменные правила и коды ошибок (стори 5а.1, 5а.2).

Стори 5а.1 (Task 4; AC-06) — коды ошибок и исключения. Стори 5а.2 (Task 2–4;
AC-02…AC-07) — подача заявки: порядок гардов, блокировка актора, лимит
активных и распознавание гонки вставки. Отмена и «мои заявки» — 5а.3,
список заявившихся — 5а.4.

Почему коды и исключения заведены здесь, а не в 5а.2: AC-06 требует
доменный код для запрета заявки на СВОЮ карточку, но сама проверка
межтабличная (`join_requests.card_id → event_cards.author_id`), `CHECK`
её не выражает, триггер запрещён D-5 (D-5a.1-2). Код — часть контракта
ошибок, и он фиксируется в той стори, которая создаёт таблицу: иначе
5а.2 вводил бы код «по месту», и клиент узнал бы о нём позже, чем
таблица появилась.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    JOIN_REQUEST_APPLICANT_STATUSES,
    CardRequestsMode,
    EventCardStatus,
    JoinRequestStatus,
)
from app.models.event_card import CARD_REQUEST_MODE_CAPACITY, EventCard
from app.models.join_request import JoinRequest
from app.repositories.chats import ChatRepository
from app.repositories.errors import constraint_name_of
from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.profile_photos import ProfilePhotoRepository
from app.repositories.users import UserRepository
from app.schemas.ws import WsEnvelope
from app.services.age_service import compute_age, today_or_now
from app.services.card_requests_cursor import (
    decode_card_requests_cursor,
    encode_card_requests_cursor,
)
from app.services.join_request_cursor import (
    decode_my_requests_cursor,
    encode_my_requests_cursor,
)
from app.services.media_service import MediaStorageError, MediaUnavailableError, get_signed_url
from app.services.notification_service import (
    notify_company_assembled,
    notify_request_received,
    notify_request_selected,
)
from app.services.ws_publisher import publish_to_chat

logger = structlog.get_logger(__name__)

# AC-07 (ADR-13): карточки нет ИЛИ она чужого сообщества — один и тот же ответ.
# Строчное имя намеренно повторяет `card_not_found` карточек (4.2): клиент
# ветвится по `code`, и «карточка недоступна» должна выглядеть одинаково на
# всех маршрутах `/cards/*`.
JOIN_REQUEST_CARD_NOT_FOUND_CODE = "card_not_found"

# AC-06 (D-5a.2-5): имя частичного уникального индекса пары «карточка +
# заявитель». Константа, а не литерал в `except`: опечатка в имени
# превратила бы `409` в `500`, и заметить это можно было бы только в гонке.
CARD_APPLICANT_UNIQUE = "uq_join_requests_card_applicant_active"

# AC-06, D-5a.3-7: размер страницы «моих заявок» — константа сервера, как
# `PLACES_PAGE_SIZE`. Параметра `limit` в контракте §7.1 нет: он вводил бы
# класс дефектов `limit=0` (молча пустая выдача) и `limit=-1` (`500` из
# драйвера). Одно значение — один источник: сервис, репозиторий и тесты
# берут его отсюда, а не пишут `20` по месту.
MY_REQUESTS_PAGE_SIZE = 20

# AC-06: верхняя граница защищает от вызова сервиса с `limit` больше
# контрактного (воркер, скрипт, будущий internal-вызов): `LIMIT` без предела
# выгрузил бы историю заявок целиком. Отдельная константа, а не `=
# MY_REQUESTS_PAGE_SIZE`, чтобы уменьшение страницы не меняло молча предел
# для внутренних вызовов (приём `MAX_PLACES_PAGE_SIZE`).
MAX_MY_REQUESTS_PAGE_SIZE = 100

# AC-05, D-5a.4-1: размер страницы списка заявившихся для автора. Константа
# сервера, как `MY_REQUESTS_PAGE_SIZE`: параметра `limit` в контракте нет,
# иначе появился бы класс дефектов `limit=0`/`limit=-1`.
CARD_REQUESTS_PAGE_SIZE = 20

# AC-05: верхняя граница для внутренних вызовов (воркер, скрипт) — тот же
# приём, что `MAX_MY_REQUESTS_PAGE_SIZE`: уменьшение страницы не должно молча
# менять предел выгрузки.
MAX_CARD_REQUESTS_PAGE_SIZE = 100


class JoinRequestSettings(Protocol):
    """AC-04, AC-07: минимум настроек, который нужен сервису."""

    default_city_slug: str
    join_request_active_limit: int
    # AC-07: TTL подписи фото объявлен в Protocol, а не читается `getattr`:
    # опечатка в имени поля не покраснела бы ни в mypy, ни в тесте и молча
    # подменилась бы дефолтом модуля — срок жизни ссылки разошёлся бы с
    # остальными фото-контрактами.
    s3_signed_url_ttl: int


# AC-06, ADR-13 (FR76): заявка на собственную карточку. `403`, а не `409`:
# действие запрещено доменом независимо от состояния карточки. Код не
# раскрывает ничего о чужих карточках — проверка идёт только по своей.
JOIN_REQUEST_OWN_CARD_CODE = "JOIN_REQUEST_OWN_CARD"

# AC-04, FR77/NFR29: у заявителя уже две активные заявки. `429`, а не `409`:
# §7.2 закрепляет `429` за лимитами (тот же выбор, что у `CARD_LIMIT_REACHED`).
JOIN_REQUEST_LIMIT_REACHED_CODE = "JOIN_REQUEST_LIMIT_REACHED"

# AC-04, FR77: активная заявка на эту карточку уже есть. `409`: конфликт
# состояния пары «карточка + заявитель», а не лимит и не запрет домена.
ALREADY_REQUESTED_CODE = "ALREADY_REQUESTED"

# AC-08 (FR83, X-36): при вместимости 2..6 заявок не бывает — карточка
# в режиме открытого набора (4.1). `409`: действие не соответствует режиму
# карточки; `422` означал бы «неверный ввод», а ввод корректен.
CAPACITY_ONE_USES_REQUESTS_CODE = "CAPACITY_ONE_USES_REQUESTS"

# AC-08, FR74/FR82: карточка больше не принимает заявки (`published` нет:
# `closed`/`full`/`finished`/`cancelled`/`draft`). `409`: конфликт состояния.
CARD_NOT_ACCEPTING_CODE = "CARD_NOT_ACCEPTING"


# AC-04, ADR-13 (FR82): заявки нет ИЛИ она чужая. Один код на оба случая:
# `403` подтвердил бы существование чужой заявки, а `404` с разными телами
# позволил бы перебором `uuid` узнать, какие заявки существуют.
REQUEST_NOT_FOUND_CODE = "request_not_found"

# AC-02 (FR104): заявка уже `selected` — выйти можно только отменой участия
# (5а.6), а не отменой заявки. `409`: конфликт состояния, действие легально
# в принципе, но не сейчас.
REQUEST_SELECTED_CODE = "REQUEST_SELECTED"

# AC-05 (FR81): `expired` и `not_selected` дают ОДИН код намеренно. Разные
# коды сделали бы `DELETE` оракулом: клиент отличил бы «вас не выбрали» от
# «истекло» точечным запросом, и FR81 («никогда не показывать not_selected»)
# был бы обойдён через ошибку вместо выдачи.
REQUEST_NOT_ACTIVE_CODE = "REQUEST_NOT_ACTIVE"


class JoinRequestOwnCardError(Exception):
    """AC-06 (FR76, ADR-13): заявка на собственную карточку.

    Бросается сервисом 5а.2 ДО вставки: правило межтабличное, и БД его
    не выражает (D-5a.1-2). Отдельный код, а не `403` без тела: клиент
    обязан показать причину, не раскрывая существование чужих карточек.
    """

    code = JOIN_REQUEST_OWN_CARD_CODE


class JoinRequestLimitReachedError(Exception):
    """AC-04 (FR77, NFR29): у заявителя уже две активные заявки.

    Считаются только `pending` (`JOIN_REQUEST_ACTIVE_STATUSES`): отмена
    освобождает слот до решения (FR82, ADR-09). `details` несёт числа,
    как `CardLimitReachedError`: `uuid` карточек не передаются — они
    раскрыли бы состав чужих заявок и позволили бы перебор.
    """

    code = JOIN_REQUEST_LIMIT_REACHED_CODE

    def __init__(self, *, limit: int, active_count: int) -> None:
        super().__init__(JOIN_REQUEST_LIMIT_REACHED_CODE)
        self.limit = limit
        self.active_count = active_count

    @property
    def details(self) -> dict[str, int]:
        """AC-04: только числа — без `uuid` и без чужих данных."""
        return {"limit": self.limit, "active_count": self.active_count}


class AlreadyRequestedError(Exception):
    """AC-04 (FR77): на эту карточку уже есть активная заявка.

    Гонку отбивает частичный `uq_join_requests_card_applicant_active`,
    а не проверка-перед-вставкой (§18.3): предварительный `SELECT`
    пропускает две параллельные вставки.
    """

    code = ALREADY_REQUESTED_CODE


class CapacityOneUsesRequestsError(Exception):
    """AC-08 (FR83): карточка не в режиме заявок.

    Режим — `EventCard.is_request_mode` (вместимость 1, 4.1). При 2..6
    заявки не создаются: присоединение мгновенное (5а.5).
    """

    code = CAPACITY_ONE_USES_REQUESTS_CODE


class CardNotAcceptingError(Exception):
    """AC-08 (FR74, FR82): карточка не принимает заявки в текущем статусе.

    Принимает только `published`. `draft` ещё не виден, `closed`/`full` —
    набор закрыт, `finished`/`cancelled` — терминальные (ADR-10).
    """

    code = CARD_NOT_ACCEPTING_CODE


class JoinRequestCardNotFoundError(Exception):
    """AC-07 (ADR-13): карточки нет ИЛИ она чужого сообщества.

    Отдельный класс от `CardNotFoundError` карточек: у заявки свои `except`-ветки
    в своём роутере, но КОД совпадает (`card_not_found`) — клиент не должен
    различать «нет карточки» и «карточка другого сообщества».
    """

    code = JOIN_REQUEST_CARD_NOT_FOUND_CODE


class JoinRequestNotFoundError(Exception):
    """AC-04 (ADR-13): заявки нет ИЛИ она чужая — ответ неотличим.

    Предикат `applicant_id` стоит в самом SQL, а не в `if` после выборки:
    проверка после выборки забывается при первом рефакторинге (класс дефекта
    `city_slug` в 3.3).
    """

    code = REQUEST_NOT_FOUND_CODE


class JoinRequestSelectedError(Exception):
    """AC-02 (FR104): заявка `selected` — отменять её нельзя.

    Выход из участия — отдельное действие (5а.6): оно освобождает место
    в карточке (`filled_count`), а отмена заявки — нет (D-5a.2-2).
    """

    code = REQUEST_SELECTED_CODE


# AC-04 стори 5б.2 (FR78): карточка уже собрана — выбирать некого. `409`:
# конфликт состояния; действие легально в принципе, но не сейчас. Отдельный
# код от `CARD_NOT_ACCEPTING`: клиент по нему показывает «Компания уже
# собрана», а не «Набор закрыт» (UX §636 — безличная формулировка).
CARD_ALREADY_FILLED_CODE = "CARD_ALREADY_FILLED"

# AC-04, D-5b.2-9: заявка не `pending` (`cancelled`/`expired`/уже `selected`).
# `409`: конфликт состояния пары «карточка + заявка». Идемпотентности НЕТ
# намеренно: `200` на повтор скрыл бы двойной клик и различил бы «выбрана
# вами» и «выбрана другим», что запрещено безличностью (FR81).
REQUEST_NOT_PENDING_CODE = "REQUEST_NOT_PENDING"


class CardAlreadyFilledError(Exception):
    """AC-04 стори 5б.2 (FR78): карточка уже `full` — место занято.

    Отдельный класс от `CardNotAcceptingError`: причина для автора разная
    («компания собрана» vs «набор закрыт»), и клиент показывает разные тексты.
    """

    code = CARD_ALREADY_FILLED_CODE


class RequestNotPendingError(Exception):
    """AC-04, AC-06 стори 5б.2: заявка не в `pending` — выбирать нечего.

    Бросается ПОСЛЕ неудачного CAS и означает, что карточка осталась прежней:
    seat-CAS откатился вместе с SAVEPOINT (D-5b.2-5). Один код на
    `cancelled`/`expired`/`selected` намеренно: различать их — значит
    рассказывать автору о прошлом заявителя, а заявителю — о решении.
    """

    code = REQUEST_NOT_PENDING_CODE


class JoinRequestNotActiveError(Exception):
    """AC-05 (FR81): заявка уже не действует (`expired` или `not_selected`).

    Один класс на оба статуса: разные коды позволили бы узнать, что заявку
    отклонили, в обход FR81.
    """

    code = REQUEST_NOT_ACTIVE_CODE


async def submit_join_request(
    session: AsyncSession,
    *,
    applicant_id: int,
    card_uuid: uuid_lib.UUID,
    message: str,
    settings: JoinRequestSettings,
    repository: JoinRequestRepository | None = None,
    now: datetime | None = None,
) -> JoinRequest:
    """AC-02…AC-07: подача заявки на карточку режима «заявки» (вместимость 1).

    Порядок шагов — контракт (Task 2.4), каждый следующий опирается на
    предыдущий:

    1. `SELECT users.id ... FOR UPDATE` ПЕРВЫМ (AC-09, ADR-09): точка
       сериализации доменного лимита. Все подачи участника встают в очередь
       здесь, поэтому `COUNT` на шаге 7 видит уже зафиксированный результат
       предыдущей;
    2. карточка по `uuid` + `city_slug` (AC-07): чужое сообщество
       неотличимо от несуществующей;
    3. `404` — нет карточки (после блокировки, но до любых проверок прав);
    4. `403 JOIN_REQUEST_OWN_CARD` — своя карточка. Раньше проверки статуса:
       запрет домена не зависит от состояния карточки, и «нельзя заявляться на
       свою» остаётся верным для черновика и для закрытой;
    5. `409 CARD_NOT_ACCEPTING` — не `published`. До проверки режима: карточка
       в `full`/`closed` не принимает заявки в принципе, и ответ «это не режим
       заявок» увёл бы клиента менять карточку, а не искать другую;
    6. `409 CAPACITY_ONE_USES_REQUESTS` — режим открытого набора (AC-04).
       Проверка по `EventCard.is_request_mode`, не повторным сравнением
       `capacity`: режим определяется моделью (FR70), и вторая копия условия
       разошлась бы при добавлении промежуточной вместимости;
    7. `COUNT` активных заявок ПОД блокировкой → `429` при `>= limit` (AC-03);
    8. `INSERT` + `flush`; гонку пары отбивает частичный `uq` (AC-06).

    `filled_count` НЕ увеличивается (D-5a.2-2): место занимается при выборе
    автора (5б), а не при подаче. `event_cards` не блокируется и не пишется.
    """
    repo = repository if repository is not None else JoinRequestRepository(session)
    # AC-02 стори 5б.1: одна точка времени на вызов — гард окна и копия
    # `expires_at` в заявке обязаны опираться на один момент (D-5a.1-5).
    # `now` — параметр для тестов (как в `join_open_card`): иначе граница
    # `expires_at == now` недоказуема, и подмена `<=` на `<` в гарде
    # выживает (CR 5б.1, мутация M15).
    moment = now if now is not None else datetime.now(UTC)

    # 1. AC-09, ADR-09: строка актора блокируется ПЕРВОЙ, до чтения карточки.
    await UserRepository(session).lock_for_update(applicant_id)

    # 2. AC-07: чтение с срезом сообщества. FK/идентификаторы наружу не идут.
    card: EventCard | None = await repo.get_card_for_request(
        card_uuid, settings.default_city_slug
    )
    # 3. AC-07: нет карточки == чужое сообщество (ADR-13, `404` с тем же телом).
    if card is None:
        raise JoinRequestCardNotFoundError()
    # 4. AC-05: своя карточка — `403`, а не `404`: автор знает, что карточка его.
    if card.author_id == applicant_id:
        raise JoinRequestOwnCardError()
    # 5. AC-05: принимает только `published` (`draft`/`closed`/`full`/`finished`/
    # `cancelled` — `409`). Проверка ДО режима: конфликт состояния важнее.
    if card.status != EventCardStatus.PUBLISHED:
        raise CardNotAcceptingError()
    # AC-02 стори 5б.1 (NFR20): истёкшее окно решения закрывает приём заявок
    # ДО проверки режима — заявка, поданная после `expires_at`, не будет
    # рассмотрена (автовыбор 5б.3 идёт по заявкам внутри окна). Тот же код
    # `CARD_NOT_ACCEPTING`, что у `closed`: причина для человека одна.
    if card.expires_at <= moment:
        raise CardNotAcceptingError()
    # 6. AC-04: `capacity in 2..6` — не режим заявок. Источник истины — модель.
    if not card.is_request_mode:
        raise CapacityOneUsesRequestsError()
    # 7. AC-03: подсчёт ПОД блокировкой актора (шаг 1). Индекс
    # `ix_join_requests_applicant_pending` ускоряет, но гонку закрывает
    # именно `FOR UPDATE` (ADR-09).
    active_count = await repo.count_active(applicant_id)
    if active_count >= settings.join_request_active_limit:
        raise JoinRequestLimitReachedError(
            limit=settings.join_request_active_limit, active_count=active_count
        )
    # 8. AC-02, AC-06: `expires_at` — копия окна карточки (D-5a.1-5); гонку
    # пары отбивает частичный `uq`, поэтому предварительного `SELECT` нет
    # (§18.3: он пропускает две параллельные вставки).
    try:
        # SAVEPOINT, а не голый `INSERT` (отклонение от Task 4.3, см. Dev Agent
        # Record): после `IntegrityError` сессия уходит в состояние «нужен
        # rollback», и любой следующий запрос в ней падает `PendingRollbackError`
        # (`500` на втором действии пользователя). Откат всей транзакции в
        # сервисе (вариант `place_service`) снял бы `FOR UPDATE` с актора до
        # возврата ответа и потушил бы чужую единицу работы; `begin_nested`
        # откатывает только вставку, оставляя транзакцию живой.
        async with session.begin_nested():
            request = await repo.create(
                card_id=card.id,
                applicant_id=applicant_id,
                message=message,
                expires_at=card.expires_at,
            )
    except IntegrityError as error:
        # AC-06, D-5a.2-5: имя ограничения читается через `constraint_name_of`
        # (форма `error.orig.orig.constraint_name`, CR 3.1) — одиночный
        # `getattr(error.orig, "constraint_name")` возвращал бы `None` всегда.
        if constraint_name_of(error) == CARD_APPLICANT_UNIQUE:
            raise AlreadyRequestedError() from error
        # Прочие `IntegrityError` (CHECK `message`, FK) — честный `500`:
        # ложный `409` скрыл бы дефект валидации, и «заявка отклонена» выглядело
        # бы как нормальный конфликт состояния.
        logger.error("join_request.insert.failed", applicant_id=applicant_id)
        raise
    # CR 12.6 H-1: новая заявка — «появился шанс на решение». Если карточка
    # была помечена «рамки отсекли всех» (`audience_skipped_at`), метка
    # снимается: новый заявитель может проходить рамки, и держать его вне
    # очереди воркера до правки карточки значило бы терять выбор. В той же
    # транзакции, что и заявка: либо обе записи, либо ни одной.
    await EventCardRepository(session).clear_audience_skipped(card_id=card.id)
    await session.commit()

    # Стори 9.2 (AC-1, FR43, ADR-11): «заявка пришла» — ТОЛЬКО после commit.
    # Внутри транзакции уведомление утверждало бы факт, которого ещё нет в БД,
    # а откат `SAVEPOINT` оставил бы адресату след несуществующей заявки.
    # Мутация M6 (вызов выше `commit`) краснит тест порядка.
    #
    # Ошибка уведомления НЕ влияет на ответ (D-9.2-12): заявка уже подана.
    # `notify_request_received` сам гасит свои сбои и возвращает `False`.
    await _notify_request_received(session, card=card, request_uuid=request.uuid)
    return request


async def _notify_request_received(
    session: AsyncSession, *, card: EventCard, request_uuid: uuid_lib.UUID
) -> None:
    """Шлёт автору `request.received`; сбой не покидает функцию (D-9.2-12).

    Вынесено отдельной функцией, чтобы точка вызова в `submit_join_request`
    оставалась одной строкой и порядок «после commit» читался буквально.

    `author_uuid` читается ОТДЕЛЬНЫМ запросом, а не берётся из `card`:
    в модели карточки лежит `author_id` (bigint), а адрес канала — публичный
    `uuid` (`ws:user:{uuid}`). `expire_on_commit=False` у фабрики сессий
    делает это чтение безопасным после `commit`.
    """
    try:
        author = await UserRepository(session).get_by_id(card.author_id)
        if author is None:
            # Автор удалён между commit и уведомлением — адресата нет.
            logger.warning("notification.recipient.missing", notification_type="request.received")
            return
        await notify_request_received(
            session,
            author_uuid=author.uuid,
            author_id=author.id,
            card_uuid=card.uuid,
            request_uuid=request_uuid,
        )
    except Exception as exc:  # noqa: BLE001 — домен уже закоммичен (D-9.2-12)
        logger.error("notification.create.failed", error=type(exc).__name__)
        await session.rollback()


# ——— 5а.3: отмена заявки и «мои заявки» (AC-01…AC-09) ———


async def cancel_join_request(
    session: AsyncSession,
    *,
    applicant_id: int,
    request_uuid: uuid_lib.UUID,
    settings: JoinRequestSettings,
    repository: JoinRequestRepository | None = None,
) -> None:
    """AC-01, AC-02, AC-04, AC-05: отмена СВОЕЙ заявки до решения автора.

    Порядок — контракт:

    1. CAS-`UPDATE` `pending -> cancelled` (Task 2.2). Успех — `commit` и выход,
       без единого чтения статуса: диагностика нужна только на отказе;
    2. CAS не сработал → читаем статус (Task 2.4), чтобы отличить четыре
       исхода: `None` → `404`; `cancelled` → тихий успех (идемпотентность);
       `selected` → `409 REQUEST_SELECTED`; `expired`/`not_selected` →
       `409 REQUEST_NOT_ACTIVE`.

    **Блокировок нет ни одной** (D-5a.3-1): отмена только уменьшает `COUNT`
    активных заявок, поэтому гонка «подача + отмена» не может превысить лимит
    — подача видит либо старое, либо новое значение, оба ≤ лимита. Порядок
    ADR-09 (`users → cards`) не нарушается: блокировок не берётся вовсе, а
    `FOR UPDATE` на `users` здесь дал бы встречную очередь «отмена ждёт подачу»
    без пользы для инварианта.

    `filled_count` и `event_cards` не трогаются (D-5a.2-2): место занимается
    при выборе автора (5б), а заявка `pending` места не занимает.
    """
    repo = repository if repository is not None else JoinRequestRepository(session)

    # 1. AC-01, AC-04, AC-05: единственный `UPDATE` с предикатом `pending`.
    if await repo.cancel_own_pending(request_uuid, applicant_id, settings.default_city_slug):
        await session.commit()
        return

    # 2. AC-04, AC-05: разбор отказа. Предикаты те же (своя заявка + сообщество),
    # иначе `404` подменялся бы `409` на чужой заявке.
    status = await repo.get_own_status(request_uuid, applicant_id, settings.default_city_slug)
    if status is None:
        raise JoinRequestNotFoundError()
    if status == JoinRequestStatus.CANCELLED:
        # AC-05: повторная отмена — успех. Клиент повторил запрос после обрыва
        # сети, и `409` сообщил бы «не удалось» о уже достигнутом состоянии.
        # `decided_at` не переписывается: CAS его не нашёл, а второй `UPDATE`
        # здесь подменил бы момент фактической отмены (NFR1–NFR4).
        return
    if status == JoinRequestStatus.SELECTED:
        # AC-02: выйти из участия можно только отменой участия (5а.6).
        raise JoinRequestSelectedError()
    # AC-05, FR81: `expired` и `not_selected` — один код. Любое неизвестное
    # значение из БД тоже попадает сюда: консервативный `409` честнее `500`
    # на данных, которых быть не должно.
    raise JoinRequestNotActiveError()


@dataclass(frozen=True)
class SelectJoinRequestResult:
    """AC-01: результат выбора — что нужно для ответа `200`.

    `chat_uuid` — идентификатор чата компании. Таблицы `chats` здесь нет
    (эпик 6), поэтому значение только возвращается и записывается в
    `event_cards.chat_uuid` (D-5b.2-1). Контракт эпика 6:
    `chats.uuid = event_cards.chat_uuid` — новое значение НЕ генерируется.
    """

    card_uuid: uuid_lib.UUID
    chat_uuid: uuid_lib.UUID


async def select_join_request(
    session: AsyncSession,
    *,
    author_id: int,
    request_uuid: uuid_lib.UUID,
    settings: JoinRequestSettings,
    repository: JoinRequestRepository | None = None,
    card_repository: EventCardRepository | None = None,
    now: datetime | None = None,
    chat_uuid: uuid_lib.UUID | None = None,
) -> SelectJoinRequestResult:
    """AC-01, AC-04, AC-06 стори 5б.2: автор выбирает одного заявившегося.

    Порядок шагов — КОНТРАКТ (Task 2.3), каждый следующий опирается на
    предыдущий:

    1. `SELECT users.id ... FOR UPDATE` ПЕРВЫМ (AC-07, ADR-09): точка
       сериализации. Порядок `users -> event_cards -> join_requests`
       обязателен: обратный дал бы встречную очередь с `join_open_card`
       и дедлок под нагрузкой;
    2. карточка + `id` заявки по `uuid` заявки (AC-04, ADR-13): чужое
       сообщество неотличимо от несуществующей заявки;
    3. `404 request_not_found` — заявки нет ВООБЩЕ; `404 card_not_found` —
       заявка есть, но карточка чужая (разные коды нужны, чтобы клиент
       отличил опечатку от чужой карточки, но оба — `404` без деталей);
    4. `404 card_not_found` — не автор. Не `403`: `403` подтвердил бы
       существование чужой карточки и её заявок (ADR-13);
    5. `409 CARD_NOT_ACCEPTING` — `capacity != 1` (D-5b.2-2). Выбор есть
       только в режиме заявок (FR70); новый код заставил бы клиент
       ветвиться по признаку из `mode`;
    6. `409 CARD_NOT_ACCEPTING` — не `published` или окно истекло. Гард
       окна из 5б.1 НЕ переписывается: `select` подчиняется тому же
       правилу, что и подача — после `expires_at` решение не принимается;
    7. `409 CARD_ALREADY_FILLED` — карточка `full` (место занято);
    8. `SAVEPOINT`: seat-CAS (ОДИН `UPDATE`: `filled_count+1`, `full`,
       `chat_uuid`) → 0 строк ⇒ перечитать статус: `full` ⇒
       `CARD_ALREADY_FILLED`, иначе `CARD_NOT_ACCEPTING`; request-CAS
       `pending -> selected` → 0 строк ⇒ `REQUEST_NOT_PENDING` (SAVEPOINT
       откатывает seat: карточка НЕ станет `full`); reject-pending —
       остальные `pending` этой карточки в `not_selected`;
    9. `commit` ДО ответа (AC-01): данные обязаны быть видны второму
       соединению сразу после `200`.

    **Почему `begin_nested`, а не голые `UPDATE`** (D-5b.2-5): если seat-CAS
    прошёл, а request-CAS нет, карточка осталась бы `full` без `selected` —
    класс дефекта X-39. Голый `UPDATE` после неудачи оставил бы транзакцию
    в состоянии «нужен rollback», и следующий запрос падал бы
    `PendingRollbackError` (`500` на втором действии пользователя).

    **Почему НЕТ идемпотентности** (D-5b.2-9): повторный `select` той же
    заявки → `409 REQUEST_NOT_PENDING`. `200` скрыл бы двойной клик и
    различил бы «выбрана вами» и «выбрана другим», что запрещено
    безличностью (FR81).

    **Почему НЕТ уведомлений** (D-5b.2-3, OQ-2): таблицы `notifications`
    в БД нет, FR45 (адресат — `author_id`) целиком в эпике 6. Здесь
    доказуемо только ОТСУТСТВИЕ applicant-видимых следов — и оно
    доказывается тестами, а не заглушкой `send_notification`.
    """
    repo = repository if repository is not None else JoinRequestRepository(session)
    cards = card_repository if card_repository is not None else EventCardRepository(session)
    # AC-02 стори 5б.1: одна точка времени на вызов — гард окна и запись
    # решения обязаны опираться на один момент. `now` — параметр для тестов:
    # иначе граница `expires_at == now` недоказуема.
    moment = now if now is not None else datetime.now(UTC)
    # AC-01 (D-5b.2-1): идентификатор чата создаётся ЗДЕСЬ, до записи — он
    # нужен в ответе `200`, а `RETURNING` вернёт уже записанное значение.
    # Параметр `chat_uuid` существует ровно для тестов: проверить, что в БД
    # легло именно переданное значение, иначе нельзя.
    chat_id = chat_uuid if chat_uuid is not None else uuid_lib.uuid4()

    # 1. AC-07, ADR-09: строка автора блокируется ПЕРВОЙ в транзакции.
    await UserRepository(session).lock_for_update(author_id)

    # 2. AC-04: карточка и `id` заявки одним запросом, срез сообщества в SQL.
    found = await repo.get_card_for_select(request_uuid, settings.default_city_slug)
    if found is None:
        # 3. AC-04: различаем «заявки нет» и «карточка чужая». Оба — `404`,
        # но разными кодами: клиент не должен путать опечатку в `uuid`
        # с чужой карточкой, при этом ни один ответ не раскрывает деталей.
        if await repo.request_exists(request_uuid):
            raise JoinRequestCardNotFoundError()
        raise JoinRequestNotFoundError()
    card, request_id, request_status = found
    # 4. AC-04, ADR-13: не автор — тот же `404`, что у чужого сообщества.
    if card.author_id != author_id:
        raise JoinRequestCardNotFoundError()
    # 4a. AC-05, D-5b.2-9: заявка уже не `pending` — ОТДЕЛЬНАЯ причина отказа,
    # и она проверяется ДО состояния карточки. Иначе повторный `select` на уже
    # собранной карточке отдавал бы `CARD_ALREADY_FILLED`, хотя AC-05 требует
    # именно `REQUEST_NOT_PENDING`: автор нажал дважды, и «компания уже
    # собрана» скрыло бы, что клик был по той же заявке.
    #
    # ПОСЛЕ проверки авторства, а не до неё: статус чужой заявки — это данные
    # заявителя, и `409` на чужой карточке выдал бы их существование (ADR-13,
    # FR81). Не автор видит `404` при любом статусе заявки.
    #
    # Один код на `cancelled`/`expired`/`selected`: различать их — рассказывать
    # автору о прошлом заявителя.
    if request_status != JoinRequestStatus.PENDING.value:
        raise RequestNotPendingError()
    # 5. AC-04 (D-5b.2-2): выбор есть только при вместимости 1 (FR70).
    if card.capacity != CARD_REQUEST_MODE_CAPACITY:
        raise CardNotAcceptingError()
    # 6. AC-04: принимает только `published`; `draft`/`closed`/`finished`/
    # `cancelled` — `409`. Отдельно `full` НЕ здесь: его код другой.
    if card.status == EventCardStatus.FULL:
        raise CardAlreadyFilledError()
    if card.status != EventCardStatus.PUBLISHED:
        raise CardNotAcceptingError()
    # AC-02 стори 5б.1 (NFR20): истёкшее окно закрывает решение тем же кодом,
    # что у `closed`: причина для автора одна — «набор закрыт».
    if card.expires_at <= moment:
        raise CardNotAcceptingError()

    card_id = card.id
    card_uuid = card.uuid

    # 7-8. AC-01, AC-06 (D-5b.2-5): место, чат, решение и отказ остальным —
    # В ОДНОМ `SAVEPOINT`. Провал request-CAS откатывает seat-CAS: карточка
    # остаётся прежней (не «полукомпания»).
    try:
        async with session.begin_nested():
            seat = await cards.select_seat(card_id=card_id, chat_uuid=chat_id)
            if seat is None:
                # UPDATE не совпал — перечитываем статус, чтобы отличить
                # «место занято» от «набор закрыли параллельно».
                await session.refresh(card)
                if card.status == EventCardStatus.FULL:
                    raise CardAlreadyFilledError()
                raise CardNotAcceptingError()
            # CR 6.2 (auditor #11, edge #4): ответ и лог строятся из ЗАПИСАННОГО
            # `seat.chat_uuid`, а не из сгенерированного `chat_id`. Иначе при
            # любой аномалии клиент получал бы идентификатор, которого нет.
            selected_chat_uuid = seat.chat_uuid
            selected_id = await repo.select_request(request_uuid=request_uuid, card_id=card_id)
            if selected_id is None:
                # AC-06: заявка не `pending` — SAVEPOINT откатит seat-CAS.
                raise RequestNotPendingError()
            # AC-01, FR81: остальные `pending` этой карточки → `not_selected`.
            # Наружу число не уходит (нет `RETURNING`): клиент не должен
            # уметь отличить «отклонили одного» от «отклонили пятерых».
            await repo.reject_pending(card_id=card_id, except_id=selected_id)
            # AC-01 стори 6.2 (D-6.2-1, TD п.1): чат открывается ТРЕТЬИМ шагом,
            # после `UPDATE` карточки (порядок `users → event_cards → chats`).
            # Внутри SAVEPOINT намеренно: если request-CAS/отказ остальным
            # упадут, откат унесёт и строку `chats`, и `chat_uuid` карточки —
            # «полукомпании» не будет.
            # `ensure_for_card` идемпотентен: `None` = чат уже открыт, и это
            # НЕ ошибка (AC-04, TD п.1).
            # CR 6.2 (edge #3, auditor #11): `select_seat` пишет `chat_uuid`
            # БЕЗУСЛОВНО (`values(chat_uuid=...)`), поэтому `seat.chat_uuid`
            # здесь всегда непуст. Прежняя ветка `else chat_id` была мёртвой и
            # маскировала рассинхрон: при её срабатывании в `chats` лёг бы
            # uuid, которого нет в карточке. `assert` превращает невозможное
            # состояние в явное падение вместо тихой подмены идентификатора.
            assert seat.chat_uuid is not None, "select_seat обязан вернуть chat_uuid"
            await ChatRepository(session).ensure_for_card(
                chat_uuid=seat.chat_uuid, event_card_id=card_id
            )

    except IntegrityError as error:
        # AC-01: `uq_event_cards_chat_uuid` — сгенерированный `uuid4` совпал
        # с существующим. Вероятность ничтожна, но `409` с ложным поводом
        # скрыл бы дефект: честный `500` + лог.
        # CR 6.2 (blind #2): `author_id` и `request_uuid` в журнал НЕ пишутся.
        # Контракт безличности (AC-08/ADR-13/FR81) действует и на ветке
        # ошибки: `author_id` — PK пользователя, а по паре «карточка + время»
        # он восстанавливает авторство, то есть даёт ровно тот оракул, который
        # FR81 запрещает. Остаётся факт сбоя; диагностика — по корреляции с
        # запросом, а не по личности.
        logger.error("join_request.select.failed")
        raise error

    # 9. AC-01: `commit` до ответа — второе соединение видит выбор сразу.
    # Стори 9.2 (Task 4.1): адресат уведомления читается ДО `commit` — после
    # него между чтением и вставкой открылось бы окно, в котором заявку
    # отменили бы, и `request.selected` ушёл бы тому, кого уже нет в составе.
    applicant = await repo.get_applicant(request_uuid=request_uuid)
    await session.commit()

    # Стори 6.5 (Task 4.1, AC-04, ADR-11): публикация `card.full` ТОЛЬКО после
    # commit. Второй путь сборки из трёх; пропустить его — значит оставить
    # `card.full` в чатах, открытых автором через заявки, без события.
    if selected_chat_uuid is not None:
        await publish_to_chat(
            chat_uuid=selected_chat_uuid,
            envelope=WsEnvelope.build(
                "card.full", {"card_uuid": str(card_uuid), "chat_uuid": str(selected_chat_uuid)}
            ),
        )

    # Стори 9.2 (Task 4.1, AC-2/AC-3, ADR-11): уведомления — ТОЛЬКО после
    # `commit`. Оба адресата известны заранее: заявитель прочитан ДО коммита,
    # автор — тот, кто совершил выбор (`author_id` в аргументах).
    #
    # `notify_request_selected` — заявителю (FR44); `notify_company_assembled`
    # — ТОЛЬКО автору (FR45 вариант A, D-9.2-8). Мутация M8 (слать сборку и
    # выбранному) даёт заявителю два уведомления вместо одного и краснит AC-3.
    #
    # `applicant is None` — заявку удалили между CAS и чтением. Уведомление
    # не шлём: адресата нет, а `request.selected` без адресата был бы строкой
    # в никуда. Автор о сборке всё равно узнаёт — компания собрана.
    #
    # **CR 9.2 r1, HIGH-1: ВЕСЬ блок в `try/except`** (D-9.2-12). До фикса
    # `UserRepository.get_by_id` и `notify_*` стояли вне защиты: близ БД,
    # таймаут пула или разрыв соединения ПОСЛЕ успешного `commit` давали
    # `500` на уже выполненной операции — клиент не мог ни повторить
    # (`409 REQUEST_NOT_PENDING`), ни понять, что выбор состоялся. Домен
    # зафиксирован, значит ответ обязан быть успешным.
    if selected_chat_uuid is not None:
        try:
            if applicant is not None:
                applicant_uuid, applicant_id = applicant
                await notify_request_selected(
                    session,
                    applicant_uuid=applicant_uuid,
                    applicant_id=applicant_id,
                    card_uuid=card_uuid,
                    chat_uuid=selected_chat_uuid,
                )
            author = await UserRepository(session).get_by_id(author_id)
            if author is not None:
                await notify_company_assembled(
                    session,
                    author_uuid=author.uuid,
                    author_id=author.id,
                    card_uuid=card_uuid,
                    chat_uuid=selected_chat_uuid,
                )
        except Exception as exc:  # noqa: BLE001 — домен закоммичен (D-9.2-12)
            # В лог НЕ пишем `author_id`/`applicant_id`: безличность FR81
            # действует и на ветке ошибки (CR 6.2, blind #7).
            logger.error(
                "notification.create.failed",
                card_uuid=str(card_uuid),
                error=type(exc).__name__,
            )

    # AC-08 стори 6.2 (TD п.6, ADR-13/FR81): в логе только `card_uuid` и
    # `chat_uuid`, и только ПОСЛЕ `commit` (CR 6.2, blind #7): запись о чате,
    # которого нет из-за откатанной транзакции, — дефект аудита того же
    # класса, что и утечка личности. `applicant_id`/`author_id` не пишутся
    # никогда: выбор — решение домена, а журнал не канал утечки.
    logger.info(
        "chat.opened",
        card_uuid=str(card_uuid),
        chat_uuid=str(selected_chat_uuid),
    )

    return SelectJoinRequestResult(card_uuid=card_uuid, chat_uuid=selected_chat_uuid)


@dataclass(frozen=True)
class MyRequestEntry:
    """AC-06, AC-07: одна заявка в «Моих заявках» — доменный объект, не схема.

    §6 project-context: сервис не знает про HTTP-форму ответа; роутер собирает
    `MyRequestItem` из этих полей. Поля плоские, включая распакованные из
    JOIN'ов (`author_name`, `place_name`) — тот же приём, что `CardFeedItem`
    ленты: вложенность в двух слоях разошлась бы при первой правке.
    """

    request_uuid: uuid_lib.UUID
    status: JoinRequestStatus
    submitted_at: datetime
    request_expires_at: datetime
    message: str
    card_uuid: uuid_lib.UUID
    card_title: str
    card_category: str
    card_starts_at: datetime
    card_expires_at: datetime
    card_status: str
    card_capacity: int
    card_filled_count: int
    place_uuid: uuid_lib.UUID
    place_name: str
    place_address_text: str
    author_uuid: uuid_lib.UUID
    author_name: str | None
    author_age: int | None
    author_photo_url: str | None


@dataclass(frozen=True)
class MyRequestsPage:
    """AC-06: страница «моих заявок» + `total` + курсор (конверт §7.1)."""

    items: list[MyRequestEntry]
    total: int
    cursor: str | None


def _sign_author_photos(
    storage_keys: dict[int, str], settings: JoinRequestSettings
) -> dict[int, str]:
    """AC-07, D-11, D-5a.3-9: подписать главные фото авторов, сбой — не ошибка.

    Подпись локальная (`generate_presigned_url` не ходит в сеть), поэтому
    вызов синхронный: `to_thread` добавил бы переключение контекста дороже
    самой операции.

    Сбой хранилища НЕ валит список: заявка и карточка полезны без фото
    (`PersonMini` покажет инициалы), а `500` на весь экран из-за одного
    отсутствующего объекта в бакете — несоразмерно. URL при этом не
    логируется никогда (D-11): в лог идёт только факт сбоя.
    """
    signed: dict[int, str] = {}
    ttl = settings.s3_signed_url_ttl
    for user_id, storage_key in storage_keys.items():
        try:
            signed[user_id] = get_signed_url(storage_key, ttl=ttl)
        except (MediaStorageError, MediaUnavailableError):
            # AC-07: `MediaUnavailableError` (реквизиты S3 не заданы — штатное
            # состояние dev/стейджа) НЕ наследник `MediaStorageError`, и без
            # этой ветки он уходил бы в общий обработчик → `500` на весь
            # экран, хотя докстринг обещает «сбой хранилища не валит список».
            logger.warning("my_requests.photo.sign_failed", user_id=user_id)
    return signed


async def list_my_requests(
    session: AsyncSession,
    *,
    applicant_id: int,
    settings: JoinRequestSettings,
    cursor: str | None = None,
    limit: int = MY_REQUESTS_PAGE_SIZE,
    repository: JoinRequestRepository | None = None,
) -> MyRequestsPage:
    """AC-03, AC-06, AC-07: список своих заявок с фильтром `not_selected` в SQL.

    Порядок шагов:

    1. курок разбирается ДО запроса: битое значение — `InvalidCursorError`,
       который роутер отображает в `422 invalid_cursor` (не `500`);
    2. страница читается на `limit + 1` строк: лишняя — признак «есть ещё».
       Так `total` и наличие следующей страницы не требуют второго `COUNT`
       по другому предикату;
    3. `total` — отдельным запросом без курсора (D-5a.3-7);
    4. фото авторов — ОДНИМ пакетным запросом (Task 6.2): по одному на
       страницу, а не на элемент;
    5. курсор строится от последней ОТДАННОЙ строки, а не от лишней.

    `limit` ограничен сверху: `LIMIT` без предела выгрузил бы историю целиком,
    а `limit=0` молча дал бы пустую выдачу (тот же довод, что `MAX_PLACES_PAGE_SIZE`).
    """
    repo = repository if repository is not None else JoinRequestRepository(session)
    # AC-06: `is not None`, а не truthiness — `?cursor=` (пустая строка) это
    # дефект клиента, и `/places` отвечает на него `422 invalid_cursor`.
    # Молчаливый сброс на первую страницу скрыл бы ошибку листания.
    after = decode_my_requests_cursor(cursor) if cursor is not None else None
    effective_limit = max(1, min(limit, MAX_MY_REQUESTS_PAGE_SIZE))

    rows = await repo.list_my_requests(
        applicant_id=applicant_id,
        city_slug=settings.default_city_slug,
        after=after,
        limit=effective_limit + 1,
    )
    has_more = len(rows) > effective_limit
    page_rows = rows[:effective_limit]

    total = await repo.count_my_requests(
        applicant_id=applicant_id, city_slug=settings.default_city_slug
    )

    author_ids = [int(row.author_id) for row in page_rows]
    storage_keys = await ProfilePhotoRepository(session).list_primary_approved_by_user_ids(
        author_ids
    )
    signed = _sign_author_photos(storage_keys, settings)

    entries = [
        MyRequestEntry(
            request_uuid=row.JoinRequest.uuid,
            status=JoinRequestStatus(row.JoinRequest.status),
            submitted_at=row.JoinRequest.submitted_at,
            request_expires_at=row.JoinRequest.expires_at,
            message=row.JoinRequest.message,
            card_uuid=row.card_uuid,
            card_title=row.card_title,
            card_category=row.card_category,
            card_starts_at=row.card_starts_at,
            card_expires_at=row.card_expires_at,
            card_status=row.card_status,
            card_capacity=row.card_capacity,
            card_filled_count=row.card_filled_count,
            place_uuid=row.place_uuid,
            place_name=row.place_name,
            place_address_text=row.place_address_text,
            author_uuid=row.author_uuid,
            author_name=row.author_name,
            # Стори 12.2, AC-3: наружу — вычисленный возраст, не колонка.
            author_age=compute_age(row.author_birth_date, row.author_age, today_or_now()),
            author_photo_url=signed.get(int(row.author_id)),
        )
        for row in page_rows
    ]

    next_cursor = None
    if has_more and entries:
        last = entries[-1]
        next_cursor = encode_my_requests_cursor(last.submitted_at, last.request_uuid)

    return MyRequestsPage(items=entries, total=total, cursor=next_cursor)


# ——— 5а.4: список заявившихся для автора (AC-01…AC-09) ———


@dataclass(frozen=True)
class CardRequestsEntry:
    """AC-01, AC-06: одна заявка в списке автора — доменный объект, не схема.

    §6 project-context: сервис не знает про HTTP-форму ответа; роутер собирает
    `CardRequestsItem` из этих полей. Поля плоские, включая распакованные из
    JOIN'ов (`applicant_name`, `applicant_age`) — тот же приём, что
    `MyRequestEntry` и `CardFeedItem`: вложенность в двух слоях разошлась бы
    при первой правке.

    `applicant_user_id` — внутренний bigint, НУЖЕН только здесь: по нему
    собираются `user_id` для пакетного запроса фото (Task 4.1). В схему он
    не попадает (D-2).
    """

    request_uuid: uuid_lib.UUID
    submitted_at: datetime
    message: str
    applicant_user_id: int
    applicant_uuid: uuid_lib.UUID
    applicant_name: str | None
    applicant_age: int | None
    photo_url: str | None
    # AC-3 стори 12.6 (FR119): проходит ли заявитель рамки карточки.
    # `None` — у карточки рамок нет, проверять нечего (не «истина»:
    # иначе «вне рамок» и «рамок нет» выглядели бы одинаково). Рамок нет
    # ⇒ `within_bounds` в ответе тоже `None`.
    within_bounds: bool | None


@dataclass(frozen=True)
class CardRequestsPage:
    """AC-03, AC-05: страница заявившихся + режим + признак выбора.

    `total` — «сколько всего под фильтром» (D-4.5-9), не «сколько осталось».
    `mode` и `selection_available` вычислены в сервисе, а не на клиенте: иначе
    правило FR70 жило бы в двух местах и разошлось бы при добавлении
    промежуточной вместимости (D-5a.4-1).
    """

    items: list[CardRequestsEntry]
    total: int
    cursor: str | None
    mode: CardRequestsMode
    selection_available: bool


def _sign_applicant_photos(
    storage_keys: dict[int, str], settings: JoinRequestSettings
) -> dict[int, str]:
    """AC-06, D-11: подписать главные фото заявителей, сбой — не ошибка.

    Приём `_sign_author_photos` (5а.3), но список свой: ошибка подписи в
    списке заявившихся не должна молча логироваться под чужим именем события
    (`my_requests.photo.sign_failed`), иначе поиск по логам показывал бы один
    экран вместо двух.

    Подпись локальная (`generate_presigned_url` не ходит в сеть), поэтому
    вызов синхронный. Сбой хранилища НЕ валит список: `PersonMini` покажет
    инициалы, а `500` на весь экран из-за одного отсутствующего объекта в
    бакете — несоразмерно. URL не логируется никогда (D-11): в лог идёт
    только факт сбоя.

    `MediaUnavailableError` ловится ВМЕСТЕ с `MediaStorageError` явно: он не
    их наследник, и без этой ветки он уходил бы в общий обработчик → `500`
    (долг CR 5а.3, патч 1 — не повторять).
    """
    signed: dict[int, str] = {}
    ttl = settings.s3_signed_url_ttl
    for user_id, storage_key in storage_keys.items():
        try:
            signed[user_id] = get_signed_url(storage_key, ttl=ttl)
        except (MediaStorageError, MediaUnavailableError):
            logger.warning("card_requests.photo.sign_failed", user_id=user_id)
    return signed


def _requests_mode(capacity: int) -> CardRequestsMode:
    """AC-03, AC-09 (FR70): режим по вместимости — ОДНО место правила.

    `capacity == 1` → заявки с выбором автора (5б.2); `2..6` → открытый
    набор с мгновенным присоединением (5а.5), выбора нет. Источник истины —
    `EventCard.is_request_mode` в модели для «заявок», но здесь нужен
    ОБРАТНЫЙ признак (открытый набор), поэтому условие выписано явно и
    помечено как единственное: `capacity` валидируется при создании карточки
    (1..6), промежуточных значений нет.
    """
    return CardRequestsMode.REQUESTS if capacity == 1 else CardRequestsMode.OPEN_SET


async def list_card_requests(
    session: AsyncSession,
    *,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    settings: JoinRequestSettings,
    cursor: str | None = None,
    limit: int = CARD_REQUESTS_PAGE_SIZE,
    repository: JoinRequestRepository | None = None,
) -> CardRequestsPage:
    """AC-01…AC-09: список заявившихся, видимый ТОЛЬКО автору карточки.

    Порядок шагов — контракт, каждый следующий опирается на предыдущий:

    1. карточка по `uuid` + `city_slug`: чужое сообщество неотличимо от
       несуществующей (AC-02, ADR-13);
    2. проверка авторства ДО любых выборок заявок (D-5a.4-3): не автор →
       тот же `404 card_not_found`, что и «нет карточки». `403` подтвердил бы
       существование карточки и факт чужих заявок;
    3. режим по вместимости (AC-03/AC-09): `capacity=1` → `pending+selected`,
       иначе только `selected`. Для открытого набора до 5а.5 список пуст — и
       это не ошибка;
    4. курсор разбирается ДО запроса: битое значение обязано стоить ноль
       запросов к БД (`422 invalid_cursor`, а не `500`);
    5. страница на `limit + 1` строк: лишняя — признак «есть ещё»;
    6. `total` отдельным запросом без курсора (D-4.5-9);
    7. фото заявителей — ОДНИМ пакетным запросом на страницу (N+1 не
       допускается: страница до 100 элементов).

    Запись НЕ выполняется: `commit` не вызывается, блокировки не берутся
    (§18.3: гонок в этой стори нет, только чтение).
    """
    repo = repository if repository is not None else JoinRequestRepository(session)

    card = await repo.get_card_for_request(card_uuid, settings.default_city_slug)
    if card is None:
        # AC-02 (ADR-13): нет карточки ИЛИ чужое сообщество — один ответ.
        raise JoinRequestCardNotFoundError()
    if card.author_id != author_id:
        # AC-02 (ADR-13): не автор — ТОТ ЖЕ `404`, что и несуществующая.
        raise JoinRequestCardNotFoundError()

    mode = _requests_mode(card.capacity)
    statuses: tuple[JoinRequestStatus, ...] = (
        JOIN_REQUEST_APPLICANT_STATUSES
        if mode is CardRequestsMode.REQUESTS
        else (JoinRequestStatus.SELECTED,)
    )

    after = decode_card_requests_cursor(cursor) if cursor is not None else None
    effective_limit = max(1, min(limit, MAX_CARD_REQUESTS_PAGE_SIZE))

    # AC-3 + AC-5a стори 12.6: предикат рамок строится ЗДЕСЬ и ОДИН раз.
    # Тот же объект-выражение уходит в автовыбор (`close_due_card`), поэтому
    # «вне рамок» в списке автора и «не выбран» в автовыборе — один и тот же
    # ответ на один и тот же вопрос. Два вызова дали бы два SQL-дерева, и
    # правка одной копии не попала бы в другую (ловушка 1 Dev Notes).
    #
    # `None`, когда рамок нет: подзапрос возраста не нужен вовсе, и в ответе
    # `within_bounds` будет `None` — «проверять нечего», а не «проходит».
    within_bounds = (
        JoinRequestRepository.build_within_bounds_predicate(
            gender_filter=card.gender_filter,
            age_min=card.age_min,
            age_max=card.age_max,
        )
        if card.has_audience_bounds
        else None
    )

    rows = await repo.list_card_requests(
        card_id=card.id,
        city_slug=settings.default_city_slug,
        statuses=statuses,
        after=after,
        limit=effective_limit + 1,
        within_bounds=within_bounds,
    )
    has_more = len(rows) > effective_limit
    page_rows = rows[:effective_limit]

    total = await repo.count_card_requests(card_id=card.id, statuses=statuses)

    author_ids = [int(row.applicant_user_id) for row in page_rows]
    storage_keys = await ProfilePhotoRepository(session).list_primary_approved_by_user_ids(
        author_ids
    )
    signed = _sign_applicant_photos(storage_keys, settings)

    entries = [
        CardRequestsEntry(
            request_uuid=row.JoinRequest.uuid,
            submitted_at=row.JoinRequest.submitted_at,
            message=row.JoinRequest.message,
            applicant_user_id=int(row.applicant_user_id),
            applicant_uuid=row.applicant_uuid,
            applicant_name=row.applicant_name,
            # Стори 12.2, AC-3: возраст вычисляется из `birth_date`.
            applicant_age=compute_age(
                row.applicant_birth_date, row.applicant_age, today_or_now()
            ),
            photo_url=signed.get(int(row.applicant_user_id)),
            # AC-3 стори 12.6 (FR119): значение пришло из SQL-проекции —
            # того же предиката, что фильтрует автовыбор (AC-5a). `None` —
            # у карточки рамок нет (проекция подставила литерал `NULL`).
            within_bounds=row.within_bounds,
        )
        for row in page_rows
    ]

    next_cursor = None
    if has_more and entries:
        last = entries[-1]
        next_cursor = encode_card_requests_cursor(last.submitted_at, last.request_uuid)

    return CardRequestsPage(
        items=entries,
        total=total,
        cursor=next_cursor,
        mode=mode,
        selection_available=mode is CardRequestsMode.REQUESTS,
    )
