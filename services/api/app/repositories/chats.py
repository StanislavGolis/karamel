"""Репозиторий чатов компании (стори 6.2; AC-01, AC-02, AC-04).

Схема `chats` создана в 6.1 (`0014`): `UNIQUE(event_card_id)` — инвариант
«один чат на карточку», `UNIQUE(uuid)` — публичный идентификатор, колонок
участников нет (ADR-04: компания вычисляется, а не хранится).

**Почему `ON CONFLICT DO NOTHING`, а не `SELECT` → `INSERT`** (D-6.2-2).
Проверка-перед-вставкой проигрывает гонку двум параллельным транзакциям:
обе видят «чата нет», обе вставляют, вторая падает `IntegrityError` → `500`
пользователю на легальном действии. `ON CONFLICT` снимает вопрос на уровне
БД: конфликт по `event_card_id` — не ошибка, а признак «чат уже открыт».

**Почему конфликт именно по `event_card_id`, а не по `uuid`.** Инвариант
домена — «один чат на карточку»; `uuid` уникален лишь как публичный ключ,
и конфликт по нему означал бы коллизию `uuid4` (сигнал сломанного
генератора, а не повторную сборку).

**Контракт возврата** (TD 6.2, п.1): `tuple[uuid, id]` при вставке и `None`
при конфликте. `None` — НЕ ошибка: сервис продолжает работу, потому что
чат уже существует и его `uuid` уже записан в `event_cards.chat_uuid`
(контракт 5б.2, D-5b.2-1).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any, cast

import structlog
from sqlalchemy import Select, exists, func, or_, select, text, true, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Chat
from app.models.enums import JoinRequestStatus
from app.models.event_card import EventCard
from app.models.join_request import JoinRequest
from app.models.message import Message
from app.models.profile import Profile
from app.models.user import User
from app.repositories.messages import MessageRepository

logger = structlog.get_logger(__name__)


def _as_uuid(value: Any) -> uuid_lib.UUID:
    """Приводит значение из БД к `UUID` (драйвер может вернуть `str`)."""
    return value if isinstance(value, uuid_lib.UUID) else uuid_lib.UUID(str(value))


class ChatRepository:
    """Запросы к `chats`: идемпотентное открытие и чтение."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ——— билдеры (проверяются без БД, §18.2) ———

    @staticmethod
    def build_ensure_statement(*, chat_uuid: uuid_lib.UUID, event_card_id: int) -> Any:
        """AC-04, D-6.2-2: вставка чата с идемпотентностью по карточке.

        `values` содержит РОВНО два поля: `uuid` (пришёл из
        `event_cards.chat_uuid`, не генерируется здесь) и `event_card_id`.
        Никаких полей участников — их в схеме нет и не должно быть (AC-08,
        ADR-04).

        `RETURNING uuid, id` — минимальный набор: `uuid` нужен для контракта
        ответа, `id` — внутренний ключ для последующих операций (D-2: наружу
        не выходит). `RETURNING *` тянул бы лишние колонки в горячем пути.
        """
        return (
            pg_insert(Chat)
            .values(uuid=chat_uuid, event_card_id=event_card_id)
            .on_conflict_do_nothing(index_elements=["event_card_id"])
            .returning(Chat.uuid, Chat.id)
        )

    @staticmethod
    def build_get_by_card_statement(*, event_card_id: int) -> Any:
        """Чтение `uuid` чата по карточке (диагностика и тесты)."""
        return select(Chat.uuid).where(Chat.event_card_id == event_card_id)

    # ——— состав компании (стори 6.3; AC-02, AC-03, AC-04) ———

    @staticmethod
    def build_participants_statement(*, card_id: int, city_slug: str) -> Any:
        """AC-01, AC-03 (ADR-04): состав компании — одним запросом.

        Состав НЕ хранится: он вычисляется из `join_requests`
        (`status='selected'`). Денормализованный список разошёлся бы с
        заявками при отмене участия (5а.6) и при замене выбранного (5б.3) —
        поэтому колонок участников нет ни в `chats`, ни здесь.

        JOIN'ы: `users` — INNER (FK `NOT NULL`), `profiles` — LEFT (анкеты
        может не быть: онбординг не пройден; участник при этом остаётся в
        составе — он часть компании, а не «профиль»).

        Срез сообщества — ПОДЗАПРОСОМ: второй рубеж ADR-13 на случай ошибки
        сервиса, ровно как в `build_card_requests_page_statement`. Без него
        состав чужой карточки читался бы по прямому `uuid`.

        `ORDER BY submitted_at ASC, id ASC` — тот же ключ, что очередь
        заявок и автовыбор (ADR-05): без tie-breaker `id` порядок при равных
        `submitted_at` был бы недетерминирован, и два запроса давали бы
        разные списки.

        Автора в выдаче НЕТ: строки `join_requests` у него не существует.
        Он добавляется сервисом ПЕРВЫМ из уже прочитанной карточки
        (D-6.3-3) — второй запрос за автором не нужен.
        """
        return cast(
            "Select[Any]",
            select(
                User.id.label("participant_user_id"),
                User.uuid.label("participant_uuid"),
                Profile.name.label("participant_name"),
                Profile.age.label("participant_age"),
                JoinRequest.submitted_at.label("participant_submitted_at"),
                # Стори 12.2, AC-3: возраст вычисляется из `birth_date`.
                # Колонка добавлена В КОНЕЦ, чтобы не сдвигать индексы
                # существующих потребителей (0..4 — контракт сервиса).
                Profile.birth_date.label("participant_birth_date"),
            )
            .join(User, User.id == JoinRequest.applicant_id)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.status == JoinRequestStatus.SELECTED,
                JoinRequest.card_id.in_(
                    select(EventCard.id).where(EventCard.city_slug == city_slug)
                ),
            )
            .order_by(JoinRequest.submitted_at.asc(), JoinRequest.id.asc()),
        )

    # ——— стори 6.6: список чатов зрителя (AC-01, AC-03, AC-04, AC-05) ———

    @staticmethod
    def _visible_chat_status_predicate() -> Any:
        """FR37: единственное место фильтрации скрытых чатов.

        Стори 8.2 (D-22): диалог скрыт жалобой — `status='hidden'` — и не
        показывается НИ ОДНОЙ стороне. Скрытие принадлежит чату, не участнику
        (D-21), поэтому предикат один на обе стороны.

        **Почему фильтр здесь, а не на клиенте** (project-context §5, §12):
        клиентская фильтрация оставила бы данные в ответе API, DevTools, кэше
        и логах прокси. Место было выделено заглушкой `true()` в 6.6 и покрыто
        тестом в ОБОИХ значениях (§18.11): подмена на `false()` обязана
        опустошить список, подмена на `true()` — вернуть скрытый чат.

        **Почему не `status != 'hidden'`.** Домен закрыт `CHECK`-ом из двух
        значений, но `!=` пропустил бы третье значение, если его добавят
        миграцией без правки кода. Явное равенство падает заметно: новый
        статус просто не виден, и это ловит первый же тест списка.
        """
        return Chat.status == "active"

    @staticmethod
    def build_list_for_viewer_statement(
        *, viewer_id: int, after: tuple[Any, uuid_lib.UUID] | None, limit: int
    ) -> Select[Any]:
        """AC-01, AC-04, AC-05: страница чатов зрителя — ОДНИМ запросом.

        **Почему `LATERAL`, а не «список + два запроса на чат»** (NFR12,
        AC-05). Наивная реализация — `1 + 2N` запросов: на каждый чат
        последнее сообщение и `count(unread)`. При 50 чатах это 101
        round-trip, и «открытие ≤1 с» перестаёт выполняться на первом же
        медленном канале. Два `LATERAL`-подзапроса дают РОВНО ОДИН запрос
        независимо от числа чатов — это проверяется тестом на счётчике
        `before_cursor_execute` (1 чат и 5 чатов: число равно).

        **Участие — тот же предикат, что гард 6.4** (второго не заводить):
        автор карточки ЛИБО `join_requests.status='selected'`. `EXISTS`, а не
        `JOIN`: заявка одна, но `JOIN` размножил бы строки при будущих
        статусах, и `total` разошёлся бы с числом элементов.

        **`total` НЕ выводится из страницы:** запрос возвращает `count(*)`
        окном (`COUNT(*) OVER ()`), потому что отдельный `COUNT` был бы
        третьим round-trip и считался бы по тем же предикатам второй раз.

        **Курсор — `(created_at, uuid)` ПАРЫ**, как в истории сообщений
        (D-2: `bigint` в query-параметре стал бы публичным адресом).
        Сортировка `COALESCE(last.created_at, c.created_at) DESC, c.uuid
        DESC`: чат без сообщений (только что открыт, AC-04) не проваливается
        в конец по `NULL`, а `uuid` служит tie-breaker'ом — без него порядок
        при равных временах недетерминирован, и страницы теряли бы строки.
        """
        # CR 6.6 (F-1): предикат участия вынесен в переменную, чтобы список и
        # подсчёт ВСЕГО использовали ОДНО определение (второго не заводим).
        participation = or_(
            EventCard.author_id == viewer_id,
            exists().where(
                JoinRequest.card_id == EventCard.id,
                JoinRequest.applicant_id == viewer_id,
                JoinRequest.status == JoinRequestStatus.SELECTED,
            ),
        )
        # CR 6.6 (F-1): `total` — ВСЕ чаты зрителя, а не строки текущей страницы.
        # `COUNT(*) OVER ()` вычислялся ПОСЛЕ фильтра курсора и на второй
        # странице давал усечённое значение (замер: 3, 2, 1 вместо 3, 3, 3).
        # Скалярный подзапрос с теми же предикатами (без курсора) остаётся
        # ОДНИМ SQL-оператором (AC-05: число запросов не растёт).
        # `correlate(None)` обязателен: без него SQLAlchemy убрал бы `chats`
        # из подзапроса как коррелированный с внешним запросом, и `count`
        # считал бы не то (или падал на неоднозначности).
        total_count = (
            select(func.count())
            .select_from(Chat)
            .join(EventCard, EventCard.id == Chat.event_card_id)
            .where(
                participation,
                ChatRepository._visible_chat_status_predicate(),
            )
            .correlate(None)
            .scalar_subquery()
            .label("total_count")
        )
        last_message = (
            select(
                Message.uuid.label("last_message_uuid"),
                Message.sender_id.label("last_message_sender_id"),
                User.uuid.label("last_message_sender_uuid"),
                Message.body.label("last_message_body"),
                Message.created_at.label("last_message_created_at"),
            )
            # Стори 6.7 (Task 3.2): `OUTER` join по той же причине, что в
            # истории сообщений: у системного сообщения `sender_id IS NULL`,
            # и `INNER JOIN` сделал бы `last_message = NULL` у чата, где
            # последним было системное — строка списка показала бы «сообщений
            # нет» при непустой переписке.
            .outerjoin(User, User.id == Message.sender_id)
            .where(Message.chat_id == Chat.id)
            .order_by(Message.created_at.desc(), Message.uuid.desc())
            .limit(1)
            .lateral("last_message")
        )
        # Стори 6.7 (Task 3.3, D-6.7-2): предикат «непрочитано мной» берётся
        # из ОДНОГО билдера репозитория сообщений. Он же используется в
        # `mark_read`; разъехавшиеся копии дали бы вечный счётчик, который
        # нечем обнулить. Системные (`sender_id IS NULL`) считаются
        # непрочитанными: состав компании изменился — это факт для участника.
        unread = (
            select(func.count().label("unread_count"))
            .where(
                Message.chat_id == Chat.id,
                Message.read_at.is_(None),
                MessageRepository.build_unread_predicate(viewer_id=viewer_id),
            )
            .lateral("unread")
        )
        statement = (
            select(
                Chat.id.label("chat_id"),
                Chat.uuid.label("chat_uuid"),
                EventCard.uuid.label("card_uuid"),
                EventCard.title.label("card_title"),
                last_message.c.last_message_uuid,
                last_message.c.last_message_sender_id,
                last_message.c.last_message_sender_uuid,
                last_message.c.last_message_body,
                last_message.c.last_message_created_at,
                unread.c.unread_count,
                func.coalesce(last_message.c.last_message_created_at, Chat.created_at).label(
                    "last_activity_at"
                ),
                total_count,
            )
            .join(EventCard, EventCard.id == Chat.event_card_id)
            .outerjoin(last_message, true())
            .outerjoin(unread, true())
            .where(
                or_(
                    EventCard.author_id == viewer_id,
                    exists().where(
                        JoinRequest.card_id == EventCard.id,
                        JoinRequest.applicant_id == viewer_id,
                        JoinRequest.status == JoinRequestStatus.SELECTED,
                    ),
                ),
                # AC-03, FR37-EXTENSION-POINT: фильтр скрытых чатов живёт
                # ЗДЕСЬ, а не на клиенте (project-context §5). Сегодня тело
                # функции — `true()`; эпик 8 заменит его одной строкой.
                ChatRepository._visible_chat_status_predicate(),
            )
        )
        if after is not None:
            after_at, after_uuid = after
            statement = statement.where(
                tuple_(
                    func.coalesce(last_message.c.last_message_created_at, Chat.created_at),
                    Chat.uuid,
                )
                < tuple_(after_at, after_uuid)
            )
        return cast(
            "Select[Any]",
            statement.order_by(
                func.coalesce(last_message.c.last_message_created_at, Chat.created_at).desc(),
                Chat.uuid.desc(),
            ).limit(limit),
        )

    async def list_for_viewer(
        self, *, viewer_id: int, after: tuple[Any, uuid_lib.UUID] | None, limit: int
    ) -> list[tuple[Any, ...]]:
        """AC-01: страница списка чатов (расшифровка тел — в сервисе).

        Возвращает кортежи `(chat_uuid, card_uuid, card_title,
        last_message_uuid, last_message_sender_uuid, last_message_ciphertext,
        last_message_created_at, unread_count, last_activity_at, total)`.
        Тело последнего сообщения приходит ШИФРТЕКСТОМ: репозиторий не знает
        ключей (D-6.4-4), расшифровка — обязанность сервиса.
        """
        result = await self._session.execute(
            self.build_list_for_viewer_statement(viewer_id=viewer_id, after=after, limit=limit)
        )
        return [tuple(row) for row in result.all()]

    # ——— операции ———

    async def ensure_for_card(
        self, *, chat_uuid: uuid_lib.UUID, event_card_id: int
    ) -> tuple[uuid_lib.UUID, int] | None:
        """AC-04: открывает чат идемпотентно.

        Возвращает `(uuid, id)` при вставке и `None`, если чат уже был.
        `None` — нормальный исход повторной сборки, а НЕ ошибка: вызывающий
        сервис в этом случае ничего не делает (чат уже существует, и его
        `uuid` уже лежит в `event_cards.chat_uuid`).
        """
        statement = self.build_ensure_statement(chat_uuid=chat_uuid, event_card_id=event_card_id)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(statement.execution_options(synchronize_session=False)),
        )
        row = result.first()
        if row is None:
            # Конфликт: чат уже открыт (повторный `select`, второй путь сборки,
            # ретрай). Не ошибка — AC-04.
            #
            # CR 6.2 (blind #10, edge #2): `None` сам по себе НЕ говорит, что
            # существующий чат имеет тот же `uuid`. Если в `chats` лежит другой
            # `uuid`, контракт 6.1 `chats.uuid = event_cards.chat_uuid` уже
            # нарушен, и молчание делало бы этот рассинхрон ненаблюдаемым.
            # Сверка и `logger.error` оставляют исход идемпотентным (AC-04),
            # но делают расхождение видимым в журнале.
            stored = await self.get_by_event_card_id(event_card_id=event_card_id)
            if stored != chat_uuid:
                logger.error(
                    "chat.ensure.mismatch",
                    card_id=event_card_id,
                    stored_uuid=str(stored) if stored is not None else None,
                )
            return None
        # `tuple(row)` — приведение `Row` к кортежу: индексация `Row` не видна
        # mypy (`Tuple index out of range`), а распаковка кортежа видна.
        values = tuple(row)
        return _as_uuid(values[0]), int(values[1])

    async def list_participants(
        self, *, card_id: int, city_slug: str
    ) -> list[tuple[int, uuid_lib.UUID, str | None, int | None, Any, Any]]:
        """AC-01, AC-03: участники компании (`selected`) в порядке присоединения.

        Возвращает кортежи `(user_id, user_uuid, name, age, submitted_at, birth_date)`:
        `user_id` нужен только для пакетной подписи фото и наружу не выходит
        (D-2). `name`/`age`/`birth_date` — из LEFT JOIN `profiles`: анкеты
        может не быть. `birth_date` — последний элемент осознанно: возраст
        вычисляет сервис (`compute_age`), а не SQL, иначе день рождения
        зависел бы от часового пояса БД и от `now()` внутри запроса.

        Автора здесь нет — его добавляет сервис (D-6.3-3).
        """
        result = await self._session.execute(
            self.build_participants_statement(card_id=card_id, city_slug=city_slug)
        )
        return [
            (int(row[0]), _as_uuid(row[1]), row[2], row[3], row[4], row[5]) for row in result.all()
        ]

    @staticmethod
    def build_get_by_uuid_statement(*, chat_uuid: uuid_lib.UUID) -> Select[Any]:
        """Стори 6.4 (AC-03) + 8.2 (AC-04): чат по `uuid`, только ВИДИМЫЙ.

        `event_card_id` нужен сразу: участие в чате определяется составом
        компании на карточке (ADR-04), и второй запрос за карточкой был бы
        лишним round-trip на самом частом экране.

        AC-04 стори 8.2: скрытый жалобой чат для прямого запроса НЕ существует
        (`404`, не `403` — существование диалога не раскрывается, ADR-13).
        Фильтр добавлен в тот же `WHERE`, поэтому число запросов не меняется.
        """
        return ChatRepository._select_chat_by_uuid(chat_uuid=chat_uuid).where(
            Chat.status == "active"
        )

    @staticmethod
    def _select_chat_by_uuid(*, chat_uuid: uuid_lib.UUID) -> Select[Any]:
        """Базовое чтение чата по `uuid` БЕЗ фильтра видимости.

        Единственное место, где собран `SELECT`; оба публичных чтения
        (`build_get_by_uuid_statement` и
        `build_get_by_uuid_including_hidden_statement`) расширяют его, а не
        дублируют список колонок: иначе третье чтение разошлось бы с первым
        по составу (класс дефекта `_load_card`, CR 7.4 H-1).
        """
        return cast(
            "Select[Any]",
            select(Chat.id, Chat.event_card_id, EventCard.uuid, EventCard.author_id)
            .join(EventCard, EventCard.id == Chat.event_card_id)
            .where(Chat.uuid == chat_uuid),
        )

    @staticmethod
    def build_get_by_uuid_including_hidden_statement(*, chat_uuid: uuid_lib.UUID) -> Select[Any]:
        """AC-05 стори 8.2: чат по `uuid` БЕЗ фильтра статуса.

        Нужно роуту подачи жалобы. После первой жалобы чат скрыт, и чтение с
        фильтром (AC-04) вернуло бы `404` — тогда `409 ALREADY_REPORTED` из
        AC-05 стал бы НЕДОСТИЖИМ, а идемпотентность сломалась бы.

        Скрывать адрес от жалобщика бессмысленно: он уже знает о диалоге.
        Посторонний до этого метода не доходит — сервис проверяет участие тем
        же предикатом (`author_id` либо `selected`).
        """
        return ChatRepository._select_chat_by_uuid(chat_uuid=chat_uuid)

    async def get_by_uuid_including_hidden(
        self, *, chat_uuid: uuid_lib.UUID
    ) -> tuple[int, int, uuid_lib.UUID, int] | None:
        """AC-05 стори 8.2: `(chat_id, event_card_id, card_uuid, author_id)` без фильтра.

        Нужно роуту подачи жалобы: после первой жалобы чат скрыт, и чтение с
        фильтром (AC-04) вернуло бы `404` — тогда `409 ALREADY_REPORTED` стал
        бы недостижим, а идемпотентность сломалась бы.

        `None` — чата нет. Скрытый чат здесь возвращается как обычный: скрытие
        от жалобщика бессмысленно, он уже знает о диалоге. Посторонний до
        этого метода не доходит — сервис проверяет участие.
        """
        result = await self._session.execute(
            self.build_get_by_uuid_including_hidden_statement(chat_uuid=chat_uuid)
        )
        row = result.first()
        if row is None:
            return None
        return int(row[0]), int(row[1]), _as_uuid(row[2]), int(row[3])

    async def lock_for_update(self, *, chat_id: int) -> str | None:
        """AC-07 стори 8.2 (D-25): блокирует строку чата и отдаёт её статус.

        Сериализует два конкурентных сценария: скрытие по жалобе и отправку
        сообщения. Без блокировки возможны оба плохих исхода — сообщение,
        вставленное в уже скрытый чат, и жалоба, «не увидевшая» только что
        отправленное сообщение (AC-07: «состояние не рассинхронизируется»).

        **Почему `FOR UPDATE`, а не оптимистический ретрай.** Предпроверка без
        блокировки окно не закрывает (P-3): между `SELECT status` и `INSERT`
        жалоба успела бы пройти. `FOR UPDATE` держит строку до `commit`.

        **Почему возвращается статус (CR 8.2, H-1).** Блокировка сама по себе
        окна НЕ закрывает: `_resolve_chat` читает чат со своим фильтром ДО
        блокировки, и жалоба, закоммитившая скрытие в промежутке, оставляла
        отправку с устаревшим «active». Проверка статуса ДОЛЖНА идти под
        блокировкой — тогда её видно после `commit` жалобы. Статус едет тем
        же `SELECT ... FOR UPDATE`, поэтому лишнего round-trip нет.

        **Дедлока нет:** блокируется ОДНА строка, и обе транзакции берут её
        первой. Порядок захвата един, значит циклического ожидания не
        возникает.

        `None` — строки нет (чат удалён между чтением и блокировкой);
        вызывающий обязан трактовать это как отказ.
        """
        result = await self._session.execute(
            text("SELECT status FROM chats WHERE id = :id FOR UPDATE"), {"id": chat_id}
        )
        row = result.first()
        return None if row is None else str(row[0])

    async def get_by_uuid(
        self, *, chat_uuid: uuid_lib.UUID
    ) -> tuple[int, int, uuid_lib.UUID, int] | None:
        """Стори 6.4 (AC-03): `(chat_id, event_card_id, card_uuid, author_id)`.

        `author_id` приходит ТЕМ ЖЕ JOIN'ом: отдельный запрос авторства стоил
        бы лишнего round-trip только на существующем чате — и это сам по себе
        тайминг-оракул (урок F-1 стори 6.3).


        `None` — чата нет: вызывающий сервис отдаёт ТОТ ЖЕ `404`, что и
        «не участник» (D-6.4-1). Различать эти случаи в ответе нельзя.
        """
        result = await self._session.execute(self.build_get_by_uuid_statement(chat_uuid=chat_uuid))
        row = result.first()
        if row is None:
            return None
        return int(row[0]), int(row[1]), _as_uuid(row[2]), int(row[3])

    async def get_by_event_card_id(self, *, event_card_id: int) -> uuid_lib.UUID | None:
        """AC-05: `uuid` чата карточки или `None`, если чата ещё нет."""
        result = await self._session.execute(
            self.build_get_by_card_statement(event_card_id=event_card_id)
        )
        value = result.scalar_one_or_none()
        return None if value is None else _as_uuid(value)
