"""Воркер напоминаний автору о приближении окна решения (стори 5б.4; FR103).

**Что делает.** Раз в цикл находит карточки, у которых окно ещё живо, но
до `expires_at` осталось не больше порога, и по каждой зовёт ядро
`remind_due_decision`: ставит `decision_reminded_at` (CAS, ADR-15) и пишет
событие `worker.card.decision.reminder`. Доставки нет — инфраструктура
уведомлений эпик 6 (FR45); доказываем ровно то, что есть.

**Почему шаг встроен в `card_windows.run_once`, а не в свой контейнер.**
Порог измеряется минутами, цикл — 60 секунд: отдельный процесс дал бы
вторую копию расписания и вторую точку отказа при том же объёме работы
(≤200 строк раз в минуту). Порядок шагов внутри `run_once` — КОНТРАКТ
(AC-07): напоминание ОБЯЗАНО пройти раньше `run_decisions`, иначе автовыбор
закрывает окно первым и напоминание «успей выбрать» не уходит никогда.

**Транзакция на карточку, не на пачку** (AC-06). `try/except` вокруг
карточки: падение одной (например разрыв соединения на `UPDATE`) не
отменяет остальные, а её транзакция откатывается целиком — маркер не
встанет, и карточка попадёт в следующий прогон (NFR20).

**Безличность (AC-08, FR81).** События содержат `card_uuid` и
`remaining_seconds`, но никогда — `author_id` или `applicant_id`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.event_cards import EventCardRepository
from app.services.card_reminder_service import ReminderOutcome, ReminderResult, remind_due_decision
from app.services.notification_service import notify_window_expiring

# Размер батча на один прогон: воркер обязан возвращать управление, даже если
# накопились тысячи карточек.
WORKER_BATCH_SIZE = 200

logger = structlog.get_logger(__name__)


async def _deliver_window_expiring(
    session_factory: async_sessionmaker[AsyncSession], *, result: ReminderResult
) -> bool:
    """AC-01 9.4 (FR110): доставка «окно истекает» автору. `True` — ушло в шину.

    **Отдельная короткая сессия, а не сессия карточки** (D-9.4-13).
    `_create_publish_mark` коммитит INSERT сам; в транзакции карточки это
    означало бы фиксацию CAS-маркера ДО публикации и потерю атомарности
    AC-06 5б.4. Поэтому вызов идёт строго после `await session.commit()`
    в `run_reminders`, и здесь уже новая граница транзакции.

    **Не бросает** (D-9.4-4). Любое исключение здесь — это потеря канала, а не
    маркера: карточка уже помечена напоминанием FR103 и в выборку не вернётся.
    Бросок наружу отменил бы обработку остальных карточек батча, хотя их
    маркеры тоже уже зафиксированы. Логируем без личности (FR81).
    """
    if result.author_uuid is None or result.author_id is None or result.expires_at is None:
        # Контракт `ReminderResult`: при `REMINDED` все три поля заполнены.
        # Пустое значение означало бы дефект ядра, а не сбой доставки; но
        # падать в воркере нельзя — карточка уже помечена.
        logger.error("worker.card.decision.notify_skipped", card_uuid=str(result.card_uuid))
        return False

    try:
        async with session_factory() as session:
            return await notify_window_expiring(
                session,
                author_uuid=result.author_uuid,
                author_id=result.author_id,
                card_uuid=result.card_uuid,
                expires_at=result.expires_at,
            )
    except Exception as error:  # noqa: BLE001 — маркер уже зафиксирован
        # D-9.4-4: маркер НЕ откатывается. `_create_publish_mark` ловит свои
        # отказы сам; сюда доходит только непредвиденное (например, разрыв
        # соединения при открытии сессии).
        logger.error(
            "notification.create.failed",
            notification_type="window.expiring",
            card_uuid=str(result.card_uuid),
            error=type(error).__name__,
        )
        return False


async def run_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    threshold_minutes: int,
    limit: int = WORKER_BATCH_SIZE,
) -> dict[str, int]:
    """AC-01, AC-03, AC-06 стори 5б.4: один прогон напоминаний. Возвращает счётчики.

    Порядок:

    1. отдельная сессия читает кандидатов `(card_id, author_id, uuid)` без
       `FOR UPDATE` (D-5b.4-3). Сессия закрывается сразу: между выборкой
       и обработкой карточку могли закрыть, и ядро вернёт `SKIPPED`;
    2. на КАЖДУЮ карточку своя сессия и `commit`. `try/except` вокруг
       карточки (AC-06): падение одной не отменяет остальные, её транзакция
       откатывается целиком — маркер не встанет, карточка попадёт в
       следующий прогон;
    3. `now` и `threshold_minutes` — параметры, а не чтение `datetime.now()`
       и `.env` внутри: тесты не зависят от реального времени и окружения,
       `sleep` в тестах нет.

    Счётчики `reminded` / `skipped` / `failed` — то, что ФАКТИЧЕСКИ
    произошло. На `SKIPPED` событий нет: лог иначе шумел бы каждые 60 с
    на каждой карточке с уже стоящим маркером.
    """
    moment = now if now is not None else datetime.now(UTC)
    started = time.monotonic()

    async with session_factory() as session:
        candidates = await EventCardRepository(session).due_reminder_ids(
            now=moment, threshold_minutes=threshold_minutes, limit=limit
        )

    reminded = 0
    skipped = 0
    failed = 0
    notified = 0
    notify_failed = 0

    for card_id, _author_id, card_uuid in candidates:
        result = None
        async with session_factory() as session:
            try:
                result = await remind_due_decision(
                    session,
                    card_id=card_id,
                    now=moment,
                    threshold_minutes=threshold_minutes,
                )
                await session.commit()
            except Exception as error:  # noqa: BLE001 — пачка обязана продолжиться
                # AC-06: транзакция карточки откатывается целиком; маркер
                # остаётся `NULL`, карточка попадёт в следующий прогон.
                await session.rollback()
                failed += 1
                logger.error(
                    "worker.card.decision.reminder_failed",
                    card_uuid=str(card_uuid),
                    error=type(error).__name__,
                    detail=str(error)[:200],
                )
                continue

        if result.outcome is not ReminderOutcome.REMINDED:
            skipped += 1
            continue

        reminded += 1

        # AC-01, D-9.4-13: уведомление FR110 — ПОСЛЕ `commit` маркера и в
        # ОТДЕЛЬНОЙ КОРОТКОЙ СЕССИИ. Внутри транзакции карточки звать нельзя:
        # `_create_publish_mark` коммитит сам, и маркер зафиксировался бы ДО
        # публикации — атомарность AC-06 стори 5б.4 («маркер и падение
        # откатываются вместе») была бы сломана, а `test_decision_reminders`
        # поймал бы это как регресс.
        #
        # D-9.4-4: ошибка доставки НЕ откатывает маркер. Маркер принадлежит
        # FR103 (напоминание в ядре), канал — FR110 (вторичное следствие).
        # Откат дал бы вечный перебор карточки каждые 60 с при недоступном
        # Redis — худший исход, чем потеря одного уведомления.
        if await _deliver_window_expiring(session_factory, result=result):
            notified += 1
        else:
            notify_failed += 1

    duration_ms = int((time.monotonic() - started) * 1000)
    if reminded or failed:
        logger.info("worker.card.decision.reminder_duration_ms", duration_ms=duration_ms)
    return {
        "reminded": reminded,
        "skipped": skipped,
        "failed": failed,
        # AC-01, Task 1.5 (9.4): исходы доставки FR110 считаются ОТДЕЛЬНО от
        # исхода напоминания. `reminded` — маркер FR103 встал; `notified` —
        # уведомление ушло в шину; `notify_failed` — маркер есть, канала нет
        # (тип отключён, Redis недоступен, вставка не прошла). Слить их в один
        # счётчик значило бы скрыть дефект доставки за успехом напоминания.
        "notified": notified,
        "notify_failed": notify_failed,
    }
