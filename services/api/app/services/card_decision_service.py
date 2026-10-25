"""Автовыбор первого по истечении окна решения (стори 5б.3; FR80, NFR21).

**Зачем отдельный сервис.** До 5б.3 переход `published -> closed` по истечении
`expires_at` существовал в двух местах: воркер `card_windows` и ленивое чтение
`GET /cards/{uuid}` (4.5). Оба звали `close_if_due` — чистый CAS без анализа
заявок. На карточке `capacity=1` с живыми `pending` это закрывало набор,
оставляя все заявки в `pending`: нарушение FR80 (`closed` без решения автора)
и AC-04 (заявившийся не узнаёт исход). Хуже того, исход зависел от трафика:
если первым дотронулся `GET`, выбор «крался» наблюдателем (D-5b.3-3).

Здесь ЕДИНСТВЕННАЯ реализация перехода по истечении окна. Воркер и ленивое
чтение вызывают одну функцию — разойтись при первой правке они не могут.

**Порядок блокировок (ADR-09) — контракт.** `users -> event_cards ->
join_requests`. Точка сериализации — строка АВТОРА (`lock_for_update`), а не
карточки: `FOR UPDATE` на `event_cards` ДО `users` дал бы встречную очередь
с `join_open_card`/`release_seat` и дедлок (урок ER 5а №3, D-5b.3-2).
У карточки один автор, поэтому два процесса (воркер и авторский `select`)
сериализуются на его строке: второй видит `full` и уходит в `SKIPPED`/`409`.

**Идемпотентность (AC-03, ADR-03).** Отдельной таблицы «что обработано» нет
и не будет: статус карточки и есть флаг. Повторный прогон перечитывает
карточку и видит не-`published` → `SKIPPED`.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import EventCardStatus
from app.models.event_card import CARD_REQUEST_MODE_CAPACITY
from app.repositories.chats import ChatRepository
from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.users import UserRepository
from app.services.join_request_service import RequestNotPendingError

logger = structlog.get_logger(__name__)


class DecisionOutcome(StrEnum):
    """Исход обработки одной карточки.

    Наружу не выходит (у воркера нет HTTP): значения нужны счётчикам и тестам.
    """

    SELECTED = "selected"
    CLOSED_NO_CHOICE = "closed_no_choice"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Результат `close_due_card`: исход и записанный `chat_uuid`.

    `chat_uuid` непустой только при `SELECTED`; при `SKIPPED`/`CLOSED_NO_CHOICE`
    он `None` — ядро не отдаёт наружу чужой идентификатор чата.
    """

    card_uuid: uuid_lib.UUID
    outcome: DecisionOutcome
    chat_uuid: uuid_lib.UUID | None = None


ChatUuidFactory = Callable[[], uuid_lib.UUID]
"""Источник `chat_uuid` для выбранного места.

Параметр нужен тестам AC-09: подменённый на занятое значение генератор
провоцирует `IntegrityError` по `uq_event_cards_chat_uuid` на ОДНОЙ карточке
и доказывает, что пачка продолжается. Повтор при коллизии не делается
намеренно: коллизия `uuid4` — сигнал сломанного генератора, и тихий ретрай
скрыл бы дефект вместо счётчика `failed`.
"""


async def close_due_card(
    session: AsyncSession,
    *,
    card_id: int,
    author_id: int,
    now: datetime,
    chat_uuid_factory: ChatUuidFactory = uuid_lib.uuid4,
) -> DecisionResult:
    """AC-01…AC-03, AC-06 стори 5б.3: закрывает окно с автовыбором первого.

    Порядок шагов — КОНТРАКТ (Task 2.2), каждый следующий опирается на
    предыдущий:

    1. `lock_for_update(author_id)` ПЕРВЫМ (AC-07, ADR-09): точка
       сериализации с авторским `select_join_request`. Без неё воркер и автор
       могли бы выбрать двух разных заявителей одной карточки;
    2. перечитать карточку по `id` обычным `SELECT` (без `FOR UPDATE`):
       не `published` или окно ещё открыто → `SKIPPED`. Это же — проверка
       идемпотентности (AC-03): `full`/`closed`/`finished`/`cancelled`
       доходят сюда и уходят без изменений;
    3. `first_pending(card_id)` — первая по `submitted_at ASC, id ASC`
       (ADR-05). Гонка «прочитали, а её отменили» разрешается на шаге 5:
       request-CAS вернёт 0 строк, и SAVEPOINT откатит место;
    4. заявок нет → `close_if_due` → `1` = `CLOSED_NO_CHOICE`, `0` = `SKIPPED`
       (карточку закрыли параллельно). `expires_at` НЕ продлевается
       (D-5b.3-1): второй раз заявок тоже не появится;
    5. заявка есть → `begin_nested()` (D-5b.2-5): seat-CAS (место + `full` +
       `chat_uuid` ОДНИМ `UPDATE`, X-39) → 0 строк ⇒ `SKIPPED` всегда: место
       занято кем-то, и повторный выбор запрещён безличностью; request-CAS
       `pending -> selected` → 0 строк ⇒ `RequestNotPendingError` (SAVEPOINT
       откатывает место, карточка остаётся `published`); `reject_pending` —
       остальные `pending` в `not_selected`.

    **Почему `begin_nested`, а не голые `UPDATE`.** Если seat-CAS прошёл,
    а request-CAS нет, карточка осталась бы `full` без `selected` — класс
    дефекта X-39. Голый `UPDATE` после неудачи оставил бы транзакцию в
    состоянии «нужен rollback» (`PendingRollbackError` на следующей карточке).

    **Почему транзакция на карточку, а не на пачку** (D-5b.3-4).
    `lock_for_update` требует чистой транзакции, а один `IntegrityError`
    потерял бы всю пачку (AC-09). Цена — N `commit` на ≤200 карточек раз
    в 60 с; незначима.

    **Почему без `FOR UPDATE SKIP LOCKED`** (D-5b.3-2, OQ-5b.3-1). `SKIP LOCKED`
    на заявках не сериализует выбор: два процесса прошли бы `SELECT` и оба
    заняли место. `FOR UPDATE` на карточке — дедлок с `join_open_card`.
    «Пропусков 0» достигается строкой автора, а не пропуском заблокированных.

    **Безличность (AC-08, FR81).** `applicant_id` не попадает ни в результат,
    ни в лог: выбор — решение системы, а не сообщение проигравшему. Лог
    содержит `card_uuid` и `chat_uuid` (идентификатор чата — общий ресурс,
    не признак заявителя), но никогда — идентификатор заявителя.
    """
    cards = EventCardRepository(session)
    requests = JoinRequestRepository(session)
    users = UserRepository(session)

    # Шаг 1: точка сериализации — строка автора (ADR-09).
    await users.lock_for_update(author_id)

    # Шаг 2: перечитываем карточку. `populate_existing` не нужен: `UPDATE`
    # идёт с `synchronize_session=False`, но объект в identity map мог быть
    # прочитан ДО `lock_for_update` — читаем скалярным `SELECT` без ORM-кэша.
    card_row = await cards.decision_state(card_id=card_id)
    if card_row is None:
        # Карточки нет — невозможно (физического удаления нет, R-04), но
        # честный `SKIPPED` лучше `AttributeError` на `None`.
        return DecisionResult(card_uuid=uuid_lib.UUID(int=0), outcome=DecisionOutcome.SKIPPED)

    card_uuid, status, expires_at, capacity = card_row

    if status != EventCardStatus.PUBLISHED or expires_at > now:
        # AC-03: идемпотентность. `full`/`closed`/`finished`/`cancelled`/
        # `draft` и открытое окно — 0 изменений.
        return DecisionResult(card_uuid=card_uuid, outcome=DecisionOutcome.SKIPPED)

    # Шаг 3: первая заявка очереди (ADR-05). Только в режиме заявок.
    # AC-01 (FR80): автовыбор существует ТОЛЬКО при `capacity=1` — тот же
    # гард, что у авторского `select_join_request` (`join_request_service:570`).
    # Дефект, найденный CR 5б.3 исполнением: без этой проверки карточка
    # открытого набора (2–6 мест) с истёкшим окном и `pending` получала
    # `SELECTED`, писала `chat_uuid` и переводила заявку в `selected`, но
    # оставалась `published` с `filled_count=1` — `select_seat` требует
    # `capacity=1` и молча не совпадал. Состояние достижимо через
    # `PATCH capacity 1 -> 2` при живых `pending` (гард «уже есть участник»
    # смотрит на `filled_count=0`, а не на заявки).
    # Открытый набор окно всё равно закрывает — но БЕЗ выбора (AC-02).
    first = (
        await requests.first_pending(card_id=card_id)
        if capacity == CARD_REQUEST_MODE_CAPACITY
        else None
    )

    if first is None:
        # Шаг 4: AC-02 — заявок нет, окно НЕ продлевается (D-5b.3-1).
        closed = await cards.close_if_due(card_id=card_id, now=now)
        if closed == 0:
            # Карточку закрыли параллельно (ленивое чтение/другой воркер).
            return DecisionResult(card_uuid=card_uuid, outcome=DecisionOutcome.SKIPPED)
        return DecisionResult(card_uuid=card_uuid, outcome=DecisionOutcome.CLOSED_NO_CHOICE)

    request_uuid, request_id, _applicant_id = first

    # Шаг 5: SAVEPOINT — seat и request либо оба, либо ни одного (D-5b.2-5).
    selected_chat_uuid: uuid_lib.UUID | None = None
    async with session.begin_nested():
        seat_row = await cards.select_seat(card_id=card_id, chat_uuid=chat_uuid_factory())
        if seat_row is None:
            # Место занято кем-то (автор выбрал сам или второй воркер успел).
            # Перечитывать статус не нужно: любой исход — `SKIPPED`, повторный
            # выбор запрещён (AC-03, AC-07).
            return DecisionResult(card_uuid=card_uuid, outcome=DecisionOutcome.SKIPPED)
        # CR 6.2 (blind #5, edge #7, auditor): доступ к `RETURNING` ПО ИМЕНИ.
        # Позиционный `seat_row[3]` держится только на текущем порядке колонок
        # и при любой правке `build_select_seat_statement` молча подставит
        # `status`/`capacity` вместо uuid. `_mapping` привязывает чтение к
        # имени колонки, и регрессия становится красной сразу.
        selected_chat_uuid = seat_row._mapping["chat_uuid"]  # noqa: SLF001
        selected_id = await requests.select_request(request_uuid=request_uuid, card_id=card_id)
        if selected_id is None:
            # Заявку отменили/выбрали между `first_pending` и CAS. SAVEPOINT
            # откатит место — карточка останется `published` для следующего
            # прогона (AC-09).
            raise RequestNotPendingError()
        await requests.reject_pending(card_id=card_id, except_id=request_id)
        # AC-01 стори 6.2 (D-6.2-4, TD п.5, риск R3): ТРЕТИЙ путь сборки.
        # Без этой строки автовыбор записывал `chat_uuid` в карточку, но
        # строки `chats` не появлялось — «компания собрана, чата нет» стало бы
        # штатным состоянием, и участник не смог бы открыть переписку.
        #
        # Порядок `users → event_cards → chats` соблюдён: `select_seat`
        # (UPDATE карточки) выполнен выше, вставка чата — третьей.
        # Внутри SAVEPOINT: откат request-CAS уносит и чат.
        await ChatRepository(session).ensure_for_card(
            chat_uuid=selected_chat_uuid, event_card_id=card_id
        )

    # AC-08 (TD п.6, ADR-13): лог `chat.opened` пишет ВЛАДЕЛЕЦ транзакции
    # ПОСЛЕ `commit` (CR-2, blind #7). Здесь `commit` не делается — владельцем
    # выступают `close_due_cards.run_decisions` и ленивое чтение
    # `event_card_service.get_card`; запись из этого места попадала бы в журнал
    # до фиксации, и откат оставлял бы «чат открыт» для чата, которого нет.
    # `chat_uuid` уходит вызывающему в `DecisionResult`.
    return DecisionResult(
        card_uuid=card_uuid,
        outcome=DecisionOutcome.SELECTED,
        chat_uuid=selected_chat_uuid,
    )
