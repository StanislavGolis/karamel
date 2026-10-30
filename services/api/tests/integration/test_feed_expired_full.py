"""OQ-5b.1-1, вариант 3 (стори 5б.2; AC-08, D-5b.2-6).

Поведение, которое обязано быть запинено:

1. `full` с ИСТЁКШИМ окном остаётся в ленте как состоявшаяся компания;
2. участие в ней невозможно: `POST /join` → `409 CARD_NOT_ACCEPTING`
   (гард окна 5б.1 не снимается);
3. после освобождения места (`DELETE /join`, 5а.6) карточка становится
   `published` с истёкшим окном → уходит ИЗ ЛЕНТЫ и закрывается воркером
   в `closed` (<=60 с). Освободившееся место НЕ переиспользуется.

Пункт 3 — принятое последствие варианта 3, а не недосмотр: альтернативы
(показывать `published` с истёкшим окном, снимать гард окна) ломают AC-02
стори 5б.1 и позволяют занять место там, где решение уже не принимается.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
from app.workers.card_windows import close_due_windows

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


class _NoLimit:
    """AC-08: лимитер без ограничений — техническая квота проверяется отдельно.

    Без подмены `POST /requests` в тесте расходует реальную квоту Redis
    (10 подач / 24 ч на аккаунт), и в полном прогоне тест получает
    `429 rate_limited` вместо доменного кода: результат зависел бы от порядка
    запуска. Тот же приём, что в `test_join_requests_api` и
    `test_card_decision_window_api`.
    """

    async def hit(self, applicant_id: int) -> int | None:
        return None


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент на общей сессии: `commit()` сервиса освобождает SAVEPOINT."""

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
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    return int(row.scalar_one())


async def _insert_full_card_with_expired_window(
    session: AsyncSession, *, author_id: int, place_id: int
) -> tuple[str, int]:
    """`full` (1/1) с истёкшим окном — целевое состояние OQ-5b.1-1."""
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Собранная', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'full', 1, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "expires": datetime.now(UTC) - timedelta(minutes=5),
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_selected(session: AsyncSession, *, card_id: int, applicant_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, decided_at, "
            "expires_at) VALUES (:card, :applicant, '', 'selected', now(), "
            "now() + interval '1 day')"
        ),
        {"card": card_id, "applicant": applicant_id},
    )


async def _feed_uuids(client: AsyncClient, token: str) -> list[str]:
    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})
    assert response.status_code == 200, response.text
    return [item["uuid"] for item in response.json()["items"]]


async def _card_status(session: AsyncSession, card_uuid: str) -> str:
    row = await session.execute(
        text("SELECT status FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    return str(row.scalar_one())


async def test_full_card_with_expired_window_stays_in_feed_and_blocks_join(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (1+2): `full` в ленте, но участие невозможно — `409`.

    Краснеет от двух правок сразу: (а) привязки предиката окна ко всем
    статусам — карточка исчезла бы из ленты (ломает OQ-5b.1-1 вариант 3);
    (б) снятия гарда окна в `join_open_card` — участник занял бы место
    в компании, где решение уже не принимается.
    """
    author, _ = await _new_user(session)
    _member, _ = await _new_user(session)
    viewer, viewer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_full_card_with_expired_window(
        session, author_id=author.id, place_id=place_id
    )

    assert card_uuid in await _feed_uuids(client, viewer_token)

    # Заявка на собранную карточку: режим верный (capacity=1), поэтому решает
    # гард окна — `409 CARD_NOT_ACCEPTING`, а не новый код.
    request_response = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "Хочу в компанию"},
        cookies={ACCESS_COOKIE_NAME: viewer_token},
    )

    assert request_response.status_code == 409, request_response.text
    assert request_response.json()["code"] == "CARD_NOT_ACCEPTING"

    # Присоединение к карточке режима заявок невозможно В ПРИНЦИПЕ (FR70) —
    # проверка режима идёт раньше статуса и окна (5а.5). Это не ослабление
    # AC-08: `join` для `capacity=1` не существует, и отдельный тест ниже
    # доказывает `CARD_NOT_ACCEPTING` для карточки открытого набора.
    join_response = await client.post(
        f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )
    assert join_response.status_code == 409, join_response.text
    assert join_response.json()["code"] == "CAPACITY_ONE_USES_REQUESTS"

    row = await session.execute(
        text("SELECT filled_count FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    assert int(row.scalar_one()) == 1, "место не должно заниматься"


async def test_join_on_full_open_set_card_with_expired_window_is_not_accepting(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (2): карточка ОТКРЫТОГО набора `full` + истёкшее окно → `409`.

    Вторая половина AC-08, недостижимая на `capacity=1` (там `join` не
    существует): для набора 2..6 режим проверку проходит, и решает гард окна.

    Краснеет от снятия гарда `expires_at <= now` в `join_open_card`.
    """
    author, _ = await _new_user(session)
    viewer, viewer_token = await _new_user(session)
    place_id = await _place(session)
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Собранная', NULL, 'cafe', :starts, 3, 60, "
            ":expires, 'full', 3, 'approved', :published_at, 'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author.id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "expires": datetime.now(UTC) - timedelta(minutes=5),
            "published_at": starts - timedelta(days=1),
        },
    )
    open_set_uuid = str(row.scalar_one())

    response = await client.post(
        f"/api/v1/cards/{open_set_uuid}/join", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"


async def test_released_seat_on_expired_window_leaves_feed_and_gets_closed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08 (3): освобождение места → уход из ленты → воркер → `closed`.

    Прямая запись последствия варианта 3: место НЕ переиспользуется.

    Краснеет от: (а) удаления предиката окна из `build_feed_statement` —
    освобождённая карточка осталась бы «открытой» в ленте с истёкшим окном;
    (б) снятия `expires_at` из `close_due_windows` — карточка навсегда
    осталась бы `published`.
    """
    author, _ = await _new_user(session)
    member, member_token = await _new_user(session)
    viewer, viewer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_full_card_with_expired_window(
        session, author_id=author.id, place_id=place_id
    )
    await _insert_selected(session, card_id=card_id, applicant_id=member.id)

    leave = await client.delete(
        f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: member_token}
    )

    assert leave.status_code == 204, leave.text
    # 5а.6: `full -> published` одним `UPDATE`, место освобождено.
    assert await _card_status(session, card_uuid) == "published"
    # Вариант 3: `published` с истёкшим окном в ленте НЕ показывается.
    assert card_uuid not in await _feed_uuids(client, viewer_token)

    # Воркер `card_windows` закрывает её в течение одного прогона.
    closed = await close_due_windows(session, now=datetime.now(UTC))
    assert closed >= 1
    assert await _card_status(session, card_uuid) == "closed"


async def test_released_seat_is_not_reusable_via_join(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08: освободившееся место на истёкшем окне занять нельзя — `409`.

    Это ЯВНОЕ последствие варианта 3 (D-5b.2-6): «участие возможно после
    отмены чужого участия» — ложная формулировка, и тест её фиксирует.
    """
    author, _ = await _new_user(session)
    member, member_token = await _new_user(session)
    newcomer, newcomer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_full_card_with_expired_window(
        session, author_id=author.id, place_id=place_id
    )
    await _insert_selected(session, card_id=card_id, applicant_id=member.id)

    assert (
        await client.delete(
            f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: member_token}
        )
    ).status_code == 204

    # Место освободилось, но занять его нельзя: карточка `capacity=1` —
    # режим заявок, а заявка упирается в истёкшее окно.
    response = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "Я вместо него"},
        cookies={ACCESS_COOKIE_NAME: newcomer_token},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"
    row = await session.execute(
        text("SELECT filled_count FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    assert int(row.scalar_one()) == 0, "место осталось свободным и не переиспользовалось"
