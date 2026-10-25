"""Ядро запроса подтверждения явки (стори 7.3; AC-03, AC-04, AC-09, AC-11, AC-12).

**Порядок операций — КОНТРАКТ, а не стиль.** Три шага в одной транзакции:

1. **CAS** `event_cards.attendance_prompted_at` (ADR-15). Пустой `RETURNING`
   = карточку уже обработали или отменили → `SKIPPED`, выход БЕЗ единого
   `INSERT`. Это единственная защита от дубля: очереди и in-memory-маркеров
   нет (D-7.3-1), потому что второй источник истины переживёт падение
   процесса иначе, чем БД.
2. **`ensure_rows` 7.2** — переиспользуется как есть (CR 7.2 H-3). Второго
   `INSERT INTO attendance` не пишется: своя вставка потеряла бы
   `ON CONFLICT`-поведение и отсечку `participant_id <> author_id` (M4).
3. **`create_attendance_requests` ВСЕГДА**, без условий в Python. Наличие
   адресатов проверяет `EXISTS` ВНУТРИ `INSERT` (R6). `rowcount` из
   `ensure_rows` НЕ является условием: он равен `0` и когда пар нет, и когда
   их уже создал ленивый путь 7.2 — различать эти случаи обязан SQL.

**Почему маркер ставится и карточке без пар** (AC-12, D-7.3-8). Альтернатива
(не ставить) — вечный `SELECT` по одной и той же карточке каждые 60 с.
Уведомлять автора «подтвердите явку» без компании бессмысленно и вредно:
он нажмёт кнопку и получит `409 ATTENDANCE_NO_PAIR` от 7.2.

**Атомарность** (AC-09, D-7.3-2). CAS + `ensure_rows` + вставка уведомлений —
ОДНА транзакция, `commit` делает вызывающий. Это превращает «0 пропусков /
0 дублей» в свойство схемы, а не дисциплины: либо карточка помечена и
адресаты записаны, либо ничего и карточка попадёт в следующий прогон.

**`commit` и `publish` здесь НЕ делаются.** Транзакция — собственность
вызывающего (как `close_due_card`), а публикация обязана идти ПОСЛЕ commit
(ADR-11) и не бросать. Поэтому сервис возвращает всё нужное для публикации,
а воркер фиксирует и публикует в этом порядке.

**Доменных исключений нет.** У воркера нет клиента, которому отдавать код
ошибки: любая ошибка — лог и следующая карточка батча.
"""

from __future__ import annotations

import json
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.attendance import AttendanceRepository
from app.repositories.event_cards import EventCardRepository
from app.repositories.notifications import NotificationsRepository
from app.schemas.ws import utc_timestamp

__all__ = ["PromptOutcome", "prompt_attendance"]


class PromptOutcome(StrEnum):
    """Исход обработки одной карточки. Пустой CAS — норма, не ошибка."""

    PROMPTED = "prompted"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PromptResult:
    """Что произошло с карточкой и что публиковать после `commit`.

    `payload` и `chat_uuid` возвращаются наружу, а не публикуются здесь:
    публикация обязана идти ПОСЛЕ `commit` (ADR-11), а транзакцией владеет
    воркер. Пустой `requests` — карточка без компании (AC-12): маркер стоит,
    строк нет, публиковать в чат нечего.

    **`requests` вместо `recipients: int`** (9.4, Task 2.2; D-9.4-7). Для
    FR111 воркеру нужны `(notification_uuid, user_uuid)` каждой строки: по
    `user_uuid` адресуется канал `ws:user:{uuid}`, по `notification_uuid`
    ставится `delivered_at` ИМЕННО этой строке. Счётчик таких данных не даёт,
    а второй `SELECT` по `(type, card_uuid)` неоднозначен (D-9.2-5).
    Число адресатов остаётся доступным как `len(requests)`: вызывающему не
    нужен отдельный счётчик, чтобы проверить «есть кому слать».
    """

    outcome: PromptOutcome
    requests: list[tuple[uuid_lib.UUID, uuid_lib.UUID]] = field(default_factory=list)
    payload: dict[str, str] | None = None
    chat_uuid: uuid_lib.UUID | None = None


async def prompt_attendance(
    session: AsyncSession,
    *,
    card_id: int,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    chat_uuid: uuid_lib.UUID | None,
    starts_at: datetime,
    now: datetime,
) -> PromptResult:
    """AC-03…AC-05, AC-09, AC-11, AC-12: запрос подтверждения по одной карточке.

    `starts_at` — из ВЫБОРКИ (`due_prompt_ids`), не из `RETURNING` (D-TD-5):
    при пустом `RETURNING` payload не собирается вовсе, и значение не может
    оказаться `None` из-за неудавшегося CAS.

    Возвращает `PromptResult` с числом адресатов и данными для публикации.
    Транзакция (`commit`) — обязанность вызывающего: сервис не фиксирует,
    иначе «падение между CAS и вставкой» стало бы неоткатываемым (M8).
    """
    cards = EventCardRepository(session)

    # AC-05, AC-08, ADR-15: CAS первым шагом. Пустой `RETURNING` означает
    # «уже обработано другим прогоном» ИЛИ «отменено между выборкой и UPDATE»
    # (статус в предикате CAS). Оба исхода — норма.
    marked = await cards.mark_attendance_prompted(card_id=card_id, now=now)
    if marked is None:
        return PromptResult(outcome=PromptOutcome.SKIPPED)

    # AC-09 (D-7.3-2): всё дальнейшее — в ТОЙ ЖЕ транзакции, что и CAS.
    # Падение любой части откатит и маркер: частично доставленного запроса
    # без маркера не существует, и наоборот.
    attendance = AttendanceRepository(session)
    await attendance.ensure_rows(card_id=card_id, author_id=author_id)

    # AC-11, решение 3 пакета: payload собирается ОДИН раз на карточку и не
    # содержит ни `user_id`, ни `author_id`, ни имён — только адресуемую
    # карточку и время начала. Безличность здесь про третьих лиц: адресат
    # видит «по карточке X пора подтвердить», а не «кто именно идёт».
    payload: dict[str, str] = {
        "card_uuid": str(card_uuid),
        "starts_at": utc_timestamp(starts_at),
    }

    notifications = NotificationsRepository(session)
    # AC-04, AC-12: вызов БЕЗ условий в Python (R6). Адресатов определяет
    # `EXISTS`-gate внутри `INSERT`: карточка без пар даёт пустой список, но
    # маркер уже стоит — карточка не будет выбрана снова.
    #
    # 9.4 (D-9.4-7): возвращаются пары `(notification_uuid, user_uuid)` —
    # по ним воркер адресует событие в `ws:user:{uuid}` и отмечает
    # `delivered_at` именно этой строке.
    requests = await notifications.create_attendance_requests(
        card_id=card_id,
        author_id=author_id,
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )

    return PromptResult(
        outcome=PromptOutcome.PROMPTED,
        requests=requests,
        payload=payload,
        chat_uuid=chat_uuid,
    )
