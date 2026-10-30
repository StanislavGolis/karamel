"""Анти-оракул состава: ВСЕ заголовки и БАЙТЫ тела (CR 6.3; AC-02, ADR-13).

DS-тест сверял только `status_code`, `json()` и `content-type`. Этого мало:
различие в `WWW-Authenticate`, `Retry-After`, `content-length` или порядке
JSON-ключей делает постороннего отличимым от «карточки нет» — то есть
раскрывает существование (NFR33). Здесь сравниваются ВСЕ заголовки и сырые
байты ответа, и отдельно — `pending`-заявитель (его тело не должно отличаться
от несуществующей карточки).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import chat_participants_service, media_service
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


@pytest.fixture(autouse=True)
def _no_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_service, "get_signed_url", lambda key, ttl=None: "https://x/y")
    monkeypatch.setattr(
        chat_participants_service, "get_signed_url", lambda key, ttl=None: "https://x/y"
    )
    media_service.reset_client()


async def _user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
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


async def _card(session: AsyncSession, *, author_id: int) -> tuple[str, int]:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    place_id = int(row.scalar_one())
    starts = datetime.now(UTC) + timedelta(days=3)
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:a, :p, :slug, 'Встреча', NULL, 'cafe', :s, 2, 60, :e, 'full', 2, "
            "'approved', :pub, 'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "slug": settings.default_city_slug,
            "s": starts,
            "e": starts - timedelta(hours=1),
            "pub": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = card.one()
    return str(card_uuid), int(card_id)


def _headers(response: Response) -> dict[str, str]:
    """Все заголовки, кроме заведомо переменных (`date`, `server`)."""
    skip = {"date", "server"}
    return {k.lower(): v for k, v in response.headers.items() if k.lower() not in skip}


async def test_all_headers_and_body_bytes_identical(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (ADR-13, NFR33): посторонний неотличим от «карточки нет».

    Сравниваются ВСЕ заголовки и СЫРЫЕ байты. Различие в `content-length`
    или наличии `WWW-Authenticate` — утечка существования.
    """
    author, _ = await _user(session)
    card_uuid, _ = await _card(session, author_id=author.id)
    outsider, token = await _user(session)
    await session.flush()

    existing = await client.get(
        f"/api/v1/cards/{card_uuid}/participants",
        headers={"cookie": f"{ACCESS_COOKIE_NAME}={token}"},
    )
    missing = await client.get(
        f"/api/v1/cards/{uuid_lib.uuid4()}/participants",
        headers={"cookie": f"{ACCESS_COOKIE_NAME}={token}"},
    )

    assert existing.status_code == missing.status_code == 404
    assert _headers(existing) == _headers(missing), (
        f"заголовки расходятся: {set(_headers(existing).items()) ^ set(_headers(missing).items())}"
    )
    assert existing.content == missing.content


async def test_query_count_identical_on_404_paths(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: «посторонний» и «карточки нет» стоят ОДИНАКОВОЕ число запросов.

    Тайминг-оракул: если на существующую карточку сервис делает больше
    запросов (читает состав, подписывает фото), посторонний отличит её от
    несуществующей по времени ответа — та же утечка, что разный статус.

    Тест ловит регрессию, при которой проверку участия перенесут ПОСЛЕ
    выборки состава: тогда на существующей карточке появился бы лишний
    запрос, и число разошлось бы.
    """
    from sqlalchemy import event

    author, _ = await _user(session)
    card_uuid, _ = await _card(session, author_id=author.id)
    outsider, token = await _user(session)
    await session.flush()

    counter = {"n": 0}

    def _bump(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", _bump)
    try:
        counter["n"] = 0
        await client.get(
            f"/api/v1/cards/{card_uuid}/participants",
            headers={"cookie": f"{ACCESS_COOKIE_NAME}={token}"},
        )
        existing_count = counter["n"]

        counter["n"] = 0
        await client.get(
            f"/api/v1/cards/{uuid_lib.uuid4()}/participants",
            headers={"cookie": f"{ACCESS_COOKIE_NAME}={token}"},
        )
        missing_count = counter["n"]
    finally:
        event.remove(listen_engine, "before_cursor_execute", _bump)

    assert existing_count == missing_count, (
        f"тайминг-оракул: существующая {existing_count} запросов, несуществующая {missing_count}"
    )


async def test_pending_applicant_bytes_identical_to_missing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: `pending`-заявитель неотличим от несуществующей карточки.

    Заявитель ЗНАЕТ `uuid` карточки — если его ответ отличается от «нет
    карточки», он узнаёт, что карточка жива, и что его заявку не выбрали
    (утечка исхода выбора, FR81).
    """
    author, _ = await _user(session)
    card_uuid, card_id = await _card(session, author_id=author.id)
    applicant, token = await _user(session)
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:c, :a, '', 'pending', now(), now() + interval '1 day')"
        ),
        {"c": card_id, "a": applicant.id},
    )
    await session.flush()

    pending = await client.get(
        f"/api/v1/cards/{card_uuid}/participants",
        headers={"cookie": f"{ACCESS_COOKIE_NAME}={token}"},
    )
    missing = await client.get(
        f"/api/v1/cards/{uuid_lib.uuid4()}/participants",
        headers={"cookie": f"{ACCESS_COOKIE_NAME}={token}"},
    )

    assert pending.status_code == missing.status_code == 404
    assert pending.content == missing.content
    assert _headers(pending) == _headers(missing)
