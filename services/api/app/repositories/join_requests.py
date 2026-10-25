"""Репозиторий заявок на участие (стори 5а.2; AC-03, AC-06, AC-07).

project-context §6: здесь только запросы, правила домена — в
`services/join_request_service.py`. Билдеры статические, чтобы текст SQL
проверялся без БД (§18.2): потеря `community_slug` или `FOR UPDATE` — реальные
дефекты, которые падение интеграционного теста объясняет косвенно.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, case, exists, func, or_, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.enums import (
    JOIN_REQUEST_ACTIVE_STATUSES,
    JOIN_REQUEST_OCCUPYING_STATUSES,
    JOIN_REQUEST_VISIBLE_STATUSES,
    JoinRequestStatus,
)
from app.models.event_card import EventCard
from app.models.join_request import JoinRequest
from app.models.place import Place
from app.models.profile import Profile
from app.models.user import User


class JoinRequestRepository:
    """Запросы к `join_requests` и чтение карточки для правил подачи."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ——— билдеры (проверяются без БД, §18.2) ———

    @staticmethod
    def build_get_for_request_statement(
        card_uuid: uuid_lib.UUID, community_slug: str
    ) -> Select[Any]:
        """AC-07 (ADR-13): карточка для подачи заявки — с срезом сообщества.

        `community_slug` обязателен в SQL, а не в `if` после выборки: карточка
        чужого сообщества должна быть НЕОТЛИЧИМА от несуществующей (`404` с тем
        же телом). Проверка после выборки забывается при первом рефакторинге —
        тот же класс дефекта, что `place_id` без среза сообщества в 3.3.

        `FOR UPDATE` здесь НЕТ намеренно (D-5a.2-3): 5а.2 не пишет `event_cards`
        и не блокирует её. Порядок ADR-09 (`users → cards`) соблюдается тем, что
        единственная блокировка — на `users` (Task 2.1).

        JOIN'ов нет: заявке нужны `author_id`, `status`, `capacity`,
        `expires_at` — все колонки `event_cards`; автор и место в ответе `201`
        не отдаются (D-5a.2-9), а лишние JOIN'ы удорожают запись.
        """
        return select(EventCard).where(
            EventCard.uuid == card_uuid,
            EventCard.community_slug == community_slug,
        )

    @staticmethod
    def build_active_count_statement(applicant_id: int) -> Select[Any]:
        """AC-03 (FR77, NFR29): число АКТИВНЫХ заявок заявителя.

        Набор статусов берётся из доменной константы
        `JOIN_REQUEST_ACTIVE_STATUSES` (`pending`), а не пишется строкой:
        добавление статуса в правило меняло бы и подсчёт, и тест, а
        расхождение двух копий дало бы «лимит не тот» без падения чего-либо
        (приём `build_active_count_statement` 4.4).

        `count(JoinRequest.id)`, а не `count(*)`: `id` — NOT NULL, план тот же,
        а `scalar_one()` даёт честный `int` для mypy-strict.
        """
        return (
            select(func.count(JoinRequest.id))
            .select_from(JoinRequest)
            .where(
                JoinRequest.applicant_id == applicant_id,
                JoinRequest.status.in_(JOIN_REQUEST_ACTIVE_STATUSES),
            )
        )

    # ——— 5а.3: отмена заявки (AC-01, AC-04, AC-05) ———

    @staticmethod
    def build_cancel_statement(
        request_uuid: uuid_lib.UUID, applicant_id: int, community_slug: str
    ) -> Any:
        """AC-01, AC-04, AC-05: CAS-отмена `pending -> cancelled` одним `UPDATE`.

        Почему `UPDATE ... WHERE status='pending'`, а не «`SELECT` → проверить
        → `UPDATE`» (§18.3): предварительный `SELECT` пропускает две
        параллельные отмены, и вторая переписала бы `decided_at` поверх
        первой — аудит (NFR1–NFR4) потерял бы момент фактического решения.
        Здесь вторая транзакция просто не находит `pending` (rowcount 0).

        `applicant_id` в предикате, а не в `if` после выборки (AC-04,
        ADR-13): чужая заявка обязана быть НЕОТЛИЧИМА от несуществующей, а
        проверка после выборки забывается при первом рефакторинге (класс
        дефекта `community_slug` в 3.3).

        `community_slug` — подзапросом, а не `UPDATE ... FROM`: карточка
        чужого сообщества не должна отменяться (ADR-01/ADR-13), и подзапрос
        читается как отдельный фильтр, не смешиваясь с `SET`.

        `decided_at` заполняется здесь же: заявка выходит из `pending`,
        и момент решения обязан быть (D-5a.1-6). `CHECK` на это намеренно
        не ставится, поэтому единственный рубеж — этот `SET`.
        """
        card_ids = select(EventCard.id).where(EventCard.community_slug == community_slug)
        return (
            update(JoinRequest)
            .where(
                JoinRequest.uuid == request_uuid,
                JoinRequest.applicant_id == applicant_id,
                JoinRequest.status == "pending",
                JoinRequest.card_id.in_(card_ids),
            )
            .values(status="cancelled", decided_at=func.now())
        )

    @staticmethod
    def build_status_lookup_statement(
        request_uuid: uuid_lib.UUID, applicant_id: int, community_slug: str
    ) -> Select[Any]:
        """AC-04, AC-05: статус СВОЕЙ заявки — только для диагностики отказа.

        Вызывается ТОЛЬКО когда CAS не сработал (rowcount 0): читать статус
        перед записью незачем, а на отказе без него не отличить `cancelled`
        (идемпотентный успех) от `selected` (`409`) и `expired`/`not_selected`
        (`409 REQUEST_NOT_ACTIVE`).

        Предикаты те же три, что у CAS: иначе диагностика отвечала бы про
        строку, которую CAS не мог тронуть, и `404` подменялся бы `409`
        на чужой заявке.
        """
        card_ids = select(EventCard.id).where(EventCard.community_slug == community_slug)
        return select(JoinRequest.status).where(
            JoinRequest.uuid == request_uuid,
            JoinRequest.applicant_id == applicant_id,
            JoinRequest.card_id.in_(card_ids),
        )

    # ——— 5а.3: страница «моих заявок» (AC-03, AC-06, AC-07) ———

    @staticmethod
    def build_my_requests_page_statement(
        *,
        applicant_id: int,
        community_slug: str,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> Select[Any]:
        """AC-03, AC-06: страница заявок заявителя в порядке подачи.

        AC-03, FR81: фильтр по `JOIN_REQUEST_VISIBLE_STATUSES` — ПОЛОЖИТЕЛЬНЫЙ
        список АТРИБУТОМ, а не строкой `status <> 'not_selected'`. Инверсия
        пропускала бы любой будущий статус в публичную выдачу молча.
        Фильтрация в SQL, а не в питоне после выборки (project-context §5):
        иначе `total` считал бы скрытые заявки, и «всего» минус «активные»
        выдавало бы число отказов (UX-контракт «не показывать всего подано»).

        `ORDER BY submitted_at, uuid` — ровно ключ курсора (D-5a.3-6).
        `uuid`, а не `id`: `id` — внутренний bigint (D-2), и он попал бы
        в query-параметр курсора. `ASC`: «Мои заявки» — короткая история,
        стабильный порядок нужен для детерминированного листания, а не для
        приоритета.

        `filled_count`/`status` карточки читаются как есть: автор мог выбрать
        другого, и экран заявителя показывает фактическое состояние карточки
        («Компания собрана»), а не вычисляет его сам (второй источник правды).
        """
        statement = (
            select(
                JoinRequest,
                EventCard.uuid.label("card_uuid"),
                EventCard.title.label("card_title"),
                EventCard.category.label("card_category"),
                EventCard.starts_at.label("card_starts_at"),
                EventCard.expires_at.label("card_expires_at"),
                EventCard.status.label("card_status"),
                EventCard.capacity.label("card_capacity"),
                EventCard.filled_count.label("card_filled_count"),
                Place.uuid.label("place_uuid"),
                Place.name.label("place_name"),
                Place.address_text.label("place_address_text"),
                User.id.label("author_id"),
                User.uuid.label("author_uuid"),
                Profile.name.label("author_name"),
                Profile.age.label("author_age"),
            )
            .join(EventCard, EventCard.id == JoinRequest.card_id)
            .join(User, User.id == EventCard.author_id)
            .outerjoin(Profile, Profile.user_id == User.id)
            .outerjoin(Place, Place.id == EventCard.place_id)
            .where(
                JoinRequest.applicant_id == applicant_id,
                JoinRequest.status.in_(JOIN_REQUEST_VISIBLE_STATUSES),
                EventCard.community_slug == community_slug,
            )
        )
        if after is not None:
            after_submitted_at, after_uuid = after
            statement = statement.where(
                tuple_(JoinRequest.submitted_at, JoinRequest.uuid)
                > (after_submitted_at, after_uuid)
            )
        return cast(
            "Select[Any]",
            statement.order_by(JoinRequest.submitted_at.asc(), JoinRequest.uuid.asc()).limit(limit),
        )

    @staticmethod
    def build_my_requests_count_statement(*, applicant_id: int, community_slug: str) -> Select[Any]:
        """AC-06, D-5a.3-7: `total` — БЕЗ курсора и без JOIN'ов к `places`/`profiles`.

        Курсор — позиция чтения, а не фильтр: `total` с курсором уменьшался бы
        от страницы к странице. JOIN'ы не нужны: считаются заявки по
        предикатам, а предикаты ссылаются только на `join_requests` и
        `event_cards`.

        `count(JoinRequest.id)`: `id` NOT NULL, план тот же, `scalar_one()`
        даёт честный `int` для mypy-strict.
        """
        return (
            select(func.count(JoinRequest.id))
            .select_from(JoinRequest)
            .join(EventCard, EventCard.id == JoinRequest.card_id)
            .where(
                JoinRequest.applicant_id == applicant_id,
                JoinRequest.status.in_(JOIN_REQUEST_VISIBLE_STATUSES),
                EventCard.community_slug == community_slug,
            )
        )

    # ——— 5а.4: список заявившихся для автора (AC-01, AC-04) ———

    @staticmethod
    def _after_predicate(after: tuple[datetime, uuid_lib.UUID] | None) -> Any:
        """AC-01, ADR-05, D-5a.4-2: keyset-предикат при `ORDER BY submitted_at, id`.

        Здесь расходится пара ключей, и это единственное место, где расхождение
        корректно разрешено:

        - сортировка — `submitted_at ASC, id ASC` (ADR-05: правило очереди, по
          нему же пойдёт автовыбор 5б.3);
        - курсор — `(submitted_at, uuid)` (D-2: внутренний bigint не выходит
          наружу даже в query-параметре).

        Наивное `tuple_(submitted_at, uuid) > (:ts, :uuid)` было бы НЕВЕРНО:
        при равных `submitted_at` порядок по `uuid` (случайный `gen_random_uuid()`)
        не совпадает с порядком по `id`, и страница либо повторила бы строку,
        либо потеряла её. Поэтому `uuid` из курсора превращается обратно в `id`
        СКАЛЯРНЫМ подзапросом внутри того же запроса: `id` не покидает сервер,
        а порядок остаётся ровно тем, что требует ADR-05.

        Подзапрос вместо второго round-trip: чтение `id` отдельным запросом
        дало бы окно между выборками (строка исчезла/добавилась) — а keyset
        обязан быть одним снимком.
        """
        if after is None:
            return None
        after_submitted_at, after_uuid = after
        anchor_id = select(JoinRequest.id).where(JoinRequest.uuid == after_uuid).scalar_subquery()
        # CR 5а.4, патч 1: НЕ `tuple_(...) > (...)`. Правая часть обычного
        # кортежа коэрсится SQLAlchemy в bind-параметры (`$4::BIGINT`), а
        # скалярный подзапрос в бинд не встаёт: asyncpg получал объект
        # `ScalarSelect` → `DBAPIError` → `500` на КАЖДОЙ второй странице.
        # Проба: 25 заявок, `cursor` от первой страницы → `500` (тест
        # «пагинация» этого не ловил: 3 заявки при странице 20, курсора нет).
        # Явные ветви рендерят подзапрос В ТЕКСТЕ запроса и остаются одним
        # keyset-снимком: `(ts, id) > (:ts, anchor)` ≡
        # `ts > :ts OR (ts = :ts AND id > anchor)`.
        return or_(
            JoinRequest.submitted_at > after_submitted_at,
            and_(
                JoinRequest.submitted_at == after_submitted_at,
                JoinRequest.id > anchor_id,
            ),
        )

    @staticmethod
    def build_card_requests_page_statement(
        *,
        card_id: int,
        community_slug: str,
        statuses: tuple[JoinRequestStatus, ...],
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> Select[Any]:
        """AC-01, AC-04: страница заявившихся на карточку — в порядке очереди.

        `statuses` — параметр, а не константа внутри: режим карточки
        (`capacity=1` → pending+selected; `capacity=2..6` → только selected)
        решает сервис (AC-03/AC-09), репозиторий лишь применяет набор. Иначе
        правило FR70 жило бы в двух местах и разошлось бы при добавлении
        промежуточной вместимости.

        Срез сообщества — ПОДЗАПРОСОМ (`card_id IN (SELECT ...)`), а не JOIN'ом:
        это второй рубеж ADR-13 на случай, если сервис ошибётся с авторством.
        JOIN к `event_cards` уже есть по `card_id` — добавлять второй смысла нет,
        а `card_id` уже получен из карточки в срезе сообщества.

        JOIN'ы: `users` — INNER (FK `NOT NULL`), `profiles` — LEFT (анкеты может
        не быть: онбординг не пройден). `places` здесь НЕ нужен: список
        заявившихся не показывает место, а лишний JOIN удорожал бы страницу.

        `ORDER BY submitted_at ASC, id ASC` — ровно ключ
        `ix_join_requests_card_queue (card_id, submitted_at, id) WHERE
        status='pending'` (миграция `0012`): индекс уже под этот порядок.
        """
        statement = (
            select(
                JoinRequest,
                User.id.label("applicant_user_id"),
                User.uuid.label("applicant_uuid"),
                Profile.name.label("applicant_name"),
                Profile.age.label("applicant_age"),
            )
            .join(User, User.id == JoinRequest.applicant_id)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.status.in_(statuses),
                JoinRequest.card_id.in_(
                    select(EventCard.id).where(EventCard.community_slug == community_slug)
                ),
            )
        )
        after_predicate = JoinRequestRepository._after_predicate(after)
        if after_predicate is not None:
            statement = statement.where(after_predicate)
        return cast(
            "Select[Any]",
            statement.order_by(JoinRequest.submitted_at.asc(), JoinRequest.id.asc()).limit(limit),
        )

    @staticmethod
    def build_card_requests_count_statement(
        *, card_id: int, statuses: tuple[JoinRequestStatus, ...]
    ) -> Select[Any]:
        """AC-05, D-4.5-9: `total` — без курсора и без JOIN'ов к `users`/`profiles`.

        Курсор — позиция чтения, а не фильтр: `total` с курсором уменьшался бы
        от страницы к странице. JOIN'ы не нужны: считаются заявки по
        предикатам, а предикаты ссылаются только на `join_requests`.

        `count(JoinRequest.id)`: `id` NOT NULL, план тот же, `scalar_one()`
        даёт честный `int` для mypy-strict.
        """
        return (
            select(func.count(JoinRequest.id))
            .select_from(JoinRequest)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.status.in_(statuses),
            )
        )

    # ——— операции ———

    async def get_card_for_request(
        self, card_uuid: uuid_lib.UUID, community_slug: str
    ) -> EventCard | None:
        """AC-07: карточка сообщества или `None` (чужая == несуществующая)."""
        result = await self._session.execute(
            self.build_get_for_request_statement(card_uuid, community_slug)
        )
        # `scalar_one_or_none()` на `Select[Any]` даёт `Any`; явная переменная
        # возвращает тип для mypy-strict и делает приведение видимым.
        card: EventCard | None = result.scalar_one_or_none()
        return card

    async def count_active(self, applicant_id: int) -> int:
        """AC-03: число активных заявок заявителя (должно читаться под блокировкой)."""
        result = await self._session.execute(self.build_active_count_statement(applicant_id))
        return int(result.scalar_one())

    async def cancel_own_pending(
        self, request_uuid: uuid_lib.UUID, applicant_id: int, community_slug: str
    ) -> bool:
        """AC-01, AC-04, AC-05: CAS-отмена; `True` — ровно одна строка изменена.

        `rowcount` — единственный признак «мы действительно отменили». `False`
        означает одно из четырёх: заявки нет, она чужая, она чужого
        сообщества, она не `pending`. Различает их сервис отдельным чтением
        статуса (Task 2.4), и только на этом пути.
        """
        result = await self._session.execute(
            self.build_cancel_statement(request_uuid, applicant_id, community_slug)
        )
        # `CursorResult`, а не `Result`: `rowcount` есть только у курсора, а
        # `AsyncSession.execute` типизирован как `Result` — берём фактический
        # объект без изменения поведения (`UPDATE` всегда даёт курсор).
        return bool(cast("CursorResult[Any]", result).rowcount == 1)

    async def get_own_status(
        self, request_uuid: uuid_lib.UUID, applicant_id: int, community_slug: str
    ) -> str | None:
        """AC-04, AC-05: статус своей заявки или `None` (нет/чужая/чужое сообщество).

        `str`, а не `JoinRequestStatus`: значение приходит из БД и может быть
        чем угодно, если CHECK когда-нибудь разойдётся с enum. Сервис
        сравнивает со значениями enum явно, и неизвестное значение даёт
        `REQUEST_NOT_ACTIVE` (консервативно), а не `AttributeError` → `500`.
        """
        result = await self._session.execute(
            self.build_status_lookup_statement(request_uuid, applicant_id, community_slug)
        )
        status: str | None = result.scalar_one_or_none()
        return status

    async def list_my_requests(
        self,
        *,
        applicant_id: int,
        community_slug: str,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> list[Any]:
        """AC-03, AC-06, AC-07: строки страницы «моих заявок».

        Возвращаются `Row`, а не ORM-граф: у связей `lazy="raise"`, а
        `selectinload` четырёх связей ради трёх колонок дороже одного JOIN.
        Вызывающий собирает схемы позиционно, поэтому форма строки — часть
        внутреннего контракта метода (порядок колонок описан в билдере).
        """
        result = await self._session.execute(
            self.build_my_requests_page_statement(
                applicant_id=applicant_id,
                community_slug=community_slug,
                after=after,
                limit=limit,
            )
        )
        return list(result.all())

    async def list_card_requests(
        self,
        *,
        card_id: int,
        community_slug: str,
        statuses: tuple[JoinRequestStatus, ...],
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> list[Any]:
        """AC-01, AC-04: строки страницы заявившихся.

        `Row`, а не ORM-граф: у связей `lazy="raise"`, а `selectinload` ради
        трёх колонок дороже одного JOIN (тот же приём, что `list_my_requests`).
        Порядок колонок — часть внутреннего контракта, описан в билдере.
        """
        result = await self._session.execute(
            self.build_card_requests_page_statement(
                card_id=card_id,
                community_slug=community_slug,
                statuses=statuses,
                after=after,
                limit=limit,
            )
        )
        return list(result.all())

    async def count_card_requests(
        self, *, card_id: int, statuses: tuple[JoinRequestStatus, ...]
    ) -> int:
        """AC-05: `total` без курсора (листание его не меняет)."""
        result = await self._session.execute(
            self.build_card_requests_count_statement(card_id=card_id, statuses=statuses)
        )
        return int(result.scalar_one())

    async def count_my_requests(self, *, applicant_id: int, community_slug: str) -> int:
        """AC-06: `total` без курсора (листание его не меняет)."""
        result = await self._session.execute(
            self.build_my_requests_count_statement(
                applicant_id=applicant_id, community_slug=community_slug
            )
        )
        return int(result.scalar_one())

    async def get_card_for_join(
        self, card_uuid: uuid_lib.UUID, community_slug: str
    ) -> EventCard | None:
        """AC-11 (ADR-13, стори 5а.5): карточка для мгновенного присоединения.

        Тот же билдер, что у заявки (`build_get_for_request_statement`): срез
        сообщества обязан жить в SQL, а не в `if` после выборки, иначе чужая
        карточка отличалась бы от несуществующей по коду ответа. Отдельный
        метод, а не переиспользование `get_card_for_request`, — потому что
        имя читается на месте вызова: маршруты разные, и правка одного
        не должна молча менять другой.

        `FOR UPDATE` НЕТ: блокировку берёт сам `UPDATE` мест (CAS), а порядок
        ADR-09 (`users → cards`) соблюдается тем, что первая блокировка —
        строка актора (Task 3.1).
        """
        result = await self._session.execute(
            self.build_get_for_request_statement(card_uuid, community_slug)
        )
        card: EventCard | None = result.scalar_one_or_none()
        return card

    async def has_participation(self, *, card_id: int, applicant_id: int) -> bool:
        """AC-09 (FR83): занимает ли участник пару «карточка + заявитель».

        Предикат — `JOIN_REQUEST_OCCUPYING_STATUSES` (`pending`+`selected`), а не
        `JOIN_REQUEST_ACTIVE_STATUSES`: уникальный индекс
        `uq_join_requests_card_applicant_active` шире лимита FR105, и проверка
        по `pending` пропустила бы уже выбранного участника. Копия
        `('pending','selected')` строкой разошлась бы с моделью при первой
        правке предиката индекса.

        Зачем проверка ДО `UPDATE` мест: единственная гарантия уникальности —
        сам индекс, и он срабатывает на `INSERT`, то есть ПОСЛЕ инкремента.
        Без предварительного чтения повторный `POST` при свободных местах
        откатывался бы `SAVEPOINT`-ом (честно, но дорого), а при ПОЛНОЙ
        карточке (последнее место занял сам участник — штатный сценарий
        FR84) отдавал бы `409 CARD_IS_FULL` вместо `409 ALREADY_JOINED`:
        клиент показал бы «мест нет» человеку, который в этой компании уже
        состоит. Гонку двух одновременных первых `POST` этот `SELECT` не
        ловит и ловить не должен — её отбивает индекс (`IntegrityError`).
        """
        result = await self._session.execute(
            select(JoinRequest.id).where(
                JoinRequest.card_id == card_id,
                JoinRequest.applicant_id == applicant_id,
                JoinRequest.status.in_(JOIN_REQUEST_OCCUPYING_STATUSES),
            )
        )
        return result.first() is not None

    # ——— 5б.2: выбор автора (AC-01, AC-06, AC-09) ———

    @staticmethod
    def build_select_statement(*, request_uuid: uuid_lib.UUID, card_id: int) -> Any:
        """AC-01 стори 5б.2: CAS `pending -> selected` ОДНИМ `UPDATE`.

        Предикат `status='pending'` — граница домена: `cancelled`/`expired`
        отменяют заявку, `selected` уже выбрана. Предварительного `SELECT`
        НЕТ (§18.3): он пропускает две параллельные попытки, и вторая
        переписала бы решение автора.

        `card_id` в предикате, а не в `if` после выборки: заявка на другую
        карточку обязана быть неотличима от несуществующей (ADR-13), и
        проверка после выборки забывается при первом рефакторинге.

        `decided_at` заполняется здесь же: заявка выходит из `pending`, и
        момент решения обязан быть (D-5a.1-6). `CHECK` на это намеренно нет,
        поэтому единственный рубеж — этот `SET`.

        `RETURNING id` — признак «строка изменена» для сервиса: `rowcount`
        у `UPDATE ... RETURNING` доступен, но `RETURNING` читается явно и
        работает одинаково на обоих диалектах тестов.
        """
        return (
            update(JoinRequest)
            .where(
                JoinRequest.uuid == request_uuid,
                JoinRequest.card_id == card_id,
                JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
            .values(status=JoinRequestStatus.SELECTED.value, decided_at=func.now())
            .returning(JoinRequest.id)
        )

    @staticmethod
    def build_reject_pending_statement(*, card_id: int, except_id: int) -> Any:
        """AC-01 стори 5б.2 (FR81): все остальные `pending` → `not_selected`.

        ОДИН `UPDATE` на всю пачку, а не цикл: заявившихся может быть много,
        и построчный цикл держал бы блокировки дольше, чем нужно (NFR47).
        `except_id` — идентификатор выбранной заявки: она уже `selected`, и
        предикат `id <> :except` защищает от случайного понижения. Дублировать
        `status='pending'` в предикате НЕЛЬЗЯ убирать: без него повторный
        вызов перевёл бы в `not_selected` и `cancelled`/`expired` (историю).

        `RETURNING` НЕТ намеренно: число отклонённых не выходит наружу —
        ни в ответ, ни в лог. Клиент не должен уметь отличить «отклонили
        одного» от «отклонили пятерых» (FR81).

        `decided_at` заполняется: заявка выходит из `pending`.
        """
        return (
            update(JoinRequest)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.status == JoinRequestStatus.PENDING.value,
                JoinRequest.id != except_id,
            )
            .values(status=JoinRequestStatus.NOT_SELECTED.value, decided_at=func.now())
        )

    @staticmethod
    def build_viewer_participation_statement(
        *, applicant_id: int, card_ids: list[int]
    ) -> Select[Any]:
        """AC-09 стори 5б.2 (D-5b.2-7, FR104): в каких карточках зритель `selected`.

        Пакетный запрос на страницу (`card_ids IN (...)`) — N+1 недопустим:
        20 карточек ленты дали бы 20 дополнительных `SELECT`. Тот же приём, что
        у подписи фото в 5а.3.

        Предикат `status='selected'`, а не `JOIN_REQUEST_OCCUPYING_STATUSES`:
        «участие» — это состоявшееся решение автора, а `pending`-заявка места
        не занимает и кнопку «Отменить участие» показывать не должна.

        Возвращает только `card_id` — наружу уходит булев признак, не `uuid`
        заявки: он позволил бы клиенту читать состояние своей роли в обход
        `/my/requests` (FR81).
        """
        return select(JoinRequest.card_id).where(
            JoinRequest.applicant_id == applicant_id,
            JoinRequest.status == JoinRequestStatus.SELECTED.value,
            JoinRequest.card_id.in_(card_ids),
        )

    @staticmethod
    def build_get_card_for_select_statement(
        request_uuid: uuid_lib.UUID, community_slug: str
    ) -> Any:
        """AC-01, AC-04 стори 5б.2: карточка и `id` заявки — по `uuid` заявки.

        На входе `select` есть только `request_uuid` (он в пути), а решение
        принимается по КАРТОЧКЕ: её статус, окно, вместимость и автор. Поэтому
        один запрос читает обе строки: `EventCard` + `JoinRequest.id` (нужен для
        `except_id` в reject-шаге).

        `community_slug` — в SQL, а не в `if` после выборки (ADR-13): карточка
        чужого сообщества обязана быть неотличима от несуществующей заявки,
        иначе перебором `uuid` можно было бы узнать о чужих карточках.

        `FOR UPDATE` НЕТ: карточку блокирует её собственный `UPDATE` мест — это
        и есть точка сериализации (D-4.4-3), а `SELECT ... FOR UPDATE` здесь
        дал бы вторую блокировку раньше времени и удлинил бы транзакцию.
        """
        return (
            select(
                EventCard,
                JoinRequest.id.label("request_id"),
                JoinRequest.status.label("request_status"),
            )
            .join(JoinRequest, JoinRequest.card_id == EventCard.id)
            .where(
                JoinRequest.uuid == request_uuid,
                EventCard.community_slug == community_slug,
            )
        )

    @staticmethod
    def build_applicant_statement(
        request_uuid: uuid_lib.UUID,
    ) -> Select[uuid_lib.UUID, int]:
        """9.2, Task 4.1: `(users.uuid, users.id)` заявителя по `uuid` заявки.

        `JOIN users` в одном запросе: двухшаговый вариант («заявка → id →
        uuid») стоил бы двух round-trip на горячем пути выбора и открыл бы
        окно между шагами, в котором заявку могли отменить.

        Возвращаются ОБА ключа: `uuid` адресует канал `ws:user:{uuid}`,
        `id` — внешний ключ строки `notifications` (bigint). Два отдельных
        `SELECT` ради одного значения каждый — лишний round-trip без выгоды.
        """
        return (
            select(User.uuid, User.id)
            .join(JoinRequest, JoinRequest.applicant_id == User.id)
            .where(JoinRequest.uuid == request_uuid)
        )

    @staticmethod
    def build_selected_applicant_statement(*, card_id: int) -> Select[uuid_lib.UUID, int]:
        """9.2, Task 4.3: `(users.uuid, users.id)` ВЫБРАННОГО заявителя карточки.

        Нужен воркеру и ленивому закрытию: `DecisionResult` ядра отдаёт только
        `card_uuid`/`outcome`/`chat_uuid`, а менять его нельзя — ядро
        `close_due_card` зовут три владельца транзакции, и лишнее поле
        в результате сдвинуло бы контракт всех трёх (Task 4.3 стори).

        Предикат `status='selected'` — единственная строка: `select_request`
        переводит `pending -> selected` одного, остальные уходят в
        `not_selected` в том же SAVEPOINT. Читается ПОСЛЕ `commit`, когда
        решение уже видно другим соединениям.
        """
        return (
            select(User.uuid, User.id)
            .join(JoinRequest, JoinRequest.applicant_id == User.id)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.status == JoinRequestStatus.SELECTED.value,
            )
        )

    async def get_selected_applicant(self, *, card_id: int) -> tuple[uuid_lib.UUID, int] | None:
        """9.2, Task 4.3: адресат `request.selected` после автовыбора.

        Отдельный `SELECT` после `commit` — цена неизменности ядра
        (см. `build_selected_applicant_statement`). Один запрос на карточку,
        у которой автовыбор СОСТОЯЛСЯ: при `SKIPPED`/`CLOSED_NO_CHOICE`
        метод не вызывается вовсе.
        """
        result = await self._session.execute(
            self.build_selected_applicant_statement(card_id=card_id)
        )
        row = result.first()
        return None if row is None else (uuid_lib.UUID(str(row[0])), int(row[1]))

    @staticmethod
    def build_request_exists_statement(request_uuid: uuid_lib.UUID) -> Select[Any]:
        """AC-04: существует ли заявка ВООБЩЕ — только для различения `404`.

        Нужен ТОЛЬКО когда `build_get_card_for_select_statement` не нашёл
        строки: иначе `404 request_not_found` (заявки нет) и
        `404 card_not_found` (заявка есть, карточка чужого сообщества) были бы
        одним кодом, и клиент не отличил бы опечатку в `uuid` от чужой карточки.
        Возвращается `id`, а не `status`: статус здесь не нужен и его чтение
        расширило бы ответ.
        """
        return select(JoinRequest.id).where(JoinRequest.uuid == request_uuid)

    async def get_card_for_select(
        self, request_uuid: uuid_lib.UUID, community_slug: str
    ) -> tuple[EventCard, int, str] | None:
        """AC-01, AC-04, AC-05: карточка, `id` и СТАТУС заявки или `None`.

        Статус читается здесь же, а не отдельным запросом: он нужен ДО
        проверок карточки (AC-05 требует `REQUEST_NOT_PENDING` на повтор
        выбора, когда карточка уже `full`), и второй `SELECT` на том же
        предикате стоил бы лишнего round-trip на горячем пути.
        """
        result = await self._session.execute(
            self.build_get_card_for_select_statement(request_uuid, community_slug)
        )
        row = result.first()
        if row is None:
            return None
        # Индексация, а не `row.t`: `Row.t` объявлен deprecated в SQLAlchemy 2.0.19.
        card: EventCard = row[0]
        return card, int(row[1]), str(row[2])

    async def get_applicant(
        self, *, request_uuid: uuid_lib.UUID
    ) -> tuple[uuid_lib.UUID, int] | None:
        """9.2, Task 4.1: `(публичный uuid, внутренний id)` заявителя.

        **Почему отдельный запрос, а не поле в `get_card_for_select`.**
        Тот кортеж уже несёт карточку, `id` и статус, и добавление ещё двух
        элементов переписало бы распаковку во всех вызывающих ради значений,
        нужных ОДНОМУ пути из трёх. Здесь адресат уведомления получается явно
        и ровно там, где он нужен.

        **Почему вызывается ДО `commit`** (Task 4.1): после `commit` между
        чтением и вставкой уведомления открылось бы окно, в котором заявку
        могли отменить/удалить, и уведомление ушло бы адресату, которого уже
        нет в составе.

        **Почему оба ключа одним запросом.** `uuid` — адрес канала
        (`ws:user:{uuid}`), `id` — внешний ключ строки `notifications`.
        Два отдельных `SELECT` на горячем пути выбора дали бы лишний
        round-trip и окно между ними.
        """
        result = await self._session.execute(self.build_applicant_statement(request_uuid))
        row = result.first()
        return None if row is None else (uuid_lib.UUID(str(row[0])), int(row[1]))

    async def request_exists(self, request_uuid: uuid_lib.UUID) -> bool:
        """AC-04: есть ли заявка с таким `uuid` в любом сообществе."""
        result = await self._session.execute(self.build_request_exists_statement(request_uuid))
        return result.first() is not None

    async def select_request(self, *, request_uuid: uuid_lib.UUID, card_id: int) -> int | None:
        """AC-01: CAS выбора; `None` — заявка не `pending` или чужая карточка."""
        result = await self._session.execute(
            self.build_select_statement(request_uuid=request_uuid, card_id=card_id)
        )
        selected_id: int | None = result.scalar_one_or_none()
        return selected_id

    async def reject_pending(self, *, card_id: int, except_id: int) -> int:
        """AC-01 (FR81): переводит остальные `pending` в `not_selected`.

        Возвращает число изменённых строк ТОЛЬКО для логов/тестов: наружу оно
        не выходит (FR81), поэтому в сервисе значение не используется для кода
        ответа.
        """
        result = await self._session.execute(
            self.build_reject_pending_statement(card_id=card_id, except_id=except_id)
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def viewer_participation_card_ids(
        self, *, applicant_id: int, card_ids: list[int]
    ) -> set[int]:
        """AC-09: подмножество `card_ids`, где зритель в составе (`selected`)."""
        if not card_ids:
            return set()
        result = await self._session.execute(
            self.build_viewer_participation_statement(applicant_id=applicant_id, card_ids=card_ids)
        )
        return {int(row[0]) for row in result.all()}

    # ——— 5б.3: автовыбор первого по истечении окна (AC-01, AC-05) ———

    @staticmethod
    def build_first_pending_statement(*, card_id: int) -> Any:
        """AC-01 стори 5б.3 (FR80, ADR-05): первая `pending` заявка карточки.

        `ORDER BY submitted_at ASC, id ASC` — порядок ADR-05 дословно. Второй
        ключ `id` обязателен: `submitted_at` у двух заявок может совпасть
        (одна секунда, батч-импорт), и без него выбор зависел бы от
        физического порядка строк — тот же класс дефекта, что в CR 5а.6,
        патч 1 (недетерминированный `first()`).

        `LIMIT 1` — ядру нужна ровно одна заявка; `N-1` отклоняет
        `reject_pending` одним `UPDATE` (5б.2), а не этот запрос.

        `FOR UPDATE SKIP LOCKED` ЗДЕСЬ НЕТ намеренно (D-5b.3-2, OQ-5b.3-1):
        блокировка заявки не сериализует выбор — два процесса всё равно
        прошли бы `SELECT` и оба заняли место, если бы не строка автора.
        Порядок блокировок `users -> event_cards -> join_requests` требует,
        чтобы заявка читалась УЖЕ после `lock_for_update(author_id)`.

        Предикат `status='pending'` совпадает с предикатом `reject_pending`
        по смыслу: выбранная заявка не попадёт в отклонённые (`except_id`),
        а `cancelled`/`expired`/`not_selected` не участвуют в выборе.

        Индекс `ix_join_requests_card_queue (card_id, submitted_at, id)
        WHERE status='pending'` — ровно ключ этого запроса; миграция не
        нужна (D-5b.3-7).
        """
        return (
            select(JoinRequest.uuid, JoinRequest.id, JoinRequest.applicant_id)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
            .order_by(JoinRequest.submitted_at, JoinRequest.id)
            .limit(1)
        )

    async def first_pending(self, *, card_id: int) -> tuple[uuid_lib.UUID, int, int] | None:
        """AC-01: `(uuid, id, applicant_id)` первой `pending` заявки или `None`.

        `uuid` возвращается вместе с `id`: `select_request` (5б.2) адресует
        заявку снаружи только по `uuid` (ADR-13), а `reject_pending` берёт
        `except_id` — внутренний ключ. Второй `SELECT` ради одного из двух
        ключей был бы лишним запросом на горячем пути воркера.

        `applicant_id` наружу из ядра НЕ выходит (AC-08, FR81): он нужен только
        для проверки состава в тестах через отдельный запрос, а в structlog
        и ответы попадает лишь `card_uuid`.
        """
        result = await self._session.execute(self.build_first_pending_statement(card_id=card_id))
        row = result.first()
        return None if row is None else (row[0], int(row[1]), int(row[2]))

    # ——— 6.1: гард выхода из режима заявок (AC-08, OQ-5b.3-3) ———

    @staticmethod
    def build_has_pending_statement(*, card_id: int) -> Any:
        """Стори 6.1 (AC-08): есть ли на карточке хотя бы одна `pending`-заявка.

        Нужен гарду `PATCH capacity 1 → 2..6`: выход из режима заявок при
        живом заявителе превратил бы его `pending` в заявку на карточку,
        которая уже не собирает заявки (OQ-5b.3-3, вариант «б»).

        CR 6.1 (находки 5, 6): именно `EXISTS`, а не `SELECT id ... LIMIT 1` —
        сервису нужно только «есть/нет», и `EXISTS` останавливается на первой
        строке, не материализуя проекцию. Путь доступа — предикат
        `status='pending'`; из существующих индексов его поддерживает
        `ix_join_requests_card_queue (card_id, submitted_at, id)
        WHERE status='pending'` (ведущая колонка `card_id`).
        `ix_join_requests_applicant_pending` здесь НЕ применим: его ведущая
        колонка — `applicant_id`, а фильтр идёт по `card_id`.

        `FOR UPDATE` НЕТ намеренно: это чистая проверка, а не CAS. Проигранная
        гонка (заявку подали между проверкой и `commit`) даёт худший случай
        «карточка вышла из режима заявок, имея одну `pending`», что не теряет
        место: `filled_count` не меняется, а заявка остаётся видимой автору.

        Предикат — `JoinRequestStatus.PENDING`, не литерал: переименование
        значения обязано менять и запрос.
        """
        return select(
            exists().where(
                JoinRequest.card_id == card_id,
                JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
        )

    async def has_pending_for_card(self, *, card_id: int) -> bool:
        """AC-08: `True`, если на карточке есть `pending`-заявка."""
        result = await self._session.execute(self.build_has_pending_statement(card_id=card_id))
        return bool(result.scalar_one())

    # ——— 5а.6: отмена участия (AC-01, AC-05…AC-07, AC-09) ———

    @staticmethod
    def build_cancel_participation_statement(*, card_id: int, applicant_id: int) -> Any:
        """AC-01, AC-05 (FR104): CAS `selected -> cancelled` одним `UPDATE`.

        Предикат `status='selected'` — граница ДОМЕНА (D-5a.6-5): `pending`-
        заявка маршрутом участия не отменяется, иначе клиент терял бы место
        в очереди, считая, что вышел из компании. `not_selected`/`expired`
        тоже не трогаются — они терминальные, и переписывать их значило бы
        подменять историю решения (NFR1–NFR4).

        Почему `UPDATE ... WHERE status='selected'`, а не «`SELECT` → проверить
        → `UPDATE`» (§18.3): предварительный `SELECT` пропускает две
        параллельные отмены, и вторая переписала бы `decided_at` поверх
        первой — аудит потерял бы момент фактического выхода. Здесь вторая
        транзакция не находит `selected` (rowcount 0), и сервис различает
        идемпотентный повтор по перечитанному статусу.

        `decided_at` заполняется здесь же: участие выходит из `selected`,
        и момент решения обязан быть (`D-5a.1-6`).

        `card_id` в предикате — внутренний ключ, а не `uuid`: карточка уже
        прочитана сервисом со срезом сообщества (ADR-13), и второй срез
        здесь разошёлся бы с первым при правке.
        """
        return (
            update(JoinRequest)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.applicant_id == applicant_id,
                JoinRequest.status == JoinRequestStatus.SELECTED,
            )
            .values(status=JoinRequestStatus.CANCELLED, decided_at=func.now())
        )

    @staticmethod
    def build_participation_status_statement(*, card_id: int, applicant_id: int) -> Select[Any]:
        """AC-05, AC-06 (FR104): статус участия — только для диагностики отказа.

        Вызывается ТОЛЬКО когда CAS не сработал (rowcount 0): читать статус
        перед записью незачем, а на отказе без него не отличить `cancelled`
        (идемпотентный успех, AC-06) от `pending` (чужая заявка, AC-07 —
        участие не найдено) и от отсутствия строки (AC-05, `404`).

        Предикаты те же два, что у CAS: иначе диагностика отвечала бы про
        строку, которую CAS не мог тронуть, и `404` подменялся бы `204`
        на чужом участии.

        CR 5а.6, патч 1 (AC-06, дефект): у пары «карточка + заявитель»
        `uq_join_requests_card_applicant_active` ЧАСТИЧНЫЙ (`pending`+
        `selected`), поэтому ТЕРМИНАЛЬНЫХ строк может быть несколько:
        `expired` после истечения окна, затем `cancelled` после отмены
        участия. Без `ORDER BY` `first()` возвращал произвольную строку, и
        ответ зависел от физического порядка: `expired` первым → `404`
        (неверно, AC-06 требует идемпотентный `204`), `cancelled` первым →
        `204`. Доказано исполнением на живой PostgreSQL 18: одна и та же
        пара дала `404` и `204` при разном порядке вставки.

        Порядок: `cancelled` в приоритете (состояние «участия нет» уже
        достигнуто — повтор обязан быть успехом), затем `id DESC` — свежая
        строка. Детерминизм важен и для ветки `pending`/`not_selected`:
        ответ не должен зависеть от `VACUUM`/`seq scan`.
        """
        return (
            select(JoinRequest.status)
            .where(
                JoinRequest.card_id == card_id,
                JoinRequest.applicant_id == applicant_id,
            )
            .order_by(
                case(
                    (JoinRequest.status == JoinRequestStatus.CANCELLED, 0),
                    else_=1,
                ),
                JoinRequest.id.desc(),
            )
            .limit(1)
        )

    async def cancel_participation(self, *, card_id: int, applicant_id: int) -> int:
        """AC-01, AC-05: выполняет CAS отмены участия и возвращает `rowcount`.

        `0` означает, что предикат не совпал: участия нет, оно уже `cancelled`
        или это `pending`-заявка. Решение о коде ошибки принимает сервис по
        ПЕРЕЧИТАННОМУ статусу — репозиторий не знает доменных кодов
        (project-context §6).
        """
        statement = self.build_cancel_participation_statement(
            card_id=card_id, applicant_id=applicant_id
        )
        result = await self._session.execute(statement.execution_options(synchronize_session=False))
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def get_participation_status(self, *, card_id: int, applicant_id: int) -> str | None:
        """AC-05, AC-06: статус пары «карточка + участник» или `None`.

        Возвращает `str`, а не `JoinRequestStatus`: «старое» значение из БД
        не должно превращаться в `ValueError` → `500` на диагностике отказа
        (класс дефекта `X-5a.3-3`). Неизвестный статус сервис трактует как
        «участия нет» — консервативный `404` честнее `500` на данных, которых
        быть не должно.
        """
        result = await self._session.execute(
            self.build_participation_status_statement(card_id=card_id, applicant_id=applicant_id)
        )
        row = result.first()
        return None if row is None else str(row[0])

    async def create_participation(
        self,
        *,
        card_id: int,
        applicant_id: int,
        now: datetime,
        expires_at: Any,
    ) -> JoinRequest:
        """AC-01, AC-09 (D-5а.5-1): вставка УЧАСТИЯ открытого набора.

        `status='selected'` сразу: читатель состава 5а.4 фильтрует именно по
        `selected`, поэтому участие не требует второго `UPDATE` и не проходит
        через `pending`. `message=''` легален — CHECK держит только
        «обрезанность» (D-5a.1-3), а не пустоту.

        `decided_at=now` заполняется сервисом (OQ-5a.1-1): участие — это уже
        решение, и в `selected` оно обязано быть заполнено так же, как после
        выбора автора в 5б. `expires_at` — копия окна карточки (D-5a.1-5).

        `flush()` без `commit`: единица работы у сервиса (D-4), а `flush`
        нужен, чтобы `IntegrityError` гонки пары возник ЗДЕСЬ, где сервис
        распознаёт имя ограничения и превращает его в `409 ALREADY_JOINED`.
        """
        participation = JoinRequest(
            card_id=card_id,
            applicant_id=applicant_id,
            message="",
            status=JoinRequestStatus.SELECTED,
            decided_at=now,
            expires_at=expires_at,
        )
        self._session.add(participation)
        await self._session.flush()
        return participation

    async def create(
        self,
        *,
        card_id: int,
        applicant_id: int,
        message: str,
        expires_at: Any,
    ) -> JoinRequest:
        """AC-02, AC-06: вставка заявки.

        `flush()` без `commit`: единица работы — на сервисе (D-4). `flush`
        нужен, чтобы `IntegrityError` гонки возник ЗДЕСЬ, где его ловит
        сервис с распознаванием имени ограничения, а не на `commit` снаружи.
        """
        request = JoinRequest(
            card_id=card_id,
            applicant_id=applicant_id,
            message=message,
            expires_at=expires_at,
        )
        self._session.add(request)
        await self._session.flush()
        return request
