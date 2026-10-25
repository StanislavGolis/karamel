"""Отметка доставки адресных уведомлений (стори 8.2; AC-06, D-26).

**Граница с эпиком 9.** Здесь только ДВА перехода состояния: `delivered_at`
(после успешной WS-публикации) и `read_at` (когда пользователь открыл чат).
Группировка, настройки, счётчики и список уведомлений — эпик 9.

**Почему отдельная транзакция, а не часть основной** (D-30). Публикация в
Redis идёт ПОСЛЕ `commit` основной транзакции (ADR-11, R8): к моменту
отметки доставки откатывать уже нечего. Если бы `delivered_at` ставился в
основной транзакции, он утверждал бы «доставлено» до публикации — то есть
ложно при падении Redis между `commit` и `publish`.

**Почему ошибка Redis не откатывает отметку.** `publish_to_chat` не бросает
(R8): HTTP-ответ уже сформирован, а клиент не должен получать `500` из-за
недоступного pub/sub. Отметка доставки в этом случае означает «попытка
доставки сделана» — иначе строка осталась бы вечно недоставленной, и
повторная публикация при следующем событии её бы не нашла.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["mark_notifications_delivered", "mark_notifications_read"]

#: AC-06: отметить доставку уведомлений жалобы.
#:
#: `payload->>'chat_uuid'` — адресат события: уведомления по РАЗНЫМ диалогам
#: живут в одной таблице, и отметка по одному `type` задела бы чужие строки.
#: Бикондиционал `delivery_order` (`delivered_at >= created_at`) выполняется
#: сам: `now()` не раньше `created_at`, потому что строка создана раньше.
#: Идемпотентно: `WHERE delivered_at IS NULL` не переписывает время первой
#: доставки при повторе (§18.11).
_MARK_DELIVERED_SQL = """
UPDATE notifications
   SET delivered_at = now()
 WHERE type = :type
   AND delivered_at IS NULL
   AND (CAST(:chat_uuid AS text) IS NOT NULL
        AND payload->>'chat_uuid' = CAST(:chat_uuid AS text)
        OR CAST(:report_uuid AS text) IS NOT NULL
        AND payload->>'report_uuid' = CAST(:report_uuid AS text))
"""

#: AC-06: отметить прочтение. `read_at >= delivered_at` (бикондиционал
#: `read_after_delivery` из 7.3) выполняется: прочтение возможно только после
#: доставки, поэтому `delivered_at IS NOT NULL` — часть предиката, а не
#: дополнительная проверка. Идемпотентно: `WHERE read_at IS NULL`.
_MARK_READ_SQL = """
UPDATE notifications
   SET read_at = now()
 WHERE type = :type
   AND read_at IS NULL
   AND delivered_at IS NOT NULL
   AND payload->>'chat_uuid' = :chat_uuid
"""


async def mark_notifications_delivered(
    session: AsyncSession,
    *,
    chat_uuid: uuid_lib.UUID | None = None,
    report_uuid: uuid_lib.UUID | None = None,
    type_: str,
) -> int:
    """Ставит `delivered_at` уведомлениям диалога ИЛИ жалобы. Число строк.

    Вызывается в ОТДЕЛЬНОЙ транзакции ПОСЛЕ `publish_to_chat` (D-30):
    основная уже закоммичена, и её откат невозможен.

    **Два предиката, а не один** (DS 8.4, D-50). У жалобы на переписку есть
    `chat_uuid` в payload (8.2). У жалобы на неявку чата не существует вовсе,
    поэтому санкционное уведомление адресуется `report_uuid` — без второго
    предиката такие строки остались бы вечно недоставленными.

    Оба параметра опциональны и проверяются на `IS NOT NULL` в SQL: пустой
    предикат (`NULL = X`) не должен совпадать ни с одной строкой, иначе
    вызов без адреса отметил бы доставку ВСЕМ уведомлениям типа.

    **`CAST(:param AS text)` обязателен, а не косметика.** При `None` asyncpg
    не может вывести тип параметра в `:chat_uuid IS NOT NULL` и падает с
    `could not determine data type of parameter $2` — то есть вызов из 8.4
    (где `chat_uuid` не передаётся вовсе) ломался бы всегда. Тот же приём,
    что `CAST(:target_type AS varchar)` в очереди 8.3 (D-40).
    """
    result = await session.execute(
        text(_MARK_DELIVERED_SQL),
        {
            "chat_uuid": str(chat_uuid) if chat_uuid is not None else None,
            "report_uuid": str(report_uuid) if report_uuid is not None else None,
            "type": type_,
        },
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def mark_notifications_read(
    session: AsyncSession, *, chat_uuid: uuid_lib.UUID, type_: str
) -> int:
    """Ставит `read_at` уведомлениям диалога. Возвращает число строк.

    Вызывается из отметки прочтения чата. `delivered_at IS NOT NULL` в
    предикате — требование бикондиционала `read_after_delivery` (7.3):
    прочтение без доставки запрещено схемой.
    """
    result = await session.execute(
        text(_MARK_READ_SQL), {"chat_uuid": str(chat_uuid), "type": type_}
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
