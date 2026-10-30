"""Схема `chats`/`messages` (стори 6.1; AC-01, AC-02, AC-03, AC-05, AC-06).

Проверки идут через `information_schema`/`pg_constraint`/`pg_indexes`, а не
через модель: модель описывает намерение, а AC говорит о ФАКТИЧЕСКОЙ схеме.
Тест на модели прошёл бы и при миграции, которая таблицу не создала
(класс дефекта `ProfilePhoto`).

§18.1: ограничения проверяются НАРУШЕНИЕМ, а не чтением каталога — наличие
`UNIQUE` в `pg_constraint` не доказывает, что вторая строка отвергается.
Каталог используется для ИМЁН (`op.f`, §18.7) и предикатов индексов.

§18.8: каждый ожидаемый отказ — в своём `begin_nested()`: после
`IntegrityError` транзакция аборчена.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01, AC-02: точный состав колонок `chats`. Равенство, а не подмножество:
# падает и при лишней колонке (участники!), и при недостающей.
EXPECTED_CHAT_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "event_card_id": ("bigint", "NO"),
    # Стори 8.2 (AC-01, D-21): состояние ДИАЛОГА. `NOT NULL`: третьего
    # состояния «статус не задан» не существует, а `server_default`
    # обеспечивает видимость существующих чатов после `0026`.
    "status": ("character varying", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}

# AC-03: состав колонок `messages`; `body` — `bytea` (шифртекст).
EXPECTED_MESSAGE_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "chat_id": ("bigint", "NO"),
    # Стори 6.7 (`0019`, AC-04): у СИСТЕМНОГО сообщения автора нет —
    # `NULL` допустим, иначе вставка системного падала бы `NOT NULL`
    # (блокер X-6.6-5). Инвариант держит `CHECK`, а не тип колонки.
    "sender_id": ("bigint", "YES"),
    # Признак системности: `NOT NULL` + `server_default`, третьего состояния нет.
    "is_system": ("boolean", "NO"),
    "body": ("bytea", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    # Стори 6.4 (`0016`): публичный адрес сообщения (D-2) и клиентский
    # идентификатор для идемпотентного ретрая (AC-04, NFR18).
    "uuid": ("uuid", "NO"),
    "client_message_id": ("uuid", "YES"),
    # Стори 6.5 (`0017`): доставка — отдельный факт от прочтения
    # (`delivered ≠ read`, AC-06). `YES` — сообщение могло не дойти до
    # подписчика: `NULL` здесь корректное состояние, а не долг.
    "delivered_at": ("timestamp with time zone", "YES"),
    # Стори 6.6 (`0018`, AC-02, FR35): ПРОЧТЕНИЕ — второе, отдельное поле.
    # `YES` — «ещё не читали»; смешение с `delivered_at` сделало бы
    # невозможным ответ «доставлено, но не прочитано» (NFR12).
    "read_at": ("timestamp with time zone", "YES"),
}

# AC-09: имена ограничений — часть контракта `downgrade`/`alembic check`.
EXPECTED_CONSTRAINTS = (
    "pk_chats",
    "uq_chats_uuid",
    "uq_chats_event_card_id",
    "fk_chats_event_card_id_event_cards",
    # Стори 8.2 (AC-01): домен состояния. Имя входит в контракт `downgrade`
    # и `alembic check`; двойной префикс (`ck_chats_ck_chats_status`) —
    # класс дефекта 3.1/4.1/6.7-D3.
    "ck_chats_status",
    "pk_messages",
    "ck_messages_body_length",
    "fk_messages_chat_id_chats",
    "fk_messages_sender_id_users",
    # Стори 6.4 (`0016`): `uuid` — публичный адрес, значит уникален.
    "uq_messages_uuid",
    # Стори 6.7 (`0019`, D-6.7-1): БИКОНДИЦИОНАЛ «is_system ⟺ sender_id IS NULL».
    # Имя входит в контракт: `downgrade` снимает его по имени и без него
    # упал бы (класс дефекта двойного префикса).
    "ck_messages_system_sender_exclusive",
)


def _next_phone() -> str:
    return f"+7907{uuid_lib.uuid4().int % 10**7:07d}"


async def _create_user(session: AsyncSession) -> int:
    """Пользователь с согласием на ПД — обязательное условие домена."""
    phone = _next_phone()
    row = await session.execute(
        text(
            "INSERT INTO users (uuid, provider, provider_user_id, phone, role, status, "
            "age_confirmed, consent_given_at, city_slug) "
            "VALUES (gen_random_uuid(), 'phone', :phone, :phone, 'user', 'active', "
            "true, now(), 'tomsk') "
            "RETURNING id"
        ),
        {"phone": phone},
    )
    return int(row.scalar_one())


async def _create_place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
    )
    return int(row.scalar_one())


async def _create_card(session: AsyncSession, *, author_id: int, place_id: int) -> int:
    """Опубликованная карточка: нужен только `id` для FK чата."""
    now = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, 'tomsk', 'Встреча', NULL, 'cafe', :starts, 3, 2880, "
            ":expires, 'published', 0, 'approved', :published_at, 'company', 'other') RETURNING id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "starts": now + timedelta(days=3),
            "expires": now + timedelta(days=1),
            "published_at": now,
        },
    )
    return int(row.scalar_one())


async def _insert_chat(session: AsyncSession, *, event_card_id: int) -> tuple[int, str]:
    chat_uuid = str(uuid_lib.uuid4())
    row = await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (:uuid, :card) RETURNING id"),
        {"uuid": chat_uuid, "card": event_card_id},
    )
    return int(row.scalar_one()), chat_uuid


async def _insert_message(
    session: AsyncSession, *, chat_id: int, sender_id: int, body: bytes
) -> int:
    row = await session.execute(
        text(
            # Стори 6.4: `uuid` NOT NULL (публичный адрес, D-2). Хелпер
            # генерирует его сам: тесты схемы не проверяют конкретное значение.
            "INSERT INTO messages (uuid, chat_id, sender_id, body) "
            "VALUES (:uuid, :chat, :sender, :body) RETURNING id"
        ),
        {
            "uuid": str(uuid_lib.uuid4()),
            "chat": chat_id,
            "sender": sender_id,
            "body": body,
        },
    )
    return int(row.scalar_one())


async def test_chats_columns_match_declared_schema(session: AsyncSession) -> None:
    """AC-01, AC-02: состав колонок `chats` — ровно пять, без участников.

    Краснеет при добавлении `user_a_id`/`user_b_id`: ADR-04 запрещает хранить
    состав компании, он вычисляется из заявок.
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'chats'"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}

    assert actual == EXPECTED_CHAT_COLUMNS
    assert not any("user" in name for name in actual)


async def test_messages_columns_match_declared_schema(session: AsyncSession) -> None:
    """AC-03: `body` — `bytea NOT NULL`, то есть шифртекст, а не текст.

    Краснеет, если колонка станет `text`: тогда в БД лежал бы читаемый
    plaintext (нарушение NFR31).
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'messages'"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}

    assert actual == EXPECTED_MESSAGE_COLUMNS


async def test_constraint_names_match_model(session: AsyncSession) -> None:
    """AC-09: имена ограничений совпадают с моделью (§18.7).

    Краснеет при расхождении имени в миграции: `downgrade`/`alembic check`
    опираются на имя, и откат падал бы на несуществующем ограничении
    (класс дефекта 3.1/4.1 — двойной префикс конвенции).
    """
    # PostgreSQL 18 выводит NOT NULL отдельными записями `contype='n'`
    # (`chats_uuid_not_null`). Это не ограничения схемы, а атрибут колонки:
    # они уже проверены через `information_schema.columns.is_nullable`.
    # Фильтр по `contype` оставляет ровно pk/unique/check/fk — то, что
    # перечислено в контракте.
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid IN ('chats'::regclass, 'messages'::regclass) "
            "AND contype IN ('p', 'u', 'c', 'f')"
        )
    )
    names = {row[0] for row in result}

    assert names == set(EXPECTED_CONSTRAINTS)


async def test_second_chat_for_same_card_is_rejected(session: AsyncSession) -> None:
    """AC-01: «один чат на карточку» отвергается БД, а не сервисом.

    Краснеет, если `UNIQUE (event_card_id)` убрать: два чата на одну встречу
    разошлись бы по составу компании, и «компания» перестала бы быть
    определённой.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    card = await _create_card(session, author_id=author, place_id=place)
    await _insert_chat(session, event_card_id=card)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_chat(session, event_card_id=card)


async def test_two_chats_for_different_cards_are_allowed(session: AsyncSession) -> None:
    """AC-01, §18.11: уникальность — по карточке, а не глобальная.

    Краснеет, если `UNIQUE` поставить на `uuid` вместо `event_card_id` и
    забыть генерацию: второй чат получил бы тот же ключ и упал бы, хотя
    карточки разные.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    first = await _create_card(session, author_id=author, place_id=place)
    second = await _create_card(session, author_id=author, place_id=place)

    first_id, _ = await _insert_chat(session, event_card_id=first)
    second_id, _ = await _insert_chat(session, event_card_id=second)

    assert first_id != second_id


async def test_duplicate_chat_uuid_is_rejected(session: AsyncSession) -> None:
    """AC-01: `uuid` уникален — публичный идентификатор не повторяется.

    Краснеет, если `uq_chats_uuid` убрать: клиент адресует чат по `uuid`,
    и два чата стали бы неразличимы.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    first = await _create_card(session, author_id=author, place_id=place)
    second = await _create_card(session, author_id=author, place_id=place)
    _, chat_uuid = await _insert_chat(session, event_card_id=first)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("INSERT INTO chats (uuid, event_card_id) VALUES (:uuid, :card)"),
                {"uuid": chat_uuid, "card": second},
            )


async def test_body_shorter_than_nonce_plus_tag_is_rejected(session: AsyncSession) -> None:
    """AC-03: тело ≤ 28 байт отвергается БД.

    Краснеет, если `CHECK octet_length(body) > 28` убрать: в БД попал бы
    блоб без nonce/tag, который невозможно расшифровать.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    card = await _create_card(session, author_id=author, place_id=place)
    chat_id, _ = await _insert_chat(session, event_card_id=card)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_message(session, chat_id=chat_id, sender_id=author, body=b"x" * 28)


async def test_body_of_nonce_plus_tag_plus_one_byte_is_accepted(
    session: AsyncSession,
) -> None:
    """AC-03, §18.11: граница `> 28` — 29 байт проходит.

    Краснеет, если `CHECK` сделать `>= 28` или `> 29`: минимальный корректный
    блоб (nonce+tag+1 байт шифртекста) отвергался бы, и односимвольное
    сообщение нельзя было бы сохранить.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    card = await _create_card(session, author_id=author, place_id=place)
    chat_id, _ = await _insert_chat(session, event_card_id=card)

    message_id = await _insert_message(session, chat_id=chat_id, sender_id=author, body=b"x" * 29)

    assert message_id > 0


async def test_deleting_card_cascades_to_chat_and_messages(session: AsyncSession) -> None:
    """AC-01, AC-03: `DELETE` карточки уносит чат и переписку (`CASCADE`).

    Краснеет, если `ondelete` заменить на `RESTRICT`: удаление карточки
    падало бы на FK, а висячий чат остался бы в БД.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    card = await _create_card(session, author_id=author, place_id=place)
    chat_id, _ = await _insert_chat(session, event_card_id=card)
    await _insert_message(session, chat_id=chat_id, sender_id=author, body=b"x" * 29)

    await session.execute(text("DELETE FROM event_cards WHERE id = :card"), {"card": card})

    chats = await session.execute(
        text("SELECT count(*) FROM chats WHERE id = :id"), {"id": chat_id}
    )
    messages = await session.execute(
        text("SELECT count(*) FROM messages WHERE chat_id = :id"), {"id": chat_id}
    )
    assert chats.scalar_one() == 0
    assert messages.scalar_one() == 0


async def test_deleting_sender_cascades_to_messages(session: AsyncSession) -> None:
    """AC-03: `DELETE` пользователя уносит его сообщения (`CASCADE`).

    Краснеет, если у `sender_id` нет `ondelete="CASCADE"`: чистка PII
    (NFR1–NFR4) стала бы невозможной без ручного удаления переписки.
    """
    sender = await _create_user(session)
    other = await _create_user(session)
    place = await _create_place(session)
    card = await _create_card(session, author_id=other, place_id=place)
    chat_id, _ = await _insert_chat(session, event_card_id=card)
    message_id = await _insert_message(session, chat_id=chat_id, sender_id=sender, body=b"x" * 29)

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": sender})

    remaining = await session.execute(
        text("SELECT count(*) FROM messages WHERE id = :id"), {"id": message_id}
    )
    assert remaining.scalar_one() == 0


async def test_messages_history_index_exists_with_exact_columns(
    session: AsyncSession,
) -> None:
    """AC-03: `ix_messages_chat_created (chat_id, created_at, id)` в живой БД.

    Краснеет, если индекс объявлен только в модели: страница истории
    сортировалась бы в памяти (Sort) на каждом запросе.
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'messages' AND indexname = 'ix_messages_chat_created'"
        )
    )
    indexdef = result.scalar_one()

    assert "(chat_id, created_at, id)" in indexdef
    assert "WHERE" not in indexdef


async def test_unread_index_exists_with_partial_predicate(session: AsyncSession) -> None:
    """Стори 6.6 (`0018`, AC-01, AC-05): индекс непрочитанных — ЧАСТИЧНЫЙ.

    Краснеет, если индекс не создан или создан БЕЗ `WHERE read_at IS NULL`:
    тогда он индексировал бы и прочитанные строки (их большинство), и подсчёт
    непрочитанных перестал бы быть дешёвым (NFR12).
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'messages' AND indexname = 'ix_messages_chat_unread'"
        )
    )
    indexdef = result.scalar_one_or_none()

    assert indexdef is not None, "индекс ix_messages_chat_unread отсутствует"
    assert "(chat_id, read_at)" in indexdef
    assert "WHERE (read_at IS NULL)" in indexdef


async def test_body_over_upper_bound_is_rejected(session: AsyncSession) -> None:
    """CR 6.1: тело длиннее `MAX_BODY_BYTES`(+nonce+tag) отвергается БД.

    Краснеет, если верхнюю границу убрать: в `BYTEA` помещается сообщение
    произвольного размера, и страница истории тянет его в память целиком.
    """
    author = await _create_user(session)
    place = await _create_place(session)
    card = await _create_card(session, author_id=author, place_id=place)
    chat_id, _ = await _insert_chat(session, event_card_id=card)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_message(
                session, chat_id=chat_id, sender_id=author, body=b"x" * (65_535 + 29)
            )


async def test_sender_index_exists(session: AsyncSession) -> None:
    """CR 6.1: `ix_messages_sender_id` создан миграцией `0014`.

    Краснеет, если индекс объявлен только в модели: каскад от `users`
    сканирует всю таблицу сообщений.
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'messages' AND indexname = 'ix_messages_sender_id'"
        )
    )
    indexdef = result.scalar_one()

    assert "(sender_id)" in indexdef


async def test_debt_index_predicates_name_exact_statuses(
    session: AsyncSession,
) -> None:
    """CR 6.1 (находка 10): предикаты содержат РОВНО нужные статусы.

    Раньше проверялось только наличие `WHERE`: сужение предиката до одного
    статуса или добавление `not_selected` (FR81 — заявителю не отдаётся)
    оставалось зелёным, и индекс молча терял покрытие.
    """
    result = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE indexname IN ('ix_join_requests_applicant_visible', "
            "'ix_event_cards_feed_visible')"
        )
    )
    defs = {row[0]: row[1] for row in result}

    applicant = defs["ix_join_requests_applicant_visible"]
    for status in ("pending", "selected", "cancelled", "expired"):
        assert f"'{status}'" in applicant, (status, applicant)
    assert "not_selected" not in applicant

    feed = defs["ix_event_cards_feed_visible"]
    for status in ("published", "full"):
        assert f"'{status}'" in feed, (status, feed)
    for status in ("draft", "closed", "finished", "cancelled"):
        assert f"'{status}'" not in feed


async def test_debt_indexes_exist_in_database(session: AsyncSession) -> None:
    """AC-06: индексы долгов X-5a.3-1 и X-5b.1-1 созданы миграцией 0014.

    Краснеет, если индекс объявлен в модели, но не создан миграцией
    (класс дефекта `ProfilePhoto`): модели «есть», схема — нет.
    """
    result = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE indexname IN ('ix_join_requests_applicant_visible', "
            "'ix_event_cards_feed_visible')"
        )
    )
    defs = {row[0]: row[1] for row in result}

    assert set(defs) == {"ix_join_requests_applicant_visible", "ix_event_cards_feed_visible"}
    # X-5a.3-1: порядок колонок — ровно ключ курсора «моих заявок».
    assert "(applicant_id, submitted_at, uuid)" in defs["ix_join_requests_applicant_visible"]
    assert "WHERE" in defs["ix_join_requests_applicant_visible"]
    # X-5b.1-1: порядок ленты и частичный предикат по статусам.
    assert "(city_slug, starts_at, uuid)" in defs["ix_event_cards_feed_visible"]
    assert "WHERE" in defs["ix_event_cards_feed_visible"]
