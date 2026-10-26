"""Репозиторий карточек события (стори 4.2, Task 2.1–2.4; AC-01, AC-05…AC-07).

project-context §6: цепочка «роутер → сервис → репозиторий». Здесь только запросы:
ни правил домена, ни HTTP-кодов. Правило «править может только автор» живёт
в сервисе, а условие `author_id` — здесь, потому что это часть запроса.

D-4: сессия передаётся аргументом — репозиторий не создаёт её сам, иначе
составная операция перестаёт быть одной транзакцией.

`commit()` не вызывается: единица работы у сервиса (как в `places.py`).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, case, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    ACTIVE_STATUSES,
    ARCHIVABLE_STATUSES,
    ATTENDANCE_PROMPTABLE_STATUSES,
    FEED_STATUSES,
    CardVerificationStatus,
    EventCardStatus,
)
from app.models.event_card import CARD_REQUEST_MODE_CAPACITY, EventCard
from app.models.place import Place
from app.models.profile import Profile
from app.models.user import User


class EventCardRepository:
    """Запросы к `event_cards` в терминах домена."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, card: EventCard) -> None:
        """Ставит объект в сессию. `flush` делает сервис — он же ловит ошибки."""
        self._session.add(card)

    @staticmethod
    def build_get_owned_statement(
        author_id: int, card_uuid: uuid_lib.UUID, *, for_update: bool = False
    ) -> Select[EventCard]:
        """AC-07 (ADR-13): карточка автора по публичному `uuid`.

        `for_update=True` — X-6.1-1 (chore P-7): `PATCH capacity` обязан читать
        карточку ПОД БЛОКИРОВКОЙ. Без неё между `get_owned` и `commit`
        проходит параллельный `join_open_card`, и гард AC-08
        (`filled_count > 0`) проверяется по устаревшему снимку: карточка
        с участником переходит в открытый набор. Порядок блокировок ADR-09
        соблюдает вызывающий сервис: `users` (автор) берётся ПЕРВОЙ.

        Оба условия обязательны ВМЕСТЕ: `uuid` без `author_id` вернул бы чужую
        карточку, и сервис позволил бы её править. Проверка «своя ли» живёт
        в SQL, а не в `if card.author_id != author_id` после выборки: второе
        условие в коде забывается при первом рефакторинге (тот же класс
        дефекта, что `place_id` без среза сообщества в 3.3).

        Чужая карточка и несуществующая неразличимы: обе дают `None` → `404`
        с одним кодом. `403` раскрыл бы существование чужой карточки (ADR-13).

        Вынесено статическим билдером, чтобы текст условия проверялся без БД
        (§18.2): потеря `author_id` — реальный дефект, который тест обязан ловить.
        """
        statement = select(EventCard).where(
            EventCard.uuid == card_uuid,
            EventCard.author_id == author_id,
        )
        # X-6.1-1: блокировка строки карточки ДО чтения `filled_count`.
        # `with_for_update()` на `SELECT ... WHERE uuid AND author_id`
        # сериализует `PATCH capacity` с `join_open_card` (5а.5): та уже берёт
        # `FOR UPDATE users` первой, а запись мест идёт `UPDATE` под этой же
        # транзакцией, поэтому ожидание на строке карточки закрывает окно
        # TOCTOU между чтением снимка и `commit`.
        if for_update:
            statement = statement.with_for_update()
        return statement

    @staticmethod
    def build_queue_statement(
        *,
        status_filter: str,
        community_slug: str,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> Select[Any]:
        """AC-03, AC-08: страница очереди модерации с автором и местом.

        Три JOIN'а явные и `LEFT` (D-4.3-6):

        - `users` — автор есть всегда (FK `NOT NULL`), но связь объявлена явно,
          потому что второй запрос на каждую карточку (N+1) на странице из 20
          превратился бы в 41 запрос;
        - `profiles` — анкеты может НЕ быть (модератор её не заполняет;
          участник мог не пройти онбординг), поэтому `LEFT`, а имя/возраст
          в схеме необязательны;
        - `places` — `LEFT`, потому что фильтр `is_active` в справочнике не
          делает строку невидимой для FK: скрытое место остаётся ссылкой.

        **Условие курсора — по атрибутам `EventCard.`, а не строкой** без
        таблицы: с JOIN'ами `created_at`/`uuid` перестают быть однозначными
        именами (долг «`_where_clause` сломается на `JOIN users`» из CR 2.6).

        `status_filter` — уже проверенное доменное значение: `'all'` означает
        «без фильтра», остальные — конкретный `verification_status`. Сравнение
        идёт с параметром, а не подстановкой строки в SQL.

        Сортировка `created_at, uuid` — ровно ключ курсора (D-4.3-8):
        иначе `WHERE (created_at, uuid) >` пропускал бы строки между
        страницами при равных метках.
        """
        statement = (
            select(
                EventCard,
                User.uuid.label("author_uuid"),
                Profile.name.label("author_name"),
                Profile.age.label("author_age"),
                Place.uuid.label("place_uuid"),
                Place.name.label("place_name"),
            )
            .join(User, User.id == EventCard.author_id)
            .outerjoin(Profile, Profile.user_id == User.id)
            .outerjoin(Place, Place.id == EventCard.place_id)
        )
        # CR 4.3, патч 1 (AC-03, ADR-01/ADR-13): срез сообщества ОБЯЗАТЕЛЕН.
        # Найдено ревью исполнением: без него очередь отдавала карточки ЧУЖОГО
        # сообщества — модератор «Томска» видел и мог отклонить карточку
        # другого города, а `total` считал всю таблицу. Слаг берётся из
        # настроек (`_resolve_community_slug`), а не из запроса: клиент не
        # выбирает сообщество (ADR-01).
        statement = statement.where(EventCard.community_slug == community_slug)
        if status_filter != "all":
            statement = statement.where(EventCard.verification_status == status_filter)
        if after is not None:
            after_created_at, after_uuid = after
            # Сравнение строки-кортежа целиком, а не двух `OR`-ветвей:
            # `(created_at, uuid) > (:a, :b)` — это ровно порядок сортировки.
            statement = statement.where(
                tuple_(EventCard.created_at, EventCard.uuid) > (after_created_at, after_uuid)
            )
        # `cast`: точный тип кортежа колонок (EventCard, UUID, str|None, ...)
        # вызывающему коду не нужен — он распаковывает строку позиционно.
        # Без `cast` mypy strict требует совпадения всех шести типов в `Select[Any]`.
        return cast(
            "Select[Any]",
            statement.order_by(EventCard.created_at.asc(), EventCard.uuid.asc()).limit(limit),
        )

    @staticmethod
    def build_queue_count_statement(*, status_filter: str, community_slug: str) -> Select[Any]:
        """AC-03, D-4.3-7: `total` очереди — БЕЗ курсора и без JOIN'ов.

        Курсор — позиция чтения, а не фильтр: `total` с курсором уменьшался бы
        от страницы к странице, и клиент не смог бы показать счётчик работы.
        JOIN'ы не нужны: считаются строки `event_cards` по фильтру.

        `EventCard.id` вместо `count(*)`: у `SELECT count(*)` PostgreSQL обязан
        прочитать строки, у `count(id)` — тот же план, но `id` уже NOT NULL,
        и `scalar_one()` даёт честный `int` для mypy-strict.
        """
        statement = select(func.count(EventCard.id)).where(
            EventCard.community_slug == community_slug
        )
        if status_filter != "all":
            statement = statement.where(EventCard.verification_status == status_filter)
        return statement

    async def list_queue_page(
        self,
        *,
        status_filter: str,
        community_slug: str,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> tuple[list[Any], int]:
        """AC-03: страница очереди и `total` за два запроса.

        Порядок двух запросов стабилен (страница первой) — при отладке в логе
        видно, какой упал (тот же приём, что `PlaceRepository.list_page`).

        `limit < 1` — `ValueError`: `LIMIT 0` дал бы молча пустую выдачу,
        `LIMIT -1` — ошибку драйвера (`500` на ошибке вызывающего).
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        page_statement = self.build_queue_statement(
            status_filter=status_filter,
            community_slug=community_slug,
            after=after,
            limit=limit,
        )
        page_result = await self._session.execute(page_statement)
        rows = list(page_result.all())

        count_statement = self.build_queue_count_statement(
            status_filter=status_filter, community_slug=community_slug
        )
        count_result = await self._session.execute(count_statement)
        total: int = count_result.scalar_one()
        return rows, total

    async def get_for_moderation(
        self, card_uuid: uuid_lib.UUID, *, community_slug: str
    ) -> EventCard | None:
        """AC-04, AC-05: карточка по публичному `uuid` без фильтра автора.

        Модератор видит ЛЮБУЮ карточку: `author_id` в условии нет намеренно.
        Несуществующая даёт `None` → `404 card_not_found`; отдельного «чужая»
        ответа для модератора не существует (ADR-13 — про авторов).

        `populate_existing=True` обязателен (урок CR 2.1, патч 11): без него
        объект из identity map вернулся бы снимком, и после `approve` в этой же
        сессии повторное чтение показало бы старый `verification_status`.
        """
        result = await self._session.execute(
            select(EventCard)
            .where(
                EventCard.uuid == card_uuid,
                # CR 4.3, патч 2 (AC-04/AC-05, ADR-13): решение по карточке
                # ЧУЖОГО сообщества запрещено тем же срезом, что в очереди.
                # Без него `approve`/`reject` работали по любому публичному
                # `uuid`, который модератор мог получить вне очереди.
                EventCard.community_slug == community_slug,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def build_author_lock_statement(author_id: int) -> Select[Any]:
        """AC-04, ADR-09 (стори 4.4): блокировка строки автора ПЕРВОЙ в транзакции.

        `FOR UPDATE` на `users` — точка сериализации лимита: две публикации
        одного автора встают в очередь здесь, и `COUNT` под блокировкой видит
        уже зафиксированный результат первой. Индекс ускоряет подсчёт, но
        гонку не закрывает (ADR-09), поэтому подсчёт обязан идти ПОСЛЕ захвата.

        Выборка `User.id`, а не `User`: блокировка строки не требует остальных
        колонок, и лишние поля тянули бы данные из БД без пользы.

        Билдер статический, чтобы текст `FOR UPDATE` проверялся без БД (§18.2):
        снятие `.with_for_update()` — реальный дефект, дающий два успешных
        `UPDATE` в гонке.
        """
        # CR 5а.2, Task 2.2: делегирование в `UserRepository` — одна реализация
        # блокировки актора на проект. Две копии `SELECT users.id ... FOR UPDATE`
        # разошлись бы при первом изменении (например, добавлении `community`),
        # и публикация с подачей заявки начали бы брать разные блокировки под
        # одним и тем же порядком ADR-09 — это прямой путь к дедлоку.
        # SQL не меняется: текст и порядок байтов те же, тесты 4.4 зелёные.
        from app.repositories.users import UserRepository

        return UserRepository.build_lock_statement(author_id)

    @staticmethod
    def build_active_count_statement(author_id: int) -> Select[Any]:
        """AC-01, AC-05 (FR71, FR74, FR100): число АКТИВНЫХ карточек автора.

        Считаются только `ACTIVE_STATUSES` (`published`, `full`, `closed`):
        ровно те состояния, где карточка видна в ленте и встреча ещё не
        завершена. `finished` (FR73) и `cancelled` (FR100) слот освобождают;
        `draft` невидим в ленте (FR99) и слот не занимает.

        Набор статусов берётся из доменной константы, а не пишется строкой:
        добавление статуса в правило меняло бы и подсчёт, и тест — расхождение
        двух копий дало бы «лимит не тот» без падения чего-либо.

        `count(EventCard.id)`, а не `count(*)`: `id` — NOT NULL, план тот же,
        а `scalar_one()` возвращает честный `int` для mypy-strict.
        """
        return (
            select(func.count(EventCard.id))
            .select_from(EventCard)
            .where(
                EventCard.author_id == author_id,
                EventCard.status.in_(ACTIVE_STATUSES),
            )
        )

    @staticmethod
    def build_publish_cas_statement(
        *,
        card_id: int,
        author_id: int,
        now: datetime,
        expires_at: datetime,
    ) -> Any:
        """AC-06 (X-21, стори 4.4): условный `UPDATE` публикации вместо `FOR UPDATE`.

        `FOR UPDATE` на `event_cards` взял бы блокировку `cards` ДО `users`
        в сценарии, где `users` ещё не заблокирован, — нарушение ADR-09.
        `UPDATE` же идёт уже ПОСЛЕ `users`, и неявная блокировка строки
        карточки встаёт в правильном порядке (ADR-09: `users` → `cards`).

        Предикат повторяет ВСЕ проверки Python (OQ-6):
        - `status = 'draft'` — защита от двойной публикации: второй `UPDATE`
          не найдёт строку и `published_at` не перезапишется (AC-06);
        - `verification_status = 'approved'` — модератор, отклонивший карточку
          между проверкой и `UPDATE`, не получит опубликованную отклонённую;
        - `starts_at > now` — публикация встречи из прошлого невозможна.

        `author_id` в предикате — часть правила доступа (ADR-13): без него
        `UPDATE` по `id` мог бы затронуть чужую карточку при ошибке в сервисе.
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.author_id == author_id,
                EventCard.status == EventCardStatus.DRAFT,
                EventCard.verification_status == CardVerificationStatus.APPROVED,
                EventCard.starts_at > now,
            )
            .values(
                status=EventCardStatus.PUBLISHED,
                published_at=now,
                expires_at=expires_at,
            )
        )

    async def publish_if_draft(
        self,
        *,
        card_id: int,
        author_id: int,
        now: datetime,
        expires_at: datetime,
    ) -> int:
        """AC-06 (X-21): выполняет CAS и возвращает `rowcount`.

        `0` означает, что предикат не совпал: карточку успели изменить
        в параллельной транзакции. Решение о КОДЕ ошибки принимает сервис
        (`_raise_publish_conflict`) по перечитанной карточке — репозиторий
        не знает доменных кодов (project-context §6).

        `synchronize_session=False` обязателен: без него ORM попытается
        согласовать identity map по критерию, содержащему `starts_at > now`,
        и упадёт на выражении, которое нельзя вычислить в Python.
        """
        statement = self.build_publish_cas_statement(
            card_id=card_id, author_id=author_id, now=now, expires_at=expires_at
        )
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        # `cast`: у `AsyncSession.execute` тип `Result`, а `rowcount` живёт
        # на `CursorResult`. Для DML он всегда курсорный — mypy этого не
        # выводит, поэтому сужение явное, а не `type: ignore`.
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def count_active(self, author_id: int) -> int:
        """AC-01: число активных карточек автора (вызов ПОД блокировкой `users`)."""
        result = await self._session.execute(self.build_active_count_statement(author_id))
        return int(result.scalar_one())

    async def lock_author(self, author_id: int) -> None:
        """AC-04, ADR-09: захватывает строку автора ПЕРВОЙ в транзакции."""
        await self._session.execute(self.build_author_lock_statement(author_id))

    @staticmethod
    def build_feed_statement(
        *,
        community_slug: str,
        now: datetime,
        category: str | None,
        has_seats: bool,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> Select[Any]:
        """AC-01, AC-04, AC-05, AC-08: страница ленты с местом и автором.

        Три JOIN'а и их вид (`INNER`/`LEFT`) повторяют очередь модерации 4.3 —
        лента и кабинет показывают одну и ту же карточку, и расхождение в
        наборе строк означало бы «модератор видит анкету, участник нет»:

        - `users` — автор есть всегда (FK `NOT NULL`), но связь объявлена
          явно, иначе имя автора тянулось бы вторым запросом на каждую
          карточку страницы (N+1 при 20 карточках = 41 запрос);
        - `profiles` — анкеты может не быть (онбординг не пройден), поэтому
          `LEFT`, а `name`/`age` в схеме необязательны;
        - `places` — `LEFT`, потому что скрытое место (`is_active=false`) не
          делает строку невидимой для FK: ссылка остаётся.

        **`status IN FEED_STATUSES`, а не `status != 'draft'`** (AC-01):
        `closed` (автор закрыл набор) и `finished`/`cancelled` (история) не
        должны попадать в ленту — «не draft» включил бы все три. Набор берётся
        из доменной константы `FEED_STATUSES` (models/enums.py), чтобы
        добавление статуса меняло и запрос, и тест.

        **`starts_at >= now`** (AC-01, FR73): прошедшая встреча не «предстоит».
        Условие по атрибуту `EventCard.`, а не строкой: с JOIN'ами `starts_at`
        перестаёт быть однозначным именем (долг CR 2.6).

        **Сортировка `starts_at ASC, uuid ASC`** (D-4.5-2) — РОВНО ключ курсора.
        Иначе `WHERE (starts_at, uuid) >` пропускал бы строки между страницами
        при равных `starts_at` (встречи «в 19:00» — типичный случай, а не
        редкий). `uuid`, а не `id`: D-2 запрещает публичный bigint.

        **`has_seats` — по арифметике `filled_count < capacity`** (AC-04),
        не по `status='full'`: так формулирует AC эпика, и CHECK
        `ck_event_cards_full_capacity` уже держит согласованность обоих
        признаков на уровне БД.

        `category` — уже проверенное доменное значение (роутер типизировал
        `PlaceCategory`) или `None`. Сравнение с параметром, не подстановка.
        """
        statement = (
            select(
                EventCard,
                User.uuid.label("author_uuid"),
                Profile.name.label("author_name"),
                Profile.age.label("author_age"),
                Place.uuid.label("place_uuid"),
                Place.name.label("place_name"),
                Place.address_text.label("place_address_text"),
                Place.is_active.label("place_is_active"),
            )
            .join(User, User.id == EventCard.author_id)
            .outerjoin(Profile, Profile.user_id == User.id)
            .outerjoin(Place, Place.id == EventCard.place_id)
            .where(
                EventCard.community_slug == community_slug,
                EventCard.status.in_(FEED_STATUSES),
                # AC-02 стори 5б.1: карточка с ИСТЁКШИМ окном решения не
                # показывается как открытая. Воркер `card_windows` закроет её
                # в течение 60 с (NFR20), но между истечением и прогоном окно
                # уже недействительно — иначе участник видел бы «окно открыто»
                # там, где решение больше не принимается.
                #
                # AC-08 стори 5б.2 (OQ-5b.1-1, вариант 3, D-5b.2-6): `full`
                # окном НЕ фильтруется — и это НЕ «участие возможно после
                # отмены чужого участия». Прежняя редакция этого комментария
                # врала: окно ограничивает приём ЗАЯВОК, а `full` уже решён,
                # поэтому собранная карточка остаётся видимой как состоявшаяся
                # компания. Участие в ней НЕВОЗМОЖНО: `POST /join` и `POST
                # /requests` отдают `409 CARD_NOT_ACCEPTING` по гарду окна
                # (AC-02 5б.1). А после освобождения места (`full -> published`,
                # 5а.6) карточка уходит ИЗ ЛЕНТЫ, потому что стала `published`
                # с истёкшим окном, и воркер закрывает её в `closed` <=60 с.
                # Место на такой карточке НЕ переиспользуется — это принятое
                # последствие варианта 3, а не недосмотр.
                or_(
                    EventCard.status != EventCardStatus.PUBLISHED,
                    EventCard.expires_at > now,
                ),
                EventCard.starts_at >= now,
                # AC-08 стори 5а.4 (X-36, FR68): карточка скрытого места
                # (`places.is_active=false`) НЕ попадает в ленту. Лента —
                # витрина, и встреча, на которую нельзя прийти, не должна
                # занимать в ней место. JOIN остаётся `LEFT`, предикат —
                # в `WHERE`: `INNER` здесь дал бы тот же результат, но
                # `build_get_visible_statement` делит с лентой форму
                # `select`-списка и обязан остаться без среза.
                Place.is_active.is_(True),
            )
        )
        if category is not None:
            statement = statement.where(EventCard.category == category)
        if has_seats:
            statement = statement.where(EventCard.filled_count < EventCard.capacity)
        if after is not None:
            after_starts_at, after_uuid = after
            # Сравнение строки-кортежа целиком, а не двух `OR`-ветвей:
            # `(starts_at, uuid) > (:a, :b)` — это ровно порядок сортировки.
            statement = statement.where(
                tuple_(EventCard.starts_at, EventCard.uuid) > (after_starts_at, after_uuid)
            )
        return cast(
            "Select[Any]",
            statement.order_by(EventCard.starts_at.asc(), EventCard.uuid.asc()).limit(limit),
        )

    @staticmethod
    def build_feed_count_statement(
        *, community_slug: str, now: datetime, category: str | None, has_seats: bool
    ) -> Select[Any]:
        """AC-07 (X-07): `total` ленты — БЕЗ курсора и без JOIN'ов.

        Курсор — позиция чтения, а не фильтр: `total` с курсором уменьшался бы
        от страницы к странице, и клиент показывал бы «всего встреч» всё
        меньше (D-4.5-9).

        JOIN'ы не нужны: считаются строки `event_cards` по фильтрам. Фильтр
        `has_seats` живёт здесь тоже — иначе `total` и `items` считались бы по
        разным предикатам, и клиент не смог бы объяснить «2 всего, 0 на
        странице».

        `count(EventCard.id)`, а не `count(*)`: `id` NOT NULL, план тот же,
        `scalar_one()` даёт честный `int` для mypy-strict.

        Расхождение с `items` в `READ COMMITTED` — допустимо (AC-07):
        параллельная публикация между двумя `execute` меняет счётчик, но не
        границы страниц. Не «чинить» ни `REPEATABLE READ`, ни блокировками.
        """
        statement = select(func.count(EventCard.id)).where(
            EventCard.community_slug == community_slug,
            EventCard.status.in_(FEED_STATUSES),
            # AC-02 стори 5б.1: карточка с ИСТЁКШИМ окном решения не
            # показывается как открытая. Воркер `card_windows` закроет её
            # в течение 60 с (NFR20), но между истечением и прогоном окно
            # уже недействительно — иначе участник видел бы «окно открыто»
            # там, где решение больше не принимается. `full` не фильтруется:
            # места кончились сами (ADR-10), и участие возможно после отмены
            # чужого участия (5а.6) — окно тут не при чём.
            or_(
                EventCard.status != EventCardStatus.PUBLISHED,
                EventCard.expires_at > now,
            ),
            EventCard.starts_at >= now,
            # AC-08 стори 5а.4 (X-36, FR68): ТОТ ЖЕ предикат, что в
            # `build_feed_statement`, иначе `total` считал бы карточки
            # скрытых мест, которых нет на странице («5 всего, 0 на
            # странице»). Подзапросом, а не JOIN: docstring ниже обещает
            # «без JOIN'ов», и план `id IN (SELECT ...)` по частичному
            # индексу `idx_places_community_active` — тот же.
            EventCard.place_id.in_(select(Place.id).where(Place.is_active.is_(True))),
        )
        if category is not None:
            statement = statement.where(EventCard.category == category)
        if has_seats:
            statement = statement.where(EventCard.filled_count < EventCard.capacity)
        return statement

    async def list_feed_page(
        self,
        *,
        community_slug: str,
        now: datetime,
        category: str | None,
        has_seats: bool,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> tuple[list[Any], int]:
        """AC-01, AC-07, AC-08: страница ленты и `total` за два запроса.

        Порядок стабилен (страница первой) — при отладке в логе видно, какой
        упал (тот же приём, что `list_queue_page` и `PlaceRepository.list_page`).

        `limit < 1` — `ValueError`: `LIMIT 0` дал бы молча пустую выдачу,
        `LIMIT -1` — ошибку драйвера (`500` на ошибке вызывающего).

        `limit` передаёт сервис уже с `+1` строкой для проверки «есть ли
        следующая страница»: репозиторий не решает, когда курсор нужен.
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        page_statement = self.build_feed_statement(
            community_slug=community_slug,
            now=now,
            category=category,
            has_seats=has_seats,
            after=after,
            limit=limit,
        )
        page_result = await self._session.execute(page_statement)
        rows = list(page_result.all())

        count_statement = self.build_feed_count_statement(
            community_slug=community_slug, now=now, category=category, has_seats=has_seats
        )
        count_result = await self._session.execute(count_statement)
        total: int = count_result.scalar_one()
        return rows, total

    @staticmethod
    def build_get_visible_statement(
        *, card_uuid: uuid_lib.UUID, community_slug: str
    ) -> Select[Any]:
        """AC-02 (ADR-13, §7.2): карточка по `uuid` в срезе сообщества.

        `author_id`'а в условии НЕТ: карточка нужна и не автору (участник
        открывает встречу из ленты). Поэтому видимость `draft` проверяется
        в сервисе по сравнению `author_id` с текущим пользователем — это
        правило домена («черновик видит только автор»), а не часть запроса.

        Срез сообщества обязателен: без него `GET /cards/{uuid}` отдавал бы
        карточку чужого города по прямому `uuid`, и `404` переставал бы быть
        единственным ответом постороннему (CR 4.3, патч 2 — тот же дефект
        в очереди модерации).

        JOIN'ы те же и в том же виде, что в ленте: карточка детального экрана
        и карточка в ленте обязаны иметь одинаковый состав полей, иначе
        клиент получил бы два разных объекта одного ресурса.
        """
        return cast(
            "Select[Any]",
            select(
                EventCard,
                User.uuid.label("author_uuid"),
                Profile.name.label("author_name"),
                Profile.age.label("author_age"),
                Place.uuid.label("place_uuid"),
                Place.name.label("place_name"),
                Place.address_text.label("place_address_text"),
                # AC-08 стори 5а.4 (X-36): детальный экран среза НЕ имеет:
                # карточка остаётся доступной по прямой ссылке (заявки и
                # участники живы), но скрытое место не раскрывается —
                # `place` в ответе будет `null`. Метка нужна, чтобы сборка
                # ответа отличала скрытое место от активного.
                Place.is_active.label("place_is_active"),
            )
            .join(User, User.id == EventCard.author_id)
            .outerjoin(Profile, Profile.user_id == User.id)
            .outerjoin(Place, Place.id == EventCard.place_id)
            .where(
                EventCard.uuid == card_uuid,
                EventCard.community_slug == community_slug,
            ),
        )

    @staticmethod
    def build_close_due_statement(*, card_id: int, now: datetime) -> Any:
        """AC-03 (ADR-03, NFR21): CAS `published → closed` по истёкшему окну.

        Предикат содержит ОБА условия (`status` и `expires_at`) — это и есть
        CAS: PostgreSQL блокирует строку на время `UPDATE`, второй конкурент
        видит уже `closed` и обновляет 0 строк. Отдельная блокировка или флаг
        не нужны, потому что сам статус и есть флаг (ADR-03).

        `expires_at <= now` обязателен: без него гонка «закрыть раньше срока»
        стирала бы окно, в котором автор ещё выбирает. `closed_at` пишется
        тем же `UPDATE` — второй запрос на «когда закрылось» не нужен.

        Автовыбор заявителя здесь НЕ выполняется (D-4.5-5): заявок нет до 5а,
        а CAS без `SELECT` заявителя остаётся идемпотентным.
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.expires_at <= now,
            )
            .values(status=EventCardStatus.CLOSED, closed_at=now)
        )

    async def close_if_due(self, *, card_id: int, now: datetime) -> int:
        """AC-03: выполняет CAS закрытия окна и возвращает `rowcount`.

        `0` — НЕ ошибка: карточку закрыли параллельно (воркер или другой
        запрос). Сервис просто перечитывает состояние.

        `synchronize_session=False` обязателен: без него ORM попытается
        согласовать identity map по критерию с `expires_at <= now` и упадёт
        на выражении, которое нельзя вычислить в Python (тот же приём, что
        в `publish_if_draft`).
        """
        statement = self.build_close_due_statement(card_id=card_id, now=now)
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def get_visible_card(
        self, *, card_uuid: uuid_lib.UUID, community_slug: str
    ) -> Any | None:
        """AC-02: строка карточки с местом и автором или `None`.

        `populate_existing=True` обязателен (урок CR 2.1, патч 11): после
        ленивого закрытия окна объект в identity map держит старый `status`,
        и клиент получил бы `published` на ответ, где сервис уже сделал
        `UPDATE` в `closed`.
        """
        result = await self._session.execute(
            self.build_get_visible_statement(
                card_uuid=card_uuid, community_slug=community_slug
            ).execution_options(populate_existing=True)
        )
        return result.first()

    # ========================================================================
    # Состояния карточки: закрытие, отмена, архив, полная вместимость
    # (стори 4.6, Task 2; AC-03…AC-06, AC-10)
    # ========================================================================

    @staticmethod
    def build_close_by_author_statement(*, card_id: int, author_id: int, now: datetime) -> Any:
        """AC-03 (FR74): CAS `published → closed` рукой автора.

        Предикат содержит `status='published'` — это и есть CAS: второй
        конкурент (повторный клик, другой таб) видит уже `closed` и обновляет
        0 строк. Проверка статуса в Python (сервис) объясняет ПОЧЕМУ, но
        гонку закрывает только это условие.

        `author_id` в предикате — часть правила доступа (ADR-13): без него
        `UPDATE` по `id` мог бы затронуть чужую карточку при ошибке в сервисе.
        `full` закрыть нельзя: набор уже остановлен по вместимости, и `closed`
        исказил бы причину (D-4.6-4).
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.author_id == author_id,
                EventCard.status == EventCardStatus.PUBLISHED,
            )
            .values(status=EventCardStatus.CLOSED, closed_at=now)
        )

    @staticmethod
    def build_cancel_statement(*, card_id: int, author_id: int) -> Any:
        """AC-04 (FR100, R-04): CAS мягкой отмены, без физического удаления.

        Разрешено из `draft`/`published`/`full`/`closed`; запрещено из
        `finished` (встреча прошла — история, FR73) и `cancelled` (повторная
        отмена — конфликт состояния, OQ-15). Условие выражено отрицанием
        двух терминальных статусов, а не перечислением разрешённых: новый
        промежуточный статус (если появится) попадёт в отменяемые сам, и
        список не разойдётся с `EventCardStatus`.

        `finished` в предикате обязателен: без него отмена переписала бы
        архивную карточку, и история явки (эпик 5) ссылалась бы на
        отменённую встречу.
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.author_id == author_id,
                EventCard.status.not_in((EventCardStatus.FINISHED, EventCardStatus.CANCELLED)),
            )
            .values(status=EventCardStatus.CANCELLED)
        )

    @staticmethod
    def build_finish_statement(*, card_id: int, now: datetime) -> Any:
        """AC-05 (FR73, NFR21): CAS `published/full/closed → finished` по времени.

        `starts_at <= now` — единственный гард: архив наступает по факту
        начала встречи, независимо от того, собралась компания или нет.
        `status IN ARCHIVABLE_STATUSES` не даёт «архивировать» уже отменённую
        или уже архивную карточку: `finished` — терминальное состояние, и
        повторный `UPDATE` его не трогает.

        Набор статусов берётся из доменной константы, а не строкой: воркер
        (`build_due_finish_ids_statement`) и предикат обязаны совпадать, иначе
        воркер выбрал бы строки, которые `UPDATE` не переведёт (молчаливый
        простой NFR20).
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.status.in_(ARCHIVABLE_STATUSES),
                EventCard.starts_at <= now,
            )
            .values(status=EventCardStatus.FINISHED)
        )

    @staticmethod
    def build_join_seat_statement(*, card_id: int, chat_uuid: uuid_lib.UUID) -> Any:
        """AC-02 (X-39, FR84, ADR-10): место занимается и `full` ставится ОДНИМ `UPDATE`.

        Стори 6.2 (AC-02, AC-07, D-6.2-1): тот же `UPDATE` пишет `chat_uuid` —
        но ТОЛЬКО когда это место последнее (`filled_count + 1 >= capacity`).
        `else_=EventCard.chat_uuid` (не `None`!): на незаполненной карточке
        ранее записанное значение обязано сохраниться, иначе прошлая сборка
        «забылась» бы при первом же присоединении (мутация M8, риск R10).

        Дописывать `chat_uuid` вторым `UPDATE` нельзя: `ck_event_cards_full_capacity`
        запрещает `published` при `filled_count = capacity`, поэтому между
        инкрементом и записью чата существовала бы строка, которую CHECK
        отвергает (`500` на легальном действии, класс дефекта X-39).

        Заменяет `build_mark_full_statement`: тот делал CAS `published → full`
        отдельным запросом, и между инкрементом `filled_count` и этим CAS
        существовала строка `published` + `filled_count = capacity`, которую
        запрещает `ck_event_cards_full_capacity` (0011). На легальном действии
        это давало `500` (X-39). Схема верна — ломался репозиторий.

        В PostgreSQL все `SET`-выражения читают СТАРОЕ значение строки, поэтому
        `filled_count + 1` в двух местах согласованы арифметически: `CASE`
        видит `capacity` и тот же старый счётчик, что и приращение. Порядок
        вычислений столбцов здесь не важен — иначе `filled_count = filled_count + 1`
        и `status = ... filled_count + 1` могли бы разойтись и дать `full`
        на одно место раньше.

        `>=`, а не `=`: если по ошибке счётчик уже перескочил вместимость
        (импорт, ручная правка), карточка обязана стать `full`, а не остаться
        `published` с отрицательным остатком. Предикат `filled_count < capacity`
        отсекает присоединение без свободных мест: `rowcount == 0` — нормальный
        исход гонки, а не ошибка (решение о коде принимает сервис).

        `status = 'published'` в предикате — единственный вход: `draft`/`closed`/
        `finished`/`cancelled`/`full` не принимают новых мест, и `UPDATE` их
        не тронет (AC-07/AC-08 различает сервис по перечитанному статусу).

        `RETURNING` обязателен (D-5а.5-7): ответ строится из фактически
        записанных `filled_count, capacity, status`, а повторный `SELECT`
        прочитал бы чужую зафиксированную версию и отдал бы клиенту неверное
        число мест.
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.filled_count < EventCard.capacity,
                # X-6.1-1 (chore P-7, P-3): режим заявок — НЕ открытый набор.
                # Без этого предиката гонка «`PATCH capacity 3 → 1` параллельно
                # с мгновенным присоединением» занимала место на карточке
                # режима заявок (FR70): `filled_count < capacity` при
                # `capacity = 1` истинно, `UPDATE` проходил, а сервисная
                # проверка `is_request_mode` читала УЖЕ устаревший снимок.
                # Предикат в CAS, а не предпроверка (P-3): проверка перед
                # записью окно не закрывает.
                EventCard.capacity > CARD_REQUEST_MODE_CAPACITY,
            )
            .values(
                filled_count=EventCard.filled_count + 1,
                # `EventCardStatus.FULL.value`, а не сам член enum: SQLAlchemy не
                # может вывести тип литерала из `StrEnum` (`CompileError: no
                # literal value renderer ... datatype NULL`), а строка ложится
                # в `status` (`String` + CHECK, D-3) без приведения.
                status=case(
                    (
                        EventCard.filled_count + 1 >= EventCard.capacity,
                        EventCardStatus.FULL.value,
                    ),
                    else_=EventCard.status,
                ),
                # AC-02, AC-07 стори 6.2: чат появляется ровно в момент сборки.
                # `else_` — САМО поле, а не `None`: на карточке, собранной
                # ранее (повторная сборка, миграция данных), значение обязано
                # сохраниться (риск R10, мутация M8).
                #
                # CR 6.2 (blind #1, edge #1): `coalesce`, а НЕ голый `chat_uuid`.
                # Сценарий: компания собрана (чата `A`), участник вышел (5а.6,
                # `full → published`, `chat_uuid` в карточке остаётся),
                # карточку добрали заново. Без `coalesce` в карточку лёг бы
                # НОВЫЙ `uuid4 = B`, а `chats.uuid` остался бы `A` — контракт
                # 6.1 `chats.uuid = event_cards.chat_uuid` нарушен НАВСЕГДА,
                # и клиент получил бы идентификатор несуществующего чата.
                # `coalesce` делает uuid стабильным после первого присвоения.
                chat_uuid=case(
                    (
                        EventCard.filled_count + 1 >= EventCard.capacity,
                        func.coalesce(EventCard.chat_uuid, chat_uuid),
                    ),
                    else_=EventCard.chat_uuid,
                ),
            )
            .returning(
                EventCard.filled_count,
                EventCard.capacity,
                EventCard.status,
                # AC-02 стори 6.2, TD п.2: без этой колонки сервис не узнает,
                # что компания собрана, и чат не будет создан (риск R9,
                # мутация M9).
                EventCard.chat_uuid,
            )
        )

    @staticmethod
    def build_select_seat_statement(*, card_id: int, chat_uuid: uuid_lib.UUID) -> Any:
        """AC-01 стори 5б.2 (X-39, FR30, FR78): место, `full` и чат — ОДНИМ `UPDATE`.

        Зеркало `build_join_seat_statement`, но вход строже: `capacity=1` и
        `filled_count < capacity` (то есть `filled_count = 0`). Почему ОДИН
        `UPDATE`, а не три подряд: `ck_event_cards_full_capacity` (`0011`)
        запрещает состояние `full` при `filled_count < capacity` И состояние
        `published` при `filled_count = capacity`. Любое разделение дало бы
        строку, которую CHECK отвергает, — `500` на легальном действии
        (класс дефекта X-39). В PostgreSQL все `SET`-выражения читают СТАРОЕ
        значение строки, поэтому `filled_count + 1`, `capacity` и `chat_uuid`
        согласованы в одном вычислении.

        `status = 'published'` в предикате — единственный вход: `draft` не
        опубликован, `closed`/`finished`/`cancelled` набор закрыли, `full` уже
        собран. `rowcount == 0` — нормальный исход гонки, а не ошибка: код
        ответа выбирает сервис по перечитанному состоянию (как в 5а.5).

        `chat_uuid` пишется здесь же, а не отдельным `UPDATE` (D-5b.2-1):
        ответ `200` обязан содержать идентификатор чата, и он должен быть
        записан В ТОЙ ЖЕ строке, где `full`. Иначе между двумя `UPDATE`
        существовала бы карточка `full` без чата — эпик 6 не смог бы
        восстановить соответствие.

        `RETURNING` обязателен: ответ строится из фактически записанных
        значений, а повторный `SELECT` прочитал бы чужую зафиксированную
        версию (D-5а.5-7).
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.filled_count < EventCard.capacity,
                # X-6.1-1 (chore P-7, P-3): зеркало предиката `join_seat`.
                # Выбор автора законен ТОЛЬКО в режиме заявок (FR70):
                # `PATCH capacity 1 → 3` параллельно с `select` иначе записал бы
                # место и чат карточке открытого набора, где выбора не бывает.
                EventCard.capacity == CARD_REQUEST_MODE_CAPACITY,
            )
            .values(
                filled_count=EventCard.filled_count + 1,
                status=case(
                    (
                        EventCard.filled_count + 1 >= EventCard.capacity,
                        EventCardStatus.FULL.value,
                    ),
                    else_=EventCard.status,
                ),
                # CR 6.2 (blind #1): тот же инвариант, что в `build_join_seat_statement`.
                # `select_seat` вызывается для `capacity=1`; при повторном выборе
                # после отмены участия (5а.6) новый `uuid4` разошёлся бы с уже
                # открытым `chats.uuid`, и контракт 6.1 был бы нарушен.
                chat_uuid=func.coalesce(EventCard.chat_uuid, chat_uuid),
            )
            .returning(
                EventCard.filled_count,
                EventCard.capacity,
                EventCard.status,
                EventCard.chat_uuid,
            )
        )

    @staticmethod
    def build_due_finish_ids_statement(*, now: datetime, limit: int) -> Select[Any]:
        """AC-05 (NFR20): строки для воркера — что уже началось, но не в архиве.

        `FOR UPDATE SKIP LOCKED` — горизонтальное масштабирование воркера
        (ADR-03/ADR-14): два процесса не ждут друг друга и не берут одну
        строку дважды. Без `SKIP LOCKED` второй воркер ждал бы первого, и
        прогон растягивался бы на длину чужой транзакции.

        `LIMIT` обязателен: воркер обязан возвращать управление, даже если
        накопились тысячи просроченных карточек. Иначе один прогон займёт
        память и удержит блокировки до конца.

        `order_by(starts_at)` — самые просроченные первыми: при постоянном
        потоке новые карточки не должны голодать (`NFR20` — «пропущенных
        окон 0»).
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        return (
            select(EventCard.id)
            .where(
                EventCard.status.in_(ARCHIVABLE_STATUSES),
                EventCard.starts_at <= now,
            )
            .order_by(EventCard.starts_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def close_by_author(self, *, card_id: int, author_id: int, now: datetime) -> int:
        """AC-03: выполняет CAS закрытия и возвращает `rowcount`.

        `0` означает, что предикат не совпал: карточку закрыли/отменили
        параллельно. Решение о коде ошибки принимает сервис по ПЕРЕЧИТАННОЙ
        карточке — репозиторий не знает доменных кодов (project-context §6).

        `synchronize_session=False` обязателен: без него ORM попытается
        согласовать identity map по критерию с `now` в Python-выражении.
        """
        statement = self.build_close_by_author_statement(
            card_id=card_id, author_id=author_id, now=now
        )
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def cancel_by_author(self, *, card_id: int, author_id: int) -> int:
        """AC-04: выполняет CAS отмены и возвращает `rowcount`.

        `0` — карточка уже `finished`/`cancelled` либо не принадлежит автору
        (последнее сервис отсекает раньше через `get_owned`).
        """
        statement = self.build_cancel_statement(card_id=card_id, author_id=author_id)
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def finish_card(self, *, card_id: int, now: datetime) -> int:
        """AC-05: выполняет CAS архива и возвращает `rowcount`.

        `0` — карточка уже в архиве/отменена или встреча ещё не началась.
        Оба случая нормальны для воркера: он перечитывает список каждые 60 с.
        """
        statement = self.build_finish_statement(card_id=card_id, now=now)
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def join_seat(self, *, card_id: int, chat_uuid: uuid_lib.UUID) -> Any:
        """AC-02, AC-03 (X-39): занимает место и возвращает записанную строку.

        Стори 6.2 (AC-02): `chat_uuid` генерируется сервисом ДО CAS и пишется
        тем же `UPDATE`, если место оказалось последним. Параметр обязателен:
        значение, созданное после `UPDATE`, потребовало бы второго запроса
        и нарушило бы X-39.

        Возврат — `Row(filled_count, capacity, status)` из `RETURNING` или
        `None`, если предикат не совпал (мест нет / статус не `published` /
        карточка исчезла). `None` — не ошибка: это нормальный проигрыш гонки,
        и код ответа выбирает сервис по перечитанному состоянию.

        `synchronize_session=False` обязателен: без него ORM попытается
        согласовать identity map по выражению `CASE` и упадёт на попытке
        вычислить его в Python. Возвращаемая строка — из БД, поэтому
        устаревший объект в сессии не мешает ответу.
        """
        statement = self.build_join_seat_statement(card_id=card_id, chat_uuid=chat_uuid)
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return result.first()

    async def select_seat(self, *, card_id: int, chat_uuid: uuid_lib.UUID) -> Any:
        """AC-01 стори 5б.2: занимает единственное место и пишет `chat_uuid`.

        Возврат — `Row(filled_count, capacity, status, chat_uuid)` из `RETURNING`
        или `None`, если предикат не совпал (карточка уже `full`, не `published`
        или исчезла). `None` — не ошибка: это нормальный проигрыш гонки, и код
        ответа выбирает сервис по перечитанному состоянию (приём 5а.5).

        `synchronize_session=False` обязателен по той же причине, что
        в `join_seat`: ORM не сможет вычислить `CASE` в Python.
        """
        statement = self.build_select_seat_statement(card_id=card_id, chat_uuid=chat_uuid)
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return result.first()

    @staticmethod
    def build_release_seat_statement(*, card_id: int) -> Any:
        """AC-01, AC-02 (FR104, ADR-10): место освобождается и `full` снимается ОДНИМ `UPDATE`.

        Зеркальная операция к `build_join_seat_statement` (стори 5а.5): там
        инкремент и `full` ставились одним запросом, здесь декремент и возврат
        в `published` — тоже одним. Раздельные `UPDATE` создали бы строку
        `full` + `filled_count < capacity`, которую запрещает
        `ck_event_cards_full_capacity` (0011) → `500` на легальном действии
        (класс дефекта X-39, но в обратную сторону).

        CHECK из `0011` СИММЕТРИЧЕН и обратный переход разрешает:
        `(filled_count < capacity OR status IN ('full','closed','finished','cancelled'))
        AND (status <> 'full' OR filled_count = capacity)`. Первая скобка
        допускает `published` при свободном месте, вторая запрещает `full`
        без заполнения. Проверено исполнением на живой PostgreSQL 18
        (D-5a.6-2): `full` 4/4 → `UPDATE` → `published` 3/4, `UPDATE 1`.
        Схема НЕ правится (решение X-39).

        В PostgreSQL все `SET`-выражения читают СТАРОЕ значение строки,
        поэтому `filled_count - 1` в обоих местах согласованы: `CASE` видит
        тот же старый счётчик, что и декремент.

        `status IN ('published','full')` — единственные входы: `closed`/
        `finished`/`cancelled` НЕ возвращаются в ленту (FR73, FR100), их
        `filled_count` остаётся историческим фактом состава (AC-11).

        `filled_count > 0` — единственный гард от отрицательного значения
        (NFR47 §6.5), симметричный `filled_count < capacity` из `join_seat`.

        `RETURNING` обязателен: ответ строится по фактически записанным
        значениям, а повторный `SELECT` прочитал бы чужую зафиксированную
        версию (D-5а.5-7).
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.status.in_((EventCardStatus.PUBLISHED, EventCardStatus.FULL)),
                EventCard.filled_count > 0,
            )
            .values(
                filled_count=EventCard.filled_count - 1,
                # `EventCardStatus.PUBLISHED.value`, а не член enum: SQLAlchemy
                # не выводит тип литерала из `StrEnum` (тот же довод, что
                # в `build_join_seat_statement`).
                status=case(
                    (
                        EventCard.filled_count - 1 < EventCard.capacity,
                        EventCardStatus.PUBLISHED.value,
                    ),
                    else_=EventCard.status,
                ),
            )
            .returning(
                EventCard.filled_count,
                EventCard.capacity,
                EventCard.status,
            )
        )

    @staticmethod
    def build_due_window_ids_statement(*, now: datetime, limit: int) -> Select[Any]:
        """AC-05 (NFR21, стори 4.6): карточки с истёкшим окном — вход воркера.

        Предикат совпадает с `build_close_due_statement` посимвольно: воркер
        выбирает ровно те строки, которые его же CAS переведёт в `closed`.
        Расхождение (например, забытый `status='published'`) дало бы выборку
        строк, которые `UPDATE` не тронет: воркер логировал бы «0 закрыто»
        при живых просроченных окнах (молчаливый простой NFR20).

        `FOR UPDATE SKIP LOCKED` — второй воркер берёт другие строки и не ждёт
        первого (ADR-03, ADR-14). `LIMIT` обязателен по той же причине, что
        у `build_due_finish_ids_statement`.
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        return (
            select(EventCard.id)
            .where(
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.expires_at <= now,
            )
            .order_by(EventCard.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def release_seat(self, *, card_id: int) -> Any:
        """AC-01, AC-02, AC-11: освобождает место и возвращает записанную строку.

        Возврат — `Row(filled_count, capacity, status)` из `RETURNING` или
        `None`, если предикат не совпал: карточка `closed`/`finished`/
        `cancelled` (место не возвращается в ленту, AC-11) либо `filled_count`
        уже 0 (нечего освобождать). `None` — НЕ ошибка отмены участия: сервис
        в этом случае всё равно отменяет участие, просто карточка не меняет
        счётчик (D-5a.6-7).

        `synchronize_session=False` обязателен по той же причине, что у
        `join_seat`: ORM попытался бы согласовать identity map по выражению
        `CASE` и упал бы на вычислении его в Python.
        """
        statement = self.build_release_seat_statement(card_id=card_id)
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return result.first()

    async def due_window_ids(self, *, now: datetime, limit: int) -> list[int]:
        """AC-05: идентификаторы карточек с истёкшим окном решения.

        Возвращает только `id`: воркеру нужен ключ для CAS, а ORM-объекты
        после `UPDATE` мимо ORM пришлось бы перечитывать.
        """
        statement = self.build_due_window_ids_statement(now=now, limit=limit)
        result = await self._session.execute(statement)
        return [int(row[0]) for row in result.all()]

    async def due_finish_ids(self, *, now: datetime, limit: int) -> list[int]:
        """AC-05: идентификаторы карточек, которые воркер обязан заархивировать.

        Возвращает `list[int]`, а не ORM-объекты: воркеру нужен только `id`
        для CAS, а объект в identity map после `UPDATE` пришлось бы
        перечитывать (`populate_existing`). Идентификаторы — внутренние,
        наружу не выходят (D-2).
        """
        statement = self.build_due_finish_ids_statement(now=now, limit=limit)
        result = await self._session.execute(statement)
        return [int(row[0]) for row in result.all()]

    async def count_overdue_windows(self, *, now: datetime, grace_minutes: int = 5) -> int:
        """AC-03 стори 10.7 (NFR64, ADR-14): число зависших окон решения.

        Предикат ADR-14 дословно: `status='published' AND expires_at <
        :now - interval '5 minutes'`. Это контроль ОТКАЗА воркера, а не
        обычная работа: `close_due_windows` закрывает окно через считанные
        секунды после `expires_at`, поэтому ненулевой результат означает, что
        воркер не отработал (лежит, падает, не видит БД) — то есть инцидент,
        о котором надо сказать человеку.

        Запас 5 минут, а не 0: цикл воркера — 60 с, дрожание расписания и
        короткая блокировка дают окно в секунды, и контроль без запаса
        поднимал бы ложный алерт на каждом прогоне.

        Индекс `ix_event_cards_due_windows` (`models/event_card.py:359-363`)
        покрывает предикат, поэтому запрос дешёвый: миграций не требуется (D-10.7-6).

        `grace_minutes` — параметр, а не литерал в SQL: граница проверяется
        тестом (`test_overdue_windows.py`) на `now()-10 мин` и `now()-1 мин`,
        и второй прогон с отрицательным запасом воспроизводит «контроль
        сработал на здоровом воркере».
        """
        statement = (
            select(func.count())
            .select_from(EventCard)
            .where(
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.expires_at < now - timedelta(minutes=grace_minutes),
            )
        )
        result = await self._session.execute(statement)
        return int(result.scalar_one())

    # ——— 5б.3: автовыбор первого по истечении окна (AC-01, AC-02, AC-03) ———

    @staticmethod
    def build_due_decision_ids_statement(*, now: datetime, limit: int) -> Any:
        """AC-01 стори 5б.3 (FR80, NFR21): карточки, где автовыбор ещё не сделан.

        Предикат тот же, что у `build_due_window_ids_statement`
        (`status='published'` + `expires_at <= now`), но состав колонок другой:
        ядру `close_due_card` нужен `author_id` — он захватывает строку автора
        ПЕРВОЙ (`lock_for_update`, ADR-09). Без него пришлось бы читать
        карточку вторым запросом до блокировки, и точка сериализации
        сместилась бы за `event_cards` (D-5b.3-2).

        `FOR UPDATE SKIP LOCKED` ЗДЕСЬ НЕТ намеренно: блокировка карточки
        раньше `users` дала бы встречную очередь с `join_open_card` и дедлок
        (урок ER 5а №3, D-5b.3-2). Взаимное исключение даёт строка автора.

        `order_by(expires_at, id)` — самые просроченные первыми и стабильный
        порядок при равных `expires_at`; `LIMIT` обязателен, чтобы воркер
        возвращал управление при тысячах карточек.

        Индекс `ix_event_cards_due_windows (expires_at) WHERE status='published'`
        покрывает предикат — миграция не нужна (D-5b.3-7).
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        return (
            select(EventCard.id, EventCard.author_id, EventCard.uuid)
            .where(
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.expires_at <= now,
            )
            .order_by(EventCard.expires_at, EventCard.id)
            .limit(limit)
        )

    @staticmethod
    def build_decision_state_statement(*, card_id: int) -> Any:
        """AC-03 стори 5б.3: состояние карточки для ядра автовыбора.

        Только три поля (`uuid`, `status`, `expires_at`): ядру нужен признак
        «идемпотентность или работа» и якорь окна. Полная строка с `JOIN`ами
        автора и места была бы лишним чтением на горячем пути воркера
        (≤200 карточек раз в 60 с).

        `FOR UPDATE` ЗДЕСЬ НЕТ (D-5b.3-2): точка сериализации — строка автора
        (`lock_for_update`), взятая ДО этого запроса. Блокировка карточки
        дала бы встречную очередь с `join_open_card` и дедлок (ADR-09).

        `populate_existing` не нужен: запрос скалярный, ORM-объект карточки
        не материализуется, и identity map не участвует в ответе.
        """
        return select(
            EventCard.uuid, EventCard.status, EventCard.expires_at, EventCard.capacity
        ).where(EventCard.id == card_id)

    async def decision_state(
        self, *, card_id: int
    ) -> tuple[uuid_lib.UUID, str, datetime, int] | None:
        """AC-03: `(uuid, status, expires_at, capacity)` карточки или `None`.

        `None` — карточки нет (физического удаления нет, R-04; исход
        невозможен, но ядро обязано вернуть `SKIPPED`, а не упасть).
        """
        result = await self._session.execute(self.build_decision_state_statement(card_id=card_id))
        row = result.first()
        return None if row is None else (row[0], str(row[1]), row[2], int(row[3]))

    async def due_decision_ids(
        self, *, now: datetime, limit: int
    ) -> list[tuple[int, int, uuid_lib.UUID]]:
        """AC-01: тройки `(card_id, author_id, uuid)` — вход ядра `close_due_card`.

        `uuid` нужен логу отказа (`worker.card.decision.failed`): на пути ошибки
        внутренний `id` наружу не выходит (D-2), а второй запрос ради одного
        лог-поля удвоил бы обращения к БД.

        Пара, а не ORM-объект: после `UPDATE` мимо ORM объект в identity map
        держал бы старый статус (`populate_existing` не поможет — `UPDATE`
        идёт через `synchronize_session=False`). Идентификаторы внутренние,
        наружу не выходят (D-2).
        """
        statement = self.build_due_decision_ids_statement(now=now, limit=limit)
        result = await self._session.execute(statement)
        return [(int(row[0]), int(row[1]), row[2]) for row in result.all()]

    @staticmethod
    def build_reminder_state_statement(*, card_id: int) -> Any:
        """AC-04, AC-05 стори 5б.4 + AC-01 стори 9.4: состояние карточки для напоминания.

        Шесть полей (`uuid`, `status`, `expires_at`, `decision_reminded_at`,
        `author_id`, `author_uuid`). Отдельный запрос, а не расширение
        `build_decision_state_statement` (D-5b.4-5): тот принадлежит ядру
        автовыбора 5б.3, и новые столбцы в нём изменили бы контракт закрытой
        стори ради посторонней задачи.

        **`join users` ради адреса доставки** (9.4, D-9.4-5). FR110 адресован
        автору, а `publish_to_user` нужен ПУБЛИЧНЫЙ `user_uuid`; в карточке
        лежит только внутренний `author_id`. Приём D-TD-5: личность берётся
        в ТОМ ЖЕ скалярном запросе, а не вторым `SELECT` — иначе между двумя
        чтениями автор карточки успел бы смениться, и уведомление ушло бы не
        тому (класс R6). Ядро обязано остаться самодостаточным: его зовут без
        предварительной выборки.

        `FOR UPDATE` нет (D-5b.4-3): напоминание ничего не блокирует. `join`
        не добавляет `FOR UPDATE` и не берёт очередь ADR-09: `users` читается
        без блокировки строки.
        """
        return (
            select(
                EventCard.uuid,
                EventCard.status,
                EventCard.expires_at,
                EventCard.decision_reminded_at,
                EventCard.author_id,
                User.uuid,
            )
            .join(User, User.id == EventCard.author_id)
            .where(EventCard.id == card_id)
        )

    async def reminder_state(
        self, *, card_id: int
    ) -> tuple[uuid_lib.UUID, str, datetime, datetime | None, int, uuid_lib.UUID] | None:
        """AC-04, AC-05 5б.4 + AC-01 9.4: шесть полей состояния или `None`.

        `(uuid, status, expires_at, decision_reminded_at, author_id,
        author_uuid)`. Две последние колонки — адрес доставки FR110
        (D-9.4-5): `author_id` нужен `create_one`, `author_uuid` —
        `publish_to_user`. Оба приходят из ОДНОГО скалярного запроса
        (`join users`), поэтому расхождение «карточка уже другого автора»
        между выборкой адресата и вставкой исключено.

        `None` — карточки нет (физического удаления нет, R-04; исход
        невозможен, но сервис обязан вернуть `SKIPPED`, а не упасть).
        Автор существует всегда (`users.id` — FK и `NOT NULL`), поэтому
        `join` не может превратить найденную карточку в `None`.
        """
        result = await self._session.execute(self.build_reminder_state_statement(card_id=card_id))
        row = result.first()
        if row is None:
            return None
        return (row[0], str(row[1]), row[2], row[3], int(row[4]), row[5])

    @staticmethod
    def build_due_reminder_ids_statement(
        *, now: datetime, threshold_minutes: int, limit: int
    ) -> Any:
        """AC-01, AC-03, AC-04, AC-05 стори 5б.4: карточки, которым пора напомнить.

        Предикат — четыре условия, каждое из них закрывает отдельный AC:

        - `status='published'` — автор ещё не сделал выбор и окно не закрыто
          (AC-04): `select`, автовыбор и отмена снимают карточку этим же
          условием, отдельного признака не нужно;
        - `decision_reminded_at IS NULL` — напоминание уже отправлено, второй
          раз не шлём (AC-03, ADR-15);
        - `expires_at > :now` СТРОГО — окно истекло, там работает автовыбор
          5б.3, а «успей выбрать» после истечения ложь (AC-05, D-5b.4-2);
        - `expires_at <= :deadline` — верхняя граница ВКЛЮЧИТЕЛЬНАЯ: «ровно за
          порог минут» уже пора напоминать (AC-01, D-5b.4-2).

        `deadline` считается здесь из `now + threshold_minutes`, а не через
        `make_interval` в SQL: порог приходит параметром (AC-02), и арифметика
        в Python не даёт разойтись типам `timestamptz` и `interval` между
        диалектами. Параметр `threshold_minutes` — единственный вход:
        вызывающий не может передать несогласованные `now` и `deadline`.

        `FOR UPDATE` ЗДЕСЬ НЕТ (D-5b.4-3): напоминание не блокирует автора,
        не читает `join_requests` и не участвует в очереди ADR-09. `users`
        не берётся вовсе — встречной очереди с `close_due_card` быть не может.

        Индекс `ix_event_cards_due_windows (expires_at) WHERE status='published'`
        покрывает диапазон по `expires_at`; дополнительный индекс не заводим
        (миграции в 5б.4 нет).
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        if threshold_minutes < 1:
            raise ValueError("threshold_minutes должен быть >= 1")
        deadline = now + timedelta(minutes=threshold_minutes)
        return (
            select(EventCard.id, EventCard.author_id, EventCard.uuid)
            .where(
                EventCard.status == EventCardStatus.PUBLISHED,
                EventCard.decision_reminded_at.is_(None),
                EventCard.expires_at > now,
                EventCard.expires_at <= deadline,
            )
            .order_by(EventCard.expires_at, EventCard.id)
            .limit(limit)
        )

    @staticmethod
    def build_mark_reminded_statement(*, card_id: int, now: datetime) -> Any:
        """AC-01, AC-03 стори 5б.4: CAS-маркер напоминания (ADR-15).

        `WHERE decision_reminded_at IS NULL` — то же поле служит и предикатом,
        и маркером: пустой `RETURNING` означает «уже отправлено другим прогоном
        или вторым воркером». Отдельной таблицы `sent_reminders` нет и не будет
        (ADR-15 прямо отверг: второй источник истины).

        `synchronize_session=False` — `UPDATE` идёт мимо ORM: объект карточки
        в identity map держал бы старое `NULL`, и последующее чтение в этой же
        сессии показало бы «не напоминали».
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.decision_reminded_at.is_(None),
                # CR 5б.4, патч 1: статус входит в предикат CAS. Между
                # чтением `reminder_state` и этим `UPDATE` карточку может
                # закрыть автор/автовыбор/отмена; без статуса маркер вставал
                # бы на `closed`, и событие «окно истекает» уходило бы после
                # истечения (AC-04/AC-05). Доказано исполнением.
                EventCard.status == EventCardStatus.PUBLISHED,
            )
            .values(decision_reminded_at=now)
            .returning(EventCard.uuid)
            .execution_options(synchronize_session=False)
        )

    async def due_reminder_ids(
        self, *, now: datetime, threshold_minutes: int, limit: int
    ) -> list[tuple[int, int, uuid_lib.UUID]]:
        """AC-01: тройки `(card_id, author_id, uuid)` — кандидаты на напоминание.

        `author_id` берётся ради симметрии с `due_decision_ids` и будущей
        доставки эпика 6, но в лог не попадает (AC-08): безличность 5б.2
        запрещает выводить идентификаторы пользователей.
        """
        statement = self.build_due_reminder_ids_statement(
            now=now, threshold_minutes=threshold_minutes, limit=limit
        )
        result = await self._session.execute(statement)
        return [(int(row[0]), int(row[1]), row[2]) for row in result.all()]

    async def mark_reminded(self, *, card_id: int, now: datetime) -> uuid_lib.UUID | None:
        """AC-03: ставит маркер и возвращает `uuid`, либо `None` — уже стоял.

        `None` — не ошибка: это гонка двух прогонов или рестарт воркера,
        и именно так идемпотентность ADR-15 выглядит снаружи.
        """
        result = await self._session.execute(
            self.build_mark_reminded_statement(card_id=card_id, now=now)
        )
        row = result.first()
        return None if row is None else row[0]

    @staticmethod
    def build_due_prompt_ids_statement(*, now: datetime, limit: int) -> Any:
        """AC-03, AC-05, AC-07, AC-13 стори 7.3: карточки, которым пора запросить явку.

        Предикат — ТРИ условия, и каждое закрывает свой AC:

        - `starts_at <= :now` ВКЛЮЧИТЕЛЬНО (AC-07): ровно в момент начала запрос
          уже уместен, секундой раньше — ещё нет (граница проверяется тестами
          `S24`/`S23` инъекцией `now`);
        - `attendance_prompted_at IS NULL` (AC-05): маркер уже стоял — запрос
          отправлен, второй не нужен (ADR-15). `IS NULL`, не `NOT
          attendance_prompted_at`: на `NULL` булево выражение даёт `NULL`, и
          `WHERE` молча пропустил бы строку (класс дефекта 6.7);
        - `status IN (ATTENDANCE_PROMPTABLE_STATUSES)` (AC-13): список берётся
          из ОДНОГО кортежа, который дословно совпадает с предикатом
          частичного индекса `ix_event_cards_prompt_due`.

        **Нижней границы по `starts_at` НЕТ намеренно (D-7.3-4).** 15 минут
        NFR22 — требование ЛАТЕНТНОСТИ, не окно допустимости: отсечка
        `starts_at > :now - interval '15 min'` превратила бы лежачего воркера
        в необратимый пропуск факта явки (карточка не будет запрошена никогда,
        история 7.5 и санкция FR109 останутся без данных). Цена — поздний
        запрос по старой карточке, вред ограничен фильтром `cancelled` и тем,
        что 7.2 позволяет подтвердить явку и без уведомления.

        `starts_at` ВХОДИТ В ВЫБОРКУ (D-TD-4, решение пользователя): payload
        уведомления требует его (Task 3.3), а второй `SELECT` заломал бы
        анти-оракул по числу SQL (урок D-3 TD 7.2). Контракт — ровно 4 запроса
        на карточку: выборка, CAS, `ensure_rows`, вставка уведомлений.

        `chat_uuid` берётся ТЕМ ЖЕ запросом (D-3 пакета): второй `SELECT`
        видимости запрещён.

        `FOR UPDATE` ЗДЕСЬ НЕТ (D-5b.4-3): воркер не блокирует автора и не
        участвует в очереди блокировок ADR-09. Атомарность даёт CAS ниже,
        а не блокировка чтения.
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        return (
            select(
                EventCard.id,
                EventCard.author_id,
                EventCard.uuid,
                EventCard.chat_uuid,
                EventCard.starts_at,
            )
            .where(
                EventCard.starts_at <= now,
                EventCard.attendance_prompted_at.is_(None),
                EventCard.status.in_(ATTENDANCE_PROMPTABLE_STATUSES),
            )
            # D-7.3 starvation (R9): самые старые первыми. Порядок стабилен и
            # детерминирован (`id` разрешает равенство `starts_at`), иначе
            # батч `LIMIT` мог бы «тасоваться» между прогонами.
            .order_by(EventCard.starts_at, EventCard.id)
            .limit(limit)
        )

    @staticmethod
    def build_mark_attendance_prompted_statement(*, card_id: int, now: datetime) -> Any:
        """AC-05, AC-08, AC-09 стори 7.3: CAS-маркер запроса явки (ADR-15).

        Одна операция решает три задачи:

        1. **Идемпотентность** (AC-05): `attendance_prompted_at IS NULL` в
           предикате. Пустой `RETURNING` — нормальный исход, а не ошибка:
           это гонка двух прогонов или рестарт воркера. Отдельной таблицы
           «что отправлено» нет и не будет (ADR-15: второй источник истины
           переживёт падение процесса иначе, чем БД).
        2. **Граница начала** (AC-07, M3): `starts_at <= :now`. Без неё
           карточка до начала получила бы запрос, и тест границы `now + 1 с`
           краснел бы по маркеру, а не только по счётчику.
        3. **Статус в предикате CAS** (AC-08, M2): между выборкой и `UPDATE`
           карточку могли отменить. Без статуса маркер встал бы на
           `cancelled`, и запрос «подтвердите явку» по отменённой встрече
           оказался бы утверждением, которого не было (урок CR 5б.4, патч 1).

        Предпроверок НЕТ (P-3, D-7.3-10): ни `SELECT status`, ни `SELECT
        attendance_prompted_at` перед `UPDATE`. Между проверкой и записью
        открывается окно, и именно в нём теряется второй запрос.

        `synchronize_session=False` — `UPDATE` идёт мимо ORM: объект карточки
        в identity map держал бы старое `NULL`.

        Возвращает `uuid` и `author_id`: пустой `RETURNING` = пропуск (AC-05).
        """
        return (
            update(EventCard)
            .where(
                EventCard.id == card_id,
                EventCard.attendance_prompted_at.is_(None),
                EventCard.starts_at <= now,
                EventCard.status.in_(ATTENDANCE_PROMPTABLE_STATUSES),
            )
            .values(attendance_prompted_at=now)
            .returning(EventCard.uuid, EventCard.author_id)
            .execution_options(synchronize_session=False)
        )

    async def due_prompt_ids(
        self, *, now: datetime, limit: int
    ) -> list[tuple[int, int, uuid_lib.UUID, uuid_lib.UUID | None, datetime]]:
        """AC-03, AC-07: пятёрки `(card_id, author_id, uuid, chat_uuid, starts_at)`.

        `starts_at` возвращается из выборки, а не из `RETURNING` (D-TD-5):
        при пустом `RETURNING` payload не собирается вовсе, и значение не
        может оказаться `None` из-за неудавшегося CAS.

        `author_id` в лог не попадает (безличность, AC-11): он нужен только
        как адресат строки `notifications`.
        """
        statement = self.build_due_prompt_ids_statement(now=now, limit=limit)
        result = await self._session.execute(statement)
        return [(int(row[0]), int(row[1]), row[2], row[3], row[4]) for row in result.all()]

    async def mark_attendance_prompted(
        self, *, card_id: int, now: datetime
    ) -> tuple[uuid_lib.UUID, int] | None:
        """AC-05: ставит маркер и возвращает `(uuid, author_id)`, либо `None`.

        `None` — уже стоял (второй прогон/второй воркер) или карточку отменили
        между выборкой и CAS. Оба исхода — норма, а не ошибка.
        """
        result = await self._session.execute(
            self.build_mark_attendance_prompted_statement(card_id=card_id, now=now)
        )
        row = result.first()
        return None if row is None else (row[0], int(row[1]))

    async def get_owned(
        self, author_id: int, card_uuid: uuid_lib.UUID, *, for_update: bool = False
    ) -> EventCard | None:
        """AC-05…AC-07: карточка автора или `None`.

        `populate_existing=True` обязателен (урок CR 2.1, патч 11): без него
        объект, уже лежащий в identity map, вернулся бы снимком, и после
        `PATCH` в этой же сессии повторное чтение показало бы старые поля.

        `for_update=True` (X-6.1-1, chore P-7) — строка карточки берётся
        `FOR UPDATE`. Вызывающий сервис обязан заблокировать `users` ПЕРЕД
        этим вызовом: ADR-09 требует порядок `users → event_cards`.
        """
        result = await self._session.execute(
            self.build_get_owned_statement(
                author_id, card_uuid, for_update=for_update
            ).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()
