"""П10: отмена карточки доходит до участника — сообщением в чате (FR100).

**Что доказывается.** Автор отменяет встречу → в чате компании появляется
СИСТЕМНОЕ сообщение с шаблоном `CARD_CANCELLED`, места освобождены, статус
`cancelled`. То есть участник узнаёт об отмене из того места, где он уже
находится (ADR-04: один чат на карточку), а не из молчаливой смены статуса.

**Почему нового теста не хватало (замер CS 12.9).** Состояние отмены
покрыто (`test_cards_state_api.py:354`, `:425`), а шаблон — отдельно
(`test_system_message_service.py:216`). Замка на СВЯЗЬ «отмена → сообщение
в чате ЭТОЙ карточки» не было: удаление вызова `append_for_card` из
`cancel_card` (`event_card_service.py:1616-1621`) прошло бы приёмку.

**Почему расшифровка, а не подстрока в блобе.** `body` в БД — шифртекст
(NFR31). Проверка «шаблон не найден в сыром блобе» доказывает только
шифрование. Чтобы утверждать «участник увидит текст отмены», блоб надо
расшифровать тем же ключом и сравнить с шаблоном.

Метка `П10` в докстринге обязательна: реестр правил
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

from app.core.config import settings
from app.core.crypto import decrypt_message_body
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


async def _user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7910{uuid_lib.uuid4().int % 10**7:07d}"
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
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    return int(row.scalar_one())


async def _card_with_chat(
    session: AsyncSession, *, author_id: int, capacity: int = 2, filled_count: int = 2
) -> tuple[str, int, str]:
    """Собранная карточка (`full`) с чатом — состояние, в котором отмена адресна."""
    place_id = await _place(session)
    starts = datetime.now(UTC) + timedelta(days=3)
    chat_uuid = str(uuid_lib.uuid4())
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, chat_uuid, kind, activity) "
            "VALUES (:a, :p, :s, 'Встреча', 'cafe', :st, :cap, 60, :e, 'full', :filled, "
            "'approved', :pub, :chat, 'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_city_slug,
            "st": starts,
            "cap": capacity,
            "e": starts - timedelta(hours=1),
            "filled": filled_count,
            "pub": starts - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_uuid, card_id = row.one()
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


async def _system_rows(
    session: AsyncSession, *, chat_uuid: str
) -> list[tuple[bool, Any, bytes, int]]:
    """Системные строки чата: `(is_system, sender_id, body, chat_id)`.

    `chat_id` нужен для расшифровки: он входит в AAD, без него `InvalidTag`.
    """
    rows = await session.execute(
        text(
            "SELECT m.is_system, m.sender_id, m.body, m.chat_id FROM messages m "
            "JOIN chats c ON c.id = m.chat_id WHERE c.uuid = :u ORDER BY m.created_at, m.id"
        ),
        {"u": chat_uuid},
    )
    return [(bool(r[0]), r[1], bytes(r[2]), int(r[3])) for r in rows.all()]


async def _card_state(session: AsyncSession, card_id: int) -> tuple[int, str]:
    row = await session.execute(
        text("SELECT filled_count, status FROM event_cards WHERE id = :id"), {"id": card_id}
    )
    filled, status = row.one()
    return int(filled), str(status)


async def test_cancel_card_reaches_participant_chat(
    client: AsyncClient, session: AsyncSession
) -> None:
    """П10: отмена автором → участник видит `CARD_CANCELLED` в чате компании.

    Краснеет от: (а) удаления `append_for_card` из `cancel_card`
    (`event_card_service.py:1616`); (б) `kind=PARTICIPANT_LEFT` вместо
    `CARD_CANCELLED` (расшифрованный текст не совпадёт с шаблоном);
    (в) записи сообщения ДО `UPDATE` статуса — тогда `ChatRepository`
    не находит чат по карточке в состоянии `cancelled`.

    **История расхождения (H-2 CR 12.9) — ЗАКРЫТА 2026-10-30.**
    Было: PRD `prd-v2-action-first.md:830` (П10) читался как «при отмене
    карточки места освобождаются», а `build_cancel_statement`
    (`repositories/event_cards.py:762-770`) меняет ТОЛЬКО `status`:
    `.values(status=EventCardStatus.CANCELLED)` — `filled_count` не
    сбрасывается. Мутация, приводящая код к прежней букве PRD
    (`filled_count=0`), оставляла этот тест зелёным — значит правило П10
    буквально им не покрывалось.

    **Решение владельца 2026-10-30 — вариант (б):** «места освобождаются»
    относится к отмене **УЧАСТИЯ** (FR104/П7), **не карточки**. Правка
    текста PRD, код НЕ меняется. PRD `:830` переформулирован: карточка
    терминальна, `filled_count` остаётся исторической величиной. `W-12.9-p`
    закрыт, расхождения между тестом, кодом и требованием больше нет —
    и код, и тест, и PRD теперь говорят одно: отмена карточки статус
    меняет, `filled_count` не трогает.

    Что тест ДЕЙСТВИТЕЛЬНО держит: отмена доходит до участника сообщением
    `CARD_CANCELLED` в чате ЭТОЙ карточки, статус становится `cancelled`.
    """
    author, author_token = await _user(session)
    member, _ = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(session, author_id=author.id)
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    response = await client.delete(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: author_token}
    )

    # Код `200`, а не `204`: `DELETE /cards/{uuid}` отвечает `EventCardRef`
    # с текущим состоянием карточки (D-4.6-2 — та же семантика, что у
    # `POST /cards/{uuid}/cancel`; `204` — это отмена УЧАСТИЯ, не карточки).
    assert response.status_code == 200, response.text
    # 1. Карточка отменена. `filled_count` остаётся историческим (см. докстринг).
    assert (await _card_state(session, card_id))[1] == "cancelled"
    # 2. В чате компании — ровно одно системное сообщение.
    rows = await _system_rows(session, chat_uuid=chat_uuid)
    assert len(rows) == 1, f"ожидалось одно сообщение об отмене, получено {len(rows)}"
    is_system, sender_id, body, chat_id = rows[0]
    assert is_system is True
    assert sender_id is None  # AC-04: автора-пользователя нет
    # 3. Участник УВИДИТ текст отмены — расшифровываем тем же ключом.
    plaintext = decrypt_message_body(body, chat_id=chat_id, sender_id=None)
    assert plaintext == SYSTEM_MESSAGE_TEMPLATES[SystemMessageKind.CARD_CANCELLED]
    # 4. NFR31: в БД шифртекст, открытого текста нет.
    assert plaintext.encode() not in body


async def test_cancel_publishes_system_message_to_chat_channel(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """П10 (M-4 CR 12.9): отмена ПУБЛИКУЕТ сообщение в канал, а не только пишет в БД.

    **Дыра, которую закрывает тест.** CR 12.9 (CR-15) поставил
    `if False and cancel_system_message is not None` (`event_card_service.py:1625`)
    — доставка в канал исчезла, а оба теста П10 остались зелёными
    (`2 passed`). Запись в БД проверялась, отправка — нет. Класс `W-11.8-c`:
    ассерт факта без ассерта эффекта. Для участника разница решающая:
    он узнаёт об отмене из события `message.created`, а не из опроса.

    **Почему перехват `publish_system_message`, а не `publish_to_chat`.**
    Первый — граница домена: он получает `chat_uuid` карточки и собранный
    `WsEnvelope`. Проверка на этом уровне утверждает «сервис отмены вызвал
    публикацию с идентификатором ИМЕННО этого чата», что и есть требование
    FR100. Перехват второго уровня проверял бы сериализацию `WsEnvelope`
    (её покрывает `test_system_message_service`).

    Пара к `test_cancel_without_chat_writes_no_message`: там чата нет —
    публикации быть не должно.
    """
    from app.services import event_card_service as ecs

    published: list[dict[str, Any]] = []

    async def _spy(*, chat_uuid: Any, message_uuid: Any, body: str, created_at: Any) -> None:
        published.append(
            {
                "chat_uuid": str(chat_uuid),
                "message_uuid": str(message_uuid),
                "body": body,
                "created_at": created_at,
            }
        )

    monkeypatch.setattr(ecs, "publish_system_message", _spy)

    author, author_token = await _user(session)
    member, _ = await _user(session)
    card_uuid, card_id, chat_uuid = await _card_with_chat(session, author_id=author.id)
    await _participant(session, card_id=card_id, user_id=member.id)
    await session.flush()

    response = await client.delete(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: author_token}
    )
    assert response.status_code == 200, response.text

    # 1. Публикация состоялась РОВНО один раз.
    assert len(published) == 1, f"ожидалась одна публикация, получено {len(published)}"
    event = published[0]
    # 2. Канал — ИМЕННО чат этой карточки, а не чат вообще.
    assert event["chat_uuid"] == chat_uuid, (
        f"событие ушло в чужой канал: {event['chat_uuid']} вместо {chat_uuid}"
    )
    # 3. Текст события — шаблон отмены, а не что-то ещё.
    assert event["body"] == SYSTEM_MESSAGE_TEMPLATES[SystemMessageKind.CARD_CANCELLED]
    # 4. `message_uuid` указывает на строку, которая реально лежит в БД: клиент
    #    по нему дедуплицирует событие (брокер работает по `(type, uuid)`).
    rows = await _system_rows(session, chat_uuid=chat_uuid)
    assert len(rows) == 1
    stored_uuid = await session.execute(
        text("SELECT m.uuid FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.uuid = :u"),
        {"u": chat_uuid},
    )
    assert event["message_uuid"] == str(stored_uuid.scalar_one()), (
        "опубликован uuid, которого нет в БД — клиент не сможет дедуплицировать"
    )


async def test_cancel_without_chat_writes_no_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """П10 (пара): несобранная карточка — чата нет, сообщения быть не должно.

    Пара к тесту выше: без неё «сообщение есть всегда» проходило бы так же,
    как «сообщение есть только у собранной». Отмена обязана работать и на
    карточке без чата (FR100 не требует компании), но объяснять некому.
    """
    author, author_token = await _user(session)
    place_id = await _place(session)
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, kind, activity) "
            "VALUES (:a, :p, :s, 'Встреча', 'cafe', :st, 3, 60, :e, 'published', 0, "
            "'approved', :pub, 'company', 'coffee') RETURNING uuid, id"
        ),
        {
            "a": author.id,
            "p": place_id,
            "s": settings.default_city_slug,
            "st": starts,
            "e": starts - timedelta(hours=1),
            "pub": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    await session.flush()

    response = await client.delete(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: author_token}
    )

    # Код `200`, а не `204`: `DELETE /cards/{uuid}` отвечает `EventCardRef`
    # с текущим состоянием карточки (D-4.6-2 — та же семантика, что у
    # `POST /cards/{uuid}/cancel`; `204` — это отмена УЧАСТИЯ, не карточки).
    assert response.status_code == 200, response.text
    assert await _card_state(session, int(card_id)) == (0, "cancelled")
    total = await session.execute(text("SELECT count(*) FROM messages"))
    assert int(total.scalar_one()) == 0, "сообщение без чата записывать некуда"
