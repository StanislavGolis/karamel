"""Запросы к `reports` и чтение пары `attendance` для жалобы (стори 7.4).

project-context §6: здесь только SQL, ни правил домена, ни HTTP-кодов.
Единица работы — у сервиса (`commit` делает роут, AC-03).

**Почему ставка жалобы — сырой `INSERT ... ON CONFLICT DO NOTHING RETURNING`.**
Стори 7.4 AC-07 и P-3 запрещают предпроверку: `SELECT EXISTS` перед `INSERT`
открывает окно между чтением и записью, и два одновременных запроса создали бы
две жалобы (урок D-7.2-1). Единственная защита — `UNIQUE (attendance_id,
reporter_id)` плюс `ON CONFLICT`: проигравший получает пустой `RETURNING`, и
это единственный надёжный признак «жалоба уже есть» (`DO NOTHING` без
`RETURNING` не отличил бы вставку от конфликта, D-7.4-3).

**Почему `target_type`/`status` литералами.** Значение закреплено `CHECK` в схеме
и `Literal`-ом в схемах ответа: параметр из Python дал бы второй источник
правды и позволил бы создать `target_type='message'` в обход модели
(AC-05, D-7.4-8).

**Почему `get_attendance_pair` возвращает `status` вместе с `id`.** Сервису
нужен статус пары для `422 ATTENDANCE_PENDING` (AC-04). Второй `SELECT` за
статусом дал бы второй предикат и второе число запросов (P-8).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.notification_settings import mute_gate_sql

__all__ = ["ReportsRepository"]

#: Стори 8.3 (AC-01, AC-04, D-31): очередь модератора — только ОТКРЫТЫЕ жалобы.
#: Предикат `status='open'` в `WHERE` совпадает с частичным индексом `0027`
#: дословно; без него планировщик индекс не выберет (D-31).
#: `reported_at, uuid` в `ORDER BY` — второй ключ уникален, поэтому sentinel
#: (D-9) здесь не нужен: tie-группа не теряет строку.
_QUEUE_SQL = """
SELECT r.uuid, r.target_type, r.status, r.reported_at, r.chat_id,
       r.event_card_id, r.attendance_id
  FROM reports AS r
 WHERE r.status = 'open'
   AND (CAST(:target_type AS varchar) IS NULL OR r.target_type = CAST(:target_type AS varchar))
{keyset}
 ORDER BY r.reported_at ASC, r.uuid ASC
 LIMIT :limit
"""

#: Тот же предикат, что у страницы: `total` — «сколько всего под фильтром»,
#: и `LIMIT` его не укорачивает (D-4.5-9).
_COUNT_QUEUE_SQL = """
SELECT count(*) FROM reports AS r
 WHERE r.status = 'open'
   AND (CAST(:target_type AS varchar) IS NULL OR r.target_type = CAST(:target_type AS varchar))
"""

#: AC-01, FR112: факты явки для страницы — ОДНИМ запросом по списку
#: `attendance_id`. Не по одной жалобе: страница до 50 строк дала бы 50
#: запросов, и число SQL стало бы зависеть от `limit` (ось NFR39, мутация M7).
_QUEUE_FACTS_SQL = """
SELECT a.id, a.status, a.author_confirmed, a.participant_confirmed,
       c.uuid AS card_uuid
  FROM attendance AS a
  JOIN event_cards AS c ON c.id = a.card_id
 WHERE a.id = ANY(:ids)
"""

#: AC-07, D-7.4-3: единственная запись жалобы. `ON CONFLICT` ссылается на
#: `uq_reports_attendance_id_reporter_id` по имени колонок — ограничение
#: объявлено в модели и в `0023` ровно на этой паре.
#: AC-03 стори 8.1 (D-14, D-15): `event_card_id` ОБЯЗАТЕЛЕН для `no_show` —
#: `ck_reports_target_scope` требует непустыми и пару, и карточку. Значение
#: приходит из уже прочитанной карточки (`card.card.id`), а не выводится
#: подзапросом: лишний `SELECT` в `INSERT` вернул бы второе число запросов
#: (P-8) и открыл бы окно между чтением и записью.
#:
#: Составной FK `(attendance_id, event_card_id)` гарантирует, что переданная
#: карточка — ТА САМАЯ, к которой относится пара: чужая карточка отвергается
#: базой, а не доверием к коду.
_CREATE_SQL = """
INSERT INTO reports (reporter_id, attendance_id, event_card_id, accused_id,
                    target_type, status, reported_at)
VALUES (:reporter_id, :attendance_id, :event_card_id, :accused_id, 'no_show', 'open', :now)
ON CONFLICT (attendance_id, reporter_id) DO NOTHING
RETURNING uuid, target_type, status, reported_at
"""

#: AC-02, AC-05 стори 8.2: жалоба на диалог. `chat_id` заполнен, `attendance_id`
#: и `event_card_id` пусты — этого требует `ck_reports_target_scope` (D-15).
#:
#: `ON CONFLICT (chat_id, reporter_id)` ссылается на ЧАСТИЧНЫЙ `UNIQUE`
#: `uq_reports_chat_id_reporter_id` (8.1). Имя ограничения не указывается:
#: PostgreSQL выводит арбитра по списку колонок и предикату, а предикат
#: (`chat_id IS NOT NULL`) выполняется автоматически для этой вставки.
#: Пустой `RETURNING` = «уже пожаловался» — единственный надёжный признак
#: (P-3: предпроверка открыла бы окно, D-7.4-3).
_CREATE_MESSAGE_REPORT_SQL = """
INSERT INTO reports (reporter_id, chat_id, accused_id, target_type, status, reported_at)
VALUES (:reporter_id, :chat_id, :accused_id, 'message', 'open', :now)
ON CONFLICT (chat_id, reporter_id) WHERE chat_id IS NOT NULL DO NOTHING
RETURNING uuid, target_type, status, reported_at
"""

#: AC-03 стори 8.2 (D-21): скрытие диалога. `WHERE status='active'` делает
#: операцию идемпотентной и НЕ трогает уже скрытый чат: повторная жалоба
#: отбивается раньше на `UNIQUE`, а здесь важно не сдвинуть `updated_at`
#: без нужды. `RETURNING` позволяет отличить «скрыли сейчас» от «уже скрыт».
_HIDE_CHAT_SQL = """
UPDATE chats SET status = 'hidden', updated_at = now()
 WHERE id = :chat_id AND status = 'active'
RETURNING id
"""

#: AC-06 стори 8.2 (FR38/FR39): уведомление обеим сторонам диалога.
#:
#: `INSERT ... SELECT` по двум адресатам: автор карточки и выбранный участник.
#: `UNION` дедуплицирует: автор не может быть участником (ADR-04), но
#: страховка дешевле повторной строки. `payload` собирается ОДИН раз на
#: жалобу и не содержит текста сообщений (NFR27).
_CREATE_REPORT_NOTIFICATIONS_SQL = f"""
INSERT INTO notifications (user_id, type, payload)
SELECT target.user_id, CAST(:type AS varchar), CAST(:payload AS jsonb)
  FROM (SELECT ec.author_id AS user_id
          FROM chats c JOIN event_cards ec ON ec.id = c.event_card_id
         WHERE c.id = :chat_id
        UNION
        SELECT jr.applicant_id
          FROM chats c JOIN join_requests jr ON jr.card_id = c.event_card_id
         WHERE c.id = :chat_id AND jr.status = 'selected'
       ) AS target
 WHERE {mute_gate_sql("target.user_id", "CAST(:type AS varchar)")}
"""

#: AC-06, AC-08 стори 8.5: жалобы, ПОДАННЫЕ пользователем.
#:
#: `ORDER BY reported_at DESC, uuid DESC` (D-70). Вторичный ключ обязателен:
#: `reported_at` у жалоб одной транзакции совпадает, и без `uuid` порядок
#: внутри tie-группы зависел бы от плана запроса — карточка показывала бы
#: разные «последние 20» при каждом открытии.
#:
#: Индекс `ix_reports_reporter_id_reported_at (reporter_id, reported_at DESC)`
#: создан в `0028` ровно под этот запрос.
_CARD_FILED_SQL = """
SELECT r.uuid, r.target_type, r.status, r.resolution, r.reported_at, r.resolved_at
  FROM reports AS r
 WHERE r.reporter_id = :user_id
 ORDER BY r.reported_at DESC, r.uuid DESC
 LIMIT :limit
"""

#: AC-06, AC-08 стори 8.5: жалобы, поданные НА пользователя (FR54).
#:
#: `accused_id` заполняется при подаче (AC-13) и у исторических жалоб —
#: backfill-ом `0028`. Пустое значение здесь означает ОДНО: обвиняемого
#: удалили (`ON DELETE SET NULL`, D-64), и такая жалоба в карточке не
#: показывается — её адресата больше нет.
_CARD_AGAINST_SQL = """
SELECT r.uuid, r.target_type, r.status, r.resolution, r.reported_at, r.resolved_at
  FROM reports AS r
 WHERE r.accused_id = :user_id
 ORDER BY r.reported_at DESC, r.uuid DESC
 LIMIT :limit
"""

#: AC-10 стори 8.5: ПОЛНЫЕ счётчики обеих осей ОДНИМ запросом.
#:
#: `FILTER (WHERE ...)` вместо двух отдельных `SELECT count(*)`: одно
#: обращение вместо двух держит контракт числа SQL карточки (Task 5.2) и не
#: даёт осям разойтись при правке одного из двух запросов.
_CARD_REPORT_COUNTS_SQL = """
SELECT count(*) FILTER (WHERE r.reporter_id = :user_id) AS filed,
       count(*) FILTER (WHERE r.accused_id = :user_id) AS against
  FROM reports AS r
 WHERE r.reporter_id = :user_id OR r.accused_id = :user_id
"""

#: AC-02 стори 8.2: состоит ли пользователь в диалоге — автор карточки ИЛИ
#: `selected` участник. Одним запросом, а не двумя: второй round-trip дал бы
#: разное число SQL для автора и участника, что само по себе оракул (урок F-1
#: стори 6.3). `EXISTS` не размножает строки при нескольких заявках.
_IS_CHAT_SIDE_SQL = """
SELECT EXISTS (
         SELECT 1
           FROM chats c JOIN event_cards ec ON ec.id = c.event_card_id
          WHERE c.id = :chat_id AND ec.author_id = :user_id
       )
    OR EXISTS (
         SELECT 1
           FROM chats c JOIN join_requests jr ON jr.card_id = c.event_card_id
          WHERE c.id = :chat_id AND jr.applicant_id = :user_id
            AND jr.status = 'selected'
       )
"""

#: AC-06, AC-12: пара «карточка + участник». Guard `participant_id <> :author_id`
#: переиспользует AC-10 стори 7.2 (X-7.1-7): подделанная строка
#: (`participant_id` = автор) не должна давать «пару» с самим собой.
_PAIR_SQL = """
SELECT id, status
  FROM attendance
 WHERE card_id = :card_id
   AND participant_id = :participant_id
   AND participant_id <> :author_id
"""


class ReportsRepository:
    """Создание жалобы на неявку и чтение пары автор↔участник."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_id_by_uuid(self, user_uuid: uuid_lib.UUID) -> int | None:
        """AC-03: `accused_uuid` → внутренний `id`.

        Отдельный маленький запрос, а не `JOIN` внутри `get_attendance_pair`:
        резолв нужен В ОБЕИХ ветках (у автора — чтобы найти пару, у участника —
        чтобы проверить, что жалуются на автора), и `JOIN` пришлось бы
        продублировать с разными предикатами. `None` — такого адресата нет;
        сервис превращает это в тот же `404`, что «карточки нет» (D-7.4-9).
        """
        result = await self._session.execute(
            text("SELECT id FROM users WHERE uuid = :uuid"), {"uuid": user_uuid}
        )
        return result.scalar_one_or_none()

    async def get_attendance_pair(
        self, *, card_id: int, author_id: int, participant_id: int
    ) -> tuple[int, str] | None:
        """AC-06: `(attendance.id, attendance.status)` пары или `None`.

        `None` означает «пары нет» — карточки нет в этом смысле, участия нет
        либо участник совпал с автором. Все три случая дают один `404`
        (D-7.4-9), поэтому различать их здесь нечем и не нужно.
        """
        result = await self._session.execute(
            text(_PAIR_SQL),
            {"card_id": card_id, "participant_id": participant_id, "author_id": author_id},
        )
        row = result.first()
        if row is None:
            return None
        return int(row.id), str(row.status)

    async def is_chat_side(self, *, chat_id: int, event_card_id: int, user_id: int) -> bool:
        """AC-02 стори 8.2: состоит ли `user_id` в диалоге.

        Два условия в одном запросе: автор карточки (`event_cards.author_id`)
        и `selected` участник (`join_requests`). ADR-04: у автора строки
        участия НЕТ, поэтому одним предикатом `join_requests` проверка не
        выражается.

        `event_card_id` принимается, но в SQL не используется: он приходит из
        того же чтения чата, что и `chat_id`, и нужен вызывающему для
        проверки участия ЗРИТЕЛЯ. Дублировать JOIN по нему значило бы считать
        одно и то же дважды.
        """
        result = await self._session.execute(
            text(_IS_CHAT_SIDE_SQL), {"chat_id": chat_id, "user_id": user_id}
        )
        return bool(result.scalar_one())

    async def create_message_report(
        self, *, chat_id: int, reporter_id: int, accused_id: int, now: datetime
    ) -> tuple[uuid_lib.UUID, str, str, datetime] | None:
        """AC-02, AC-05 стори 8.2: вставляет жалобу на диалог.

        **`accused_id` — параметр, а не второй запрос** (8.5 AC-13, P-8).
        `report_message` уже разрешил `accused_uuid` в `accused_id` шагом (3)
        и проверил, что это вторая сторона диалога. Повторное вычисление
        обвиняемого здесь дало бы лишний `SELECT` на каждой подаче жалобы.
        Без записи FR54 работал бы только для жалоб, попавших под backfill
        `0028`.

        `None` — жалоба уже есть (пустой `RETURNING` после `ON CONFLICT`).
        Возвращает ровно четыре значения ответа; `chat_id`/`reporter_id` в
        кортеж не входят намеренно — чего нет в результате, нельзя случайно
        сериализовать (тот же приём, что в `create_no_show_report`).
        """
        result = await self._session.execute(
            text(_CREATE_MESSAGE_REPORT_SQL),
            {
                "reporter_id": reporter_id,
                "chat_id": chat_id,
                "accused_id": accused_id,
                "now": now,
            },
        )
        row = result.first()
        if row is None:
            return None
        return (row.uuid, str(row.target_type), str(row.status), row.reported_at)

    async def hide_chat(self, *, chat_id: int) -> bool:
        """AC-03 стори 8.2: скрывает диалог. `True` — скрыт сейчас.

        Идемпотентно: `WHERE status='active'` не трогает уже скрытый чат.
        Возвращает факт перехода, а не состояние: вызывающему сервису нужен
        он для решения, публиковать ли `chat.hidden` повторно.
        """
        result = await self._session.execute(text(_HIDE_CHAT_SQL), {"chat_id": chat_id})
        return result.first() is not None

    async def create_report_notifications(self, *, chat_id: int, payload: str, type_: str) -> int:
        """AC-06 стори 8.2: уведомления обеим сторонам диалога.

        `payload` — уже сериализованная JSON-строка, собранная ОДИН раз:
        все адресаты получают побайтово одинаковое содержимое, и текст
        сообщений в него не попадает (NFR27).
        """
        result = await self._session.execute(
            text(_CREATE_REPORT_NOTIFICATIONS_SQL),
            {"chat_id": chat_id, "payload": payload, "type": type_},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def create_no_show_report(
        self,
        *,
        attendance_id: int,
        event_card_id: int,
        reporter_id: int,
        accused_id: int,
        now: datetime,
    ) -> tuple[uuid_lib.UUID, str, str, datetime] | None:
        """AC-03, AC-07: вставляет жалобу; `None` — она уже есть.

        **`accused_id` — параметр** (8.5 AC-13, P-8): `report_no_show` уже
        разрешил `accused_uuid` и проверил, что это сторона пары. Второй
        `SELECT` здесь дал бы N+1 на каждой подаче и разошёлся бы с проверкой
        при первой правке правила.

        Возвращает ровно четыре значения ответа (`AC-08`): `uuid`, `target_type`,
        `status`, `reported_at`. `reporter_id`/`attendance_id` в кортеж не
        входят намеренно — чего нет в результате, нельзя случайно
        сериализовать. Пустой `RETURNING` = конфликт по
        `uq_reports_attendance_id_reporter_id` (D-7.4-3).
        """
        result = await self._session.execute(
            text(_CREATE_SQL),
            {
                "reporter_id": reporter_id,
                "attendance_id": attendance_id,
                "event_card_id": event_card_id,
                "accused_id": accused_id,
                "now": now,
            },
        )
        row = result.first()
        if row is None:
            return None
        return (row.uuid, str(row.target_type), str(row.status), row.reported_at)

    # ——— очередь модератора (стори 8.3) ———

    async def list_queue(
        self,
        *,
        target_type: str | None,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> list[tuple[uuid_lib.UUID, str, str, datetime, int | None, int | None, int | None]]:
        """AC-01, AC-04: страница очереди открытых жалоб по возрасту.

        Порядок — `reported_at ASC, uuid ASC`, предикат — КОРТЕЖНОЕ сравнение,
        а не две `OR`-ветви: у `OR`-варианта легко ошибиться в приоритетах и
        потерять строки tie-группы (`now()` в одной транзакции совпадает).
        Тот же приём, что в `build_history_statement` сообщений.

        Sentinel (D-9) здесь не нужен: второй ключ `uuid` уникален, поэтому
        потерять строку из tie-группы невозможно.
        """
        keyset = ""
        params: dict[str, Any] = {"target_type": target_type, "limit": limit}
        if after is not None:
            keyset = "   AND (r.reported_at, r.uuid) > (:after_at, :after_uuid)"
            params["after_at"], params["after_uuid"] = after
        result = await self._session.execute(text(_QUEUE_SQL.format(keyset=keyset)), params)
        return [
            (
                row.uuid,
                str(row.target_type),
                str(row.status),
                row.reported_at,
                int(row.chat_id) if row.chat_id is not None else None,
                int(row.event_card_id) if row.event_card_id is not None else None,
                int(row.attendance_id) if row.attendance_id is not None else None,
            )
            for row in result.all()
        ]

    async def count_queue(self, *, target_type: str | None) -> int:
        """AC-04: `total` под фильтром — «сколько всего», не «сколько на странице»."""
        result = await self._session.execute(text(_COUNT_QUEUE_SQL), {"target_type": target_type})
        return int(result.scalar_one())

    async def queue_attendance_facts(
        self, *, attendance_ids: list[int]
    ) -> dict[int, tuple[str, bool, bool, uuid_lib.UUID]]:
        """AC-01, FR112: факты явки для страницы ОДНИМ запросом.

        Запрос по одной жалобе дал бы число SQL, зависящее от `limit`
        (мутация M7, ось NFR39): страница до 50 строк стоила бы 50 round-trip.

        Возвращает `{attendance_id: (status, author_confirmed,
        participant_confirmed, card_uuid)}` — карточка нужна модератору,
        чтобы открыть контекст встречи.
        """
        if not attendance_ids:
            return {}
        result = await self._session.execute(text(_QUEUE_FACTS_SQL), {"ids": attendance_ids})
        return {
            int(row.id): (
                str(row.status),
                bool(row.author_confirmed),
                bool(row.participant_confirmed),
                row.card_uuid,
            )
            for row in result.all()
        }

    # ——— карточка пользователя (стори 8.5) ———

    async def card_filed(
        self, *, user_id: int, limit: int
    ) -> list[tuple[uuid_lib.UUID, str, str, str | None, datetime, datetime | None]]:
        """AC-06, AC-08 (8.5): жалобы, ПОДАННЫЕ пользователем.

        Порядок `reported_at DESC, uuid DESC` (D-70): вторичный ключ `uuid`
        делает порядок детерминированным внутри tie-группы. Пагинации нет,
        поэтому sentinel `2^63-1` из D-9 не нужен — `LIMIT` режет список
        целиком, потерь строк нет.

        `uuid` в кортеже — публичный адрес жалобы; внутренний `id` наружу не
        выходит (D-2).
        """
        result = await self._session.execute(
            text(_CARD_FILED_SQL), {"user_id": user_id, "limit": limit}
        )
        return [
            (r[0], str(r[1]), str(r[2]), None if r[3] is None else str(r[3]), r[4], r[5])
            for r in result.all()
        ]

    async def card_against(
        self, *, user_id: int, limit: int
    ) -> list[tuple[uuid_lib.UUID, str, str, str | None, datetime, datetime | None]]:
        """AC-06, AC-08 (8.5): жалобы, поданные НА пользователя.

        Работает по `accused_id` (D-64). До 8.5 обвиняемый вычислялся
        `_ACCUSED_SQL`-ом из 8.4 — четырёхветвевым `UNION` с JOIN-ами по трём
        таблицам; для одного решения это было приемлемо, для карточки,
        открываемой на каждую жалобу, — нет.
        """
        result = await self._session.execute(
            text(_CARD_AGAINST_SQL), {"user_id": user_id, "limit": limit}
        )
        return [
            (r[0], str(r[1]), str(r[2]), None if r[3] is None else str(r[3]), r[4], r[5])
            for r in result.all()
        ]

    async def card_counts(self, *, user_id: int) -> tuple[int, int]:
        """AC-10 (8.5): ПОЛНЫЕ числа поданных и предъявленных жалоб.

        ОДИН запрос, а не два: условное агрегирование даёт обе оси за одно
        обращение. Два запроса увеличили бы базовое число SQL карточки и
        разошлись бы с контрактом «9 SQL» (Task 5.2), а ось NFR39 в 8.5 —
        именно равенство числа обращений на двух ветвях.
        """
        result = await self._session.execute(text(_CARD_REPORT_COUNTS_SQL), {"user_id": user_id})
        row = result.one()
        return (int(row[0]), int(row[1]))
