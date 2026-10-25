"""Чтение санкций для карточки модератора (стори 8.5; AC-07, AC-08, AC-10).

**Почему репозиторий появляется только сейчас.** До 8.5 у `sanctions` был
единственный потребитель — запись решения (8.4), и та шла сырым `INSERT` в
сервисе: читать таблицу было некому. Карточка модератора читает её дважды
(список + полный счётчик), и второй сырой SQL в сервисе развёл бы запросы
по двум модулям.

**Почему оба запроса заканчиваются на индексе.**
`ix_sanctions_user_id_created_at (user_id, created_at DESC)` создан в `0025`
именно под `WHERE user_id = :id ORDER BY created_at DESC` — оба запроса
попадают в него целиком, включая сортировку. Новый индекс не нужен.

**Почему `moderator_uuid` тянется `LEFT JOIN`.** `moderator_id` —
`ON DELETE SET NULL` (D-17): удаление аккаунта модератора не отменяет
санкцию. `LEFT JOIN` даёт `NULL` в этом случае; `JOIN` выбросил бы строку
из истории, и модератор увидел бы «санкций нет» вместо «санкция есть,
автор удалён».
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SanctionsRepository"]

#: AC-07, AC-08 (8.5): санкции пользователя, новые сверху.
#:
#: `ORDER BY s.created_at DESC, s.uuid DESC` (D-70): вторичный ключ `uuid`
#: обязателен — две санкции одной транзакции имеют одинаковый `created_at`,
#: и без него порядок внутри tie-группы зависел бы от плана запроса.
_CARD_ITEMS_SQL = """
SELECT s.uuid, s.type, s.reason, s.created_at, m.uuid AS moderator_uuid
  FROM sanctions AS s
  LEFT JOIN users AS m ON m.id = s.moderator_id
 WHERE s.user_id = :user_id
 ORDER BY s.created_at DESC, s.uuid DESC
 LIMIT :limit
"""

#: AC-10 (8.5): ПОЛНОЕ число санкций. Отдельный запрос, а не `COUNT(*) OVER ()`
#: в списке: оконная функция считалась бы ПОСЛЕ `LIMIT`, и счётчик показал бы
#: длину страницы — ровно тот дефект, который `total` очереди 8.3 закрывал (P-9).
_CARD_COUNT_SQL = "SELECT count(*) FROM sanctions WHERE user_id = :user_id"


class SanctionsRepository:
    """Чтение санкций пользователя для карточки модератора."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def card_items(
        self, *, user_id: int, limit: int
    ) -> list[tuple[uuid_lib.UUID, str, str, datetime, uuid_lib.UUID | None]]:
        """AC-07, AC-08 (8.5): санкции пользователя, новые сверху, до `limit`.

        Кортеж — ровно поля `SanctionBrief`: адрес, вид, причина, момент,
        адрес модератора (`None`, если аккаунт удалён). Внутренние `id`
        наружу не выходят (D-2).
        """
        result = await self._session.execute(
            text(_CARD_ITEMS_SQL), {"user_id": user_id, "limit": limit}
        )
        return [
            (r[0], str(r[1]), str(r[2]), r[3], None if r[4] is None else r[4]) for r in result.all()
        ]

    async def card_count(self, *, user_id: int) -> int:
        """AC-10 (8.5): полное число санкций, не длина усечённого списка."""
        result = await self._session.execute(text(_CARD_COUNT_SQL), {"user_id": user_id})
        return int(result.scalar_one())
