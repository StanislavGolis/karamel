"""Модель заявки на участие `join_requests` (стори 5а.1; AC-01…AC-07).

AC-01 (FR76): заявка на карточку в режиме «заявки» (вместимость 1, 4.1)
несёт сообщение заявителя и живёт в одном из состояний `JoinRequestStatus`.

D-2: `id` — внутренний bigint, наружу не выходит никогда; публичный
идентификатор — `uuid` (адресация только по нему).

D-5a.1-1 (AC-04, FR82): уникальность пары «карточка + заявитель» —
**частичная**, только по `JOIN_REQUEST_OCCUPYING_STATUSES`. Безусловный
`UNIQUE` запретил бы повторную подачу после отмены (FR82), а `selected`
обязан оставаться под уникальностью — двух активных ролей на одной карточке
быть не может.

D-5a.1-2 (AC-06, ADR-13): запрет заявки на СВОЮ карточку здесь не выражен
намеренно. Правило межтабличное (`join_requests.card_id →
 event_cards.author_id`), `CHECK` такого не выражает, а триггер запрещён D-5.
Проверка — сервисный гард 5а.2, доменный код ошибки заведён в 5а.1
(`services/join_request_service.py`).

D-5a.1-3 (AC-03): CHECK на `message` держит только «обрезанность»
(`message = btrim(message, <пробельные>)`), но НЕ пустоту. Пустоту отбивает
схема входа 5а.2 (`min_length=1` после `normalize_text`) — как `title`
у `EventCardCreate` (D-4.1-1). CHECK на пустоту дал бы `500` на вставке
из воркера без пользы для контракта.

OQ-5a.1-1 (решение DS): `CHECK (decided_at IS NULL OR status <> 'pending')`
НЕ ставится — он дал бы `500` на легальном переходе при забытом поле
(класс дефекта X-39). Заполнение `decided_at` — обязанность сервиса.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import JOIN_REQUEST_VISIBLE_STATUSES, JoinRequestStatus


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений StrEnum.

    AC-02, D-3: список не дублируется строкой ни здесь, ни в миграции —
    иначе добавление значения в `JoinRequestStatus` не изменило бы
    ограничение в БД, и статус, прошедший Pydantic, отвергался бы PostgreSQL.

    Локальная копия (`user.py`, `place.py`, `event_card.py`, здесь) —
    существующий долг X-12 в `deferred-work.md` (Task 2.5). Приватные имена
    из `place.py`/`event_card.py` не импортируются сознательно.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


# AC-03: `btrim(x)` без второго аргумента режет ТОЛЬКО пробел U+0020.
# Набор «пробельных» задан явно — тот же, что у `places.name` и
# `event_cards.title` (CR 3.1, патч 8). NBSP (U+00A0) и узкий NBSP (U+202F)
# входят в набор: без них `message = '\u00a0'` проходил бы CHECK и попадал
# в очередь автора визуально пустым.
_WHITESPACE = "E' \\t\\r\\n\\f\\v\u00a0\u202f'"


def _trimmed(column: str) -> str:
    """`btrim(col, <пробельные>)` — единая запись для строковых CHECK."""
    return f"btrim({column}, {_WHITESPACE})"


class JoinRequest(TimestampMixin, Base):
    """Заявка на участие в карточке режима «заявки» (AC-01).

    Жизненный цикл (`JoinRequestStatus`): `pending` → `selected` |
    `not_selected` | `expired` | `cancelled`. Переходы реализуют 5а.2–5а.6;
    здесь только набор значений и инварианты уровня БД.

    `expires_at` — копия `event_cards.expires_at` на момент подачи
    (D-5a.1-5): автовыбор 5б сравнивает окно без `JOIN`, а перенос окна
    карточки не «оживляет» уже просроченные заявки.
    """

    __tablename__ = "join_requests"

    # AC-01, D-2: внутренний PK, наружу не выходит.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # AC-01: публичный идентификатор. `default` в Python нужен, чтобы объект
    # был готов до flush; `server_default` — для вставок вне ORM.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid_lib.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # AC-01: карточка, на которую подана заявка. `ON DELETE CASCADE` —
    # удаление карточки уносит её заявки: заявка без карточки не имеет
    # смысла ни для автовыбора (FR77), ни для списка заявившихся (5а.4).
    card_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("event_cards.id", ondelete="CASCADE"), nullable=False
    )
    # AC-01: заявитель. `ON DELETE CASCADE` — удаление учётки уносит её заявки
    # (обработка ПД, NFR1–NFR4): анонимных заявок не бывает.
    applicant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # AC-03, FR76: сообщение заявителя. Предел 300 — заявка читается автором
    # в списке заявившихся (5а.4) без раскрытия. Обрезка по краям — CHECK.
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    # AC-02: состояние заявки.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=JoinRequestStatus.PENDING,
        server_default=text("'pending'"),
    )
    # AC-01: момент подачи. Отдельно от `created_at`: пара нужна индексу
    # очереди автовыбора (`ix_join_requests_card_queue`), и `submitted_at`
    # может быть переставлен при повторной подаче без переписывания аудита.
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # AC-01, D-5a.1-6: момент решения. NULL, пока заявка `pending`;
    # заполняется сервисом при любом выходе из `pending` (5а.3/5б).
    # CHECK на это НЕ ставится (OQ-5a.1-1): забытое поле дало бы `500`
    # на легальном переходе.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AC-01, D-5a.1-5: копия окна решения карточки на момент подачи.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # AC-01: `PrimaryKeyConstraint` объявлен явно, чтобы имя строилось по
        # convention (`pk_join_requests`) и совпадало с миграцией `0012`.
        PrimaryKeyConstraint("id"),
        # AC-01: публичный идентификатор уникален — адресация им однозначна.
        # Имя ПОЛНОЕ: у конвенции `uq` нет токена `%(constraint_name)s`,
        # поэтому короткое `name="uuid"` дало бы `uuid` без префикса
        # и разошлось бы с миграцией (дефект 3.1).
        UniqueConstraint("uuid", name="uq_join_requests_uuid"),
        # AC-02: состояния — только значения `JoinRequestStatus`.
        CheckConstraint(_in_clause("status", tuple(JoinRequestStatus)), name="status"),
        # AC-03: сообщение без пробельных по краям. РАВЕНСТВО, а не `<> ''`:
        # `<> ''` отбивал бы только пустоту, и `' привет'`/`'привет'`
        # сосуществовали бы (класс дефекта `places.name`, CR 3.1, патч 8).
        CheckConstraint(f"message = {_trimmed('message')}", name="message"),
        # AC-04, FR77/NFR29: одна АКТИВНАЯ заявка на пару «карточка +
        # заявитель». Частичный — только по статусам, занимающим пару.
        # Терминальные статусы в предикат не входят: иначе повторная подача
        # после отмены (FR82) была бы запрещена безусловным `UNIQUE`.
        # Имя полное — индексы в naming convention не участвуют.
        Index(
            "uq_join_requests_card_applicant_active",
            "card_id",
            "applicant_id",
            unique=True,
            postgresql_where=text("status IN ('pending','selected')"),
        ),
        # AC-05, FR77: подсчёт лимита «не больше двух активных» и «мои заявки».
        # Частичный по `pending`: терминальные заявки историю не нагружают.
        Index(
            "ix_join_requests_applicant_pending",
            "applicant_id",
            postgresql_where=text("status = 'pending'"),
        ),
        # Стори 6.1 (AC-06, долг X-5a.3-1): страница «мои заявки» (5а.3).
        # Предикат — четыре ВИДИМЫХ заявителю статуса; `not_selected` исключён
        # (FR81: он никогда не отдаётся). Порядок колонок — ровно ключ
        # курсора `(submitted_at, uuid)`: `submitted_at` без `uuid` не
        # разрешает равенство меток, и `WHERE (submitted_at, uuid) >` терял
        # бы строки между страницами. Частичный `WHERE status='pending'`
        # (выше) под `status IN (...)` неприменим — отсюда отдельный индекс.
        Index(
            "ix_join_requests_applicant_visible",
            "applicant_id",
            "submitted_at",
            "uuid",
            # CR 6.1 (находка 7): предикат собирается из доменной константы
            # `JOIN_REQUEST_VISIBLE_STATUSES` (четыре видимых заявителю
            # статуса, без `not_selected` — FR81), а не пишется строкой.
            postgresql_where=text(
                "status IN ("
                + ",".join(f"'{status.value}'" for status in JOIN_REQUEST_VISIBLE_STATUSES)
                + ")"
            ),
        ),
        # AC-05, ADR-05: очередь автовыбора (FR77) и список заявившихся (5а.4).
        # Порядок колонок — ровно ключ сортировки `submitted_at ASC, id ASC`.
        # Одного `submitted_at` недостаточно: при равных метках порядок не
        # определён, и курсор `WHERE (submitted_at, id) >` терял бы или
        # дублировал строки между страницами. `id`, а не `uuid`: очередь
        # внутренняя, наружу не отдаётся, а сортировка по `uuid` дороже.
        Index(
            "ix_join_requests_card_queue",
            "card_id",
            "submitted_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )
