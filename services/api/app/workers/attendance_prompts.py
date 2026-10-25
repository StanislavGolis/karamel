"""Воркер запроса подтверждения явки (стори 7.3; FR107, NFR22, NFR20).

**Что делает.** Раз в цикл находит карточки, у которых встреча уже началась
(`starts_at <= now`), подтверждение ещё не запрашивалось
(`attendance_prompted_at IS NULL`), а статус допускает запрос
(`full`/`closed`/`finished`), и по каждой:

1. ставит CAS-маркер (ADR-15) и создаёт строки `attendance` через
   `ensure_rows` 7.2;
2. пишет адресные уведомления `attendance.request` — по одной строке на
   участника принятой заявки и одну на автора карточки (AC-04);
3. после `commit` публикует событие в канал чата (ADR-11), если чат есть.

**Почему шаг встроен в `card_windows.run_once`, а не в свой контейнер.**
Порог — 60 секунд, работа — ≤200 строк раз в минуту: отдельный процесс дал бы
вторую копию расписания и вторую точку отказа при том же объёме (NFR20,
один планировщик).

**Транзакция на карточку, не на пачку** (AC-09). `try/except` вокруг
карточки: падение одной не отменяет остальные, а её транзакция откатывается
целиком — маркер НЕ встанет, уведомлений не будет, карточка попадёт в
следующий прогон. Обратное (маркер в отдельной транзакции) означало бы
тихую потерю факта: карточка помечена обработанной, запроса нет, повторить
нельзя — она вышла из выборки навсегда.

**Безличность (AC-11, решение 3 пакета).** В логах — только `card_uuid`;
ни `user_id`, ни `author_id`, ни имён. Payload уведомления тоже безличный:
`card_uuid` и `starts_at`, ничего больше.
"""

from __future__ import annotations

import time
import uuid as uuid_lib
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.event_cards import EventCardRepository
from app.repositories.notifications import NotificationsRepository
from app.schemas.ws import WsEnvelope
from app.services.attendance_prompt_service import PromptOutcome, prompt_attendance
from app.services.ws_publisher import publish_to_chat, publish_to_user

#: Размер батча на один прогон: воркер обязан возвращать управление, даже если
#: накопились тысячи карточек. Локальная константа, а не импорт из
#: `card_windows`: иначе получился бы цикл импортов (`card_windows` импортирует
#: этот модуль ради шага в `run_once`).
WORKER_BATCH_SIZE = 200

logger = structlog.get_logger(__name__)


async def run_attendance_prompts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = WORKER_BATCH_SIZE,
) -> dict[str, int]:
    """AC-03…AC-07, AC-09, AC-12: один прогон запросов. Возвращает счётчики.

    Порядок:

    1. отдельная сессия читает кандидатов `(card_id, author_id, uuid,
       chat_uuid, starts_at)` без `FOR UPDATE` (D-5b.4-3). Сессия закрывается
       сразу: между выборкой и обработкой карточку могли отменить, и CAS
       вернёт пустой `RETURNING` — это норма (AC-08);
    2. на КАЖДУЮ карточку своя сессия, `commit`, затем `publish`. Падение
       одной не отменяет остальные;
    3. `now` — параметр, а не `datetime.now()` внутри: границы `starts_at ==
       now` и `now - 3 часа` проверяются инъекцией, `sleep` в тестах нет.

    `chat_uuid is None` — штатная ситуация (D-7.2-10): у карточки может не
    быть чата, строки `notifications` при этом уже записаны и ждут доставки
    эпика 9. Публикация — ускоритель, не условие.

    Счётчики — то, что ФАКТИЧЕСКИ произошло. На `SKIPPED` событий нет:
    лог иначе шумел бы каждые 60 с на каждой уже обработанной карточке.
    """
    moment = now if now is not None else datetime.now(UTC)
    started = time.monotonic()

    async with session_factory() as session:
        candidates = await EventCardRepository(session).due_prompt_ids(now=moment, limit=limit)

    prompted = 0
    skipped = 0
    failed = 0
    # AC-02 9.4 (Task 2.5): исходы доставки в личный канал считаются отдельно
    # от исхода запроса. `prompted` — маркер встал и строки созданы;
    # `delivered` — событие реально ушло в шину и `delivered_at` заполнен;
    # `publish_failed` — строка есть, канала нет (Redis недоступен/ошибка).
    delivered_count = 0
    publish_failed_total = 0

    for card_id, author_id, card_uuid, chat_uuid, starts_at in candidates:
        result = None
        async with session_factory() as session:
            try:
                result = await prompt_attendance(
                    session,
                    card_id=card_id,
                    author_id=author_id,
                    card_uuid=card_uuid,
                    chat_uuid=chat_uuid,
                    starts_at=starts_at,
                    now=moment,
                )
                # AC-09: ОДИН `commit` на карточку — CAS, строки `attendance`
                # и уведомления фиксируются вместе либо не фиксируются вовсе.
                await session.commit()
            except Exception as error:  # noqa: BLE001 — пачка обязана продолжиться
                # AC-09: транзакция карточки откатывается целиком; маркер
                # остаётся `NULL`, карточка попадёт в следующий прогон.
                await session.rollback()
                failed += 1
                logger.error(
                    "worker.attendance.prompt_failed",
                    card_uuid=str(card_uuid),
                    error=type(error).__name__,
                    detail=str(error)[:200],
                )
                continue

        if result is None or result.outcome is not PromptOutcome.PROMPTED:
            skipped += 1
            continue

        prompted += 1

        # 9.4 (R4): ОДИН `event_uuid` и ОДИН конверт на событие, хотя адресатов
        # несколько (чат + личные каналы). Без общего ключа дедупа член
        # компании, подписанный и на чат, и на свой канал, получил бы два
        # одинаковых приглашения: payload у них один и тот же.
        # `attendance.request` НЕ входит в `WS_TRANSITION_EVENT_TYPES`
        # (D-7.3-9: приглашение повторяемо), поэтому `build` сам ключ не
        # ставит — он передаётся явно из этой точки. Клиент дедупит по
        # `(type, event_uuid)` и склеивает доставки одного события.
        # `build` без аргумента (тест S48) по-прежнему даёт `event_uuid=None`:
        # решение принимается здесь, а не в схеме.
        envelope = (
            None
            if result.payload is None
            else WsEnvelope.build(
                "attendance.request",
                result.payload,
                moment,
                event_uuid=str(uuid_lib.uuid4()),
            )
        )

        # AC-09 7.3 (D-9.4-9), AC-02 9.4: публикация в `ws:chat` СОХРАНЯЕТСЯ.
        # Это событие для комнаты встречи, доказанное `test_attendance_prompts_ws.py`
        # (включая порядок через пробу второго соединения). 9.4 ДОБАВЛЯЕТ
        # личный канал, а не заменяет чат: замена лишила бы чат события.
        #
        # AC-12, D-7.3-8, CR 7.3 H-1: гард чата прежний (`requests` непуст
        # и `chat_uuid is not None`): карточка без компании не публикует
        # приглашение — оно ведёт участника в `409 ATTENDANCE_NO_PAIR` (7.2).
        if envelope is not None and result.requests and result.chat_uuid is not None:
            # CR 7.3 MEDIUM: `publish_to_chat` не бросает ТОЛЬКО на
            # `RedisError`/`OSError`. Любое иное исключение (например, из
            # локальной доставки) не должно обрывать батч: строки уже
            # закоммичены, остальные карточки обязаны обработаться.
            try:
                await publish_to_chat(chat_uuid=result.chat_uuid, envelope=envelope)
            except Exception as error:  # noqa: BLE001 — публикация не условие
                logger.error(
                    "worker.attendance.publish_failed",
                    card_uuid=str(card_uuid),
                    error=type(error).__name__,
                )

        # AC-02 9.4 (FR111, D-9.4-6): ЛИЧНЫЙ канал каждого адресата. Гарды
        # РАЗВЯЗАНЫ с чатом намеренно: `chat_uuid is None` — штатная ситуация
        # (D-7.2-10), и до 9.4 карточка без чата не доставляла запрос вообще —
        # это и был дефект, который закрывает FR111. Пустой `requests` —
        # карточка без компании: слать некому, цикл не исполняется.
        delivered: list[uuid_lib.UUID] = []
        card_publish_failed = 0
        if envelope is not None:
            for notification_uuid, user_uuid in result.requests:
                # Ветка в СВОЁМ `try/except`: сбой одного адресата не отменяет
                # остальных (AC-06). `publish_to_user` не бросает на
                # `RedisError`/`OSError` (уходит в локальную деградацию) и
                # возвращает `False`, поэтому исключение здесь — непредвиденное.
                try:
                    if await publish_to_user(user_uuid=user_uuid, envelope=envelope):
                        delivered.append(notification_uuid)
                    else:
                        card_publish_failed += 1
                except Exception as error:  # noqa: BLE001 — остальные адресаты важнее
                    card_publish_failed += 1
                    logger.error(
                        "worker.attendance.publish_failed",
                        card_uuid=str(card_uuid),
                        error=type(error).__name__,
                    )

        # D-9.4-10, §18.11: `delivered_at` пачкой ОДНИМ `UPDATE ... ANY`.
        # Только успешные — неуспешные остаются `NULL` (D-9.2-5): заполненное
        # значение значит «ушло в шину», и соврать остальным инстансам нельзя.
        # Отметка в отдельной транзакции: строки `notifications` уже
        # закоммичены вместе с маркером (AC-09), и ошибка отметки не должна
        # их откатывать.
        if delivered:
            try:
                async with session_factory() as notify_session:
                    await NotificationsRepository(notify_session).mark_delivered_many(
                        notification_uuids=delivered
                    )
                    await notify_session.commit()
            except Exception as error:  # noqa: BLE001 — событие уже ушло
                logger.error(
                    "notification.delivery.mark.failed",
                    notification_type="attendance.request",
                    card_uuid=str(card_uuid),
                    error=type(error).__name__,
                )
                card_publish_failed += len(delivered)
                delivered = []

        delivered_count += len(delivered)
        publish_failed_total += card_publish_failed

    duration_ms = int((time.monotonic() - started) * 1000)
    if prompted or failed:
        logger.info(
            "worker.attendance.prompted",
            count=prompted,
            duration_ms=duration_ms,
        )
    return {
        "prompted": prompted,
        "skipped": skipped,
        "failed": failed,
        "delivered": delivered_count,
        "publish_failed": publish_failed_total,
    }
