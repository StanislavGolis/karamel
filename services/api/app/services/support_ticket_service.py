"""Сервис обращения в поддержку (стори 9.5; AC-01…AC-03; FR55–FR56).

**Почему нормализация повторяется здесь, хотя она есть в схеме** (Task 3.5).
Схема — единственный вход для HTTP-маршрута, но сервис вызывается и напрямую
(тесты, будущий потребитель 9.6). Если бы сервис полагался на схему, вызов
мимо неё записал бы `'   '` в `body` — а `CHECK body_not_blank` отверг бы
вставку `DBAPIError`-ом, то есть `500` вместо `422`. Нормализация в сервисе
делает правило независимым от транспорта.

**Почему `Total`-запрос отдельный, а не `count(*) OVER ()` над страницей.**
Оконная функция посчитала бы строки ТОЛЬКО после `LIMIT`-фильтра (в
PostgreSQL `LIMIT` применяется после оконной функции, но keyset-предикат
сужает выборку), и `total` уменьшался бы со второй страницы. Один `SELECT`
с двумя `count` был бы дороже: `count(*)` по индексу без выборки строк дешевле.

**Курсор — общий кодек `reports_queue_cursor`** (Task 3.4): пара
`(datetime, uuid)` — ровно наш ключ `(created_at, id)`. Второй кодек дал бы
второй формат курсора на два экрана одного UI (долг `X-44`-класса, D-9.5-8).
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.support_tickets import SupportTicketsRepository
from app.services.profile_normalize import normalize_bio
from app.services.reports_queue_cursor import (
    INVALID_CURSOR_CODE,
    InvalidCursorError,
    decode_queue_cursor,
    encode_queue_cursor,
)

__all__ = [
    "INVALID_CURSOR_CODE",
    "InvalidCursorError",
    "SupportTicketEntry",
    "SupportTicketsPage",
    "TicketBodyRequiredError",
    "create_ticket",
    "list_tickets",
]


class TicketBodyRequiredError(Exception):
    """AC-04: тело обращения пустое после нормализации → `422 body_required`.

    Отдельный класс, а не `ValueError`: `ValueError` в проекте ловят широко,
    и `422` получил бы любой сбой внутри сервиса.
    """

    code = "body_required"


@dataclass(frozen=True, slots=True)
class SupportTicketEntry:
    """AC-01…AC-03: один элемент списка (без ORM-объекта в роутере).

    `dict` из репозитория превращается здесь, а не в роутере: роутер не
    должен знать имена колонок — иначе SQL-деталь протекла бы в HTTP-слой
    (project-context §6).
    """

    #: CR 9.5 LOW-5: конкретные типы вместо `Any`. `Any` не защищал от
    #: подмены: опечатка в имени поля дала бы `AttributeError` в роутере, а
    #: не ошибку `mypy`. `uuid_lib.UUID`/`datetime` — то, что реально
    #: возвращает `RETURNING` через asyncpg.
    uuid: uuid_lib.UUID
    body: str
    status: str
    created_at: datetime
    answer: str | None
    answered_at: datetime | None


@dataclass(frozen=True, slots=True)
class SupportTicketsPage:
    """AC-02: страница `{items, total, next_cursor}`."""

    items: list[SupportTicketEntry]
    total: int
    next_cursor: str | None


async def create_ticket(session: AsyncSession, *, user_id: int, body: str) -> SupportTicketEntry:
    """AC-01: создаёт обращение в статусе `open`.

    Нормализация — повторно (Task 3.5): защита от вызова мимо схемы.
    Пустое тело — `TicketBodyRequiredError`, а не `INSERT` с пустой строкой:
    `CHECK body_not_blank` отверг бы её `DBAPIError`-ом (`500`).

    `answer`/`answered_at` у нового обращения — `None` по построению
    (`status='open'`, `CHECK ck_support_tickets_answer_pair`), поэтому
    заполняются явно, а не читаются из БД: второго запроса не нужно (P-8).
    """
    normalized = normalize_bio(body)
    if not normalized:
        raise TicketBodyRequiredError

    row = await SupportTicketsRepository.create(session, user_id=user_id, body=normalized)
    return SupportTicketEntry(
        uuid=row["uuid"],
        body=row["body"],
        status=row["status"],
        created_at=row["created_at"],
        answer=row["answer"],
        answered_at=row["answered_at"],
    )


async def list_tickets(
    session: AsyncSession, *, user_id: int, limit: int, cursor: str | None = None
) -> SupportTicketsPage:
    """AC-02, AC-03: страница СВОИХ обращений, новые сверху.

    Битый курсор → `InvalidCursorError` (`422 invalid_cursor`): ошибка ввода,
    а не `500` — курсор непрозрачен и приходит от сервера.

    `limit + 1` строк читается затем, чтобы отличить «страница последняя» от
    «есть ещё» без второго запроса: лишняя строка отбрасывается, а её
    отсутствие даёт `next_cursor = None` (D-9.5-8).
    """
    # `cursor is not None`, а не `if cursor`: пустая строка — ЯВНЫЙ мусор
    # клиента, и она обязана дать `422 invalid_cursor`, а не «курсора нет».
    # Приём `moderation_reports_service` — одна форма ответа на один вход.
    position = decode_queue_cursor(cursor) if cursor is not None else None
    rows = await SupportTicketsRepository.list_by_user(
        session, user_id=user_id, limit=limit + 1, cursor=position
    )
    total = await SupportTicketsRepository.count_by_user(session, user_id=user_id)

    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = (
        encode_queue_cursor(visible[-1]["created_at"], visible[-1]["uuid"])
        if has_more and visible
        else None
    )

    items = [
        SupportTicketEntry(
            uuid=row["uuid"],
            body=row["body"],
            status=row["status"],
            created_at=row["created_at"],
            answer=row["answer"],
            answered_at=row["answered_at"],
        )
        for row in visible
    ]
    return SupportTicketsPage(items=items, total=total, next_cursor=next_cursor)
