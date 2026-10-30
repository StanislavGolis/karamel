"""Список чатов и непрочитанные через API (стори 6.6; AC-01…AC-06).

Реальный PostgreSQL, схема поднята Alembic (`0018` добавила
`messages.read_at`). Главные утверждения:

- **AC-01, AC-05**: `GET /chats` — один запрос, число НЕ зависит от числа
  чатов (счётчик `before_cursor_execute` на 1 и 5 чатах, NFR12);
- **AC-02**: `POST /chats/{uuid}/read` обнуляет `unread_count` и идемпотентен
  (повтор не переписывает `read_at`);
- **AC-02, ADR-13**: посторонний и несуществующий чат — побайтово один ответ
  И одинаковое число SQL-запросов (тайминг-оракул, урок F-1 стори 6.3);
- **AC-04**: чат без сообщений (или только с системными) остаётся в списке;
- **AC-06**: свои сообщения не считаются непрочитанными.
"""

from __future__ import annotations

import os
import time
import uuid as uuid_lib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_message_body
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
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


# ——— посев —————————————————————————————————————————————————————————————————


async def _user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
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


async def _card_with_chat(
    session: AsyncSession, *, author_id: int, capacity: int = 2, title: str = "Встреча"
) -> tuple[str, int, str]:
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_id = int(place.scalar_one())
    starts = datetime.now(UTC) + timedelta(days=3)
    chat_uuid = str(uuid_lib.uuid4())
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, chat_uuid, kind, activity) VALUES (:a, :p, :s, :t, "
            "'cafe', :st, :cap, 60, :e, 'full', :cap, 'approved', :pub, :chat,"
            "'company', 'coffee') "
            "RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_city_slug,
            "t": title,
            "st": starts,
            "cap": capacity,
            "e": starts - timedelta(hours=1),
            "pub": starts - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_uuid, card_id = card.one()
    await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (:u, :c)"),
        {"u": chat_uuid, "c": int(card_id)},
    )
    return str(card_uuid), int(card_id), chat_uuid


async def _participant(session: AsyncSession, *, card_id: int, user_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:c, :a, '', 'selected', now(), "
            "now() + interval '1 day', now())"
        ),
        {"c": card_id, "a": user_id},
    )


async def _message(
    session: AsyncSession,
    *,
    chat_uuid: str,
    sender_id: int,
    body: str = "привет",
    created_at: datetime | None = None,
    read_at: datetime | None = None,
) -> str:
    """Вставляет сообщение штатным шифрованием (NFR31)."""
    chat_row = await session.execute(text("SELECT id FROM chats WHERE uuid = :u"), {"u": chat_uuid})
    chat_id = int(chat_row.scalar_one())
    message_uuid = str(uuid_lib.uuid4())
    ciphertext = encrypt_message_body(
        body, chat_id=chat_id, sender_id=sender_id, key=settings.chat_message_key
    )
    if created_at is None:
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, read_at) "
                "VALUES (:u, :c, :s, :b, :r)"
            ),
            {"u": message_uuid, "c": chat_id, "s": sender_id, "b": ciphertext, "r": read_at},
        )
    else:
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, created_at, read_at) "
                "VALUES (:u, :c, :s, :b, :created, :r)"
            ),
            {
                "u": message_uuid,
                "c": chat_id,
                "s": sender_id,
                "b": ciphertext,
                "created": created_at,
                "r": read_at,
            },
        )
    return message_uuid


def _auth(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


@dataclass
class Company:
    author: User
    author_token: str
    member: User
    member_token: str
    card_uuid: str
    card_id: int
    chat_uuid: str


async def _company(session: AsyncSession, *, title: str = "Встреча") -> Company:
    author, author_token = await _user(session)
    member, member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(session, author_id=author.id, title=title)
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()
    return Company(
        author=author,
        author_token=author_token,
        member=member,
        member_token=member_token,
        card_uuid=card_uuid,
        card_id=card_id,
        chat_uuid=chat_uuid,
    )


@dataclass
class _QueryCounter:
    count: int = field(default=0)

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1


def _listen(session: AsyncSession, counter: _QueryCounter) -> Any:
    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", counter)
    return listen_engine


# ——— AC-01: список и последнее сообщение ———


async def test_list_returns_chat_with_last_message_and_unread(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: превью последнего сообщения и счётчик — в одном ответе.

    Краснеет, если последнее сообщение выбирается НЕ по `created_at DESC`:
    участник увидел бы в списке старую реплику и не понял, что пришло новое.
    """
    company = await _company(session)
    await _message(
        session,
        chat_uuid=company.chat_uuid,
        sender_id=company.member.id,
        body="старое",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _message(
        session,
        chat_uuid=company.chat_uuid,
        sender_id=company.member.id,
        body="свежее",
        created_at=datetime.now(UTC),
    )
    await session.flush()

    response = await client.get("/api/v1/chats", headers=_auth(company.author_token))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["cursor"] is None
    item = body["items"][0]
    assert item["chat_uuid"] == company.chat_uuid
    assert item["card_uuid"] == company.card_uuid
    assert item["last_message"]["body"] == "свежее"
    assert item["last_message"]["sender_uuid"] == str(company.member.uuid)
    assert item["unread_count"] == 2


async def test_own_messages_are_not_counted_as_unread(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: свои сообщения не «непрочитанные».

    Краснеет от мутации M5 (убрать `sender_id <> viewer`): у автора после
    каждой собственной отправки висело бы «1 непрочитанное», и счётчик
    перестал бы означать «есть новое для меня».
    """
    company = await _company(session)
    await _message(session, chat_uuid=company.chat_uuid, sender_id=company.author.id)
    await _message(session, chat_uuid=company.chat_uuid, sender_id=company.member.id)
    await session.flush()

    author_view = await client.get("/api/v1/chats", headers=_auth(company.author_token))
    member_view = await client.get("/api/v1/chats", headers=_auth(company.member_token))

    assert author_view.json()["items"][0]["unread_count"] == 1
    assert member_view.json()["items"][0]["unread_count"] == 1


async def test_chat_without_messages_is_listed(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: чат без сообщений (системных ещё нет — 6.7) ВИДЕН в списке.

    Краснеет, если список строится `INNER JOIN` на сообщения: только что
    собранная компания «исчезла» бы из списка, пока никто не написал.
    """
    company = await _company(session)

    response = await client.get("/api/v1/chats", headers=_auth(company.author_token))

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["chat_uuid"] == company.chat_uuid
    assert item["last_message"] is None
    assert item["unread_count"] == 0


async def test_outsider_does_not_see_foreign_chat(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, ADR-13: чужой чат в список не попадает вовсе.

    Краснеет, если убрать предикат участия: посторонний увидел бы превью
    переписки компании (FR86), причём без единого 403 — тихо.
    """
    await _company(session)
    outsider, outsider_token = await _user(session)
    await session.flush()

    response = await client.get("/api/v1/chats", headers=_auth(outsider_token))

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "cursor": None}


async def test_list_orders_by_last_activity_desc(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: свежая активность — сверху (мутация M4: инверсия порядка).

    Краснеет при `ASC`: новое сообщение не поднимало бы чат наверх, и
    участник искал бы его в конце списка.
    """
    old = await _company(session, title="Старый")
    fresh = await _company(session, title="Свежий")
    # Зритель — автор ПЕРВОГО чата; во второй его добавляем участником,
    # иначе список по контракту (ADR-04) вернул бы только один чат, и порядок
    # проверялся бы на выборке из одной строки.
    await _participant(session, card_id=fresh.card_id, user_id=old.author.id)
    now = datetime.now(UTC)
    await _message(
        session,
        chat_uuid=old.chat_uuid,
        sender_id=old.member.id,
        created_at=now - timedelta(hours=2),
    )
    await _message(
        session,
        chat_uuid=fresh.chat_uuid,
        sender_id=fresh.member.id,
        created_at=now,
    )
    await session.flush()

    response = await client.get("/api/v1/chats", headers=_auth(old.author_token))
    titles = [item["card_title"] for item in response.json()["items"]]

    assert titles == ["Свежий", "Старый"]


async def test_cursor_paginates_without_gaps(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: курсорная пагинация проходит все чаты без пропусков и дублей.

    Краснеет при offset-пагинации или неверном tie-breaker: вставка нового
    сообщения между страницами сдвигала бы границы.
    """
    first = await _company(session, title="A")
    now = datetime.now(UTC)
    for index in range(3):
        await _message(
            session,
            chat_uuid=first.chat_uuid,
            sender_id=first.member.id,
            created_at=now - timedelta(minutes=index),
        )
    second = await _company(session, title="B")
    # Тот же зритель во втором чате: пагинация проверяется на ДВУХ строках
    # одного списка, а не на двух разных пользователях.
    await _participant(session, card_id=second.card_id, user_id=first.author.id)
    await _message(
        session,
        chat_uuid=second.chat_uuid,
        sender_id=second.member.id,
        created_at=now - timedelta(hours=1),
    )
    await session.flush()

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        query = "?limit=1" + (f"&cursor={cursor}" if cursor else "")
        response = await client.get(f"/api/v1/chats{query}", headers=_auth(first.author_token))
        assert response.status_code == 200
        payload = response.json()
        seen.extend(item["chat_uuid"] for item in payload["items"])
        cursor = payload["cursor"]
        if cursor is None:
            break

    assert sorted(seen) == sorted([first.chat_uuid, second.chat_uuid])
    assert len(seen) == len(set(seen))


async def test_invalid_cursor_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: битый курсор — ошибка ВВОДА (`422`), а не `500`.

    Краснеет, если разбор курсора живёт после запроса к БД: сервер обвинял
    бы себя в значении, которое не формировал.
    """
    company = await _company(session)

    response = await client.get(
        "/api/v1/chats?cursor=not-a-cursor", headers=_auth(company.author_token)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


# ——— AC-02: отметка прочтения ———


async def test_mark_read_zeroes_unread_and_is_idempotent(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: прочтение обнуляет счётчик и НЕ переписывает время первого.

    Краснеет от мутации M1 (снять `UPDATE read_at`): счётчик остался бы
    прежним, и «непрочитанное» никогда не исчезало бы. Идемпотентность —
    §18.11: повторный вызов возвращает тот же ответ, а `read_at` не меняется.
    """
    company = await _company(session)
    await _message(session, chat_uuid=company.chat_uuid, sender_id=company.member.id)
    await _message(session, chat_uuid=company.chat_uuid, sender_id=company.member.id)
    await session.flush()

    first = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/read", headers=_auth(company.author_token)
    )
    assert first.status_code == 200
    assert first.json()["unread_count"] == 0

    stored = await session.execute(
        text(
            "SELECT read_at FROM messages WHERE chat_id = "
            "(SELECT id FROM chats WHERE uuid = :u) ORDER BY id"
        ),
        {"u": company.chat_uuid},
    )
    read_at_first = [row[0] for row in stored.all()]
    assert all(value is not None for value in read_at_first)

    second = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/read", headers=_auth(company.author_token)
    )
    assert second.status_code == 200
    assert second.json() == first.json()

    stored_again = await session.execute(
        text(
            "SELECT read_at FROM messages WHERE chat_id = "
            "(SELECT id FROM chats WHERE uuid = :u) ORDER BY id"
        ),
        {"u": company.chat_uuid},
    )
    assert [row[0] for row in stored_again.all()] == read_at_first

    listing = await client.get("/api/v1/chats", headers=_auth(company.author_token))
    assert listing.json()["items"][0]["unread_count"] == 0


async def test_mark_read_does_not_touch_own_messages(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: свои сообщения остаются `read_at = NULL` — их не «читают».

    Краснеет, если снять `sender_id <> viewer` из `UPDATE`: факт «прочитано»
    появился бы у автора на собственной строке, и семантика счётчика
    разошлась бы между чтением и записью.
    """
    company = await _company(session)
    await _message(session, chat_uuid=company.chat_uuid, sender_id=company.author.id)
    await _message(session, chat_uuid=company.chat_uuid, sender_id=company.member.id)
    await session.flush()

    await client.post(
        f"/api/v1/chats/{company.chat_uuid}/read", headers=_auth(company.author_token)
    )

    stored = await session.execute(
        text(
            "SELECT sender_id, read_at FROM messages WHERE chat_id = "
            "(SELECT id FROM chats WHERE uuid = :u) ORDER BY id"
        ),
        {"u": company.chat_uuid},
    )
    rows = [(int(row[0]), row[1]) for row in stored.all()]
    own = [value for sender, value in rows if sender == company.author.id]
    others = [value for sender, value in rows if sender != company.author.id]

    assert own and all(value is None for value in own)
    assert others and all(value is not None for value in others)


async def test_outsider_read_is_404_identical_to_missing_chat(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, ADR-13: посторонний и несуществующий чат — ПОБАЙТОВО один ответ.

    Краснеет, если вернуть `403`: он подтвердил бы существование чата
    (FR86). Сравниваются тело, заголовки и число SQL-запросов — совпадение
    только статуса тайминг-оракул не закрывает (урок F-1 стори 6.3).
    """
    company = await _company(session)
    outsider, outsider_token = await _user(session)
    await session.flush()

    counter = _QueryCounter()
    listen_engine = _listen(session, counter)
    try:
        counter.count = 0
        foreign = await client.post(
            f"/api/v1/chats/{company.chat_uuid}/read", headers=_auth(outsider_token)
        )
        foreign_count = counter.count

        counter.count = 0
        missing = await client.post(
            f"/api/v1/chats/{uuid_lib.uuid4()}/read", headers=_auth(outsider_token)
        )
        missing_count = counter.count
    finally:
        event.remove(listen_engine, "before_cursor_execute", counter)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content
    assert _comparable_headers(foreign) == _comparable_headers(missing)
    assert foreign_count == missing_count, (
        f"тайминг-оракул: существующий {foreign_count}, несуществующий {missing_count}"
    )


def _comparable_headers(response: Response) -> dict[str, str]:
    """Заголовки без даты/длины: они законно различаются между запросами."""
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"date", "content-length"}
    }


# ——— AC-05: без N+1 ———


async def test_query_count_does_not_grow_with_chat_count(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05 (NFR12): число SQL-запросов НЕ зависит от числа чатов.

    Краснеет от мутации M3 (последнее сообщение отдельным запросом на чат):
    на 5 чатах было бы на 4 запроса больше, и «открытие ≤1 с» держалось бы
    только на пустой базе.
    """
    first = await _company(session, title="Первый")
    await _message(session, chat_uuid=first.chat_uuid, sender_id=first.member.id)
    await session.flush()

    counter = _QueryCounter()
    listen_engine = _listen(session, counter)
    try:
        counter.count = 0
        one = await client.get("/api/v1/chats", headers=_auth(first.author_token))
        assert one.status_code == 200
        count_one = counter.count

        for index in range(4):
            extra = await _company(session, title=f"Доп {index}")
            await _message(session, chat_uuid=extra.chat_uuid, sender_id=extra.member.id)
            await _participant(session, card_id=extra.card_id, user_id=first.author.id)
        await session.flush()

        counter.count = 0
        many = await client.get("/api/v1/chats", headers=_auth(first.author_token))
        assert many.status_code == 200
        count_many = counter.count
    finally:
        event.remove(listen_engine, "before_cursor_execute", counter)

    assert len(many.json()["items"]) == 5
    assert count_one == count_many, f"N+1: на 1 чате {count_one} запросов, на 5 чатах {count_many}"


@pytest.mark.perf
async def test_list_opens_within_one_second(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01 (NFR12): открытие списка ≤1 с на 5 чатах.

    Маркер `perf`: тест информирующий, PR не блокирует (среда CI шумная).
    Полноценный замер — на стенде (§18.5).
    """
    owner = await _company(session, title="Главный")
    for index in range(4):
        extra = await _company(session, title=f"Ещё {index}")
        await _message(session, chat_uuid=extra.chat_uuid, sender_id=extra.member.id)
        await _participant(session, card_id=extra.card_id, user_id=owner.author.id)
    await session.flush()

    started = time.monotonic()
    response = await client.get("/api/v1/chats", headers=_auth(owner.author_token))
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1.0, f"список открывался {elapsed:.3f} с (NFR12: ≤1 с)"


async def test_total_is_stable_across_cursor_pages(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (CR 6.6, находка F-1): `total` — ВСЕ чаты, а не строки страницы.

    Краснеет на `COUNT(*) OVER ()`: оконная функция считается ПОСЛЕ фильтра
    курсора, и на второй странице `total` усекался (замер до фикса:
    `[3, 2, 1]` при трёх чатах). Клиент, рисующий «Всего чатов: N», показывал
    бы уменьшающееся число при каждом «Ещё».

    `total` берётся скалярным подзапросом с теми же предикатами, но БЕЗ
    курсора; число SQL-запросов при этом не растёт (AC-05).
    """
    author, author_token = await _user(session)
    now = datetime.now(UTC)
    for index in range(3):
        card_uuid, card_id, chat_uuid = await _card_with_chat(
            session, author_id=author.id, title=f"Чат {index}"
        )
        await _message(
            session,
            chat_uuid=chat_uuid,
            sender_id=author.id,
            created_at=now - timedelta(minutes=index),
        )
    await session.flush()

    totals: list[int] = []
    cursor: str | None = None
    for _ in range(5):
        query = "?limit=1" + (f"&cursor={cursor}" if cursor else "")
        response = await client.get(f"/api/v1/chats{query}", headers=_auth(author_token))
        assert response.status_code == 200
        payload = response.json()
        totals.append(payload["total"])
        cursor = payload["cursor"]
        if cursor is None:
            break

    assert totals == [3, 3, 3], f"total усекается курсором: {totals}"
