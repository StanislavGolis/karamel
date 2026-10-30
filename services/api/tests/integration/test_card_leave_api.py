"""Отмена участия в открытом наборе через API (стори 5а.6; AC-01…AC-14).

Реальный PostgreSQL, схема поднята Alembic. Главные утверждения:

- **обратный переход `full → published`** — ОДНИМ `UPDATE`, без `500` от
  `ck_event_cards_full_capacity` (`0011` симметричен, D-5a.6-2);
- место и участие меняются в ОДНОМ `SAVEPOINT` — место не утекает;
- `404` не раскрывает, была ли отмена раньше (AC-05 vs AC-06);
- лимит FR105 участием не расходуется, повторное присоединение проходит;
- закрытая карточка участие отменяет, но место не возвращает (AC-11).

§18.2: каждый тест проверяет наблюдаемое следствие (строка в БД, код,
состояние счётчика), а не факт вызова.
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


def _city() -> str:
    return settings.default_city_slug


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
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 4,
    filled_count: int = 0,
    status: str = "published",
    city_slug: str | None = None,
) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, :filled, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": city_slug or _city(),
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            "status": status,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_participation(
    session: AsyncSession, *, card_id: int, applicant_id: int, status: str = "selected"
) -> int:
    row = await session.execute(
        text(
            "INSERT INTO join_requests "
            "(card_id, applicant_id, message, status, decided_at, expires_at) "
            "VALUES (:card, :applicant, '', :status, now(), now() + interval '1 day') RETURNING id"
        ),
        {"card": card_id, "applicant": applicant_id, "status": status},
    )
    return int(row.scalar_one())


async def _leave(client: AsyncClient, token: str, card_uuid: str) -> Any:
    return await client.delete(
        f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: token}
    )


async def _join(client: AsyncClient, token: str, card_uuid: str) -> Any:
    return await client.post(f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: token})


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str]:
    row = await session.execute(
        text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    filled, status = row.one()
    return int(filled), str(status)


async def _participation_status(session: AsyncSession, request_id: int) -> tuple[str, bool]:
    row = await session.execute(
        text("SELECT status, decided_at IS NOT NULL FROM join_requests WHERE id = :id"),
        {"id": request_id},
    )
    status, has_decided = row.one()
    return str(status), bool(has_decided)


# --- AC-01: декремент места и отмена участия -------------------------------


async def test_leave_decrements_seat_and_cancels_participation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (FR104): `404` нет, `204` есть; место −1, участие `cancelled`.

    Краснеет, если не выполнять `UPDATE` места либо не переводить участие
    в `cancelled` (тогда `filled_count` уменьшился бы без отмены).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2
    )
    request_id = await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 204, response.text
    assert await _card_state(session, card_id) == (1, "published")
    status, has_decided = await _participation_status(session, request_id)
    assert status == "cancelled"
    assert has_decided is True


# --- AC-02: обратный переход full -> published -----------------------------


async def test_leave_returns_full_card_to_published_in_one_update(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR104, ADR-10): `full (4/4)` → `published (3/4)` ОДНИМ `UPDATE`.

    Краснеет от снятия `CASE` в `build_release_seat_statement`: тогда
    `filled_count` уменьшился бы, а статус остался `full`, что запрещает
    `ck_event_cards_full_capacity` (`0011`) → `500` на легальном действии.
    Это и есть доказательство симметрии CHECK (D-5a.6-2) — тест-инвариант
    покраснеет, если `0011` когда-нибудь ослабят или перепишут.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=4, status="full"
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 204, response.text
    assert await _card_state(session, card_id) == (3, "published")


async def test_no_row_with_full_status_and_free_seat_exists(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (инвариант схемы): `full` при `filled_count < capacity` не существует.

    Краснеет, если декремент и `CASE` разнести на два `UPDATE`: между ними
    строка была бы `full` + `filled_count < capacity` и CHECK отверг бы её
    (`500`). Тест проверяет итоговое состояние БД, а не коды ответов.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=3, filled_count=3, status="full"
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204

    row = await session.execute(
        text(
            "SELECT count(*) FROM event_cards "
            "WHERE id = :id AND status = 'full' AND filled_count < capacity"
        ),
        {"id": card_id},
    )
    assert int(row.scalar_one()) == 0


# --- AC-05, AC-06: 404 против идемпотентного 204 ---------------------------


async def test_leave_without_participation_returns_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05 (FR104): участия нет → `404 participation_not_found`.

    Краснеет, если возвращать `204` на любом исходе: клиент считал бы
    успехом операцию, которой не было.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4
    )

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "participation_not_found"
    # Место НЕ утекло: `release_seat` и `cancel_participation` в одном
    # SAVEPOINT, поэтому неуспешная отмена откатывает и декремент. Краснеет,
    # если вынести `release_seat` из `begin_nested`: счётчик уменьшился бы
    # без отмены участия (утечка чужого места).
    assert await _card_state(session, card_id) == (0, "published")


async def test_leave_with_zero_filled_count_does_not_go_negative(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (NFR47, §6.5): `filled_count = 0` не уходит в минус.

    Аномальное, но легальное для схемы состояние (CHECK на неотрицательность
    нет): участие есть, счётчик 0. Отмена обязана пройти, а место остаться 0.

    Краснеет от снятия `filled_count > 0` из `build_release_seat_statement`:
    `UPDATE` записал бы `-1`, и инвариант `0 <= filled_count` нарушился бы
    без единой ошибки БД.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=0
    )
    request_id = await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 204, response.text
    assert await _card_state(session, card_id) == (0, "published")
    status, _ = await _participation_status(session, request_id)
    assert status == "cancelled"


async def test_repeat_leave_is_idempotent_204(client: AsyncClient, session: AsyncSession) -> None:
    """AC-06 (FR104): повторный `DELETE` → `204`, `decided_at` не переписан.

    Краснеет, если на повтор отдавать `409`: обрыв сети после `commit`
    выглядел бы сбоем уже выполненной операции (приём 5а.3, AC-05 там).
    Второе утверждение — `decided_at` не меняется: иначе аудит потерял бы
    момент фактической отмены (NFR1–NFR4).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2
    )
    request_id = await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204
    row = await session.execute(
        text("SELECT decided_at FROM join_requests WHERE id = :id"), {"id": request_id}
    )
    first_decided = row.scalar_one()

    second = await _leave(client, token, card_uuid)

    assert second.status_code == 204, second.text
    row = await session.execute(
        text("SELECT decided_at FROM join_requests WHERE id = :id"), {"id": request_id}
    )
    assert row.scalar_one() == first_decided


# --- AC-07: pending-заявка маршрутом участия не отменяется -----------------


async def test_pending_request_is_not_cancelled_by_join_route(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07 (FR104): `pending`-заявка → `404`, домены не смешиваются.

    Краснеет от снятия `status='selected'` в предикате CAS: маршрут участия
    молча отменял бы заявку, и клиент терял бы место в очереди, считая,
    что вышел из компании (D-5a.6-5).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=1
    )
    request_id = await _insert_participation(
        session, card_id=card_id, applicant_id=member.id, status="pending"
    )

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "participation_not_found"
    status, _ = await _participation_status(session, request_id)
    assert status == "pending"
    assert await _card_state(session, card_id) == (0, "published")


# --- AC-08: чужое сообщество ----------------------------------------------


async def test_leave_in_other_city_returns_card_not_found(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (ADR-13): чужое сообщество неотличимо от несуществующей карточки.

    Краснеет, если искать карточку без среза `city_slug` (`if` после
    выборки забывается при первом рефакторинге — класс дефекта 3.3).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        capacity=4,
        filled_count=1,
        city_slug="other-city",
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"
    assert await _card_state(session, card_id) == (1, "published")


async def test_leave_missing_card_returns_same_body_as_other_city(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (ADR-13): несуществующая карточка — то же тело, что у чужой.

    Краснеет, если различать «нет карточки» и «чужая» разными кодами:
    перебор `uuid` раскрыл бы существование чужих карточек.
    """
    _, token = await _new_user(session)

    response = await _leave(client, token, str(uuid_lib.uuid4()))

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"


# --- AC-09: повторное присоединение после отмены ---------------------------


async def test_after_leave_member_can_join_again(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09 (FR105): `cancelled` освобождает пару, повторный `POST /join` → `200`.

    Краснеет, если частичный `uq_join_requests_card_applicant_active`
    покрывал бы `cancelled` (тогда повтор отдавал бы `409 ALREADY_JOINED`).
    Второе утверждение — лимит FR105 не расходуется: `selected` вне
    `JOIN_REQUEST_ACTIVE_STATUSES` (D-5a.6-8).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=1
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200, response.text
    assert response.json()["filled_count"] == 1
    assert await _card_state(session, card_id) == (1, "published")


async def test_pending_limit_is_not_consumed_by_participation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09 (FR105): участие не занимает слот лимита 2 активных заявок.

    Краснеет, если `JOIN_REQUEST_ACTIVE_STATUSES` начнёт включать `selected`:
    тогда после отмены участия пользователь с двумя заявками не смог бы
    подать третью, хотя участие слот не занимало (D-5a.6-8).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=1
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204

    row = await session.execute(
        text("SELECT count(*) FROM join_requests WHERE applicant_id = :a AND status = 'pending'"),
        {"a": member.id},
    )
    assert int(row.scalar_one()) == 0


# --- AC-10: карточка снова в ленте ----------------------------------------


async def test_released_card_is_visible_in_feed_again(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-10 (FR104): карточка снова в ленте со свободным местом.

    Краснеет, если снимать место без возврата статуса: `full` остаётся
    `full` и в ленте не появляется с местом (FR84 — обратное направление).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=2, filled_count=2, status="full"
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    found = [item for item in items if item["uuid"] == card_uuid]
    assert found, "карточка должна вернуться в ленту"
    assert found[0]["filled_count"] == 1
    assert found[0]["status"] == "published"


# --- AC-11: закрытая карточка ---------------------------------------------


async def test_leave_on_closed_card_does_not_touch_counter(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-11: `closed` — участие отменяется, место и статус НЕ меняются.

    Краснеет от снятия `status IN ('published','full')` из предиката:
    закрытая карточка вернулась бы в ленту (FR73/FR100 нарушены).
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2, status="closed"
    )
    request_id = await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 204, response.text
    assert await _card_state(session, card_id) == (2, "closed")
    status, _ = await _participation_status(session, request_id)
    assert status == "cancelled"


# --- AC-12: capacity=1 -----------------------------------------------------


async def test_leave_on_request_mode_card_returns_participation_not_found(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-12 (FR105): `capacity=1` → `404`, а не `409 CAPACITY_ONE_USES_REQUESTS`.

    Краснеет, если вернуть `409` из режима: клиент увидел бы форму ответа
    присоединения там, где операции «выйти» не существует.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=1
    )

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "participation_not_found"


# --- AC-13: message=null в /my/requests -----------------------------------


async def test_my_requests_returns_null_message_for_cancelled_participation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-13 (долг X-5a.5-3): `message` участия — `null`, не пустая строка.

    Краснеет, если вернуть `str` без `or None`: в «Моих заявках» участник
    увидел бы пустой текст вместо отсутствия текста.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=1
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204

    response = await client.get("/api/v1/my/requests", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    cancelled = [i for i in items if i["status"] == "cancelled"]
    assert cancelled, "отменённое участие должно быть видно (VISIBLE_STATUSES)"
    assert cancelled[0]["message"] is None


# --- AC-14: commit до ответа ----------------------------------------------


async def test_leave_commits_before_response(client: AsyncClient, session: AsyncSession) -> None:
    """AC-14 (NFR11): `commit` до ответа — данные видны следующему соединению.

    Краснеет, если не вызывать `commit` в сервисе: `204` пришёл бы раньше
    фиксации, и параллельный читатель увидел бы старое место.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id)

    assert (await _leave(client, token, card_uuid)).status_code == 204

    # Чтение НОВЫМ соединением без общей сессии теста.
    row = await session.execute(
        text("SELECT filled_count FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    assert int(row.scalar_one()) == 1


# --- CR 5а.6, патч 1: диагностика статуса детерминирована -----------------


@pytest.mark.parametrize("order", [("expired", "cancelled"), ("cancelled", "expired")])
async def test_repeat_leave_is_204_when_pair_has_expired_and_cancelled_rows(
    client: AsyncClient, session: AsyncSession, order: tuple[str, str]
) -> None:
    """AC-06 (FR104), дефект CR 5а.6: ответ не зависит от порядка строк пары.

    `uq_join_requests_card_applicant_active` ЧАСТИЧНЫЙ (`pending`+`selected`),
    поэтому у пары «карточка + заявитель» может быть НЕСКОЛЬКО терминальных
    строк: `expired` (окно истекло) и `cancelled` (участие отменено).

    До патча `get_participation_status` брал `first()` без `ORDER BY`, и
    `DELETE /join` отдавал `404` или `204` в зависимости от физического
    порядка строк — доказано исполнением на живой PostgreSQL 18. Тест
    параметризован обоими порядками вставки: без `ORDER BY` один из двух
    прогонов краснеет.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2
    )
    for status in order:
        await _insert_participation(session, card_id=card_id, applicant_id=member.id, status=status)

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 204, response.text
    # AC-05 не нарушен: строк участия у пары ЕСТЬ, поэтому `404` неверен.
    row = await session.execute(
        text(
            "SELECT count(*) FROM join_requests WHERE card_id = :card AND applicant_id = :applicant"
        ),
        {"card": card_id, "applicant": member.id},
    )
    assert int(row.scalar_one()) == 2


async def test_expired_participation_without_cancelled_row_returns_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07 (FR104), дефект CR 5а.6: `expired` — не успех, действия нет.

    Краснеет, если диагностика принимает `expired` за `cancelled` (тогда
    `204` на участие, которого нет) — мутация M5 в CR выжила без этого
    теста. Строка одна: пара «карточка + заявитель» занята только
    терминальной `expired`, `selected` нет вовсе, отменять нечего.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2
    )
    await _insert_participation(session, card_id=card_id, applicant_id=member.id, status="expired")

    response = await _leave(client, token, card_uuid)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "participation_not_found"
    # Место не тронуто: отмены не было, `release_seat` откатан SAVEPOINT'ом.
    row = await session.execute(
        text("SELECT filled_count FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    assert int(row.scalar_one()) == 2
