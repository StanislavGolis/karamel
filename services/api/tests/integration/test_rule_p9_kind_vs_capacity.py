"""П9: механику задаёт ВМЕСТИМОСТЬ, а не ТИП встречи (FR70, FR118).

**Что доказывается.** Тип (`kind`) — про смысл встречи: `company` (компания
для активности) или `date` (свидание). Механику участия задаёт
`capacity`: `1` — режим заявок (`POST /join` отбивает
`409 CAPACITY_ONE_USES_REQUESTS`), `2..6` — открытый набор (мгновенное
присоединение, `201`).

Правило П9 говорит именно это: «тип компании → мгновенно; даты → заявки»
— НЕВЕРНОЕ прочтение. Верное: **ось типа не подменяет ось
вместимости**. Поэтому проверяются все четыре комбинации, а не две: если
бы код ветвился по `kind`, одна из четырёх покраснела бы.

**Почему нужен именно новый тест (замер CS 12.9).** Существующие тесты
покрывают только ось ВМЕСТИМОСТИ (`test_capacity_one_join_is_rejected`,
`test_join_increments_seats`) и всегда на карточке `company`. Комбинация
«`date` + `capacity=3`» не проверялась нигде, поэтому ветвление по `kind`
прошло бы приёмку.

Метка `П9` в докстринге обязательна: реестр правил
(`test_rules_registry.py`) находит тест по ней (AC-1).
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
from app.services.join_request_service import CAPACITY_ONE_USES_REQUESTS_CODE
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


class _NoLimit:
    """Лимитер без Redis: `POST /requests` под ним проверяет домен, а не шину."""

    async def hit(self, applicant_id: int) -> int | None:
        return None


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент на общей сессии: `commit()` сервиса освобождает SAVEPOINT."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    # M-1 (CR 12.9): `POST /requests` идёт через `get_join_request_rate_limiter`.
    # Без подмены он полез бы в Redis — тест перестал бы быть интеграционным
    # (AC-6: только PostgreSQL).
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


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    kind: str,
    capacity: int,
) -> tuple[str, int]:
    """Карточка с ЯВНО заданными `kind` и `capacity` — обе оси под контролем."""
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', 0, 'approved', :published_at, :kind, 'coffee') "
            "RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
            "kind": kind,
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _join(client: AsyncClient, token: str, card_uuid: str) -> Any:
    return await client.post(
        f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: token}
    )


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str]:
    row = await session.execute(
        text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    filled, status = row.one()
    return int(filled), str(status)


@pytest.mark.parametrize("kind", ["company", "date"])
async def test_open_set_joins_immediately_regardless_of_kind(
    client: AsyncClient, session: AsyncSession, kind: str
) -> None:
    """П9: `capacity=3` → мгновенное присоединение (`200`) при ЛЮБОМ `kind`.

    Код `200`, а не `201`: контракт `POST /cards/{uuid}/join` возвращает
    `EventCardRef` с текущим состоянием карточки (замер: `test_card_join_api.py`
    везде ассертит `200`). Пара к тесту ниже: если бы код ветвился по типу
    («даты → заявки»), `date + capacity=3` вернул бы `409`/`403`.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, kind=kind, capacity=3
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 200, f"{kind} + capacity=3: {response.text}"
    # Место занято тем же действием: не «заявка подана», а «участие есть».
    assert await _card_state(session, card_id) == (1, "published")


@pytest.mark.parametrize("kind", ["company", "date"])
async def test_capacity_one_uses_requests_regardless_of_kind(
    client: AsyncClient, session: AsyncSession, kind: str
) -> None:
    """П9: `capacity=1` → `409 CAPACITY_ONE_USES_REQUESTS` при ЛЮБОМ `kind`.

    Пара к тесту выше. Заодно краснеет от подмены кода на `CARD_IS_FULL`:
    клиент показал бы «мест нет» там, где единственное действие — подать
    заявку (докстринг `card_join_service`, шаг 5).
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, kind=kind, capacity=1
    )

    response = await _join(client, token, card_uuid)

    assert response.status_code == 409, f"{kind} + capacity=1: {response.text}"
    assert response.json()["code"] == CAPACITY_ONE_USES_REQUESTS_CODE
    assert response.json()["code"] != "CARD_IS_FULL"
    # Отказ не должен двигать места: режим заявок вообще не занимает место.
    assert await _card_state(session, card_id) == (0, "published")


@pytest.mark.parametrize("kind", ["company", "date"])
async def test_requests_rejected_when_capacity_allows_open_join(
    client: AsyncClient, session: AsyncSession, kind: str
) -> None:
    """П9 (M-1 CR 12.9): `POST /requests` при `capacity>1` → `409 CAPACITY_ONE_USES_REQUESTS`.

    **Дыра, которую закрывает тест.** CR 12.9 (CR-13) снял проверку
    `if not card.is_request_mode:` в `join_request_service.py:361` — и все
    5 тестов П9 остались зелёными (`SURVIVED`), потому что били по
    `POST /join`. Свойство «заявки только при `capacity=1`» было покрыто
    наполовину: вход проверялся, выход — нет. Без этой проверки заявку
    можно подать на карточку `capacity=2..6`, где участие мгновенное, и
    автор получил бы заявку на открытый набор.

    Пара к `test_open_set_joins_immediately_regardless_of_kind`: там
    `POST /join` на `capacity=3` проходит, здесь `POST /requests` на той же
    карточке — отбивается. Одна ось, два противоположных исхода.
    """
    author, _ = await _new_user(session)
    _, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, kind=kind, capacity=3
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "Хочу присоединиться"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409, f"{kind} + capacity=3 через /requests: {response.text}"
    assert response.json()["code"] == CAPACITY_ONE_USES_REQUESTS_CODE
    # Заявка не создана и место не занято: отказ доменный, а не косметический.
    assert await _card_state(session, card_id) == (0, "published")
    requests_count = await session.execute(
        text("SELECT count(*) FROM join_requests WHERE card_id = :c"), {"c": card_id}
    )
    assert int(requests_count.scalar_one()) == 0, "заявка создана несмотря на отказ"


def test_kind_axis_does_not_decide_join_mechanics() -> None:
    """П9: режим участия вычисляется ТОЛЬКО из вместимости (модель, не текст).

    Краснеет, если `is_request_mode` начнёт смотреть на `kind`: тогда
    появился бы второй источник правды, и четыре комбинации выше перестали
    бы быть исчерпывающими.
    """
    from app.models.event_card import EventCard

    for capacity, expected in ((1, True), (2, False), (6, False)):
        card = EventCard(author_id=1, place_id=1, capacity=capacity)
        assert card.is_request_mode is expected
