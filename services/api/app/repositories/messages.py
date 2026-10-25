"""Репозиторий сообщений чата (стори 6.4; AC-01, AC-02, AC-04).

`body` здесь — УЖЕ шифртекст: репозиторий не знает про ключи (D-6.4-4).
Шифрование живёт в сервисе, потому что «забыть зашифровать» на новом пути
записи должно быть невозможно, а репозиторий не должен зависеть от настроек.

**Идемпотентность — `ON CONFLICT (chat_id, client_message_id) DO NOTHING`**
(D-6.4-2, NFR18). `SELECT` → `INSERT` проигрывает гонку двум параллельным
ретраям после обрыва сети: оба видят «нет», оба вставляют, второй падает
`IntegrityError` → `500` на легальном действии (урок D-6.2-2). Конфликт
идёт по ПАРЕ «чат + клиентский идентификатор»: один и тот же UUID,
сгенерированный клиентом для разных чатов, — разные сообщения.

**История — keyset, а не `OFFSET`** (AC-02). `OFFSET` на растущей переписке
сдвигает границы при вставке нового сообщения между страницами, и клиент
теряет или дублирует строки. Кортежное `(created_at, id) > (:a, :b)` — ровно
порядок `ix_messages_chat_created`, и `id` служит tie-breaker'ом для
сообщений с одинаковым `created_at` (в одной транзакции `now()` совпадает).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.user import User


def _as_uuid(value: Any) -> uuid_lib.UUID:
    """Приводит значение из БД к `UUID` (драйвер может вернуть `str`)."""
    return value if isinstance(value, uuid_lib.UUID) else uuid_lib.UUID(str(value))


class MessageRepository:
    """Запросы к `messages`: идемпотентная вставка и страница истории."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ——— билдеры (проверяются без БД, §18.2) ———

    @staticmethod
    def build_insert_statement(
        *,
        chat_id: int,
        sender_id: int | None,
        body: bytes,
        message_uuid: uuid_lib.UUID,
        client_message_id: uuid_lib.UUID | None,
        is_system: bool = False,
    ) -> Any:
        """AC-01, AC-04: вставка сообщения с идемпотентностью по клиенту.

        `body` — шифртекст (`nonce ‖ ct ‖ tag`), не текст: колонки с открытым
        текстом нет ни в схеме, ни здесь (NFR31).

        `RETURNING uuid, created_at, id` — минимальный набор: `uuid` нужен для
        ответа, `created_at` для порядка, `id` — внутренний tie-breaker и
        наружу не выходит (D-2). `RETURNING *` тянул бы `body` (шифртекст)
        обратно в память на каждом `POST`.
        """
        # Стори 6.7 (AC-04, D-6.7-1): признак системности пишется ЯВНО. Без
        # него у системной строки остался бы `server_default = false`, и
        # `CHECK`-инвариант «is_system ⟺ sender_id IS NULL» не сработал бы:
        # сообщение без автора стало бы «обычным» и сломало бы `own`-логику.
        statement = pg_insert(Message).values(
            chat_id=chat_id,
            sender_id=sender_id,
            body=body,
            uuid=message_uuid,
            client_message_id=client_message_id,
            is_system=is_system,
        )
        # AC-04: при `client_message_id = NULL` (системные сообщения 6.7)
        # конфликта быть не может — частичный индекс не срабатывает.
        #
        # CR 6.4 (собственный дефект, найден исполнением): уникальность
        # объявлена ЧАСТИЧНЫМ индексом (`WHERE client_message_id IS NOT NULL`),
        # и PostgreSQL отказывается сопоставлять его без того же предиката:
        # `InvalidColumnReferenceError: there is no unique or exclusion
        # constraint matching the ON CONFLICT specification`. `index_where`
        # обязателен и должен совпадать с предикатом индекса дословно.
        if client_message_id is not None:
            statement = statement.on_conflict_do_nothing(
                index_elements=["chat_id", "client_message_id"],
                index_where=text("client_message_id IS NOT NULL"),
            )
        return statement.returning(Message.uuid, Message.created_at, Message.id)

    @staticmethod
    def build_history_statement(
        *, chat_id: int, after: tuple[datetime, uuid_lib.UUID] | None, limit: int
    ) -> Select[Any]:
        """AC-02: страница истории — фильтр по чату + keyset-курсор.

        `ORDER BY created_at ASC, uuid ASC` — ключ `ix_messages_chat_created_uuid`.
        Второй ключ — `uuid`, а НЕ внутренний `id` (D-2): курсор уходит клиенту
        в query-параметре, и `bigint` в нём стал бы публичным адресом,
        разрешающим перебор `/1..N` (тот же довод, что в `card_feed_cursor`).

        Предикат — КОРТЕЖНОЕ сравнение `(created_at, uuid) > (:a, :b)`, а не две
        `OR`-ветви: у `OR`-варианта легко ошибиться в приоритетах и потерять
        сообщения с одинаковым `created_at` (в одной транзакции `now()`
        совпадает у всех строк).
        """
        # `sender_uuid` — JOIN, а не второй запрос на сообщение: страница до
        # 100 строк дала бы 100 дополнительных `SELECT` (N+1). Внутренний
        # `sender_id` наружу не выходит (D-2) — только `uuid`.
        statement = (
            select(
                Message.uuid,
                Message.sender_id,
                User.uuid.label("sender_uuid"),
                Message.body,
                Message.created_at,
            )
            # Стори 6.7 (Task 3.1): `OUTER` join, иначе системное сообщение
            # (`sender_id IS NULL`) ИСЧЕЗАЕТ из истории: `INNER JOIN` не
            # сопоставляет NULL, и строка молча выпадает из страницы. Это
            # не ошибка, которую видно в логах, — это тихая потеря факта
            # «участник отменил участие», ради которого стори и делалась.
            .outerjoin(User, User.id == Message.sender_id)
            .where(Message.chat_id == chat_id)
        )
        if after is not None:
            statement = statement.where(tuple_(Message.created_at, Message.uuid) > after)
        return cast(
            "Select[Any]",
            statement.order_by(Message.created_at.asc(), Message.uuid.asc()).limit(limit),
        )

    @staticmethod
    def build_fragment_statement(*, chat_id: int, limit: int) -> Any:
        """AC-02 стори 8.3: последние `limit` сообщений диалога для модератора.

        `ORDER BY created_at DESC, uuid DESC LIMIT n` — и переворот ВЫЗЫВАЮЩИМ.
        `ASC LIMIT n` вернул бы ПЕРВЫЕ сообщения диалога, а модератору нужен
        конец разговора, из-за которого подана жалоба; переворот после `DESC`
        даёт «от старых к новым» без второго запроса.

        `sender_uuid` — `OUTER` join (как в истории): иначе системное сообщение
        (`sender_id IS NULL`) молча выпадает из фрагмента, и модератор не
        увидит, что участник отменил участие.

        Ключ — существующий `ix_messages_chat_created_uuid (chat_id,
        created_at, uuid)`: направление сканирования PostgreSQL выбирает сам,
        новый индекс не нужен (D-33).
        """
        statement = (
            select(
                Message.uuid,
                Message.sender_id,
                User.uuid.label("sender_uuid"),
                Message.body,
                Message.created_at,
            )
            .outerjoin(User, User.id == Message.sender_id)
            .where(Message.chat_id == chat_id)
        )
        return cast(
            "Select[Any]",
            statement.order_by(Message.created_at.desc(), Message.uuid.desc()).limit(limit),
        )

    @staticmethod
    def build_get_by_client_id_statement(
        *, chat_id: int, client_message_id: uuid_lib.UUID
    ) -> Select[Any]:
        """AC-04: дочитывание существующего сообщения после конфликта.

        `ON CONFLICT DO NOTHING` не возвращает строку, а клиенту нужен `201`
        с ТЕМ ЖЕ `uuid` — иначе повтор после обрыва выглядел бы как потеря.
        """
        return cast(
            "Select[Any]",
            select(Message.uuid, Message.created_at, Message.body, Message.sender_id).where(
                Message.chat_id == chat_id,
                Message.client_message_id == client_message_id,
            ),
        )

    # ——— операции ———

    async def insert_if_absent(
        self,
        *,
        chat_id: int,
        sender_id: int | None,
        body: bytes,
        message_uuid: uuid_lib.UUID,
        client_message_id: uuid_lib.UUID | None,
        is_system: bool = False,
    ) -> tuple[uuid_lib.UUID, datetime, int] | None:
        """AC-01, AC-04: вставляет сообщение, если такого клиентского id нет.

        `None` — сообщение уже было (повтор после обрыва): это НЕ ошибка,
        вызывающий сервис дочитывает существующее по `client_message_id`.
        """
        statement = self.build_insert_statement(
            chat_id=chat_id,
            sender_id=sender_id,
            body=body,
            message_uuid=message_uuid,
            client_message_id=client_message_id,
            is_system=is_system,
        )
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(statement.execution_options(synchronize_session=False)),
        )
        row = result.first()
        if row is None:
            return None
        values = tuple(row)
        return _as_uuid(values[0]), values[1], int(values[2])

    async def get_by_client_id(
        self, *, chat_id: int, client_message_id: uuid_lib.UUID
    ) -> tuple[uuid_lib.UUID, datetime, bytes, int] | None:
        """AC-04: `uuid`, `created_at`, ШИФРТЕКСТ и автор уже записанного сообщения.

        `body` возвращается шифртекстом (расшифровка — в сервисе, D-6.4-4):
        без него реплей отдавал бы текст НОВОГО запроса под старым `uuid`
        (CR 6.4, находка F-1). `sender_id` нужен как часть AAD.
        """
        result = await self._session.execute(
            self.build_get_by_client_id_statement(
                chat_id=chat_id, client_message_id=client_message_id
            )
        )
        row = result.first()
        if row is None:
            return None
        return _as_uuid(row[0]), row[1], bytes(row[2]), int(row[3])

    async def list_history(
        self, *, chat_id: int, after: tuple[datetime, uuid_lib.UUID] | None, limit: int
    ) -> list[tuple[uuid_lib.UUID, int | None, uuid_lib.UUID | None, bytes, datetime]]:
        """AC-02: страница истории БЕЗ расшифровки — это делает сервис."""
        result = await self._session.execute(
            self.build_history_statement(chat_id=chat_id, after=after, limit=limit)
        )
        # Стори 6.7 (AC-04): `sender_id`/`sender_uuid` бывают `NULL` у
        # системных сообщений. Прежний код делал `int(None)` — и страница
        # истории падала бы `TypeError` → `500` ровно на том чате, ради
        # которого стори делалась (поймано интеграционным тестом).
        return [
            (
                _as_uuid(row[0]),
                None if row[1] is None else int(row[1]),
                None if row[2] is None else _as_uuid(row[2]),
                bytes(row[3]),
                row[4],
            )
            for row in result.all()
        ]

    @staticmethod
    def build_mark_delivered_statement(*, chat_id: int, message_uuid: uuid_lib.UUID) -> Any:
        """Стори 6.5 (Task 6.2, AC-06): CAS-отметка доставки.

        `WHERE delivered_at IS NULL` — не украшение, а условие идемпотентности:
        повторная доставка (реконнект, дубль из Redis) не должна переписывать
        время первого факта. Без предиката второй `UPDATE` вернул бы 1 строку,
        и «доставлено один раз» стало бы непроверяемым (§18.11: признак нужен
        в обоих значениях — 1 строка и 0 строк).

        `chat_id` в предикате обязателен: `message_uuid` глобально уникален,
        но условие по чату не даёт отметить сообщение чужого чата, если когда-то
        появится второй путь вызова с неверным `uuid`.
        """
        return (
            update(Message)
            .where(
                Message.uuid == message_uuid,
                Message.chat_id == chat_id,
                Message.delivered_at.is_(None),
            )
            .values(delivered_at=func.now())
        )

    @staticmethod
    def build_unread_predicate(*, viewer_id: int) -> Any:
        """Стори 6.7 (D-6.7-2, AC-04): «непрочитано МНОЙ» — ОДНО определение.

        **Системные сообщения считаются непрочитанными.** `read_at` означает
        «человек открыл чат и увидел», а не «ему написал человек»: системное
        сообщение меняет состав компании, и участник обязан это заметить
        (UX §9: факт, не оценка). Без `is_(None)`-ветви предикат `sender_id
        != :viewer` на `NULL` даёт `NULL` → ложь, и такое сообщение осталось
        бы непрочитанным НАВСЕГДА.

        Один билдер на счётчик списка и на `mark_read` намеренно: разъехавшиеся
        предикаты дали бы вечный «1 непрочитанное», которое нечем обнулить
        (проверяется парой `test_chat_list_counts_system_message_as_unread`
        и `test_mark_read_closes_system_message`: счётчик 1 → `mark_read` → 0).

        Собственные сообщения по-прежнему не считаются (AC-06 стори 6.6):
        иначе у автора после каждой отправки висело бы «1 непрочитанное».
        """
        return or_(Message.sender_id.is_(None), Message.sender_id != viewer_id)

    @staticmethod
    def build_mark_read_statement(*, chat_id: int, viewer_id: int) -> Any:
        """Стори 6.6 (Task 2.1, AC-02, AC-06): отметка прочтения чата.

        Три предиката, каждый — не украшение:

        - `chat_id = :id` — отметка не выходит за пределы ОДНОГО чата
          (второй рубеж на случай нового пути вызова с неверным `id`);
        - `read_at IS NULL` — условие ИДЕМПОТЕНТНОСТИ: повторное открытие
          не переписывает время ПЕРВОГО прочтения, и «прочитано» остаётся
          неизменным фактом (иначе счётчик `unread_count` зависел бы от
          последнего открытия, а не от факта прочтения);
        - `build_unread_predicate(viewer)` — СВОИ сообщения не читаются
          «глазами другого» (иначе у автора после каждой отправки висело бы
          «1 непрочитанное»), а системные (`sender_id IS NULL`) читаются:
          см. D-6.7-2. Тот же билдер использует счётчик списка чатов, поэтому
          «прочитано» и «непрочитано» не могут разойтись.

        Возвращается `rowcount` (Task 2.2): 1+ строк — «было что читать»,
        0 — «всё уже прочитано». Без этого числа идемпотентность
        проверялась бы только текстом запроса (§18.11: оба значения).
        """
        return (
            update(Message)
            .where(
                Message.chat_id == chat_id,
                Message.read_at.is_(None),
                MessageRepository.build_unread_predicate(viewer_id=viewer_id),
            )
            .values(read_at=func.now())
        )

    async def mark_read(self, *, chat_id: int, viewer_id: int) -> int:
        """Стори 6.6 (AC-02): проставляет `read_at` непрочитанным.

        Возвращает `rowcount`: число сообщений, прочитанных ПЕРВЫЙ раз.
        `0` — нормальный исход повторного открытия, а не ошибка.
        """
        result = await self._session.execute(
            self.build_mark_read_statement(chat_id=chat_id, viewer_id=viewer_id)
        )
        # `CursorResult`, а не `Result`: `rowcount` есть только у курсора
        # UPDATE (тот же довод, что в `mark_delivered`).
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def mark_delivered(self, *, chat_id: int, message_uuid: uuid_lib.UUID) -> int:
        """Отмечает доставку; возвращает `rowcount` (1 — первый раз, 0 — повтор).

        Возвращаемое число — единственный способ отличить «доставили сейчас»
        от «уже было» в тесте и в будущей квитанции `message.delivered` (6.6).
        """
        result = await self._session.execute(
            self.build_mark_delivered_statement(chat_id=chat_id, message_uuid=message_uuid)
        )
        # `CursorResult`, а не `Result`: `rowcount` есть только у курсора
        # UPDATE. Приведение явное — mypy со `strict` иначе падает
        # `"Result" has no attribute "rowcount"`, и проверка типов перестаёт
        # что-либо значить для этого метода (проверено прогоном).
        return int(cast(CursorResult[Any], result).rowcount or 0)
