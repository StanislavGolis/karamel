"""Тесты патчей CR 4.5 (2026-10-24): сужение `category_invalid`, коммит только
при мутации, гонка ленивого закрытия окна на двух соединениях.

§18.1: каждый тест краснеет от отката конкретного патча.
§18.2: проверяется наблюдаемое поведение (тело ответа, число коммитов,
итоговый статус строки), а не константа в самом тесте.
§18.3: гонка проверяется на ДВУХ соединениях с настоящими коммитами —
в одной транзакции второй запрос видит незакоммиченные изменения первого.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="нужен DATABASE_URL"),
]


def _community() -> str:
    return settings.default_community_slug


def _future(**delta: int) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
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
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _community()},
    )
    return int(row.scalar_one())


async def _card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    status: str = "published",
) -> tuple[int, str]:
    starts = starts_at or _future(days=2)
    expires = expires_at or (starts - timedelta(hours=1))
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 2, 60, "
            ":expires, :status, 0, 'approved', :published_at) RETURNING id, uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": _community(),
            "starts": starts,
            "expires": expires,
            "status": status,
            "published_at": starts - timedelta(days=1) if status != "draft" else None,
        },
    )
    mapping = row.mappings().one()
    return int(mapping["id"]), str(mapping["uuid"])


# --- CR-патч 1: `category_invalid` только для query ленты ---------------------


async def test_bad_category_in_request_body_keeps_invalid_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 4.5, патч 1: тело `POST /cards` с мусорной категорией — `invalid_request`.

    Регресс, найденный ревью ИСПОЛНЕНИЕМ: расширение ветки `category_invalid`
    на префикс `/api/v1/cards` меняло ответ уже одобренной стори 4.2 —
    `POST /cards` с `category: 'movies'` в теле отдавал `category_invalid`
    с перечнем категорий, хотя это форма создания, а не фильтр.

    Краснеет, если снять сужение по `loc[0] == "query"`: клиент 4.2 получит
    чужой код и начнёт показывать выпадашку категорий над полем формы.
    """
    _user, token = await _new_user(session)

    response = await client.post(
        "/api/v1/cards",
        json={
            "title": "x",
            "category": "movies",
            "starts_at": _future(days=2).isoformat(),
            "capacity": 2,
        },
        headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


async def test_bad_category_in_query_keeps_domain_code(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 4.5, патч 1: query-параметр ленты — по-прежнему `category_invalid`.

    Краснеет, если сузить ветку СЛИШКОМ сильно (например, только `/places`):
    AC-05 требует доменный код и перечень на фильтре ленты.
    """
    _user, token = await _new_user(session)

    response = await client.get(
        "/api/v1/cards?category=movies", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "category_invalid"
    assert "cinema" in body["details"]["allowed"]


async def test_places_body_category_still_gets_domain_code(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 4.5, патч 1: `/places` с телом не изменился — регресс 3.2 исключён.

    Краснеет, если сужение сделать общим (по `loc` для всех путей): админка
    справочника потеряла бы перечень категорий на ошибке формы (CR 3.1,
    патч 9).
    """
    # Проверка через HTTP здесь не даёт результата: без сессии маршрут отдаёт
    # `401` ДО валидации Pydantic, поэтому обработчик вызывается напрямую ниже.
    await client.post(
        "/api/v1/places", json={"name": "x", "category": "movies", "address_text": "y"}
    )

    # Без сессии — `401` до валидации; поэтому проверяется обработчик напрямую
    # (как в `test_error_envelope_debts.py`), а не через HTTP.
    from fastapi.exceptions import RequestValidationError
    from starlette.requests import Request

    from app.main import validation_error_handler

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/places", "headers": []})
    error = RequestValidationError(
        [{"type": "enum", "loc": ("body", "category"), "msg": "x", "input": "movies"}]
    )
    handler_response = await validation_error_handler(request, error)

    assert handler_response.status_code == 422
    body = handler_response.body.decode()
    assert "category_invalid" in body
    assert "allowed" in body


# --- CR-патч 2: коммит в `get_card` только при мутации ------------------------


async def test_get_card_without_mutation_does_not_commit(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 4.5, патч 2: просмотр без истёкшего окна НЕ коммитит.

    До патча `session.commit()` стоял в конце `get_card` безусловно: каждый
    `GET /cards/{uuid}` (самый частый запрос продукта) открывал и фиксировал
    транзакцию записи — лишний WAL и удержание соединения из пула.

    §18.2: проверяется НАБЛЮДАЕМОЕ число вызовов `commit` на конкретном
    сценарии, а не константа. Краснеет от возврата безусловного `commit`
    (тогда счётчик станет 1 вместо 0).
    """
    user, token = await _new_user(session)
    place = await _place(session)
    _card_id, card_uuid = await _card(session, author_id=user.id, place_id=place)

    commits: list[int] = []
    original_commit = AsyncSession.commit

    async def _counting_commit(self: AsyncSession) -> None:
        commits.append(1)
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _counting_commit, raising=True)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert commits == [], f"чтение без мутации закоммитило {len(commits)} раз"


async def test_get_card_with_expired_window_commits(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 4.5, патч 2: при фактической мутации коммит ОБЯЗАТЕЛЕН.

    Краснеет, если убрать `commit` вовсе: закрытие окна осталось бы только
    в сессии, и второй `GET` (другое соединение) снова увидел бы
    `published` — то есть ленивое закрытие перестало бы работать.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    _card_id, card_uuid = await _card(
        session,
        author_id=user.id,
        place_id=place,
        starts_at=_future(hours=2),
        expires_at=_future(hours=-1),
    )

    commits: list[int] = []
    original_commit = AsyncSession.commit

    async def _counting_commit(self: AsyncSession) -> None:
        commits.append(1)
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _counting_commit, raising=True)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    assert len(commits) == 1, "закрытие окна обязано фиксироваться"


# --- §18.3: гонка ленивого закрытия на ДВУХ соединениях -----------------------


@pytest.fixture
async def engine() -> AsyncIterator[Any]:
    """Собственный движок: настоящие коммиты, видимые всем соединениям."""
    database_engine = create_async_engine(DATABASE_URL, poolclass=None)
    try:
        yield database_engine
    finally:
        await database_engine.dispose()


async def test_lazy_close_race_updates_exactly_one_row(engine: Any) -> None:
    """AC-03, ADR-03, §18.3: два `close_if_due` одновременно → ровно один `UPDATE`.

    Доказательство — СЧЁТЧИК обновлённых строк, а не коды ответов: оба вызова
    возвращают успех, и по ответу гонку не увидеть (тот же приём, что
    в `test_card_publish_race.py`).

    Краснеет, если снять `status='published'` из предиката CAS: второй `UPDATE`
    нашёл бы строку и перезаписал `closed_at` — «продлил» закрытие.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with factory() as setup:
        phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(setup).create(
            provider="phone", provider_user_id=phone, phone=phone, gender="female"
        )
        await setup.commit()
        author_id = user.id

        place_row = await setup.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug, is_active) "
                "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _community()},
        )
        place_id = int(place_row.scalar_one())

        card_row = await setup.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, "
                "description, category, starts_at, capacity, decision_window_minutes, "
                "expires_at, status, filled_count, verification_status, published_at) "
                "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 2, 60, "
                ":expires, 'published', 0, 'approved', :published_at) RETURNING id"
            ),
            {
                "author": author_id,
                "place": place_id,
                "slug": _community(),
                "starts": _future(hours=1),
                "expires": _future(hours=-1),
                "published_at": _future(days=-1),
            },
        )
        card_id = int(card_row.scalar_one())
        await setup.commit()

    moment = datetime.now(UTC)

    async def _close() -> int:
        from app.repositories.event_cards import EventCardRepository

        async with factory() as runner:
            updated = await EventCardRepository(runner).close_if_due(card_id=card_id, now=moment)
            await runner.commit()
            return updated

    try:
        first, second = await asyncio.gather(_close(), _close())

        assert sorted([first, second]) == [0, 1], (
            f"гонка закрытия: ожидалось ровно одно обновление, получено {first} и {second}"
        )

        async with factory() as check:
            status_row = await check.execute(
                text("SELECT status FROM event_cards WHERE id = :id"), {"id": card_id}
            )
            assert status_row.scalar_one() == "closed"
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM event_cards WHERE id = :id"), {"id": card_id})
            await cleanup.execute(text("DELETE FROM places WHERE id = :id"), {"id": place_id})
            await cleanup.execute(text("DELETE FROM users WHERE id = :id"), {"id": author_id})
            await cleanup.commit()
