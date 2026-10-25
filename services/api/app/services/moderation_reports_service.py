"""Очередь жалоб и фрагмент переписки для модератора (стори 8.3).

**Порядок — КОНТРАКТ** (D-3, D-29, урок H-1 из CR 8.2): гард → субъект → вход
→ чтение. Курсор (вход клиента) разбирается ПОСЛЕ резолва субъекта, иначе код
ответа на один и тот же битый вход зависел бы от состояния БД: несуществующая
жалоба + битый курсор давали бы `422` вместо `404`, и существование жалобы
читалось бы снаружи.

**Только чтение.** Ни `commit`, ни блокировок: 8.3 ничего не пишет, кроме
строки лога доступа (AC-03), и та идёт через логгер, а не через БД.

**`FRAGMENT_LIMIT` — константа, а не параметр** (D-32). FR40/NFR27 говорят
«не более 10», и клиентский `?limit=1000` обошёл бы ограничение, ради
которого AC написан. Параметр запроса здесь — вопрос объёма раскрываемой
переписки, а не удобства пагинации.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_message_body
from app.repositories.access_logs import AccessLogRepository
from app.repositories.messages import MessageRepository
from app.repositories.reports import ReportsRepository
from app.schemas.report import (
    ReportFragmentMessage,
    ReportFragmentResponse,
    ReportQueueAttendance,
    ReportQueueItem,
    ReportQueuePage,
)
from app.services.reports_queue_cursor import decode_queue_cursor, encode_queue_cursor

__all__ = [
    "FRAGMENT_LIMIT",
    "REPORT_NOT_FOUND_CODE",
    "ReportNotFoundError",
    "list_reports",
    "read_fragment",
]

logger = structlog.get_logger(__name__)

#: AC-02, FR40, NFR27, D-32: «не более 10» — константа, не параметр клиента.
FRAGMENT_LIMIT = 10

#: AC-03: жалобы нет ИЛИ она не про переписку — ОДИН код.
#: Отдельный код для «жалоба есть, но другого типа» сообщил бы модератору
#: существование объекта, к которому он и так имеет доступ; пользы нет, а
#: ветка маппинга появилась бы лишняя.
REPORT_NOT_FOUND_CODE = "report_not_found"

#: NFR52, NFR63: порог просрочки. Из настроек, а не литералом: значение
#: продуктовое, и хардкод разошёлся бы с `.env` стенда.
_DEFAULT_OVERDUE_HOURS = 20


class ReportNotFoundError(Exception):
    """AC-02: жалобы нет или фрагмента у неё не существует — `404`."""

    code = REPORT_NOT_FOUND_CODE


_RESOLVE_REPORT_SQL = """
SELECT r.id, r.target_type, r.chat_id
  FROM reports AS r
 WHERE r.uuid = :uuid
"""


def _overdue_hours() -> int:
    """Порог NFR52 из настроек; дефолт — если поле не заполнено.

    Узкая обёртка вместо прямого `settings.reports_overdue_hours`: тест
    подменяет НАСТРОЙКУ, а не эту функцию, и проверяет тот же путь, что прод.
    """
    return int(getattr(settings, "reports_overdue_hours", _DEFAULT_OVERDUE_HOURS))


async def list_reports(
    session: AsyncSession,
    *,
    target_type: str | None,
    limit: int,
    cursor: str | None,
    now: datetime | None = None,
) -> ReportQueuePage:
    """AC-01, AC-04, AC-05: страница очереди открытых жалоб по возрасту.

    `now` — только для тестов порога: в рабочем коде время берётся здесь.
    Подмена в тесте избавляет от ожидания 20 часов и не меняет поведения.

    Число SQL — константа, не зависящая от `limit`: страница (1), `total` (1),
    факты явки (1, только если на странице есть `no_show`). Запрос фактов по
    одной жалобе дал бы зависимость от `limit` (ось NFR39, мутация M7).
    """
    repository = ReportsRepository(session)
    position: tuple[datetime, uuid_lib.UUID] | None = None
    if cursor is not None:
        # AC-04, D-3: курсор — вход клиента, поэтому разбирается ПОСЛЕ того, как
        # субъект определён. Здесь субъекта-объекта нет (очередь не адресна),
        # но порядок тот же: сначала гард (в роуте), потом вход.
        position = decode_queue_cursor(cursor)

    rows = await repository.list_queue(target_type=target_type, after=position, limit=limit)
    total = await repository.count_queue(target_type=target_type)

    attendance_ids = [row[6] for row in rows if row[6] is not None]
    facts = await repository.queue_attendance_facts(attendance_ids=attendance_ids)

    moment = now if now is not None else datetime.now(UTC)
    threshold = timedelta(hours=_overdue_hours())

    items: list[ReportQueueItem] = []
    for row_uuid, row_target, _status, reported_at, _chat_id, _card_id, attendance_id in rows:
        attendance: ReportQueueAttendance | None = None
        if attendance_id is not None and attendance_id in facts:
            status, author_ok, participant_ok, card_uuid = facts[attendance_id]
            attendance = ReportQueueAttendance(
                status=status,
                author_confirmed=author_ok,
                participant_confirmed=participant_ok,
                card_uuid=card_uuid,
            )
        items.append(
            ReportQueueItem(
                uuid=row_uuid,
                target_type=row_target,  # type: ignore[arg-type]
                status="open",
                reported_at=reported_at,
                overdue=(moment - reported_at) > threshold,
                attendance=attendance,
            )
        )

    next_cursor = encode_queue_cursor(rows[-1][3], rows[-1][0]) if len(rows) == limit else None
    return ReportQueuePage(items=items, total=total, cursor=next_cursor)


async def read_fragment(
    session: AsyncSession,
    *,
    report_uuid: uuid_lib.UUID,
    moderator_id: int,
    moderator_uuid: uuid_lib.UUID,
    route: str | None = None,
) -> ReportFragmentResponse:
    """AC-02, AC-03: не более 10 последних сообщений диалога + аудит доступа.

    Порядок — КОНТРАКТ: (1) жалоба по `uuid`; (2) тип; (3) сообщения;
    (4) расшифровка; (5) лог ПОСЛЕ расшифровки и ДО возврата.

    **Почему лог после расшифровки** (D-34): запись «отдал N сообщений» при
    упавшей расшифровке была бы ложным следом в аудите, и разбирающий инцидент
    искал бы утечку, которой не было.

    **Текста сообщений в логе нет** (AC-03, NFR27): только идентификаторы и
    `message_count`. Ретенция и запись о запрещённой попытке — 8.6.
    """
    resolved = await session.execute(text(_RESOLVE_REPORT_SQL), {"uuid": report_uuid})
    found = resolved.first()
    if found is None:
        # AC-02: жалобы нет — `404`. До чтения сообщений: иначе существование
        # жалобы читалось бы по числу запросов.
        raise ReportNotFoundError()

    report_id, target_type, chat_id = int(found[0]), str(found[1]), found[2]
    if target_type != "message" or chat_id is None:
        # У жалобы на неявку фрагмента не существует. Тот же `404`, что и у
        # несуществующей: модератору различие ничего не даёт, а ветка маппинга
        # стала бы лишней.
        raise ReportNotFoundError()

    messages = await _read_last_messages(session, chat_id=int(chat_id))

    # Расшифровка — ДО лога (D-34): упавшая расшифровка не должна оставить
    # запись об успешной выдаче.
    decrypted: list[ReportFragmentMessage] = []
    for message_uuid, sender_id, sender_uuid, body, created_at in messages:
        plain = decrypt_message_body(
            body,
            chat_id=int(chat_id),
            sender_id=sender_id,
            key=settings.chat_message_key,
        )
        decrypted.append(
            ReportFragmentMessage(
                uuid=message_uuid, sender_uuid=sender_uuid, body=plain, created_at=created_at
            )
        )

    # D-39: контекст чата читается ДО лога, потому что `card_id` — обязательное
    # поле записи аудита (Task 3.3, AC-03). Второй запрос за `uuid` после лога
    # дал бы либо лог без карточки, либо два round-trip на один факт: `chats`
    # отдаёт и `uuid`, и `event_card_id` одной строкой.
    chat_uuid, card_id, card_uuid = await _chat_context(session, chat_id=int(chat_id))

    # AC-03, FR60, NFR32: кто, когда, к какому диалогу/карточке/жалобе, сколько
    # и факт расшифровки. НИ текста сообщений, ни тела жалобы (NFR27).
    logger.info(
        "moderation.fragment.read",
        moderator_id=moderator_id,
        moderator_uuid=str(moderator_uuid),
        report_id=report_id,
        report_uuid=str(report_uuid),
        chat_id=int(chat_id),
        card_id=card_id,
        message_count=len(decrypted),
        decrypted=True,
    )

    # Стори 8.6 (AC-01, D-90): долговременная запись аудита — ПОСЛЕ расшифровки
    # и в ТОЙ ЖЕ транзакции. Строка до расшифровки утверждала бы доступ,
    # которого не было; `commit` делает роут, поэтому при ошибке записи
    # ответ не уходит и строки не остаётся.
    await AccessLogRepository(session).log_fragment_read(
        moderator_uuid=moderator_uuid,
        report_uuid=report_uuid,
        chat_uuid=chat_uuid,
        card_uuid=card_uuid,
        message_count=len(decrypted),
        route=route,
    )

    return ReportFragmentResponse(
        report_uuid=report_uuid,
        chat_uuid=chat_uuid,
        message_count=len(decrypted),
        items=decrypted,
    )


async def _read_last_messages(
    session: AsyncSession, *, chat_id: int
) -> list[tuple[uuid_lib.UUID, int | None, uuid_lib.UUID | None, bytes, datetime]]:
    """AC-02: последние `FRAGMENT_LIMIT` сообщений, порядок — от старых к новым.

    `ORDER BY created_at DESC, uuid DESC LIMIT n` + переворот в коде: `ASC
    LIMIT n` вернул бы ПЕРВЫЕ сообщения диалога, а модератору нужен конец
    разговора, из-за которого подана жалоба. Ключ — существующий
    `ix_messages_chat_created_uuid`.
    """
    result = await session.execute(
        MessageRepository.build_fragment_statement(chat_id=chat_id, limit=FRAGMENT_LIMIT)
    )
    rows: list[Any] = list(result.all())
    rows.reverse()
    return [(row.uuid, row.sender_id, row.sender_uuid, row.body, row.created_at) for row in rows]


async def _chat_context(
    session: AsyncSession, *, chat_id: int
) -> tuple[uuid_lib.UUID, int, uuid_lib.UUID]:
    """`(адрес диалога, id карточки, адрес карточки)` для ответа и аудита.

    D-39: одна строка вместо трёх запросов — все три значения живут в одной
    строке `chats`/`event_cards`, и разносить их по `SELECT`-ам значило бы
    трижды платить за факт, известный сразу.

    **`card_uuid` добавлен стори 8.6** (AC-01, D-92): аудит хранит только
    публичные UUID — внутренний `card_id` наружу и в лог не идёт (D-2).
    """
    result = await session.execute(
        text(
            "SELECT c.uuid, c.event_card_id, ec.uuid "
            "  FROM chats AS c JOIN event_cards AS ec ON ec.id = c.event_card_id "
            " WHERE c.id = :id"
        ),
        {"id": chat_id},
    )
    row = result.one()
    return row[0], int(row[1]), row[2]
