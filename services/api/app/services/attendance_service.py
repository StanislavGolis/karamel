"""Подтверждение явки обеими сторонами (стори 7.2; AC-01…AC-11; CR 7.2 H-1…H-3, M-1, M-2).

**Что здесь решается, а что — нет.** Сервис отвечает на вопросы «видит ли
зритель карточку», «участник ли он», «наступило ли `starts_at`, идёт ли
запись» — и делегирует два последних шага: CAS-запись в БД (репозиторий)
и публикацию WS (после `commit`). Статус в БД вычисляет SQL `CASE`
репозитория по новым значениям флагов, а НЕ `derive_attendance_status`
(два источника правды на одном переходе недопустимы; TD, A-SRC-01).

**Порядок проверок — контракт анти-оракула** (TD, D-4/D-7.2-8):

1. `get_card` → `404 card_not_found` (нет карточки / чужое сообщество /
   `draft` чужого). Единственный вход видимости, второй запрос не заводится;
2. зритель не автор и не участник → ТОТ ЖЕ `404`: `422` или `403` здесь
   раскрыли бы существование карточки постороннему (ADR-13, NFR33);
3. `starts_at > now` → `422 ATTENDANCE_NOT_OPENED` (AC-07). Время, а не
   `status == 'finished'`: ленивый переход мог не выполниться (D-7.2-7);
4. guard `participant_id != author_id` (AC-10, X-7.1-7) — в сервисе И в
   `WHERE` репозитория: тест обязан ловить обход через репозиторий (D-7.2-3);
5. CAS `UPDATE ... WHERE status='pending' RETURNING` — единственная защита
   терминального статуса (AC-06, P-3). Предпроверки нет;
6. `publish` — ПОСЛЕ `commit` (ADR-11) и только на переход АГРЕГАТА карточки.

**Два статуса, а не один** (решение пользователя по CR 7.2 M-1):

- `status` у участника — статус ЕГО пары (адресный факт, основание санкции
  FR109); у автора пары нет, поэтому его `status` — агрегат карточки;
- `card_status` — агрегат по ВСЕМ парам; отдаётся только автору, потому что
  только он видит карточку как целое.

Агрегат ВЫЧИСЛЯЕТСЯ НА ЧТЕНИИ и в БД не хранится: второго источника правды
нет, инвариант строки (F1–F12) не тронут.

**`attendance.done` — одно событие на КАРТОЧКУ, не на пару** (CR 7.2 H-1/H-2).
Публикуется только когда ВСЕ пары финальны, и несёт `card_status`. Пока хоть
одна пара `pending` — события нет, ответ `200`.

**Ленивое создание строк** (CR 7.2 H-3). `attendance` наполняет воркер 7.3,
но подтверждение обязано работать до него: сервис вызывает `ensure_rows`
(`INSERT ... ON CONFLICT DO NOTHING`) в той же транзакции. Если пары так и не
появилось (нет принятой заявки) — `409 ATTENDANCE_NO_PAIR`, а не тихий `200`.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AttendanceStatus
from app.repositories.attendance import AttendanceRepository
from app.repositories.join_requests import JoinRequestRepository
from app.schemas.attendance import (
    ATTENDANCE_NO_PAIR_CODE,
    ATTENDANCE_NOT_OPENED_CODE,
    ATTENDANCE_SELF_CONFIRM_CODE,
    AttendanceState,
)
from app.schemas.ws import WsEnvelope
from app.services.event_card_service import (
    CardFeedItem,
    CardNotFoundError,
    CardSettings,
    get_card,
)
from app.services.ws_publisher import publish_to_chat

logger = structlog.get_logger(__name__)

#: chore X-7.4-4 (D-2), NFR39: заведомо несуществующий `card_id` для
#: выравнивания числа SQL на `404`-ветках. `event_cards.id` — IDENTITY,
#: начинается с 1, поэтому 0 не совпадёт ни с одной реальной карточкой.
#: Тот же приём, что `_MISSING_CARD_ID` в `chat_message_service` (D-6.4-6).
_MISSING_CARD_ID = 0

__all__ = [
    "AttendanceNoPairError",
    "AttendanceNotFoundError",
    "AttendanceNotOpenedError",
    "AttendanceSelfConfirmError",
    "confirm_attendance",
    "get_attendance",
]


class AttendanceSettings(CardSettings, Protocol):
    """Минимум настроек: `CardSettings` нужен `get_card` (срез сообщества)."""


class AttendanceNotFoundError(Exception):
    """AC-08, D-7.2-8: нет карточки, нет строки явки ИЛИ зритель не участник.

    Один класс на все три случая: разные коды или тела разошлись бы при
    первой правке и выдали бы существование карточки (ADR-13, NFR33).
    Наружу отдаётся тот же `404 card_not_found`, что у карточки.
    """

    code = "card_not_found"


class AttendanceNotOpenedError(Exception):
    """AC-07: подтверждение до `starts_at` — `422`, не `409`."""

    code = ATTENDANCE_NOT_OPENED_CODE


class AttendanceSelfConfirmError(Exception):
    """AC-10 (X-7.1-7): «участник» факта — сам автор карточки.

    `403`, не `422`: тело запроса корректно, запрещено ДЕЙСТВИЕ — иначе
    `(true,true)` дало бы `confirmed`, а `(false,false)` — `no_show` без
    второго человека, то есть подделку факта и ложную санкцию (FR109).
    """

    code = ATTENDANCE_SELF_CONFIRM_CODE


class AttendanceNoPairError(Exception):
    """CR 7.2 H-3: пары «автор ↔ зритель» нет — подтверждать нечего.

    `409`: состояние карточки не позволяет запись, хотя запрос корректен.
    Тихий `200` здесь означал бы потерю факта (клиент уверен, что подтвердил).
    """

    code = ATTENDANCE_NO_PAIR_CODE


async def _load_card(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    viewer_id: int,
    settings: AttendanceSettings,
    now: datetime,
) -> tuple[CardFeedItem, bool]:
    """Карточка через единственный вход видимости (4.5) + проверка участия.

    `get_card` уже даёт `viewer_participates` (D-5b.2-7) и `chat_uuid`
    (D-3: второй `SELECT` за `chat_uuid` сломал бы анти-оракул по числу
    запросов).
    """
    # chore X-7.4-4 (D-2), ось NFR39: число SQL на `404` НЕ должно различать
    # «карточки нет» и «не участник». До правки было 2 против 7: на
    # not-found пути запрос участия не выполнялся вовсе, и существование
    # карточки читалось по числу обращений (тело, код и заголовки совпадали).
    # Приём тот же, что в `_resolve_chat` (D-6.4-6): заведомо несуществующий
    # `card_id` для выравнивания (`event_cards.id` — IDENTITY, начинается с 1).
    card: CardFeedItem | None = None
    try:
        card = await get_card(
            session, card_uuid=card_uuid, viewer_id=viewer_id, settings=settings, now=now
        )
    except CardNotFoundError:
        # Не падаем сразу: ниже выполняется запрос участия, как и на
        # успешном пути. Иначе «нет карточки» стоил бы на один запрос
        # меньше «не участник» — та же утечка, что чинит `_load_card`.
        card = None

    if card is None:
        # Выравнивание числа SQL: на успешном пути `get_card` УЖЕ выполнил
        # запрос участия (поле `viewer_participates` считается внутри), а на
        # not-found пути его не было — отсюда разрыв. Повторяем ТОТ ЖЕ запрос
        # здесь, и «нет карточки» перестаёт быть дешевле.
        await JoinRequestRepository(session).viewer_participation_card_ids(
            applicant_id=viewer_id, card_ids=[_MISSING_CARD_ID]
        )
        # AC-08: «нет карточки» и «не участник» — один класс и одно тело.
        raise AttendanceNotFoundError()

    # Успешный путь: второй запрос участия НЕ делается. `get_card` уже
    # посчитал `viewer_participates` — дубль стоил бы лишнего round-trip и
    # сломал бы выравнивание в обратную сторону (4 против 3).
    participating = card.viewer_participates

    is_author = card.card.author_id == viewer_id
    if not is_author and not participating:
        # Проверка ДО времени: иначе `422` раскрыл бы постороннему
        # существование карточки (TD, A-SELF-ORDER-01, R7).
        raise AttendanceNotFoundError()
    return card, is_author


def _aggregate_status(statuses: list[AttendanceStatus]) -> AttendanceStatus:
    """ЕДИНСТВЕННОЕ правило агрегата по парам карточки (CR 7.2 M-2).

    Используется и для чтения (`card_status`), и для решения о публикации
    `attendance.done` — двух копий правила нет, разойтись нечему.

    - есть хоть одна незафиксированная пара → `pending`: факт не собран;
    - все финальные и одинаковые → этот статус;
    - финальные и разные → `disputed`: часть пар не подтвердилась
      (санкция в 7.2 не применяется вообще, FR89).

    Пустой список — строк ещё нет (компания не собрана): `pending`, не ошибка.
    """
    if not statuses:
        return AttendanceStatus.PENDING
    if any(status == AttendanceStatus.PENDING for status in statuses):
        return AttendanceStatus.PENDING
    first = statuses[0]
    return first if all(status == first for status in statuses) else AttendanceStatus.DISPUTED


def _state_for_author(my_answer: bool | None, statuses: list[AttendanceStatus]) -> AttendanceState:
    """Состояние автора: его ответ + агрегат карточки (CR 7.2 M-1).

    `status` и `card_status` совпадают: у автора нет своей пары, и «его
    статус» — это и есть состояние карточки. Поле `card_status` остаётся
    отдельным, чтобы контракт не зависел от того, как читается `status`
    у участника (там это статус пары, а не агрегат).
    """
    aggregate = _aggregate_status(statuses)
    return AttendanceState(my_answer=my_answer, status=aggregate, card_status=aggregate)


async def get_attendance(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    viewer_id: int,
    settings: AttendanceSettings,
    now: datetime | None = None,
) -> AttendanceState:
    """AC-08 (NFR39): собственное состояние подтверждения — и НИЧЕГО БОЛЬШЕ.

    Порядок: карточка и участие (`_load_card`) → своё состояние. Чужой флаг
    не читается вовсе, поэтому его нельзя раскрыть ни полем, ни кодом, ни
    числом SQL: запрос выбирает `CASE` по стороне зрителя.

    `my_answer = None` означает «ещё не ответил» и НЕ приводится к `false`
    (P-8, инвентаризация предикатов строка 13).
    """
    moment = now if now is not None else datetime.now(UTC)
    card, is_author = await _load_card(
        session, card_uuid=card_uuid, viewer_id=viewer_id, settings=settings, now=moment
    )

    repo = AttendanceRepository(session)
    author_id = card.card.author_id
    card_id = card.card.id

    if is_author:
        overview = await repo.get_overview(card_id=card_id, author_id=author_id)
        if overview is None:
            # Компания не собрана: подтверждать нечего и ошибки нет (D-7.2-10).
            return _state_for_author(None, [])
        my_answer, statuses = overview
        return _state_for_author(my_answer, statuses)

    state = await repo.get_state(
        card_id=card_id, viewer_id=viewer_id, author_id=author_id, participant_id=viewer_id
    )
    if state is None:
        # Строки нет: участник в компании есть, но пары «автор ↔ он» нет.
        raise AttendanceNotFoundError()
    my_answer, status = state
    return AttendanceState(my_answer=my_answer, status=status)


async def confirm_attendance(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    viewer_id: int,
    arrived: bool,
    settings: AttendanceSettings,
    now: datetime | None = None,
) -> AttendanceState:
    """AC-01…AC-11: подтверждение «пришёл / не пришёл» стороной зрителя.

    Запись — ОДИН `UPDATE` без предпроверки (P-3, D-7.2-1). Предпроверка
    (`SELECT status` → `if pending` → `UPDATE`) открыла бы окно между чтением
    и записью: именно так теряется второе подтверждение и перезаписывается
    терминальный статус (R1).

    `now` — ИНЪЕКЦИЯ (TD, A-TIME-02): без неё граница `starts_at == now`
    непроверяема, а `freezegun`/`sleep` в проекте запрещены.
    """
    moment = now if now is not None else datetime.now(UTC)
    card, is_author = await _load_card(
        session, card_uuid=card_uuid, viewer_id=viewer_id, settings=settings, now=moment
    )

    author_id = card.card.author_id
    card_id = card.card.id

    # AC-10 (X-7.1-7): «участник» факта не может быть автором карточки.
    #
    # Проверка стоит в УЧАСТНИЧЕСКОЙ ветке: у автора карточки участника нет
    # вовсе (его флаг дублируется в каждой паре, D-7.1-2), и сравнение
    # `author_id == author_id` отбивало бы ЛЮБОЕ подтверждение автора.
    # Второй уровень — `participant_id <> :author_id` в `UPDATE` (D-7.2-3).
    participant_id = viewer_id
    if not is_author and participant_id == author_id:
        raise AttendanceSelfConfirmError()

    # AC-07: время начала — по `starts_at`, не по `status` (D-7.2-7).
    if card.card.starts_at > moment:
        raise AttendanceNotOpenedError()

    repo = AttendanceRepository(session)
    # CR 7.2 H-3: строки пар могли ещё не создать (воркер 7.3 не запускался).
    # `INSERT ... ON CONFLICT DO NOTHING` идемпотентен и идёт в этой же
    # транзакции, поэтому CAS ниже видит строки.
    await repo.ensure_rows(card_id=card_id, author_id=author_id)

    written = False
    if is_author:
        transitions = await repo.confirm_author_side(
            card_id=card_id, author_id=author_id, arrived=arrived, now=moment
        )
        written = bool(transitions)
    else:
        transition = await repo.confirm(
            card_id=card_id,
            participant_id=participant_id,
            author_id=author_id,
            author_side=False,
            arrived=arrived,
            now=moment,
        )
        # ВАЖНО: первое подтверждение возвращает `pending`, и это тоже запись
        # (`written=True`). Без коммита первое подтверждение потерялось бы.
        written = transition is not None

    overview = await repo.get_overview(card_id=card_id, author_id=author_id)
    if overview is None:
        # CR 7.2 H-3: пары нет даже после ленивой вставки — принятой заявки
        # нет. Молчаливый `200` потерял бы факт.
        raise AttendanceNoPairError()

    my_answer, statuses = overview
    card_status = _aggregate_status(statuses)

    if written:
        await session.commit()

    # AC-09 (CR 7.2 H-1/H-2): событие ОДНО на карточку и только когда ВСЕ
    # пары финальны. Пока хоть одна пара `pending` — факт не собран, и
    # событие объявило бы фиксацию, которой нет.
    #
    # Решение принимается по состоянию В ЭТОЙ транзакции, а публикация идёт
    # ПОСЛЕ `commit` (ADR-11): при двух одновременных последних подтверждениях
    # оба могут увидеть `pending` и событие потеряется — это редкий случай,
    # и его закроет воркер 7.3 (он владеет переходами по расписанию).
    chat_uuid = card.card.chat_uuid
    if written and card_status in _TERMINAL and chat_uuid is not None:
        # AC-09 говорит «несёт `event_uuid` в payload», а конверт ставит его
        # сам (D-7.2-6, CR 7.2 M-5). Оба требования выполнены: `event_uuid`
        # генерируется ОДИН раз здесь и попадает и в конверт, и в payload.
        # Дедуп брокера смотрит на конверт, клиент — на payload; расхождение
        # значений между ними было бы дефектом, поэтому источник один.
        event_uuid = str(uuid_lib.uuid4())
        envelope = WsEnvelope.build(
            "attendance.done",
            {
                "event_uuid": event_uuid,
                "card_uuid": str(card_uuid),
                "chat_uuid": str(chat_uuid),
                "status": card_status.value,
            },
            moment,
            event_uuid=event_uuid,
        )
        await publish_to_chat(chat_uuid=chat_uuid, envelope=envelope)

    if is_author:
        return _state_for_author(my_answer, statuses)
    state = await repo.get_state(
        card_id=card_id, viewer_id=viewer_id, author_id=author_id, participant_id=participant_id
    )
    if state is None:
        # Гонка: строку удалили между CAS и чтением. Ответ по `overview`
        # был бы чужим; честный `409` лучше выдуманного статуса.
        raise AttendanceNoPairError()
    # ВАЖНО (AC-08): участнику отдаётся ЕГО флаг и статус ЕГО пары. `my_answer`
    # из `get_overview` — флаг АВТОРА карточки, и вернуть его участнику значило
    # бы раскрыть чужой ответ (NFR39).
    own_answer, own_status = state
    return AttendanceState(my_answer=own_answer, status=own_status)


#: Терминальные статусы агрегата: только они означают «факт собран» (AC-02…AC-04).
_TERMINAL = (
    AttendanceStatus.CONFIRMED,
    AttendanceStatus.NO_SHOW,
    AttendanceStatus.DISPUTED,
)
