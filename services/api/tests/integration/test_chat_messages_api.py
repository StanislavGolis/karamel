"""Обмен сообщениями в чате через API (стори 6.4; AC-01…AC-06).

Реальный PostgreSQL, схема поднята Alembic (`0016` добавила `messages.uuid`
и `client_message_id`). Главные утверждения:

- **AC-01, NFR31**: в БД лежит ШИФРТЕКСТ — проверяется реальным `SELECT body`,
  а не по ответу API: ответ расшифрован, и по нему нельзя судить о хранилище;
- **AC-04, NFR18**: повторный `POST` с тем же `client_message_id` даёт ТОТ ЖЕ
  `uuid` и `count(messages) == 1` (идемпотентность, а не «не упало»);
- **AC-03, ADR-13**: посторонний и несуществующий чат — ПОБАЙТОВО один ответ
  и ОДИНАКОВОЕ число SQL-запросов (тайминг-оракул, урок F-1 стори 6.3);
- **AC-02, FR31**: 45 сообщений листаются тремя страницами без пропусков;
- **AC-06**: подмена байта шифртекста в БД → `500 message_corrupted`.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
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
    session: AsyncSession, *, author_id: int, capacity: int = 2
) -> tuple[str, int, str]:
    """Карточка + чат (собранная компания, контракт 6.2)."""
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
            "verification_status, published_at, chat_uuid,"
            "kind, activity) VALUES (:a, :p, :s, 'Встреча', "
            "'cafe', :st, :cap, 60, :e, 'full', :cap, 'approved', :pub, :chat,"
            "'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_city_slug,
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


@dataclass
class ChatFixture:
    """Собранная компания: автор и участник, чат открыт."""

    author: User
    author_token: str
    member: User
    member_token: str
    card_uuid: str
    card_id: int
    chat_uuid: str


async def _chat_company(session: AsyncSession, *, capacity: int = 2) -> ChatFixture:
    author, author_token = await _user(session)
    member, member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=capacity
    )
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()
    return ChatFixture(
        author=author,
        author_token=author_token,
        member=member,
        member_token=member_token,
        card_uuid=card_uuid,
        card_id=card_id,
        chat_uuid=chat_uuid,
    )


def _auth(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


def _payload(body: str, client_id: uuid_lib.UUID | None = None) -> dict[str, str]:
    return {
        "body": body,
        "client_message_id": str(client_id or uuid_lib.uuid4()),
    }


# ——— AC-01, NFR31: шифрование в БД ———


async def test_message_is_encrypted_at_rest_and_decrypted_in_response(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, NFR31: в БД шифртекст, в ответе — исходный текст.

    §18.1: тест краснеет, если шифрование уберут (в БД появится открытый
    текст) И если расшифровку сломают (ответ не совпадёт). Проверяются ОБА
    утверждения: одно лишь «в БД нет текста» прошло бы и при мусоре в ответе.
    """
    company = await _chat_company(session)
    body = "Встречаемся у входа в 19:00"

    response = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload(body),
        headers=_auth(company.member_token),
    )

    assert response.status_code == 201
    sent = response.json()
    assert sent["body"] == body
    assert sent["sender_uuid"] == str(company.member.uuid)

    # NFR31: реальный SELECT, а не вывод по ответу API.
    stored = await session.execute(
        text("SELECT body FROM messages WHERE uuid = :u"), {"u": sent["uuid"]}
    )
    raw = bytes(stored.scalar_one())
    assert body.encode("utf-8") not in raw
    assert len(raw) > 28

    history = await client.get(
        f"/api/v1/chats/{company.chat_uuid}/messages", headers=_auth(company.author_token)
    )
    assert history.status_code == 200
    assert [item["body"] for item in history.json()["items"]] == [body]


async def test_author_can_write_to_own_chat(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, ADR-04: автор — участник компании, хотя строки в заявках нет.

    §18.11: проверяется ДРУГОЕ значение признака. Гард, пропускающий только
    `selected`, отклонил бы автора от его же чата.
    """
    company = await _chat_company(session)

    response = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("автор пишет"),
        headers=_auth(company.author_token),
    )

    assert response.status_code == 201


# ——— AC-04, NFR18: идемпотентность ———


async def test_repeat_with_same_client_id_does_not_duplicate(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04, NFR18: ретрай после обрыва не создаёт второе сообщение.

    Проверяется `count(messages) == 1` и совпадение `uuid`: «не упало»
    прошло бы и при двух строках с разными `uuid`.
    """
    company = await _chat_company(session)
    client_id = uuid_lib.uuid4()

    first = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("повтор", client_id),
        headers=_auth(company.member_token),
    )
    second = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("повтор", client_id),
        headers=_auth(company.member_token),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["uuid"] == second.json()["uuid"]

    count = await session.execute(
        text(
            "SELECT count(*) FROM messages WHERE chat_id = (SELECT id FROM chats WHERE uuid = :u)"
        ),
        {"u": company.chat_uuid},
    )
    assert count.scalar_one() == 1


async def test_different_client_ids_create_two_messages(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: §18.11 — второе значение признака.

    Разные `client_message_id` — РАЗНЫЕ сообщения, даже при одинаковом тексте:
    идемпотентность не должна превращаться в «дедупликацию по тексту», иначе
    человек не сможет отправить «ок» дважды.
    """
    company = await _chat_company(session)

    await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("ок"),
        headers=_auth(company.member_token),
    )
    await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("ок"),
        headers=_auth(company.member_token),
    )

    count = await session.execute(
        text(
            "SELECT count(*) FROM messages WHERE chat_id = (SELECT id FROM chats WHERE uuid = :u)"
        ),
        {"u": company.chat_uuid},
    )
    assert count.scalar_one() == 2


# ——— AC-03, ADR-13: анти-оракул ———


def _headers(response: Response) -> dict[str, str]:
    skip = {"date", "server"}
    return {k.lower(): v for k, v in response.headers.items() if k.lower() not in skip}


async def test_outsider_gets_identical_404_to_missing_chat(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, ADR-13: посторонний и несуществующий чат — один ответ.

    Сравниваются ВСЕ заголовки и сырые байты: различие в `WWW-Authenticate`
    или `content-length` раскрыло бы существование чата (урок CR 6.3).
    """
    company = await _chat_company(session)
    outsider, outsider_token = await _user(session)
    await session.flush()

    existing = await client.get(
        f"/api/v1/chats/{company.chat_uuid}/messages", headers=_auth(outsider_token)
    )
    missing = await client.get(
        f"/api/v1/chats/{uuid_lib.uuid4()}/messages", headers=_auth(outsider_token)
    )

    assert existing.status_code == 404
    assert existing.status_code == missing.status_code
    assert existing.content == missing.content
    assert _headers(existing) == _headers(missing)
    assert existing.json() == {"code": "chat_not_found"}


async def test_outsider_send_is_404_not_403(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, FR86: посторонний не может писать — `404`, не `403`.

    `403` подтвердил бы существование чата и приватность переписки.
    """
    company = await _chat_company(session)
    outsider, outsider_token = await _user(session)
    await session.flush()

    response = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("чужое"),
        headers=_auth(outsider_token),
    )

    assert response.status_code == 404
    assert response.json() == {"code": "chat_not_found"}
    count = await session.execute(text("SELECT count(*) FROM messages"))
    assert count.scalar_one() == 0


async def test_query_count_identical_on_404_paths(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: тайминг-оракул — одинаковое число SQL-запросов на двух 404.

    Урок F-1 стори 6.3: совпадающие тело и заголовки не спасают, если
    существующий чат стоит на один запрос больше.
    """
    company = await _chat_company(session)
    outsider, outsider_token = await _user(session)
    await session.flush()

    counter = _QueryCounter()
    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", counter)
    try:
        counter.count = 0
        await client.get(
            f"/api/v1/chats/{company.chat_uuid}/messages", headers=_auth(outsider_token)
        )
        existing_count = counter.count

        counter.count = 0
        await client.get(
            f"/api/v1/chats/{uuid_lib.uuid4()}/messages", headers=_auth(outsider_token)
        )
        missing_count = counter.count
    finally:
        event.remove(listen_engine, "before_cursor_execute", counter)

    assert existing_count == missing_count, (
        f"тайминг-оракул: существующий {existing_count}, несуществующий {missing_count}"
    )


@dataclass
class _QueryCounter:
    count: int = field(default=0)

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1


# ——— AC-02, FR31: пагинация без потерь ———


async def test_pagination_covers_all_messages_without_gaps(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, FR31: 45 сообщений — три страницы, ни пропуска, ни дубля.

    Краснеет, если курсор сравнивает только `created_at` (сообщения с
    одинаковым временем терялись бы) или если `next_cursor` ставится на
    последней странице (вечный цикл).
    """
    company = await _chat_company(session)
    total = 45
    chat_id = await _chat_id(session, company.chat_uuid)
    for index in range(total):
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, created_at) "
                "VALUES (:u, :c, :s, :b, :at)"
            ),
            {
                "u": str(uuid_lib.uuid4()),
                "c": chat_id,
                "s": company.member.id,
                # Одинаковое время у всех — предел для tie-breaker'а: порядок
                # обязан быть детерминирован по `uuid`.
                "b": _encrypted(company, chat_id, company.member.id, f"msg-{index}"),
                "at": datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
            },
        )
    await session.flush()

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": "20"}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            f"/api/v1/chats/{company.chat_uuid}/messages",
            params=params,
            headers=_auth(company.member_token),
        )
        assert response.status_code == 200
        body = response.json()
        collected.extend(item["body"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages <= 10, "курсор не завершается — бесконечная пагинация"

    assert pages == 3
    assert len(collected) == total
    assert len(set(collected)) == total
    assert sorted(collected) == sorted(f"msg-{index}" for index in range(total))


async def test_last_cursor_present_on_every_page_and_tail_loadable(
    client: AsyncClient, session: AsyncSession
) -> None:
    """X-6.5-5 (chore P-7): `last_cursor` есть ВСЕГДА, и по нему читается хвост.

    Клиент догружает пропущенное после обрыва WS именно по этому полю.
    `next_cursor` для этого не годится: на ПОСЛЕДНЕЙ странице он `null`, а
    именно там лежит хвост, который надо догрузить.

    Краснеет от:
    - возврата `last_cursor = next_cursor` (на последней странице пропадёт);
    - отсутствия поля в ответе (клиент не смог бы построить запрос);
    - курсора, указывающего НЕ на последний элемент (догрузка вернула бы
      уже показанное).
    """
    company = await _chat_company(session)
    chat_id = await _chat_id(session, company.chat_uuid)
    for index in range(3):
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, created_at) "
                "VALUES (:u, :c, :s, :b, :at)"
            ),
            {
                "u": str(uuid_lib.uuid4()),
                "c": chat_id,
                "s": company.member.id,
                "b": _encrypted(company, chat_id, company.member.id, f"msg-{index}"),
                "at": datetime(2026, 10, 24, 12, 0, index, tzinfo=UTC),
            },
        )
    await session.flush()

    response = await client.get(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        headers=_auth(company.member_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    # Последняя (и единственная) страница: `next_cursor` пуст по контракту,
    # а `last_cursor` обязан быть.
    assert body["next_cursor"] is None
    assert isinstance(body["last_cursor"], str) and body["last_cursor"]

    # Догрузка по курсору хвоста: ничего нового — пустая страница, а не дубли.
    tail = await client.get(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        params={"cursor": body["last_cursor"]},
        headers=_auth(company.member_token),
    )
    assert tail.status_code == 200
    assert tail.json()["items"] == []
    # Пустая страница — курсора хвоста тоже нет (нечего запоминать).
    assert tail.json()["last_cursor"] is None


async def _chat_id(session: AsyncSession, chat_uuid: str) -> int:
    row = await session.execute(text("SELECT id FROM chats WHERE uuid = :u"), {"u": chat_uuid})
    return int(row.scalar_one())


def _encrypted(company: ChatFixture, chat_id: int, sender_id: int, body: str) -> bytes:
    from app.core.crypto import encrypt_message_body

    return encrypt_message_body(
        body,
        chat_id=chat_id,
        sender_id=sender_id,
        key=settings.chat_message_key,
    )


async def test_invalid_cursor_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: битый курсор — ошибка ВВОДА (`422`), а не `500`."""
    company = await _chat_company(session)

    response = await client.get(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        params={"cursor": "не-курсор"},
        headers=_auth(company.member_token),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


# ——— AC-06: повреждённый шифртекст ———


async def test_corrupted_ciphertext_is_explicit_error(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06, D-6.4-5: подмена байта в БД — `500 message_corrupted`.

    Тихий мусор в переписке хуже явной ошибки: человек прочитал бы
    испорченный текст как сообщение партнёра.
    """
    company = await _chat_company(session)
    chat_id = await _chat_id(session, company.chat_uuid)
    message_uuid = str(uuid_lib.uuid4())
    blob = bytearray(_encrypted(company, chat_id, company.member.id, "секрет"))
    # Портим ПОСЛЕДНИЙ байт — это тег GCM: подмена обязана обнаружиться.
    blob[-1] ^= 0xFF
    await session.execute(
        text("INSERT INTO messages (uuid, chat_id, sender_id, body) VALUES (:u, :c, :s, :b)"),
        {"u": message_uuid, "c": chat_id, "s": company.member.id, "b": bytes(blob)},
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/chats/{company.chat_uuid}/messages", headers=_auth(company.member_token)
    )

    assert response.status_code == 500
    assert response.json() == {"code": "message_corrupted"}
    # Тело испорченного сообщения не утекло в ответ об ошибке.
    assert "секрет" not in response.text


# ——— AC-05, NFR19: БД недоступна ———


async def test_db_unavailable_returns_explicit_503(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05, NFR19: БД недоступна → `503 service_unavailable`, не тихий сбой.

    `OperationalError` бросается на УЖЕ авторизованном пути — в репозитории
    чатов, а не в `get_session`: подмена сессии сломала бы аутентификацию
    (пользователь не читается из БД), и тест проверял бы `401`, а не NFR19.
    Клиент обязан получить МАШИННЫЙ код, а не «сообщение отправлено» без записи.
    """
    from app.repositories.chats import ChatRepository

    company = await _chat_company(session)

    async def _boom(self: ChatRepository, **kwargs: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(ChatRepository, "get_by_uuid", _boom)

    response = await client.post(
        f"/api/v1/chats/{company.chat_uuid}/messages",
        json=_payload("не дойдёт"),
        headers=_auth(company.member_token),
    )

    assert response.status_code == 503
    assert response.json() == {"code": "service_unavailable"}
    # Ничего не записалось: «тихого» успеха нет.
    count = await session.execute(text("SELECT count(*) FROM messages"))
    assert count.scalar_one() == 0
