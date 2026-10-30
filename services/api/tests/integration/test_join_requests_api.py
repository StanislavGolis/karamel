"""Подача заявки через API (стори 5а.2, Task 8.1–8.4, 8.7; AC-02…AC-07, AC-10).

Реальный PostgreSQL, схема поднята Alembic. Карточки в нужных состояниях
кладутся СЫРЫМ SQL там, где правила сервиса до этого состояния не доводят
(`full`, `finished`, вместимость 2..6): иначе проверялся бы не тот сценарий.

§18.1: каждый тест краснеет от снятия конкретной защиты — она названа в
докстринге. §18.2: результат проверяется по строке в БД, а не только по коду
ответа.
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

from app.api.v1.join_requests import get_join_request_rate_limiter
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


def _city() -> str:
    return settings.default_city_slug


def _future(**delta: int) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


class _NoLimit:
    """AC-10: лимитер без ограничений — технический лимит проверяется отдельно.

    Иначе каждый тест домена расходовал бы квоту 10/24ч одного аккаунта, и
    поведение зависело бы от порядка запуска: 11-я подача в наборе получала бы
    `429 rate_limited` вместо ожидаемого доменного кода.
    """

    async def hit(self, applicant_id: int) -> int | None:
        return None


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой сессией и без технического лимита."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_join_request_rate_limiter] = _NoLimit
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_join_request_rate_limiter, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    """Участник с согласием на ПД (OQ-5a.2-3: подача под `ConsentUser`)."""
    phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession, *, slug: str | None = None) -> int:
    """Место сообщества и его внутренний `id` (нужен для FK карточки)."""
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": slug or _city()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    status: str = "published",
    capacity: int = 1,
    filled_count: int = 0,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    city_slug: str | None = None,
) -> str:
    """Карточка в нужном состоянии — сырым SQL, минуя правила сервиса."""
    starts = starts_at or _future(days=3)
    expires = expires_at or (starts - timedelta(hours=1))
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, "
            "60, :expires, :status, :filled, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": city_slug or _city(),
            "starts": starts,
            "capacity": capacity,
            "expires": expires,
            "status": status,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1) if status != "draft" else None,
        },
    )
    return str(row.scalar_one())


async def _requests(session: AsyncSession, card_uuid: str) -> list[dict[str, Any]]:
    """Строки заявок карточки — прямое доказательство записи (§18.2)."""
    result = await session.execute(
        text(
            "SELECT status, message, expires_at, decided_at FROM join_requests "
            "WHERE card_id = (SELECT id FROM event_cards WHERE uuid = :uuid)"
        ),
        {"uuid": card_uuid},
    )
    return [dict(row) for row in result.mappings()]


async def _post(client: AsyncClient, card_uuid: str, token: str, message: str = "привет") -> Any:
    return await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": message},
        cookies={ACCESS_COOKIE_NAME: token},
    )


# --- AC-02: happy path -------------------------------------------------------


async def test_submit_request_creates_pending_with_card_expiry(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR76, D-5a.1-5): `201 {uuid, status, submitted_at}`, окно — с карточки.

    Краснеет, если: взять окно из настроек вместо `card.expires_at` (заявка
    пережила бы дедлайн карточки), вернуть `message` в теле (OQ-5a.2-2),
    проставить `decided_at` на создании.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    expires = _future(days=2)
    card_uuid = await _insert_card(
        session,
        author_id=author.id,
        place_id=await _place(session),
        expires_at=expires,
        starts_at=_future(days=3),
    )

    response = await _post(client, card_uuid, token)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"uuid", "status", "submitted_at"}
    assert body["status"] == "pending"
    assert response.headers["cache-control"] == "no-store"

    rows = await _requests(session, card_uuid)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["decided_at"] is None
    assert rows[0]["expires_at"] == expires
    assert rows[0]["message"] == "привет"


async def test_request_does_not_touch_filled_count(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (D-5a.2-2): заявка НЕ занимает место — `filled_count` не меняется.

    Краснеет, если инкрементировать `filled_count` при подаче: при `capacity=1`
    карточка сразу ушла бы в `full`, и окно решения потеряло бы смысл.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=await _place(session))

    assert (await _post(client, card_uuid, token)).status_code == 201

    row = await session.execute(
        text("SELECT filled_count, status FROM event_cards WHERE uuid = :uuid"),
        {"uuid": card_uuid},
    )
    filled, status = row.one()
    assert int(filled) == 0
    assert status == "published"


# --- AC-04, AC-05: отказы ----------------------------------------------------


async def test_capacity_above_one_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04 (FR70): `capacity=3` → `409 CAPACITY_ONE_USES_REQUESTS`.

    Краснеет, если снять проверку `is_request_mode`: заявки появились бы у
    карточки открытого набора, где присоединение мгновенное (5а.5).
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=await _place(session), capacity=3
    )

    response = await _post(client, card_uuid, token)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CAPACITY_ONE_USES_REQUESTS"
    # CR 5а.2, патч 3: тела ошибок тоже обязаны быть `no-store` — `409`/`404`
    # раскрывают существование карточки, и кэширование ответа утекло бы между
    # пользователями. Заголовок фактически ставит `NoStoreMiddleware` (он
    # покрывает и тела ошибок), поэтому тест фиксирует НАБЛЮДАЕМОЕ поведение
    # контракта, а не конкретный источник заголовка: снятие `_NO_STORE` из
    # `_error()` мутацию не ловит по построению (заголовок остаётся), снятие
    # middleware — ловит.
    assert response.headers["cache-control"] == "no-store"
    assert await _requests(session, card_uuid) == []


@pytest.mark.parametrize("status", ["draft", "closed", "full", "finished", "cancelled"])
async def test_not_accepting_card_returns_409(
    client: AsyncClient, session: AsyncSession, status: str
) -> None:
    """AC-05 (FR74): пять статусов — пять `409 CARD_NOT_ACCEPTING`, без записи.

    Краснеет, если снять проверку статуса: заявка легла бы на закрытую встречу
    или на черновик, которого участник вообще не видит.

    `filled_count` для `full` доводится до `capacity` — иначе CHECK
    `ck_event_cards_full_capacity` отверг бы саму вставку.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    # `full` требует `filled_count = capacity` (CHECK `ck_event_cards_full_capacity`):
    # иначе сама вставка отвергается, и тест проверял бы не тот отказ.
    card_uuid = await _insert_card(
        session,
        author_id=author.id,
        place_id=await _place(session),
        status=status,
        filled_count=1 if status == "full" else 0,
    )

    response = await _post(client, card_uuid, token)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"
    assert await _requests(session, card_uuid) == []


async def test_own_card_returns_403(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: заявка на свою карточку → `403 JOIN_REQUEST_OWN_CARD`.

    Краснеет, если снять гард: автор занял бы собственное место и увидел бы
    себя в списке заявителей.
    """
    author, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=await _place(session))

    response = await _post(client, card_uuid, token)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "JOIN_REQUEST_OWN_CARD"
    assert await _requests(session, card_uuid) == []


async def test_other_city_card_is_indistinguishable_from_missing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07 (ADR-13): чужое сообщество и отсутствие карточки — ОДИН ответ.

    Краснеет, если убрать `city_slug` из запроса: карточка другого
    сообщества стала бы доступна (утечка), а если добавить разный код для двух
    случаев — существование чужой карточки раскрылось бы перебором.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    foreign = await _insert_card(
        session,
        author_id=author.id,
        place_id=await _place(session, slug="kemerovo"),
        city_slug="kemerovo",
    )

    foreign_response = await _post(client, foreign, token)
    missing_response = await _post(client, str(uuid_lib.uuid4()), token)

    assert foreign_response.status_code == 404, foreign_response.text
    assert missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()
    assert foreign_response.json()["code"] == "card_not_found"


# --- AC-03: лимит активных ---------------------------------------------------


async def test_third_pending_request_returns_429(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03 (FR77, NFR29): две активные заявки — третья `429` с числами.

    Краснеет, если снять `>= limit` или заменить `JOIN_REQUEST_ACTIVE_STATUSES`
    набором пошире: лимит начал бы считаться иначе, чем объявлено.
    """
    author, _ = await _new_user(session)
    applicant, token = await _new_user(session)
    place = await _place(session)
    first = await _insert_card(session, author_id=author.id, place_id=place)
    second = await _insert_card(session, author_id=author.id, place_id=place)
    third = await _insert_card(session, author_id=author.id, place_id=place)

    assert (await _post(client, first, token)).status_code == 201
    assert (await _post(client, second, token)).status_code == 201

    response = await _post(client, third, token)

    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "JOIN_REQUEST_LIMIT_REACHED"
    assert body["details"] == {"limit": 2, "active_count": 2}
    assert await _requests(session, third) == []
    _ = applicant


async def test_cancelled_request_frees_slot(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (FR82): отменённая заявка освобождает слот — снова `201`.

    Краснеет, если считать все заявки без фильтра по статусу: участник,
    отменивший заявку, не смог бы подать новую (NFR29 нарушен).
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place = await _place(session)
    first = await _insert_card(session, author_id=author.id, place_id=place)
    second = await _insert_card(session, author_id=author.id, place_id=place)
    third = await _insert_card(session, author_id=author.id, place_id=place)

    assert (await _post(client, first, token)).status_code == 201
    assert (await _post(client, second, token)).status_code == 201
    await session.execute(
        text(
            "UPDATE join_requests SET status = 'cancelled' WHERE card_id = "
            "(SELECT id FROM event_cards WHERE uuid = :uuid)"
        ),
        {"uuid": first},
    )

    response = await _post(client, third, token)

    assert response.status_code == 201, response.text


async def test_selected_occupies_pair_but_not_limit(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, AC-06: `selected` не занимает слот лимита, но пару блокирует.

    Краснеет двумя правками: (1) если считать `selected` в лимите — участник
    с выбранной заявкой не смог бы подать на другую карточку; (2) если убрать
    `selected` из предиката `uq` — на этой карточке появилась бы вторая заявка.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place = await _place(session)
    card = await _insert_card(session, author_id=author.id, place_id=place)
    other = await _insert_card(session, author_id=author.id, place_id=place)

    assert (await _post(client, card, token)).status_code == 201
    await session.execute(
        text(
            "UPDATE join_requests SET status = 'selected' WHERE card_id = "
            "(SELECT id FROM event_cards WHERE uuid = :uuid)"
        ),
        {"uuid": card},
    )

    repeat = await _post(client, card, token)
    assert repeat.status_code == 409, repeat.text
    assert repeat.json()["code"] == "ALREADY_REQUESTED"

    assert (await _post(client, other, token)).status_code == 201


# --- AC-06: гонка пары -------------------------------------------------------


async def test_duplicate_request_returns_already_requested(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: повторная активная заявка на ту же карточку → `409 ALREADY_REQUESTED`.

    Краснеет, если распознавать гонку по `getattr(error.orig, "constraint_name")`
    (это `None` под asyncpg, CR 3.1): клиент получил бы `500` вместо `409`.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=await _place(session))

    assert (await _post(client, card_uuid, token)).status_code == 201
    second = await _post(client, card_uuid, token)

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "ALREADY_REQUESTED"


# --- AC-10: технический лимит и гард согласия -------------------------------


async def test_rate_limited_envelope(session: AsyncSession) -> None:
    """AC-10 (FR61): превышение частоты — `429 rate_limited` + `retry_after`.

    Краснеет, если вернуть доменный код `JOIN_REQUEST_LIMIT_REACHED`: клиент
    показал бы «отмените заявку» вместо «подождите».
    """

    class _Exhausted:
        async def hit(self, applicant_id: int) -> int | None:
            return 120

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_join_request_rate_limiter] = _Exhausted
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            _, token = await _new_user(session)
            response = await http_client.post(
                f"/api/v1/cards/{uuid_lib.uuid4()}/requests",
                json={"message": "привет"},
                cookies={ACCESS_COOKIE_NAME: token},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_join_request_rate_limiter, None)

    assert response.status_code == 429, response.text
    assert response.json() == {"code": "rate_limited", "retry_after": 120}
    assert response.headers["retry-after"] == "120"


async def test_empty_message_is_rejected_before_service(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: пустое сообщение — `422` от схемы, до каких-либо запросов.

    Краснеет, если `min_length=1` убрать: пустая строка прошла бы CHECK в БД
    (D-5a.1-8) и попала бы в очередь автора.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=await _place(session))

    response = await _post(client, card_uuid, token, message="   ")

    assert response.status_code == 422, response.text
    assert await _requests(session, card_uuid) == []


async def test_numeric_card_id_is_not_a_valid_path(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-12 (Task 8.7): `/cards/1/requests` не даёт `200`/`500`.

    Краснеет, если объявить путь как `str` и парсить внутри: перебор
    внутренних `id` стал бы возможен (D-2), а битый UUID давал бы `500`.
    """
    _, token = await _new_user(session)
    response = await client.post(
        "/api/v1/cards/1/requests", json={"message": "привет"}, cookies={ACCESS_COOKIE_NAME: token}
    )
    assert response.status_code not in {200, 500}
    assert response.status_code == 422
