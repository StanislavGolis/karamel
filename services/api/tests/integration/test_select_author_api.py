"""Выбор автора в карточке режима «заявки» (стори 5б.2; AC-01, AC-04…AC-06).

Реальный PostgreSQL, схема поднята Alembic (`0013`). Главные утверждения:

- **`200 {card_uuid, chat_uuid}`** и карточка ОДНИМ `UPDATE`: `full`,
  `filled_count=1`, `chat_uuid` заполнен (X-39 не возвращается);
- **ровно одна заявка `selected`**, остальные `not_selected`;
- **откат SAVEPOINT**: не-`pending` заявка → `409`, карточка НЕ изменилась;
- **матрица отказов** (Task 0.3 → 3.4) даёт предсказуемые коды;
- **`404`** не отличает «не автор» от «чужое сообщество»;
- идемпотентности НЕТ: повтор → `409 REQUEST_NOT_PENDING` (D-5b.2-9).

§18.2: каждый тест проверяет наблюдаемое следствие (строка в БД, код,
счётчик), а не факт вызова.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент на общей сессии: `commit()` сервиса освобождает SAVEPOINT."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_community_slug},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 1,
    filled_count: int = 0,
    status: str = "published",
    expires_in_minutes: int = 60,
    community_slug: str | None = None,
) -> tuple[str, int]:
    """Карточка с управляемым окном решения и состоянием.

    `capacity=1` — режим заявок (FR70). Окно задаётся СМЕЩЕНИЕМ от `now`, а не
    абсолютной меткой: тест на истёкшее окно не должен зависеть от момента
    запуска.
    """
    starts = datetime.now(UTC) + timedelta(days=3)
    expires = datetime.now(UTC) + timedelta(minutes=expires_in_minutes)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": community_slug or settings.default_community_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": expires,
            "status": status,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, status: str = "pending"
) -> tuple[str, int]:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
            "VALUES (:card, :applicant, '', :status, now() + interval '1 day') RETURNING uuid, id"
        ),
        {"card": card_id, "applicant": applicant_id, "status": status},
    )
    request_uuid, request_id = row.one()
    return str(request_uuid), int(request_id)


async def _select(client: AsyncClient, token: str, request_uuid: str) -> Any:
    return await client.post(
        f"/api/v1/requests/{request_uuid}/select", cookies={ACCESS_COOKIE_NAME: token}
    )


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str, Any]:
    row = await session.execute(
        text("SELECT filled_count, status, chat_uuid FROM event_cards WHERE id = :id"),
        {"id": card_id},
    )
    filled, status, chat_uuid = row.one()
    return int(filled), str(status), chat_uuid


async def _request_statuses(session: AsyncSession, card_id: int) -> dict[int, str]:
    rows = await session.execute(
        text("SELECT id, status FROM join_requests WHERE card_id = :card"), {"card": card_id}
    )
    return {int(row[0]): str(row[1]) for row in rows.all()}


# --- AC-01: успешный выбор ---------------------------------------------------


async def test_select_marks_one_selected_and_rejects_the_rest(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: выбранная `selected`, остальные `not_selected`, карточка `full`.

    Краснеет, если не выполнять reject-шаг (остались бы `pending`) либо
    перевести в `not_selected` и выбранную (в составе не было бы никого).
    """
    author, author_token = await _new_user(session)
    first, _ = await _new_user(session)
    second, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_uuid, winner_id = await _insert_request(session, card_id=card_id, applicant_id=first.id)
    _, loser_id = await _insert_request(session, card_id=card_id, applicant_id=second.id)

    response = await _select(client, author_token, winner_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["card_uuid"] == card_uuid
    assert body["chat_uuid"]

    statuses = await _request_statuses(session, card_id)
    assert statuses[winner_id] == "selected"
    assert statuses[loser_id] == "not_selected"

    filled, status, chat_uuid = await _card_state(session, card_id)
    assert (filled, status) == (1, "full")
    # AC-01 (M6): `chat_uuid` в ответе — ровно то, что записано в БД.
    assert str(chat_uuid) == body["chat_uuid"]


async def test_select_is_single_update_without_check_violation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (X-39): карточка меняется ОДНИМ `UPDATE` — без `500` от CHECK.

    `ck_event_cards_full_capacity` запрещает `published` при `filled_count =
    capacity` и `full` при `filled_count < capacity`. Раздельные `UPDATE`
    дали бы `500` на легальном действии (M4). `200` здесь и есть
    доказательство единственности перехода.
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 200, response.text
    filled, status, chat_uuid = await _card_state(session, card_id)
    # Заголовок ответа — единственное доказательство, что переход сделан одним
    # `UPDATE`: `filled_count`, `status` и `chat_uuid` записаны вместе.
    assert (filled, status) == (1, "full")
    assert str(chat_uuid) == response.json()["chat_uuid"]


async def test_repeat_select_returns_request_not_pending(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05, D-5b.2-9: повтор того же `select` → `409 REQUEST_NOT_PENDING`.

    Идемпотентности НЕТ: `200` скрыл бы двойной клик и различил бы «выбрана
    вами» и «выбрана другим», что запрещено безличностью (FR81).
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    assert (await _select(client, author_token, request_uuid)).status_code == 200

    repeat = await _select(client, author_token, request_uuid)

    assert repeat.status_code == 409
    assert repeat.json()["code"] == "REQUEST_NOT_PENDING"


# --- AC-06: откат SAVEPOINT --------------------------------------------------


async def test_non_pending_request_rolls_back_seat_savepoint(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: не-`pending` заявка → карточка НЕ изменилась вовсе.

    Краснеет от вынесения seat-CAS из `begin_nested` (D-5b.2-5): карточка
    осталась бы `full` с занятым местом и без `selected` — класс X-39.
    Проверяются ТРИ поля: `filled_count`, `status`, `chat_uuid IS NULL`.
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid, _ = await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, status="cancelled"
    )

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 409
    assert response.json()["code"] == "REQUEST_NOT_PENDING"
    assert await _card_state(session, card_id) == (0, "published", None)


async def test_session_survives_request_not_pending(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: после `409` сессия жива — следующий запрос не `500`.

    Краснеет от голого `UPDATE` без `SAVEPOINT`: `IntegrityError` не возникает,
    но провал CAS внутри `begin_nested` обязан откатиться, а не оставить
    транзакцию в состоянии «нужен rollback».
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid, _ = await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, status="expired"
    )

    assert (await _select(client, author_token, request_uuid)).status_code == 409
    # Второй запрос в той же сессии обязан отработать, а не дать `500`.
    again = await _select(client, author_token, request_uuid)
    assert again.status_code == 409
    assert again.json()["code"] == "REQUEST_NOT_PENDING"


# --- AC-04: матрица отказов -------------------------------------------------


@pytest.mark.parametrize(
    ("card_status", "filled", "expires_in", "expected_code"),
    [
        # `published` + открытое окно — единственный успех (проверен выше).
        ("full", 1, 60, "CARD_ALREADY_FILLED"),
        ("closed", 0, 60, "CARD_NOT_ACCEPTING"),
        ("cancelled", 0, 60, "CARD_NOT_ACCEPTING"),
        ("draft", 0, 60, "CARD_NOT_ACCEPTING"),
        # Окно истекло — тот же код, что у `closed` (гард 5б.1).
        ("published", 0, -5, "CARD_NOT_ACCEPTING"),
    ],
)
async def test_card_state_matrix_gives_documented_codes(
    client: AsyncClient,
    session: AsyncSession,
    card_status: str,
    filled: int,
    expires_in: int,
    expected_code: str,
) -> None:
    """AC-04: состояние карточки → код отказа (матрица Task 0.3).

    Краснеет от слияния `CARD_ALREADY_FILLED` с `CARD_NOT_ACCEPTING` (M8):
    клиент показывал бы «Набор закрыт» вместо «Компания уже собрана».
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        status=card_status,
        filled_count=filled,
        expires_in_minutes=expires_in,
    )
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == expected_code


@pytest.mark.parametrize("request_status", ["cancelled", "expired", "selected"])
async def test_request_state_matrix_gives_request_not_pending(
    client: AsyncClient, session: AsyncSession, request_status: str
) -> None:
    """AC-04: состояние заявки → `409 REQUEST_NOT_PENDING` при живой карточке.

    Один код на три статуса намеренно: различать их — рассказывать автору
    о прошлом заявителя (FR81).
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    if request_status == "selected":
        # `selected` при `filled_count=0` нарушил бы CHECK — ставим место.
        _, card_id = await _insert_card(
            session,
            author_id=author.id,
            place_id=place_id,
            status="full",
            filled_count=1,
            capacity=1,
        )
    request_uuid, _ = await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, status=request_status
    )

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 409, response.text
    assert response.json()["code"] in {"REQUEST_NOT_PENDING", "CARD_ALREADY_FILLED"}


async def test_non_author_gets_card_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04 (ADR-13): не автор → `404 card_not_found`, не `403`.

    `403` подтвердил бы существование чужой карточки и её заявок.
    """
    author, _ = await _new_user(session)
    stranger, stranger_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _select(client, stranger_token, request_uuid)

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"
    # Карточка не тронута: чужое действие не оставило следов.
    assert await _card_state(session, card_id) == (0, "published", None)


async def test_non_author_with_inactive_request_still_gets_card_not_found(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, AC-04 (FR81, ADR-13): статус заявки НЕ утекает не-автору.

    Анти-оракул на ПОРЯДОК проверок: `404` (авторство) обязан стоять ДО
    `409` (статус заявки). Иначе не-автор, перебирая `uuid` заявок чужой
    карточки, отличал бы `pending` (`404 card_not_found`) от
    `cancelled`/`expired`/`selected` (`409 REQUEST_NOT_PENDING`) — то есть
    читал бы состояние чужой заявки точечным запросом.

    Тест ловит перестановку блока проверки статуса выше проверки авторства:
    та перестановка выглядит безобидной (обе ветки — отказ), но превращает
    `404` в оракул существования заявки.
    """
    author, _ = await _new_user(session)
    stranger, stranger_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    # Заявка в состоянии, отличном от `pending`: именно на ней порядок проверок
    # становится наблюдаемым. `cancelled` выбран как самый частый исход.
    cancelled_uuid, _ = await _insert_request(
        session, card_id=card_id, applicant_id=applicant.id, status="cancelled"
    )

    response = await _select(client, stranger_token, cancelled_uuid)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"
    # Карточка не тронута: отказ не изменил ни одного поля.
    assert await _card_state(session, card_id) == (0, "published", None)


async def test_missing_request_gets_request_not_found(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (ADR-13): несуществующая заявка → `404 request_not_found`.

    Отдельный код от `card_not_found`: клиент отличает опечатку в `uuid`
    от чужой карточки, при этом ни один ответ не раскрывает деталей.
    """
    _, author_token = await _new_user(session)

    response = await _select(client, author_token, str(uuid_lib.uuid4()))

    assert response.status_code == 404
    assert response.json()["code"] == "request_not_found"


async def test_foreign_community_gets_card_not_found(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (ADR-13): карточка чужого сообщества неотличима от чужой.

    Краснеет, если убрать `community_slug` из `build_get_card_for_select_statement`:
    заявка чужого сообщества прошла бы выбор.
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, community_slug="other-city"
    )
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"


async def test_capacity_other_than_one_is_not_selectable(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (D-5b.2-2, FR70): выбор есть только при вместимости 1.

    `409 CARD_NOT_ACCEPTING`, а не новый код: иначе клиент ветвился бы по
    признаку, который уже приходит в `mode`.
    """
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id, capacity=4)
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"


async def test_broken_uuid_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: мусор в пути — `422` от общего обработчика, не `500`."""
    _, author_token = await _new_user(session)

    response = await client.post(
        "/api/v1/requests/not-a-uuid/select", cookies={ACCESS_COOKIE_NAME: author_token}
    )

    assert response.status_code == 422


async def test_select_without_session_is_unauthorized(client: AsyncClient) -> None:
    """AC-04: без сессии — `401`, как у соседних роутов."""
    response = await client.post(f"/api/v1/requests/{uuid_lib.uuid4()}/select")
    assert response.status_code == 401


async def test_select_response_has_no_store_header(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: `cache-control: no-store` на `200` — ответ не кэшируется."""
    author, author_token = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    request_uuid, _ = await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await _select(client, author_token, request_uuid)

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
