"""Напоминание автору о приближении окна решения (стори 5б.4; FR103, ADR-15).

**Зачем отдельный сервис, а не строка в воркере.** Правило «кому и когда
напоминаем» — доменное, а не транспортное: воркер `decision_reminders`
и любой будущий триггер (эпик 6) обязаны видеть ОДНО условие. Если бы
предикат жил только в запросе воркера, второй вызывающий завёл бы вторую
копию и разошёлся бы с первой при первой правке (урок 5б.3: до него
закрытие окна существовало в двух местах и «кралось» наблюдателем).

**Идемпотентность (AC-03, ADR-15).** Отдельной таблицы «кому напомнили»
нет и не будет: маркер — колонка `event_cards.decision_reminded_at`,
поставленная CAS-ом (`WHERE decision_reminded_at IS NULL`). Повторный
прогон и рестарт воркера видят пустой `RETURNING` и уходят в `SKIPPED`.
Redis-очередь как источник «отправлено» отвергнута архитектурой: она
может потерять ключ.

**Доставки здесь нет.** Инфраструктуры уведомлений нет до эпика 6
(FR45, зафиксировано в CS 5б.2, OQ-2). Заглушку не создаём: наблюдаемый
факт — structlog-событие и маркер в БД, эпик 6 либо подпишется на событие,
либо заменит его настоящей отправкой. Доказывать «уведомление отправлено»
нечем — доказываем ровно то, что есть: маркер поставлен ровно один раз.

**Безличность (AC-08, FR81).** В лог не попадает ни `author_id`, ни
`applicant_id`: идентификатор заявителя тут вообще не читается, а автор —
получатель напоминания, не признак проигравшего. Логи пишутся с `card_uuid`.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import EventCardStatus
from app.repositories.event_cards import EventCardRepository

logger = structlog.get_logger(__name__)


class ReminderOutcome(StrEnum):
    """Исход обработки одной карточки напоминанием.

    Наружу не выходит (у воркера нет HTTP): значения нужны счётчикам и тестам.
    """

    REMINDED = "reminded"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReminderResult:
    """Результат `remind_due_decision`: исход, `uuid` карточки и адрес автора.

    `remaining_seconds` заполняется только при `REMINDED` — сколько осталось
    до `expires_at` на момент напоминания; нужно логу и тексту уведомления,
    чтобы «осталось N минут» не считалось на клиенте.

    **`author_id`/`author_uuid` — только при `REMINDED`** (9.4, Task 1.3).
    Они нужны воркеру, чтобы создать уведомление FR110 ПОСЛЕ `commit`
    маркера, в отдельной сессии (D-9.4-13). При `SKIPPED` адрес не
    заполняется: доставки нет, а лишний идентификатор в результате —
    приглашение случайно использовать его при пустом `RETURNING`.

    Почему адрес отдаёт сервис, а не воркер читает `users` сам: маркер и
    адресат обязаны прийти из ОДНОГО чтения (D-9.4-5). Второй `SELECT`
    открыл бы окно «карточка уже другого автора» и вернул бы тихий дефект.
    """

    card_uuid: uuid_lib.UUID
    outcome: ReminderOutcome
    remaining_seconds: int | None = None
    author_id: int | None = None
    author_uuid: uuid_lib.UUID | None = None
    expires_at: datetime | None = None


async def remind_due_decision(
    session: AsyncSession,
    *,
    card_id: int,
    now: datetime,
    threshold_minutes: int,
) -> ReminderResult:
    """AC-01, AC-03, AC-04, AC-05, AC-08 стори 5б.4: одно напоминание или `SKIPPED`.

    Порядок шагов — КОНТРАКТ:

    1. скалярный `SELECT` (`uuid`, `status`, `expires_at`,
       `decision_reminded_at`). Карточки нет → `SKIPPED`;
    2. `status != published` → `SKIPPED` (AC-04): автор уже выбрал, окно
       закрыто автовыбором, карточка отменена или ушла в архив. Отдельного
       признака «выбор сделан» не нужно — статус и есть этот признак;
    3. `decision_reminded_at IS NOT NULL` → `SKIPPED` (AC-03): между
       выборкой кандидатов и этим чтением напоминание поставил второй прогон;
    4. `expires_at <= now` → `SKIPPED` (AC-05): окно истекло, там работает
       автовыбор 5б.3, а «успей выбрать» после истечения — ложь;
    5. `expires_at > now + threshold` → `SKIPPED` (AC-01): рано. Верхняя
       граница ВКЛЮЧИТЕЛЬНАЯ — ровно за порог минут напоминаем;
    6. CAS-маркер `mark_reminded`. `None` → `SKIPPED` (AC-03, гонка двух
       прогонов); иначе `REMINDED`.

    Шаги 2–5 не избыточны по отношению к предикату выборки: между
    `due_reminder_ids` и этим вызовом карточку мог закрыть автор, воркер
    автовыбора или другой процесс. Проверка в сервисе делает ядро
    самодостаточным — его можно звать без предварительной выборки.

    `commit` НЕ делается: владелец транзакции — вызывающий (в воркере это
    отдельная сессия на карточку, в тесте — фикстура с SAVEPOINT).
    """
    cards = EventCardRepository(session)

    # Шаг 1: состояние карточки. Без ORM-объекта и без FOR UPDATE (D-5b.4-3).
    state = await cards.reminder_state(card_id=card_id)
    if state is None:
        # Физического удаления нет (R-04): исход невозможен, но честный
        # `SKIPPED` лучше `AttributeError` на `None`.
        return ReminderResult(card_uuid=uuid_lib.UUID(int=0), outcome=ReminderOutcome.SKIPPED)

    # 9.4 (Task 1.1): шесть полей — две последние колонки адрес доставки FR110.
    card_uuid, status, expires_at, reminded_at, author_id, author_uuid = state

    # AC-04: автор уже сделал выбор или окно закрыто/отменено.
    if status != EventCardStatus.PUBLISHED:
        return ReminderResult(card_uuid=card_uuid, outcome=ReminderOutcome.SKIPPED)

    # AC-03: маркер уже стоит (второй прогон, рестарт, второй воркер).
    if reminded_at is not None:
        return ReminderResult(card_uuid=card_uuid, outcome=ReminderOutcome.SKIPPED)

    # AC-05: окно истекло — «успей выбрать» уже неправда, там автовыбор.
    if expires_at <= now:
        return ReminderResult(card_uuid=card_uuid, outcome=ReminderOutcome.SKIPPED)

    # AC-01, D-5b.4-2: рано. `>` (а не `>=`): ровно за порог — уже напоминаем.
    if expires_at > now + timedelta(minutes=threshold_minutes):
        return ReminderResult(card_uuid=card_uuid, outcome=ReminderOutcome.SKIPPED)

    # Шаг 6: CAS-маркер (ADR-15). `None` — маркер поставил другой прогон.
    marked_uuid = await cards.mark_reminded(card_id=card_id, now=now)
    if marked_uuid is None:
        return ReminderResult(card_uuid=card_uuid, outcome=ReminderOutcome.SKIPPED)

    remaining_seconds = int((expires_at - now).total_seconds())
    logger.info(
        "worker.card.decision.reminder",
        card_uuid=str(card_uuid),
        remaining_seconds=remaining_seconds,
        threshold_minutes=threshold_minutes,
    )
    # AC-01 9.4 (Task 1.3): адрес автора отдаётся ТОЛЬКО при `REMINDED`.
    # Доставка идёт после `commit` маркера, в отдельной сессии (D-9.4-13):
    # `_create_publish_mark` коммитит сам, и вызов внутри транзакции карточки
    # зафиксировал бы маркер ДО публикации, сломав AC-06 (атомарность 5б.4).
    return ReminderResult(
        card_uuid=card_uuid,
        outcome=ReminderOutcome.REMINDED,
        remaining_seconds=remaining_seconds,
        author_id=author_id,
        author_uuid=author_uuid,
        expires_at=expires_at,
    )
