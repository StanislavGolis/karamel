"""Безличность отказа (стори 5б.2; AC-02, AC-03; FR81).

Главный риск стори (R-001 TD, score 9): `not_selected` не должен быть
выводим НИ ИЗ ОДНОГО байта ответа. Доказывается тремя независимыми
способами, каждый из которых краснеет от своей мутации:

1. SQL-фильтр `GET /my/requests` — проигравший не видит своей заявки
   (мутация M1: `not_selected` в `JOIN_REQUEST_VISIBLE_STATUSES`);
2. SQL-фильтр `GET /cards/{uuid}/requests` — автор видит только `selected`
   (мутация M2);
3. **глубокое равенство JSON** «проигравший == наблюдатель, который не
   подавал» + отсутствие запрещённых подстрок в сыром теле.

Плюс анти-оракул на `DELETE /requests/{uuid}`: `not_selected` и `expired`
дают ОДИН код `409 REQUEST_NOT_ACTIVE`. Различие раскрыло бы отказ через
ошибку вместо выдачи (FR81 обходится не только списком).
"""

from __future__ import annotations

import json
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

# AC-03: подстроки, которых не должно быть НИ В ОДНОМ ответе проигравшему.
# Список продублирован в тесте намеренно: он же читается человеком при
# ревью, и ссылка на константу сервиса сделала бы тест тавтологией.
FORBIDDEN_SUBSTRINGS = ("not_selected", "rejected", "отклон", "не выбран")


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


async def _insert_card(session: AsyncSession, *, author_id: int, place_id: int) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": starts,
            "expires": datetime.now(UTC) + timedelta(minutes=60),
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, status: str = "pending"
) -> str:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, expires_at) "
            "VALUES (:card, :applicant, '', :status, now() + interval '1 day') RETURNING uuid"
        ),
        {"card": card_id, "applicant": applicant_id, "status": status},
    )
    return str(row.scalar_one())


async def _select(client: AsyncClient, token: str, request_uuid: str) -> Any:
    return await client.post(
        f"/api/v1/requests/{request_uuid}/select", cookies={ACCESS_COOKIE_NAME: token}
    )


async def _get(
    client: AsyncClient, token: str, path: str, params: dict[str, Any] | None = None
) -> Any:
    return await client.get(path, params=params, cookies={ACCESS_COOKIE_NAME: token})


# --- AC-02: `not_selected` не покидает сервер ---------------------------------


async def test_loser_my_requests_hides_the_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR81, M1): у проигравшего заявки нет ни в `items`, ни в `total`.

    Краснеет от добавления `not_selected` в `JOIN_REQUEST_VISIBLE_STATUSES`:
    заявка появилась бы в списке, и человек узнал бы об отказе.

    `total` проверяется отдельно: фильтр в `WHERE` даёт и его, но копия
    условия в `COUNT` (если бы она была) разошлась бы молча.
    """
    author, author_token = await _new_user(session)
    winner, _ = await _new_user(session)
    loser, loser_token = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)
    await _insert_request(session, card_id=card_id, applicant_id=loser.id)

    assert (await _select(client, author_token, winner_request)).status_code == 200

    response = await _get(client, loser_token, "/api/v1/my/requests")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    for word in FORBIDDEN_SUBSTRINGS:
        assert word not in response.text.lower()


async def test_winner_my_requests_shows_selected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: выбранный видит свою заявку со `status='selected'`.

    Симметричная половина M1: фильтр не должен скрывать `selected`.
    """
    author, author_token = await _new_user(session)
    winner, winner_token = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)

    assert (await _select(client, author_token, winner_request)).status_code == 200

    response = await _get(client, winner_token, "/api/v1/my/requests")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "selected"


async def test_author_sees_only_selected_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR81, M2): автор видит ровно одну заявку — `selected`.

    Краснеет от добавления `not_selected` в `JOIN_REQUEST_APPLICANT_STATUSES`.
    """
    author, author_token = await _new_user(session)
    winner, _ = await _new_user(session)
    loser, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)
    await _insert_request(session, card_id=card_id, applicant_id=loser.id)

    assert (await _select(client, author_token, winner_request)).status_code == 200

    response = await _get(client, author_token, f"/api/v1/cards/{card_uuid}/requests")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    # `CardRequestsItem` не отдаёт `status` вовсе (AC-01 5а.4): автору не нужен
    # машинный статус, он и так знает своё решение. Поэтому единственность
    # выбранной доказывается `uuid`: виден РОВНО тот, кого выбрали, а заявка
    # проигравшего не появилась (M2 краснеет: `total` стал бы 2).
    assert body["items"][0]["uuid"] == winner_request
    for word in FORBIDDEN_SUBSTRINGS:
        assert word not in response.text.lower()


# --- AC-03: экраны проигравшего и наблюдателя идентичны -----------------------


def _without_server_time(body: dict[str, Any]) -> dict[str, Any]:
    """Копия тела карточки без `server_time`.

    Отклонение от буквального AC-03, зафиксированное в Dev Agent Record:
    `server_time` — серверный ЯКОРЬ отсчёта (NFR14, 5б.1), он обязан
    отличаться у двух последовательных запросов по определению. Сравнивать
    его — значит требовать остановки времени. Все остальные поля, включая
    `viewer_participates`, `filled_count`, `status` и `chat_uuid`, сравниваются
    глубоко и полностью.
    """
    copy = dict(body)
    copy.pop("server_time", None)
    return copy


async def test_card_screen_is_identical_for_loser_and_observer(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03 (R-001, score 9): тела `GET /cards/{uuid}` глубоко равны.

    Два РАЗНЫХ клиента: проигравший (его заявка стала `not_selected`) и
    наблюдатель, который не подавал вовсе. Любое поле, зависящее от статуса
    заявки, сделало бы их различимыми — это и есть утечка отказа.

    Краснеет от: `viewer_participates` для проигравшего (или любого поля
    «моя заявка» в ответе), от `not_selected` в тексте, от включения
    идентификатора заявки в тело карточки.
    """
    author, author_token = await _new_user(session)
    winner, _ = await _new_user(session)
    loser, loser_token = await _new_user(session)
    _observer, observer_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)
    await _insert_request(session, card_id=card_id, applicant_id=loser.id)

    assert (await _select(client, author_token, winner_request)).status_code == 200

    loser_response = await _get(client, loser_token, f"/api/v1/cards/{card_uuid}")
    observer_response = await _get(client, observer_token, f"/api/v1/cards/{card_uuid}")

    assert loser_response.status_code == 200, loser_response.text
    assert observer_response.status_code == 200, observer_response.text

    loser_body = json.loads(loser_response.text)
    observer_body = json.loads(observer_response.text)
    assert _without_server_time(loser_body) == _without_server_time(observer_body)

    # Признак участия у обоих `false` — проигравший НЕ участник, и это
    # единственная правда о нём, которую контракт позволяет показать.
    assert loser_body["viewer_participates"] is False
    assert loser_body["filled_count"] == 1
    assert loser_body["status"] == "full"
    assert loser_body["chat_uuid"]

    for response in (loser_response, observer_response):
        for word in FORBIDDEN_SUBSTRINGS:
            assert word not in response.text.lower()


async def test_feed_does_not_leak_not_selected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03: лента тоже не раскрывает отказ (сырой текст ответа).

    Проверяется ОТДЕЛЬНО от детального экрана: лента собирается другой
    веткой сервиса (`list_cards` + пакетный запрос участия), и утечка
    могла бы появиться только в ней.
    """
    author, author_token = await _new_user(session)
    winner, _ = await _new_user(session)
    loser, loser_token = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)
    await _insert_request(session, card_id=card_id, applicant_id=loser.id)

    assert (await _select(client, author_token, winner_request)).status_code == 200

    response = await _get(client, loser_token, "/api/v1/cards")

    assert response.status_code == 200, response.text
    for word in FORBIDDEN_SUBSTRINGS:
        assert word not in response.text.lower()


# --- Анти-оракул: `DELETE /requests/{uuid}` -----------------------------------


async def test_delete_of_not_selected_is_indistinguishable_from_expired(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03 (FR81): `not_selected` и `expired` → ОДИН код `REQUEST_NOT_ACTIVE`.

    Анти-оракул: безличность обходится не только списком, но и ошибкой.
    Разные коды на `DELETE` дали бы проигравшему точный ответ «вас не
    выбрали» точечным запросом.
    """
    author, author_token = await _new_user(session)
    winner, _ = await _new_user(session)
    loser, loser_token = await _new_user(session)
    expired_user, expired_token = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)
    loser_request = await _insert_request(session, card_id=card_id, applicant_id=loser.id)
    expired_request = await _insert_request(
        session, card_id=card_id, applicant_id=expired_user.id, status="expired"
    )

    assert (await _select(client, author_token, winner_request)).status_code == 200

    loser_delete = await client.delete(
        f"/api/v1/requests/{loser_request}", cookies={ACCESS_COOKIE_NAME: loser_token}
    )
    expired_delete = await client.delete(
        f"/api/v1/requests/{expired_request}", cookies={ACCESS_COOKIE_NAME: expired_token}
    )

    assert loser_delete.status_code == 409
    assert expired_delete.status_code == 409
    assert loser_delete.json() == expired_delete.json()
    assert loser_delete.json()["code"] == "REQUEST_NOT_ACTIVE"


# --- Повторная подача после выбора -------------------------------------------


async def test_resubmit_after_selection_uses_full_card_code(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: после выбора подача → `CARD_NOT_ACCEPTING` (как у `full` в 5б.1).

    Нового кода не вводится: причина та же — карточка собрана, набор закрыт.
    Отдельный код (`CARD_ALREADY_FILLED`) различил бы «вас не выбрали» от
    «карточка собрана до вашей подачи».
    """
    author, author_token = await _new_user(session)
    winner, _ = await _new_user(session)
    loser, loser_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
    winner_request = await _insert_request(session, card_id=card_id, applicant_id=winner.id)
    await _insert_request(session, card_id=card_id, applicant_id=loser.id)

    assert (await _select(client, author_token, winner_request)).status_code == 200

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "Ещё раз"},
        cookies={ACCESS_COOKIE_NAME: loser_token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"
