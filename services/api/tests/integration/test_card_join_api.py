"""Мгновенное присоединение к открытому набору через API (стори 5а.5).

Реальный PostgreSQL, схема поднята Alembic. Главные утверждения:

- **X-39:** `capacity=1` и заполнение `capacity=N` дают `full` ОДНИМ `UPDATE`,
  без `500` от `ck_event_cards_full_capacity` (AC-02);
- ответ строится из `RETURNING`, а не из второго чтения (D-5а.5-7);
- коды различают «мест нет» и «набор закрыт» (AC-08 vs AC-07);
- участие живёт в `join_requests(status='selected')` без миграции (D-5а.5-1);
- лимит 2 активных заявок участием не расходуется (AC-12, FR105).

§18.2: каждый тест проверяет наблюдаемое следствие (строка в БД, код,
число мест), а не факт вызова; §18.1 — в docstring указано, что снять,
чтобы тест покраснел.
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
from app.repositories.join_requests import JoinRequestRepository
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
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    # Участие требует согласия на обработку ПД (`ConsentUser`).
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


async def _join(client: AsyncClient, token: str, card_uuid: str) -> Any:
    return await client.post(f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: token})


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str]:
    row = await session.execute(
        text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    filled, status = row.one()
    return int(filled), str(status)


async def _participations(session: AsyncSession, card_id: int) -> list[tuple[int, str, str]]:
    row = await session.execute(
        text(
            "SELECT applicant_id, status, message FROM join_requests "
            "WHERE card_id = :id ORDER BY id"
        ),
        {"id": card_id},
    )
    return [(int(a), str(s), str(m)) for a, s, m in row.all()]


# --- AC-01: happy path -----------------------------------------------------


async def test_join_increments_seats_and_keeps_published(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (FR83): `capacity=4, filled=2` → `200`, `filled_count=3`, `published`.

    Краснеет, если ответ собирать не из `RETURNING`, а из повторного `SELECT`
    после `commit` (D-5а.5-7), либо если инкремент не выполнять вовсе.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, filled_count=2
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filled_count"] == 3
    assert body["capacity"] == 4
    assert body["status"] == "published"
    # AC-14: чат — эпик 6, поле присутствует как `null`.
    assert body["chat_uuid"] is None
    assert await _card_state(session, card_id) == (3, "published")
    assert await _participations(session, card_id) == [(member.id, "selected", "")]


async def test_join_last_seat_returns_chat_uuid(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02 стори 6.2, D-6.2-5 (CR-2 HIGH): ответ на ПОСЛЕДНЕЕ место несёт `chat_uuid`.

    Краснеет, если в роутере оставить хардкод `chat_uuid=None`: чат уже
    создан в этой же транзакции, но клиент получал бы `null` и не мог
    открыть переписку без перезагрузки карточки. Второе значение признака
    (§18.11) — соседний тест `test_join_increments_seats_and_keeps_published`
    (`capacity=4, filled=2`): там `chat_uuid` обязан остаться `null`.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=2, filled_count=1
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "full"
    assert body["filled_count"] == 2
    chat_uuid = body["chat_uuid"]
    assert chat_uuid is not None, body
    # Контракт 6.1: `chats.uuid == event_cards.chat_uuid`, и ответ отдаёт
    # РОВНО записанное значение, а не сгенерированное в роутере.
    row = await session.execute(
        text("SELECT uuid FROM chats WHERE event_card_id = :id"), {"id": card_id}
    )
    assert str(row.scalar_one()) == chat_uuid


async def test_participation_row_carries_selected_empty_message_and_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (D-5а.5-1): участие — `selected`, `message=''`, `decided_at`, окно карточки.

    Краснеет, если вставлять `pending` (тогда 5а.4 не увидит участника),
    оставить `decided_at` пустым (участие — уже решение) или не скопировать
    `expires_at` карточки (D-5a.1-5).
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4
    )

    assert (await _join(client, token, card_uuid)).status_code == 200

    row = await session.execute(
        text(
            "SELECT jr.status, jr.message, jr.decided_at, jr.expires_at, ec.expires_at "
            "FROM join_requests jr JOIN event_cards ec ON ec.id = jr.card_id "
            "WHERE jr.card_id = :id"
        ),
        {"id": card_id},
    )
    status, message, decided_at, jr_expires, card_expires = row.one()
    assert status == "selected"
    assert message == ""
    assert decided_at is not None
    assert jr_expires == card_expires


# --- AC-02: X-39 — `full` одним UPDATE -------------------------------------


async def test_capacity_one_join_is_rejected_before_any_write(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06 (FR70): `capacity=1` → `409 CAPACITY_ONE_USES_REQUESTS`, мест не тронуто.

    Режим берётся у модели (`is_request_mode`), а не сравнением `capacity`
    в сервисе: вторая копия условия разошлась бы при новой вместимости.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=1
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409
    assert response.json()["code"] == "CAPACITY_ONE_USES_REQUESTS"
    assert await _card_state(session, card_id) == (0, "published")
    assert await _participations(session, card_id) == []


async def test_last_seat_transitions_to_full_in_same_update(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, X-39 (FR84, ADR-10): занятие последнего места → `full` БЕЗ `500`.

    Главный тест стори. Краснеет на схеме `0011` в паре с двухшаговым путём:
    инкремент даёт `published` + `filled_count = capacity`, CHECK
    `ck_event_cards_full_capacity` его отвергает, и клиент получает `500`.
    Один `UPDATE` с `CASE` проходит CHECK, потому что промежуточного
    состояния не существует ни в одной версии строки.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=1 + 1, filled_count=1
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filled_count"] == 2
    assert body["status"] == "full"
    assert await _card_state(session, card_id) == (2, "full")


async def test_full_card_returns_card_is_full_not_not_accepting(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (FR84): `full` → `409 CARD_IS_FULL`, отличный от `CARD_NOT_ACCEPTING`.
    П8 (FR84): `filled_count == capacity` → карточка `full`, вход закрыт.

    Краснеет, если коды слить: клиент не отличит «мест нет» (можно вернуться
    после отмены участия, 5а.6) от «набор закрыт» (окончательно).
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=3, filled_count=3, status="full"
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_IS_FULL"
    assert await _card_state(session, card_id) == (3, "full")


# --- AC-07: не принимает ---------------------------------------------------


@pytest.mark.parametrize("card_status", ["draft", "closed", "finished", "cancelled"])
async def test_non_published_card_is_not_accepting(
    client: AsyncClient, session: AsyncSession, card_status: str
) -> None:
    """AC-07 (FR74): `draft`/`closed`/`finished`/`cancelled` → `CARD_NOT_ACCEPTING`.

    Краснеет, если проверку статуса снять: `UPDATE` не совпал бы, и клиент
    получил бы `CARD_IS_FULL` (неверное действие) вместо «набор закрыт».
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _ = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, status=card_status
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"


# --- AC-09: идемпотентность пары -------------------------------------------


async def test_repeat_join_returns_already_joined(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09 (FR83): повторное присоединение → `409 ALREADY_JOINED`.

    Гонку пары держит частичный `uq_join_requests_card_applicant_active`,
    а не проверка-перед-вставкой (§18.3). Краснеет, если читать
    `constraint_name_of` неправильно: дубль уходил бы сырым `500`.

    Второй `POST` выполняется при СВОБОДНЫХ местах, поэтому `UPDATE` мест
    успевает пройти и откатывается вместе с `SAVEPOINT`; `filled_count`
    обязан остаться прежним.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4
    )

    assert (await _join(client, token, card_uuid)).status_code == 200
    filled_after_first, _ = await _card_state(session, card_id)

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ALREADY_JOINED"
    assert len(await _participations(session, card_id)) == 1
    # Место не удвоилось: `UPDATE` откатился вместе с вставкой.
    assert (await _card_state(session, card_id))[0] == filled_after_first


async def test_racing_pair_insert_rolls_back_seat(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-09 (CR 5а.5): гонка пары отбивается индексом, место НЕ утекает.

    Предварительная проверка `has_participation` закрывает только
    ПОСЛЕДОВАТЕЛЬНЫЙ повтор. Параллельную вставку ловит частичный `uq` на
    `INSERT` — уже ПОСЛЕ инкремента мест. Здесь эта ветка воспроизводится
    точно: проверка намеренно отвечает «пары нет», хотя строка участия уже
    есть (так выглядела бы гонка двух первых `POST`).

    Краснеет, если `UPDATE` мест вынести из `begin_nested`: откат снимет
    только вставку, `filled_count` вырастет без участника (дефект, найденный
    DS). Краснеет и от снятия `constraint_name_of` — дубль уйдёт сырым `500`.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4
    )
    assert (await _join(client, token, card_uuid)).status_code == 200
    filled_before, _ = await _card_state(session, card_id)

    async def _no_participation(self: Any, *, card_id: int, applicant_id: int) -> bool:
        return False

    monkeypatch.setattr(JoinRequestRepository, "has_participation", _no_participation)
    response = await _join(client, token, card_uuid)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ALREADY_JOINED"
    # Место не удвоилось: инкремент откатился вместе с `SAVEPOINT`.
    assert (await _card_state(session, card_id))[0] == filled_before
    assert len(await _participations(session, card_id)) == 1


async def test_repeat_join_on_full_card_returns_already_joined(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-09 vs AC-08 (CR 5а.5, дефект): повтор ПОСЛЕ занятия последнего места.

    Участник, занявший последнее место, переводит карточку в `full`; его
    повторный `POST` обязан вернуть `ALREADY_JOINED`, а не `CARD_IS_FULL`.
    Иначе клиент показывает «мест нет» человеку, который в этой компании уже
    состоит, и предлагает искать другую карточку.

    Краснеет от снятия предварительной проверки пары (`has_participation`)
    либо от переноса проверки `full` ПЕРЕД ней.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=2, filled_count=1
    )

    assert (await _join(client, token, card_uuid)).status_code == 200
    assert await _card_state(session, card_id) == (2, "full")

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ALREADY_JOINED"
    assert await _card_state(session, card_id) == (2, "full")
    assert len(await _participations(session, card_id)) == 1


async def test_capacity_one_full_card_stays_request_mode(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06 (CR 5а.5, дефект): `capacity=1` в состоянии `full` — режим заявок.

    AC-06 не оговаривает статус карточки: режим определяется ВМЕСТИМОСТЬЮ.
    Прежний порядок (проверка `full` до `is_request_mode`) отдавал
    `CARD_IS_FULL`, и клиент не предлагал единственное доступное действие —
    подать заявку.

    Краснеет от переноса проверки `is_request_mode` ПОСЛЕ статусных веток.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=1, filled_count=1, status="full"
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CAPACITY_ONE_USES_REQUESTS"
    assert await _card_state(session, card_id) == (1, "full")
    assert await _participations(session, card_id) == []


# --- AC-10: своя карточка --------------------------------------------------


async def test_author_cannot_join_own_card(client: AsyncClient, session: AsyncSession) -> None:
    """AC-10 (FR76): своя карточка → `403 JOIN_REQUEST_OWN_CARD`.

    Тот же код, что у заявок 5а.2: две формы одного запрета заставили бы
    клиента ветвиться по маршруту. `403`, а не `404`: автор знает, что
    карточка его.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 403
    assert response.json()["code"] == "JOIN_REQUEST_OWN_CARD"
    assert await _card_state(session, card_id) == (0, "published")


# --- AC-11: 404 ------------------------------------------------------------


async def test_missing_card_returns_card_not_found(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-11 (ADR-13): несуществующая карточка → `404 card_not_found`."""
    _, token = await _new_user(session)

    response = await _join(client, token, str(uuid_lib.uuid4()))

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


async def test_foreign_city_card_is_indistinguishable_from_missing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-11 (ADR-13): чужая карточка и несуществующая дают ОДИН ответ.

    Краснеет, если срез `city_slug` убрать из SQL и проверять после
    выборки: тела ответов разошлись бы (`403`/`409` против `404`), и перебор
    `uuid` раскрыл бы существование карточек чужого сообщества.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _ = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4, city_slug="othertown"
    )

    foreign = await _join(client, token, card_uuid)
    missing = await _join(client, token, str(uuid_lib.uuid4()))

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"code": "card_not_found"}


# --- AC-12: FR105 ----------------------------------------------------------


async def test_pending_requests_do_not_block_or_consume_join(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-12 (FR105): 2 `pending`-заявки не мешают присоединению и не меняются.

    Лимит 2 считается ТОЛЬКО по `pending` (`JOIN_REQUEST_ACTIVE_STATUSES`),
    а участие живёт в `selected`. Краснеет, если считать участие в лимите
    или добавлять его в `count_active`: FR105 нарушился бы, и участник
    открытого набора не смог бы параллельно заявиться в закрытый.
    """
    author, _ = await _new_user(session)
    member, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=4
    )

    # Две активные заявки участника на ДРУГИЕ карточки (capacity=1).
    for _ in range(2):
        other_uuid, other_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=1
        )
        assert other_uuid
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
                "VALUES (:card, :applicant, 'привет', 'pending', now() + interval '1 day')"
            ),
            {"card": other_id, "applicant": member.id},
        )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200, response.text
    row = await session.execute(
        text("SELECT count(*) FROM join_requests WHERE applicant_id = :id AND status = 'pending'"),
        {"id": member.id},
    )
    assert int(row.scalar_one()) == 2
    assert await _card_state(session, card_id) == (1, "published")


# --- AC-14: лента и has_seats ---------------------------------------------


async def test_full_card_leaves_has_seats_feed(client: AsyncClient, session: AsyncSession) -> None:
    """AC-14 (FR84): после `full` карточка есть в ленте, но не под `has_seats=true`.

    `has_seats` считается по `filled_count < capacity` (4.5), поэтому
    объединённый `UPDATE` обязан обновить оба поля согласованно. Краснеет,
    если `status` ставить без инкремента или инкремент без `CASE`: фильтр
    опирался бы на арифметику, расходящуюся со статусом.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=2, filled_count=1
    )

    assert (await _join(client, token, card_uuid)).status_code == 200
    assert await _card_state(session, card_id) == (2, "full")

    feed_all = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})
    assert feed_all.status_code == 200
    assert card_uuid in {item["uuid"] for item in feed_all.json()["items"]}

    feed_seats = await client.get(
        "/api/v1/cards", params={"has_seats": "true"}, cookies={ACCESS_COOKIE_NAME: token}
    )
    assert feed_seats.status_code == 200
    assert card_uuid not in {item["uuid"] for item in feed_seats.json()["items"]}


async def test_join_response_is_not_cached(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, AC-14: ответ на присоединение не кешируется.

    `no-store` ставится middleware на каждый ответ: `200` раскрывает факт
    участия, `409`/`404` — существование карточки.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _ = await _insert_card(session, author_id=author.id, place_id=place_id, capacity=4)

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


async def test_numeric_card_path_is_not_resolved(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-11 (ADR-13, §7): `/cards/1/join` не резолвится в карточку.

    Числовой путь отбивает Pydantic (`422`), а не драйвер (`500`): иначе
    перебор `/1..N` раскрыл бы существование карточек по коду ответа.
    """
    _, token = await _new_user(session)

    response = await client.post("/api/v1/cards/1/join", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 422
