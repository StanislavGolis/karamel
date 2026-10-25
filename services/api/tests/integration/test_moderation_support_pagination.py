"""Пагинация очереди обращений (стори 9.6; AC-01, AC-04; Task 7.7–7.8).

**Курсор из НЕ-очереди — ключевой тест** (Task 7.8, CR 9.5 MEDIUM-1, M3).
Если из подзапроса разрешения `uuid → id` убрать `AND status = 'open'`,
отвеченное обращение резолвится в `id`, и сравнение `(created_at, id) >`
считается от его позиции — то есть выдача зависит от существования строки,
которую модератор в очереди не видит. Тогда разница ответов на «uuid
отвеченного» и «несуществующий uuid» читает состояние обращения снаружи.

**Сид ставит `open`-строку ПЕРВОЙ** (урок `L-9.5-7`), иначе тест слеп:
если проверяемая строка создана раньше поддельной, обе версии кода дадут
одну и ту же страницу, и мутация выживет.

**`created_at` задаётся данными** (правило `testing-rules.md`): порядок идёт
по `(created_at, id)`, и пагинация по совпадающему `created_at` — это ровно
tie-группа, ради которой второй ключ `id` и существует. `sleep` запрещён.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reports_queue_cursor import encode_queue_cursor
from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

_QUEUE_URL = "/api/v1/moderation/support/tickets"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _insert_ticket(
    session: AsyncSession,
    *,
    user_id: int,
    body: str,
    status: str = "open",
    answer: str | None = None,
    answered_at: datetime | None = None,
    created_at: datetime | None = None,
) -> tuple[uuid_lib.UUID, int]:
    result = await session.execute(
        text(
            "INSERT INTO support_tickets "
            "(user_id, body, status, answer, answered_at, created_at) "
            "VALUES (:user_id, :body, :status, :answer, :answered_at, "
            "COALESCE(:created_at, now())) "
            "RETURNING uuid, id"
        ),
        {
            "user_id": user_id,
            "body": body,
            "status": status,
            "answer": answer,
            "answered_at": answered_at,
            "created_at": created_at,
        },
    )
    row = result.one()
    return row.uuid, int(row.id)


async def _page(client: AsyncClient, token: str, **params: Any) -> Any:
    return await client.get(_QUEUE_URL, headers=cookie_headers(token), params=params)


async def test_pagination_limit_one_no_duplicates_same_created_at(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, D-9.6-13, §18.11: `limit=1` — без дублей и потерь в tie-группе.

    Три открытые строки с ОДИНАКОВЫМ `created_at`: порядок держит второй ключ
    `id`, и без него страницы начинали бы повторяться или терять элемент.
    Проверяются оба значения `next_cursor`: строка на первой странице и
    `null` на последней.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    moment = datetime.now(UTC) - timedelta(hours=1)
    expected: list[str] = []
    for index in range(3):
        ticket_uuid, _ticket_id = await _insert_ticket(
            session,
            user_id=author.id,
            body=f"Обращение {index}",
            created_at=moment,
        )
        expected.append(str(ticket_uuid))

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        response = await _page(client, moderator_token, **params)
        assert response.status_code == 200, response.text
        payload = response.json()
        pages += 1
        assert len(payload["items"]) == 1
        assert payload["total"] == 3
        seen.extend(item["uuid"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break
        assert pages < 5, "пагинация не сходится — курсор повторяется"

    assert pages == 3, f"ожидалось три страницы, получено {pages}"
    assert seen == expected, "страницы повторяют или теряют элементы (tie-group)"
    assert len(set(seen)) == len(seen), "дубли между страницами"


async def test_broken_cursor_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, D-9.6-2: битый курсор — `422 invalid_cursor`, не `500`.

    Значение пришло от клиента: `500` обвинил бы сервер в том, что он не
    формировал. Код `invalid_cursor` — из переиспользованного кодека
    `reports_queue_cursor`, поэтому второй класс ошибки не появился.
    """
    _moderator, moderator_token = await make_user(session, role="moderator")

    response = await _page(client, moderator_token, cursor="not-a-cursor")

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_cursor", response.json()


async def test_cursor_from_answered_ticket_equals_unknown_uuid(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Task 7.8, M3, CR 9.5 MEDIUM-1: курсор из НЕ-очереди не меняет выдачу.

    Подделанный курсор, указывающий на ОТВЕЧЕННОЕ обращение, обязан дать ТУ ЖЕ
    страницу, что и несуществующий `uuid`. Без `AND status = 'open'` в
    подзапросе отвеченная строка резолвится в `id`, и tie-группа по
    `created_at` начинает отдаваться — по ответу API читается состояние
    обращения, которого модератор в очереди не видит (оракул утечки).

    **Почему сид именно такой** (урок `L-9.5-7`). Кортежное сравнение в
    PostgreSQL коротко замыкается: при РАЗНЫХ `created_at` результат не
    зависит от `NULL` во втором элементе, поэтому тест с разными метками
    слеп — обе версии кода дают одну страницу. Проверять надо TIE-ГРУППУ:
    `open`-строка создаётся ПОСЛЕ отвеченной с ТЕМ ЖЕ `created_at`, то есть
    имеет больший `id`. Тогда без предиката она попадает на страницу, а с
    предикатом — нет.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    now = datetime.now(UTC)
    tie_moment = now - timedelta(hours=2)

    # Открытая строка ДО курсора: обе версии кода её не отдают (сравнение
    # по времени), она нужна только чтобы очередь была непустой.
    visible, _visible_id = await _insert_ticket(
        session, user_id=author.id, body="Открытое раньше", created_at=now - timedelta(hours=3)
    )
    # Отвеченное создано ПЕРВЫМ — значит его `id` меньше.
    answered, answered_id = await _insert_ticket(
        session,
        user_id=author.id,
        body="Отвеченное",
        status="answered",
        answer="Готово",
        answered_at=tie_moment + timedelta(minutes=1),
        created_at=tie_moment,
    )
    # Открытая строка с ТЕМ ЖЕ `created_at`, но БОЛЬШИМ `id` — ровно tie-группа.
    tie_open, tie_open_id = await _insert_ticket(
        session, user_id=author.id, body="Открытое в той же секунде", created_at=tie_moment
    )
    assert answered_id < tie_open_id, "сид не создаёт tie-группу после отвеченной строки"

    unknown_cursor = encode_queue_cursor(tie_moment, uuid_lib.uuid4())
    answered_cursor = encode_queue_cursor(tie_moment, answered)

    unknown_response = await _page(client, moderator_token, cursor=unknown_cursor)
    answered_response = await _page(client, moderator_token, cursor=answered_cursor)

    assert unknown_response.status_code == 200, unknown_response.text
    assert answered_response.status_code == 200, answered_response.text
    assert answered_response.json()["items"] == unknown_response.json()["items"], (
        "курсор из отвеченного обращения читает его состояние через выдачу (M3)"
    )
    # Обе страницы пусты: вторая половина ключа для неизвестного `uuid` — `NULL`,
    # а `NULL` не проходит сравнение даже внутри tie-группы.
    assert unknown_response.json()["items"] == [], (
        "keyset не отсекает NULL-позицию: тест доказывал бы не то"
    )
    assert tie_open not in [item["uuid"] for item in answered_response.json()["items"]]
    assert str(visible) not in [item["uuid"] for item in answered_response.json()["items"]]


async def test_cursor_from_open_ticket_moves_forward(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, D-9.6-13: курсор открытой строки сдвигает страницу ВПЕРЁД.

    Контроль к тесту выше: курсор действительно работает. Без него `[]` в
    предыдущем тесте проходил бы и при полностью сломанном keyset — тест
    доказывал бы «курсор не ломает», хотя не работает ничего.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    now = datetime.now(UTC)

    first, first_id = await _insert_ticket(
        session, user_id=author.id, body="Первое", created_at=now - timedelta(hours=3)
    )
    second, second_id = await _insert_ticket(
        session, user_id=author.id, body="Второе", created_at=now - timedelta(hours=2)
    )
    assert first_id < second_id
    first_row = (
        await session.execute(
            text("SELECT created_at FROM support_tickets WHERE uuid = :u"), {"u": first}
        )
    ).scalar_one()

    response = await _page(client, moderator_token, cursor=encode_queue_cursor(first_row, first))

    assert response.status_code == 200, response.text
    uuids = [item["uuid"] for item in response.json()["items"]]
    assert uuids == [str(second)], "курсор открытой строки не сдвинул страницу"


async def test_limit_bounds_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, D-9.6-3: `limit` вне 1…50 — `422` от валидации.

    Верхняя граница — часть контракта: без неё `limit=100000` заставил бы
    сервер собрать всю таблицу в память.
    """
    _moderator, moderator_token = await make_user(session, role="moderator")

    too_big = await _page(client, moderator_token, limit=51)
    too_small = await _page(client, moderator_token, limit=0)

    assert too_big.status_code == 422, too_big.text
    assert too_small.status_code == 422, too_small.text
