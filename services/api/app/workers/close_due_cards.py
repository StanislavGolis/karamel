"""Воркер автовыбора первого по истечении окна (стори 5б.3; FR80, NFR21).

**Что делает.** Каждые 60 секунд находит карточки, у которых `expires_at <= now`
и статус `published`, и по каждой вызывает ядро `close_due_card`: есть `pending`
— первый по очереди ADR-05 становится `selected`, карточка `full`; заявок нет —
карточка `closed` (D-5b.3-1). Второй точки входа нет: ленивое чтение
`GET /cards/{uuid}` зовёт ТО ЖЕ ядро (D-5b.3-3).

**Транзакция на карточку, не на пачку** (D-5b.3-4). `lock_for_update(author_id)`
требует чистой транзакции: общая сессия связала бы карточки, и один
`IntegrityError` откатил бы всю пачку — нарушение AC-09. Цена — N `commit`
на ≤200 карточек раз в 60 с; незначима.

**Наблюдаемость (ADR-14).** События `worker.card.decision.selected`,
`worker.card.decision.closed`, `worker.card.decision.failed` (с `card_uuid`,
типом ошибки) и `worker.card.decision.duration_ms`. На `SKIPPED` событий НЕТ:
иначе лог шумел бы каждые 60 с на каждой уже обработанной карточке.

**Безличность (AC-08, FR81).** Ни одно событие не содержит `applicant_id`
или `user_id` заявителя — только `card_uuid`. Проигравший не должен уметь
восстановить факт отказа из логов.
"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_lib
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session_factory
from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.users import UserRepository
from app.schemas.ws import WsEnvelope
from app.services.card_decision_service import (
    DecisionOutcome,
    DecisionResult,
    close_due_card,
)
from app.services.notification_service import (
    notify_company_assembled,
    notify_request_selected,
)
from app.services.ws_publisher import publish_to_chat

# NFR21: «не позднее 5 минут» — при цикле 60 с запас четырёхкратный.
WORKER_INTERVAL_SECONDS = 60
# Размер батча на один прогон: воркер обязан возвращать управление, даже
# если накопились тысячи карточек.
WORKER_BATCH_SIZE = 200

logger = structlog.get_logger(__name__)

ChatUuidFactory = Callable[[], uuid_lib.UUID]


async def run_decisions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = WORKER_BATCH_SIZE,
    chat_uuid_factory: ChatUuidFactory = uuid_lib.uuid4,
) -> dict[str, int]:
    """AC-03, AC-04, AC-05, AC-09: один прогон автовыбора. Возвращает счётчики.

    Порядок:

    1. отдельная сессия читает кандидатов `(card_id, author_id, uuid)` — без
       `FOR UPDATE` (D-5b.3-2). Сессия закрывается сразу: между выборкой и
       обработкой окно могло закрыться ленивым чтением или вторым воркером —
       это нормально, ядро вернёт `SKIPPED` и счётчик не соврёт;
    2. на КАЖДУЮ карточку — своя сессия и `commit` внутри неё. `try/except`
       вокруг карточки (AC-09): падение одной (например `IntegrityError` по
       `uq_event_cards_chat_uuid`) не отменяет остальные, а её транзакция
       откатывается целиком — карточка остаётся `published` и попадёт
       в следующий прогон;
    3. `now` — параметр, а не `datetime.now(UTC)` внутри: тесты не зависят
       от реального времени (Task 3.7), `sleep` в тестах нет.

    Счётчики `selected` / `closed_no_choice` / `skipped` / `failed` — то, что
    ФАКТИЧЕСКИ произошло; по ним пишется structlog (ADR-14). На `SKIPPED`
    событий нет: лог иначе шумит каждые 60 с на уже обработанных карточках.
    """
    moment = now if now is not None else datetime.now(UTC)
    started = time.monotonic()

    async with session_factory() as session:
        candidates = await EventCardRepository(session).due_decision_ids(now=moment, limit=limit)

    selected = 0
    closed_no_choice = 0
    skipped = 0
    failed = 0

    for card_id, author_id, card_uuid in candidates:
        result: DecisionResult | None = None
        async with session_factory() as session:
            try:
                result = await close_due_card(
                    session,
                    card_id=card_id,
                    author_id=author_id,
                    now=moment,
                    chat_uuid_factory=chat_uuid_factory,
                )
                await session.commit()
            except Exception as error:  # noqa: BLE001 — пачка обязана продолжиться
                # AC-09: транзакция карточки откатывается целиком; карточка
                # остаётся `published` и попадёт в следующий прогон.
                await session.rollback()
                failed += 1
                logger.error(
                    "worker.card.decision.failed",
                    card_uuid=str(card_uuid),
                    error=type(error).__name__,
                    detail=str(error)[:200],
                )
                continue

        if result.outcome is DecisionOutcome.SELECTED:
            selected += 1
            logger.info(
                "worker.card.decision.selected",
                card_uuid=str(result.card_uuid),
                chat_uuid=str(result.chat_uuid) if result.chat_uuid else None,
            )
            if result.chat_uuid is not None:
                # Стори 6.5 (Task 4.1, AC-04, ADR-11; CR 6.5, X-6.5-1): событие
                # сборки публикуется ЗДЕСЬ, а не в ядре `close_due_card`:
                # коммит транзакции — выше (строка 103), внутри этого цикла,
                # и владелец коммита — воркер. Ядро зовут три владельца, и
                # только здесь факт фиксации известен. До этой правки событие
                # не уходило НИКОГДА: автовыбор собирал компанию, а участники
                # не получали `card.full` (нарушение FR84/FR30).
                await publish_to_chat(
                    chat_uuid=result.chat_uuid,
                    envelope=WsEnvelope.build(
                        "card.full",
                        {
                            "card_uuid": str(result.card_uuid),
                            "chat_uuid": str(result.chat_uuid),
                        },
                    ),
                )
                # AC-08 стори 6.2 (TD п.6, ADR-13; CR-2, blind #7): событие
                # сборки пишет ВЛАДЕЛЕЦ транзакции ПОСЛЕ `session.commit()`
                # (выше). Ядро `close_due_card` этого не делает намеренно:
                # его зовут три владельца, и только здесь коммит уже выполнен.
                # `applicant_id` не пишется никогда — выбор делает система
                # (FR81), в журнале только общий ресурс `chat_uuid`.
                logger.info(
                    "chat.opened",
                    card_uuid=str(result.card_uuid),
                    chat_uuid=str(result.chat_uuid),
                )
                # Стори 9.2 (Task 4.3, AC-2/AC-3, ADR-11): уведомления —
                # ПОСЛЕ `session.commit()` (выше) и в ОТДЕЛЬНОЙ короткой
                # сессии: транзакция карточки уже закрыта блоком `async with`,
                # а держать её открытой ради уведомлений значило бы продлить
                # блокировку строки автора на время сетевого вызова.
                #
                # Ядро `close_due_card` не трогается (Task 4.3): его зовут три
                # владельца, и адресат `request.selected` читается здесь
                # отдельным запросом по `card_id`.
                #
                # Ошибка уведомления не влияет ни на пачку, ни на счётчики:
                # домен уже зафиксирован, повторной обработки не будет.
                await _notify_auto_selection(
                    session_factory,
                    card_id=card_id,
                    author_id=author_id,
                    card_uuid=result.card_uuid,
                    chat_uuid=result.chat_uuid,
                )
        elif result.outcome is DecisionOutcome.CLOSED_NO_CHOICE:
            # Стори 9.2 (AC-4, D-9.2-7): при закрытии без выбора НЕ создаётся
            # ни одного уведомления — ни автору, ни заявителям. Мутация M7
            # (слать здесь `card.closed_impersonal`) краснит AC-4.
            closed_no_choice += 1
            logger.info("worker.card.decision.closed", card_uuid=str(result.card_uuid))
        else:
            skipped += 1

    duration_ms = int((time.monotonic() - started) * 1000)
    if selected or closed_no_choice or failed:
        logger.info("worker.card.decision.duration_ms", duration_ms=duration_ms)
    return {
        "selected": selected,
        "closed_no_choice": closed_no_choice,
        "skipped": skipped,
        "failed": failed,
    }


async def _notify_auto_selection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    card_id: int,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    chat_uuid: uuid_lib.UUID,
) -> None:
    """9.2, Task 4.3: `request.selected` + `company.assembled` после автовыбора.

    **Своя короткая сессия.** Транзакция карточки закрыта (`async with` выше),
    и её переиспользование потребовало бы держать блокировку строки автора
    открытой на время сетевого вызова — прямой обход ADR-11.

    **Ядро не трогается** (Task 4.3): `close_due_card` зовут три владельца
    транзакции, и добавление адресата в `DecisionResult` сдвинуло бы контракт
    всех трёх. Выбранный заявитель читается здесь по `card_id`.

    **`CLOSED_NO_CHOICE`/`SKIPPED` сюда не доходят вовсе** — вызывающий зовёт
    функцию только в ветке `SELECTED` (AC-4; мутация M7 краснит AC-4).

    Ошибка не пробрасывается: пачка обязана продолжиться (AC-09 воркера),
    а домен уже зафиксирован.
    """
    try:
        async with session_factory() as session:
            applicant = await JoinRequestRepository(session).get_selected_applicant(card_id=card_id)
            if applicant is not None:
                applicant_uuid, applicant_id = applicant
                await notify_request_selected(
                    session,
                    applicant_uuid=applicant_uuid,
                    applicant_id=applicant_id,
                    card_uuid=card_uuid,
                    chat_uuid=chat_uuid,
                )
            author = await UserRepository(session).get_by_id(author_id)
            if author is not None:
                await notify_company_assembled(
                    session,
                    author_uuid=author.uuid,
                    author_id=author.id,
                    card_uuid=card_uuid,
                    chat_uuid=chat_uuid,
                )
    except Exception as exc:  # noqa: BLE001 — домен зафиксирован, пачка продолжается
        logger.error(
            "notification.create.failed",
            card_uuid=str(card_uuid),
            error=type(exc).__name__,
        )


async def main() -> None:
    """Цикл воркера: прогон решений, затем пауза. Ошибка прогона не убивает процесс.

    Падение одного прогона (БД недоступна, гонка с миграцией) не должно
    останавливать контейнер: `restart: unless-stopped` перезапустил бы его, но
    потерял бы уже выполненные переходы следующего прогона. Логируем и живём
    дальше — состояние карточек не теряется, они просто обработаются позже.
    """
    while True:
        try:
            await run_decisions(get_session_factory())
        except Exception as error:  # noqa: BLE001 — воркер обязан жить
            logger.error(
                "worker.run.failed",
                error=type(error).__name__,
                detail=str(error)[:500],
            )
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
