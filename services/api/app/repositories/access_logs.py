"""Запись аудита доступа (стори 8.6; AC-01, AC-07, AC-08).

**Только `INSERT`.** В этом модуле нет ни `UPDATE`, ни `DELETE` по
`access_logs` — аудит append-only (D-84), и тест AC-07 проверяет текст всех
SQL-констант на их отсутствие. Триггер не вводится: он молча блокировал бы
легитимную очистку по ретенции (эпик 10), которую придётся делать осознанным
`DELETE`.

**Текста нет по сигнатуре** (D-91). Ни один метод не принимает `body`,
`reason` или `message`: утечка невыразима не только в схеме, но и в вызове —
нельзя передать то, для чего нет параметра.

**Одна функция на действие, не одна общая.** У `fragment_read` заполнены
`report_uuid`/`chat_uuid`/`card_uuid`/`message_count` и пуст `subject_uuid`;
у `card_read` — наоборот. Общий метод с шестью опциональными аргументами
позволил бы записать `message_count` у карточки и `subject_uuid` у фрагмента,
то есть создать невыразимую в домене строку.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AccessLogRepository", "FRAGMENT_READ", "CARD_READ"]

#: AC-01, AC-08: домен действия. Значения совпадают с `CHECK action_allowed`.
FRAGMENT_READ = "fragment_read"
CARD_READ = "card_read"

#: AC-01: запись доступа к переписке. `subject_uuid` НЕ передаётся: адресат
#: доступа — диалог, а не отдельный пользователь.
_INSERT_FRAGMENT_SQL = """
INSERT INTO access_logs (
    moderator_uuid, action, report_uuid, chat_uuid, card_uuid, message_count, route
) VALUES (
    :moderator_uuid, 'fragment_read', :report_uuid, :chat_uuid, :card_uuid,
    :message_count, :route
)
"""

#: AC-08: запись доступа к карточке. `chat_uuid`/`card_uuid`/`report_uuid`/
#: `message_count` НЕ передаются: карточка не раскрывает переписку, и
#: заполнять их значило бы утверждать доступ, которого не было.
_INSERT_CARD_SQL = """
INSERT INTO access_logs (moderator_uuid, action, subject_uuid, route)
VALUES (:moderator_uuid, 'card_read', :subject_uuid, :route)
"""


class AccessLogRepository:
    """Append-only запись фактов доступа модератора."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log_fragment_read(
        self,
        *,
        moderator_uuid: uuid_lib.UUID,
        report_uuid: uuid_lib.UUID,
        chat_uuid: uuid_lib.UUID,
        card_uuid: uuid_lib.UUID,
        message_count: int,
        route: str | None,
    ) -> None:
        """AC-01, AC-03: факт открытия фрагмента переписки.

        `message_count` — ФАКТИЧЕСКИ отданное число (`len(decrypted)`), не
        `FRAGMENT_LIMIT`: при трёх сообщениях в диалоге в аудите должно стоять
        три, иначе запись утверждала бы, что модератор видел больше.

        `commit` делает роут: запись аудита фиксируется вместе с ответом
        (D-90), и при падении чтения строки не остаётся.
        """
        await self._session.execute(
            text(_INSERT_FRAGMENT_SQL),
            {
                "moderator_uuid": moderator_uuid,
                "report_uuid": report_uuid,
                "chat_uuid": chat_uuid,
                "card_uuid": card_uuid,
                "message_count": message_count,
                "route": route,
            },
        )

    async def log_card_read(
        self,
        *,
        moderator_uuid: uuid_lib.UUID,
        subject_uuid: uuid_lib.UUID,
        route: str | None,
    ) -> None:
        """AC-08: факт открытия карточки пользователя (D-86).

        Только `subject_uuid`: остальные адреса карточки не касаются, и
        заполнять `chat_uuid`/`card_uuid`/`report_uuid` значило бы утверждать
        доступ к переписке, которого не было.
        """
        await self._session.execute(
            text(_INSERT_CARD_SQL),
            {
                "moderator_uuid": moderator_uuid,
                "subject_uuid": subject_uuid,
                "route": route,
            },
        )
