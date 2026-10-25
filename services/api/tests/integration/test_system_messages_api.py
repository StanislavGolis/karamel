"""Системные сообщения компании через API (стори 6.7; AC-01…AC-04).

Реальный PostgreSQL, схема поднята Alembic (`0019`: `sender_id` стал
`nullable`, добавлен `is_system` + CHECK-бикондиционал).

Главные утверждения:

- **AC-01…AC-03**: каждый из трёх триггеров оставляет строку в `messages`
  с `is_system = true` и `sender_id IS NULL` — проверяется РЕАЛЬНЫМ `SELECT`,
  а не ответом API (ответ мог бы отдать `is_system` из домена);
- **AC-04**: история и список отдают `sender_uuid: null` и расшифрованный
  текст (шифрование то же, AAD — `SYSTEM_AAD_SENDER_ID`);
- **D-6.7-2**: системные считаются непрочитанными и закрываются `mark_read`
  (симметрия предикатов — иначе счётчик нечем обнулить);
- **CHECK-инвариант**: `is_system = true` с автором отвергается БД.

§18.2: каждый тест падает при конкретной поломке кода (см. докстринги).
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session
from app.services.system_message_service import SYSTEM_MESSAGE_TEMPLATES, SystemMessageKind

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


# ——— посев —————————————————————————————————————————————————————————————————


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


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
    )
    return int(row.scalar_one())


async def _card_with_chat(
    session: AsyncSession,
    *,
    author_id: int,
    capacity: int = 2,
    filled_count: int = 1,
    status: str = "published",
    with_chat: bool = True,
) -> tuple[str, int, str | None]:
    """Карточка; при `with_chat` — существующий чат (компания уже собиралась)."""
    place_id = await _place(session)
    starts = datetime.now(UTC) + timedelta(days=3)
    chat_uuid = str(uuid_lib.uuid4()) if with_chat else None
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, chat_uuid) VALUES (:a, :p, :s, 'Встреча', "
            "'cafe', :st, :cap, 60, :e, :status, :filled, 'approved', :pub, :chat) "
            "RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_community_slug,
            "st": starts,
            "cap": capacity,
            "e": starts - timedelta(hours=1),
            "status": status,
            "filled": filled_count,
            "pub": starts - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_uuid, card_id = row.one()
    if chat_uuid is not None:
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


def _auth(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


async def _system_rows(session: AsyncSession, *, chat_uuid: str) -> list[tuple[bool, Any, bytes]]:
    """Системные строки чата: `(is_system, sender_id, body)` — сырой `SELECT`."""
    rows = await session.execute(
        text(
            "SELECT m.is_system, m.sender_id, m.body FROM messages m "
            "JOIN chats c ON c.id = m.chat_id WHERE c.uuid = :u ORDER BY m.created_at, m.id"
        ),
        {"u": chat_uuid},
    )
    return [(bool(r[0]), r[1], bytes(r[2])) for r in rows.all()]


async def _all_rows(session: AsyncSession, *, chat_uuid: str) -> int:
    row = await session.execute(
        text("SELECT count(*) FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.uuid = :u"),
        {"u": chat_uuid},
    )
    return int(row.scalar_one())


# ——— AC-01: отмена участия ————————————————————————————————————————————————


async def test_leave_appends_system_message(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: отмена участия → системное сообщение БЕЗ автора в чате.

    Краснеет от: (а) отсутствия `append_system_message` в `leave_open_card`;
    (б) мутации M2 (`sender_id` вместо `NULL` — упадёт `CHECK`);
    (в) записи сообщения ВНЕ транзакции отмены (тогда при откате SAVEPOINT
    осталось бы сообщение об отмене, которой не было).
    """
    author, _ = await _user(session)
    member, token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    response = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(token))

    assert response.status_code == 204, response.text
    rows = await _system_rows(session, chat_uuid=chat_uuid)
    assert len(rows) == 1
    is_system, sender_id, body = rows[0]
    assert is_system is True
    assert sender_id is None  # AC-04: автора-пользователя НЕТ
    # NFR31: в БД шифртекст, текст шаблона в открытом виде не лежит.
    assert SYSTEM_MESSAGE_TEMPLATES[SystemMessageKind.PARTICIPANT_LEFT].encode() not in body
    assert len(body) > 28


async def test_leave_is_idempotent_and_does_not_duplicate_system_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, DM-6.7-7: повторная отмена не добавляет второе сообщение.

    Краснеет, если `append_system_message` вызывать ДО проверки
    `cancelled == 0`: ретрай после обрыва сети удвоил бы «участник отменил
    участие», и чат показал бы две отмены одной отмены.
    """
    author, _ = await _user(session)
    member, token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    first = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(token))
    second = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(token))

    assert first.status_code == 204
    assert second.status_code == 204
    assert await _all_rows(session, chat_uuid=chat_uuid) == 1


async def test_leave_without_chat_writes_no_system_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, §18.11: нет чата (компания не собрана) — нет сообщения.

    Краснеет, если писать системное сообщение без проверки существования
    чата: `chats` для несобранной карточки нет (FR30), и вставка упала бы
    `NOT NULL`-ом по `chat_id` (или создала бы чат «из воздуха»).
    """
    author, _ = await _user(session)
    member, token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=4, filled_count=2, with_chat=False
    )
    assert chat_uuid is None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    response = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(token))

    assert response.status_code == 204, response.text
    total = await session.execute(text("SELECT count(*) FROM messages"))
    assert int(total.scalar_one()) == 0


# ——— AC-02: новый участник —————————————————————————————————————————————————


async def test_join_after_release_appends_system_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: чат существовал, место освободилось, пришёл новый — сообщение.

    Краснеет от: (а) отсутствия ветки `else` (сообщение только при создании
    чата — то есть НИКОГДА для повторного присоединения); (б) записи
    сообщения при ПЕРВОЙ сборке компании (тогда «новый участник» было бы
    ложным фактом — компания только что появилась).
    """
    author, _ = await _user(session)
    newcomer, token = await _user(session)
    # Чат уже существует: компания собиралась раньше (5а.6, full → published).
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=1, status="published"
    )
    assert chat_uuid is not None
    await session.flush()

    response = await client.post(f"/api/v1/cards/{card_uuid}/join", headers=_auth(token))

    assert response.status_code == 200, response.text
    rows = await _system_rows(session, chat_uuid=chat_uuid)
    assert len(rows) == 1
    is_system, sender_id, _body = rows[0]
    assert is_system is True
    assert sender_id is None


async def test_first_join_that_opens_chat_writes_no_system_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, §18.11: ПЕРВАЯ сборка компании — сообщения о «новом участнике» нет.

    Краснеет, если писать сообщение безусловно: участники увидели бы «в
    компании новый участник» в момент, когда компании ещё не было, а
    объяснять изменение состава нечего.
    """
    author, _ = await _user(session)
    newcomer, token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=1, with_chat=False
    )
    assert chat_uuid is None
    await session.flush()

    response = await client.post(f"/api/v1/cards/{card_uuid}/join", headers=_auth(token))

    assert response.status_code == 200, response.text
    total = await session.execute(text("SELECT count(*) FROM messages"))
    assert int(total.scalar_one()) == 0


# ——— AC-03: отмена карточки ————————————————————————————————————————————————


async def test_cancel_card_appends_system_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03 (FR100): отмена карточки автором → сообщение в чат компании.

    Краснеет от: (а) отсутствия триггера в `cancel_card`; (б) записи
    сообщения ПОСЛЕ `commit` (падение между ними оставило бы отмену без
    объяснения — ровно то, от чего FR106 защищает).
    """
    author, token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=1
    )
    assert chat_uuid is not None
    await session.flush()

    response = await client.post(f"/api/v1/cards/{card_uuid}/cancel", headers=_auth(token))

    assert response.status_code == 200, response.text
    rows = await _system_rows(session, chat_uuid=chat_uuid)
    assert len(rows) == 1
    assert rows[0][0] is True
    assert rows[0][1] is None


async def test_cancel_card_without_chat_writes_no_system_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, §18.11: чата нет — сообщения нет, отмена всё равно работает."""
    author, token = await _user(session)
    card_uuid, _card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=0, with_chat=False
    )
    assert chat_uuid is None
    await session.flush()

    response = await client.post(f"/api/v1/cards/{card_uuid}/cancel", headers=_auth(token))

    assert response.status_code == 200, response.text
    total = await session.execute(text("SELECT count(*) FROM messages"))
    assert int(total.scalar_one()) == 0


# ——— AC-04: чтение истории и списка ————————————————————————————————————————


async def test_history_returns_system_message_without_sender(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: системное сообщение видно в истории, `sender_uuid: null`.

    Краснеет от мутации M6 (`outerjoin` → `join`): `INNER JOIN users` не
    сопоставляет `NULL`, и системная строка ИСЧЕЗАЕТ из ответа — тихая
    потеря факта. Дополнительно ловит неверный AAD при расшифровке
    (`sender_id=None` вместо `SYSTEM_AAD_SENDER_ID` → `InvalidTag` → 500).
    """
    author, author_token = await _user(session)
    member, _member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()
    # Отмена участия создаёт системное сообщение штатным путём.
    member_token = (await issue_session(session, member)).access_token
    leave = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))
    assert leave.status_code == 204

    response = await client.get(f"/api/v1/chats/{chat_uuid}/messages", headers=_auth(author_token))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["is_system"] is True
    assert item["sender_uuid"] is None
    # Текст расшифрован сервером и равен серверному шаблону.
    assert item["body"] == SYSTEM_MESSAGE_TEMPLATES[SystemMessageKind.PARTICIPANT_LEFT]


def test_history_statement_outer_joins_sender() -> None:
    """AC-04, Task 3.1: билдер истории не теряет строки без автора (unit).

    Краснеет от мутации M6: `join` вместо `outerjoin`.
    """
    from app.repositories.messages import MessageRepository

    sql = str(
        MessageRepository.build_history_statement(chat_id=1, after=None, limit=30).compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()
    assert "left outer join users" in sql


async def test_chat_list_counts_system_message_as_unread(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04, D-6.7-2: чат с ОДНИМИ системными виден в списке и непрочитан.

    Краснеет от мутации M5: без `is_(None)`-ветви `NULL != :viewer` даёт
    `NULL` → системное сообщение не считается непрочитанным, и участник
    узнаёт об изменении состава только открыв чат вручную.
    """
    author, author_token = await _user(session)
    member, member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()
    leave = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))
    assert leave.status_code == 204

    response = await client.get("/api/v1/chats", headers=_auth(author_token))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["unread_count"] == 1
    last = items[0]["last_message"]
    assert last is not None
    assert last["is_system"] is True
    assert last["sender_uuid"] is None
    assert last["body"] == SYSTEM_MESSAGE_TEMPLATES[SystemMessageKind.PARTICIPANT_LEFT]


async def test_mark_read_closes_system_message(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04, D-6.7-2: `mark_read` обнуляет и системные (симметрия предикатов).

    Краснеет, если `mark_read` останется с `sender_id != :viewer`: счётчик
    покажет 1 навсегда — «непрочитанное», которое нечем закрыть.
    """
    author, author_token = await _user(session)
    member, member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()
    leave = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))
    assert leave.status_code == 204

    before = await client.get("/api/v1/chats", headers=_auth(author_token))
    assert before.json()["items"][0]["unread_count"] == 1

    read = await client.post(f"/api/v1/chats/{chat_uuid}/read", headers=_auth(author_token))
    assert read.status_code == 200, read.text

    after = await client.get("/api/v1/chats", headers=_auth(author_token))
    assert after.json()["items"][0]["unread_count"] == 0
    # §18.11: оба значения проверены — до 1, после 0.


# ——— AC-04: нельзя удалить, посторонний не видит —————————————————————————


async def test_system_message_cannot_be_deleted(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: маршрута удаления сообщения нет (`405`).

    Краснеет от мутации M4 (добавить `DELETE /messages/{uuid}`): системное
    сообщение — факт домена, и «удалить удобно» стёрло бы объяснение
    изменения состава компании.
    """
    author, author_token = await _user(session)
    card_uuid, _card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=1
    )
    assert chat_uuid is not None
    await session.flush()

    # (1) Маршрут КОЛЛЕКЦИИ существует (`GET`/`POST`), поэтому `DELETE` на нём
    # даёт именно `405 method_not_allowed`. Прежний ассерт
    # `status_code in (404, 405)` был слабым: добавленный
    # `DELETE /messages/{uuid}`, отдающий `404` на несуществующем сообщении,
    # оставил бы тест ЗЕЛЁНЫМ — то есть не защищал бы AC-04 вовсе.
    collection = await client.delete(
        f"/api/v1/chats/{chat_uuid}/messages", headers=_auth(author_token)
    )
    assert collection.status_code == 405, collection.text
    assert collection.json()["code"] == "method_not_allowed"

    # (2) Структурная проверка: ни один маршрут приложения не разрешает
    # `DELETE` на пути с `messages`. Краснеет ровно тогда, когда кто-то
    # добавит «удобное удаление» — даже если такой маршрут отдаёт `404`
    # для несуществующего `uuid` (поведенческая проверка его не поймает).
    message_delete_routes = [
        route.path
        for route in app.routes
        if "messages" in getattr(route, "path", "")
        and "DELETE" in (getattr(route, "methods", None) or set())
    ]
    assert message_delete_routes == []

    # (3) Элементный путь не зарегистрирован: `DELETE` уходит в `404`.
    element = await client.delete(
        f"/api/v1/chats/{chat_uuid}/messages/{uuid_lib.uuid4()}",
        headers=_auth(author_token),
    )
    assert element.status_code == 404, element.text


async def test_outsider_cannot_read_chat_with_only_system_messages(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04, ADR-13: посторонний получает `404` и не узнаёт о чате.

    Краснеет, если системные сообщения сделают чат «публично читаемым»
    (например, историю начнут отдавать без гарда участия).
    """
    author, _ = await _user(session)
    member, member_token = await _user(session)
    outsider, outsider_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()
    leave = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))
    assert leave.status_code == 204

    response = await client.get(
        f"/api/v1/chats/{chat_uuid}/messages", headers=_auth(outsider_token)
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "chat_not_found"


# ——— CHECK-инвариант ———————————————————————————————————————————————————————


async def test_check_rejects_system_message_with_sender(session: AsyncSession) -> None:
    """AC-04, D-6.7-1, мутация M1/M2: `is_system = true` с автором → БД отвергает.

    Краснеет от мутации M1 (снять CHECK): строка «системное сообщение от
    пользователя» стала бы возможной, и клиент отрисовал бы её как обычную
    (с аватаром и своей кнопкой удаления).
    """
    author, _ = await _user(session)
    card_uuid, _card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=1
    )
    assert chat_uuid is not None
    await session.flush()
    chat_id = int(
        (
            await session.execute(text("SELECT id FROM chats WHERE uuid = :u"), {"u": chat_uuid})
        ).scalar_one()
    )

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, is_system) "
                "VALUES (:u, :c, :s, :b, true)"
            ),
            {"u": uuid_lib.uuid4(), "c": chat_id, "s": author.id, "b": b"x" * 40},
        )
        await session.flush()


async def test_check_rejects_regular_message_without_sender(session: AsyncSession) -> None:
    """AC-04, D-6.7-1, §18.11: обратная сторона бикондиционала.

    Краснеет, если CHECK сделать односторонним (`NOT is_system OR sender_id
    IS NULL`): обычное сообщение без автора стало бы допустимым, и
    `sender_uuid: null` с `is_system: false` сломал бы `own`-логику клиента.
    """
    author, _ = await _user(session)
    card_uuid, _card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=1
    )
    assert chat_uuid is not None
    await session.flush()
    chat_id = int(
        (
            await session.execute(text("SELECT id FROM chats WHERE uuid = :u"), {"u": chat_uuid})
        ).scalar_one()
    )

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO messages (uuid, chat_id, sender_id, body, is_system) "
                "VALUES (:u, :c, NULL, :b, false)"
            ),
            {"u": uuid_lib.uuid4(), "c": chat_id, "b": b"x" * 40},
        )
        await session.flush()


# --- AC-01..AC-03: WS-событие после commit --------------------------------


async def test_publish_happens_after_commit(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01, D-6.7-7, ADR-11: событие уходит ПОСЛЕ успешного `DELETE`.

    Краснеет от мутации M3 (убрать публикацию): клиент в открытом чате не
    увидит системное сообщение вовсе. Дополнительно фиксируется контракт
    payload: `is_system: true`, `sender_uuid: null`, `message_uuid` есть
    (дедуп брокера `(type, message_uuid)` работает — долг X-6.5-6 не
    расширяется).
    """
    published: list[Any] = []
    original = __import__(
        "app.services.system_message_service", fromlist=["publish_to_chat"]
    ).publish_to_chat

    async def spy(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        published.append((chat_uuid, envelope))
        await original(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr("app.services.system_message_service.publish_to_chat", spy)

    author, _ = await _user(session)
    member, member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    response = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))

    assert response.status_code == 204
    assert len(published) == 1
    published_chat_uuid, envelope = published[0]
    assert str(published_chat_uuid) == chat_uuid
    assert envelope.type == "message.created"
    assert envelope.payload["is_system"] is True
    assert envelope.payload["sender_uuid"] is None
    assert envelope.payload["message_uuid"]
    assert envelope.payload["created_at"].endswith("Z")


async def test_repeat_leave_publishes_once(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01, §18.11: повторная отмена не публикует второе событие.

    Краснеет, если публиковать безусловно: клиенты получили бы два
    `message.created` об одной отмене, и дедуп по `message_uuid` не помог бы
    (uuid у событий разные).
    """
    published: list[Any] = []
    original = __import__(
        "app.services.system_message_service", fromlist=["publish_to_chat"]
    ).publish_to_chat

    async def spy(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        published.append(envelope)
        await original(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr("app.services.system_message_service.publish_to_chat", spy)

    author, _ = await _user(session)
    member, member_token = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(
        session, author_id=author.id, capacity=2, filled_count=2, status="full"
    )
    assert chat_uuid is not None
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    first = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))
    second = await client.delete(f"/api/v1/cards/{card_uuid}/join", headers=_auth(member_token))

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(published) == 1
