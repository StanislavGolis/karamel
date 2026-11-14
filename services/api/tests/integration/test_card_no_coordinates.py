"""Карточка не принимает координаты в теле (стори 13.1, Task 5.1; AC-5, NFR34).

`places` координат не хранит (`information_schema` — `Task 5.3`), но это
защита ТАБЛИЦЫ. Клиент, передавший `lat`/`lng` в теле `POST /cards`, не
получал бы ошибки, если бы схема молча отбрасывала лишние поля: автор видел
бы `201`, считал бы, что точка встречи сохранена, и не понял бы, почему
карта пуста. `extra="forbid"` (`EventCardCreate`) превращает это в `422`.

§18.2: тест краснеет от снятия `extra="forbid"` — без него ответ `201`.
Пара к `test_no_coordinates_guard.py` (тот гард смотрит КОНТРАКТ ответов,
этот — ПРИЁМ тела).
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
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession) -> tuple[Any, str]:
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        status="active",
    )
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> str:
    """Активное место города: без него карточка не создаётся вообще.

    Имя уникально на город (`uq_places_city_name`), поэтому `uuid4` в хвосте:
    иначе второй тест в сессии упал бы на дубле, а не на проверяемом правиле.
    """
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'пр. Ленина, 1', :slug, true) RETURNING uuid"
        ),
        {
            "name": f"Кафе {uuid_lib.uuid4().hex[:8]}",
            "slug": settings.city_slugs[0],
        },
    )
    return str(row.scalar_one())


def _payload(place_uuid: str, **overrides: Any) -> dict[str, Any]:
    """Валидное тело создания: тест добавляет ровно одно лишнее поле."""
    base: dict[str, Any] = {
        "category": "cafe",
        "place_uuid": place_uuid,
        "kind": "company",
        "activity": "boardgames",
        "starts_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        "capacity": 3,
    }
    base.update(overrides)
    return base


async def _post(client: AsyncClient, token: str, payload: dict[str, Any]) -> Any:
    return await client.post("/api/v1/cards", json=payload, cookies={ACCESS_COOKIE_NAME: token})


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        # `lat`/`lng` ⊂ `COORDINATE_FIELDS` → глобальный обработчик валидации
        # отдаёт специфичный `invalid_coordinates` (`main.py:283`). Это ВЕРНО:
        # клиент различает «сломана форма» и «геолокация вернула мусор».
        ("lat", 56.4977, "invalid_coordinates"),
        ("lng", 84.9481, "invalid_coordinates"),
        # `coordinates`/`geo` в реестре координат НЕ перечислены (реестр узкий
        # по замыслу: `lat`/`lng`/`accuracy`) — их отбивает `extra="forbid"`,
        # и код общий. Проверяются обе формы: клиент выберет любую, чтобы
        # «передать точку одним полем».
        ("coordinates", [56.4977, 84.9481], "invalid_request"),
        ("geo", {"lat": 56.4977, "lng": 84.9481}, "invalid_request"),
    ],
)
async def test_coordinates_in_body_are_rejected(
    field: str,
    value: Any,
    expected_code: str,
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """AC-5: любая форма координаты в теле карточки → `422`.

    Четыре формы намеренно: `lat`/`lng` — прямое имя, `coordinates` —
    пара чисел массивом, `geo` — контейнер. Гард, ловящий только первое,
    пропустил бы третье (`{lat, lng}` внутри объекта).

    Код ответа — ЧАСТЬ проверки, а не украшение: два разных кода (`invalid_coordinates`
    и `invalid_request`) ведут клиента к разным действиям, и подмена одного
    другим сломала бы обработку, оставшись зелёной по статусу.
    """
    _, token = await _new_user(session)
    place_uuid = await _place(session)

    response = await _post(client, token, _payload(place_uuid, **{field: value}))

    assert response.status_code == 422, (
        f"координата в поле {field!r} прошла схему: {response.status_code} {response.text}"
    )
    assert response.json()["code"] == expected_code
    # Таблица не изменилась: отказ не должен создавать «пустую» карточку.
    count = await session.execute(text("SELECT count(*) FROM event_cards"))
    assert int(count.scalar_one()) == 0


async def test_valid_body_creates_card(client: AsyncClient, session: AsyncSession) -> None:
    """AC-5: пара к отказу — тело БЕЗ координат создаёт карточку (`201`).

    Краснеет, если запретить лишние поля «чересчур» (например, отвергать
    любой `dict` в теле): легальный запрос перестал бы работать.
    """
    _, token = await _new_user(session)
    place_uuid = await _place(session)

    response = await _post(client, token, _payload(place_uuid))

    assert response.status_code == 201, response.text
    assert "uuid" in response.json()
