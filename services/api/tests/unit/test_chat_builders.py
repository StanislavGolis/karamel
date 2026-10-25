"""Текст билдеров чата (стори 6.2; AC-01, AC-02, AC-04, AC-07).

§18.2: проверяется наблюдаемое свойство SQL (предикат, `RETURNING`, состав
`values`), а не факт объявления метода. Билдеры статические именно ради
этого: гонку и порядок блокировок доказывают интеграционные тесты и спаи.

TD 6.2: U1, U2, U5, U6 (×2 — §18.11 «оба значения» для ключа конфликта),
U7, U8, U8b.
"""

from __future__ import annotations

import uuid as uuid_lib

from app.repositories.chats import ChatRepository
from app.repositories.event_cards import EventCardRepository

CARD_ID = 42
CHAT_UUID = uuid_lib.UUID("66666666-6666-4666-8666-666666666666")


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами (приём 4.1)."""
    return str(
        statement.compile(  # type: ignore[attr-defined]
            compile_kwargs={"literal_binds": True}
        )
    )


# ——— U1: идемпотентность по event_card_id ———


def test_ensure_statement_conflicts_on_event_card_id() -> None:
    """U1, AC-04, D-6.2-2: `ON CONFLICT (event_card_id) DO NOTHING`.

    Краснеет от мутации M1 (ключ конфликта `uuid`): повторная сборка давала бы
    `IntegrityError` по `uq_chats_event_card_id` и `500` пользователю, а
    конфликт по `uuid` маскировал бы коллизию генератора.
    """
    sql = _sql(ChatRepository.build_ensure_statement(chat_uuid=CHAT_UUID, event_card_id=CARD_ID))

    lowered = sql.lower()
    assert "on conflict" in lowered
    # §18.11: ключ конфликта — ИМЕННО карточка, а не `uuid`.
    conflict_clause = lowered.split("on conflict")[1]
    assert "event_card_id" in conflict_clause
    assert "do nothing" in lowered
    assert "uuid" not in conflict_clause.split("do nothing")[0]


# ——— U2: безличность вставки ———


def test_ensure_statement_has_no_participant_fields() -> None:
    """U2, AC-08, ADR-04: в `values` нет полей участников.

    Краснеет, если в INSERT вернутся `user_a_id`/`user_b_id`/`applicant_id`:
    состав компании вычисляется из заявок, а не хранится (ADR-04).
    """
    sql = _sql(
        ChatRepository.build_ensure_statement(chat_uuid=CHAT_UUID, event_card_id=CARD_ID)
    ).lower()
    insert_part = sql.split("values")[1].split("on conflict")[0]

    for forbidden in ("user", "sender", "applicant", "participant", "member"):
        assert forbidden not in insert_part, forbidden


# ——— U5: минимальный RETURNING ———


def test_ensure_statement_returns_uuid_and_id_only() -> None:
    """U5: `RETURNING uuid, id`, не `*`.

    Краснеет при `RETURNING *`: в горячем пути тянулись бы лишние колонки
    (`created_at`, `updated_at`, `event_card_id`), которые сервис не читает.
    """
    sql = _sql(
        ChatRepository.build_ensure_statement(chat_uuid=CHAT_UUID, event_card_id=CARD_ID)
    ).lower()
    returning = sql.split("returning")[1]

    assert "uuid" in returning
    assert "id" in returning
    assert "created_at" not in returning
    assert "updated_at" not in returning
    assert "*" not in returning


# ——— U6: никакого SELECT перед INSERT ———


def test_ensure_statement_has_no_select() -> None:
    """U6, D-6.2-2: проверка-перед-вставкой отвергнута явно.

    Краснеет, если вернуть анти-паттерн `SELECT … WHERE event_card_id` + `INSERT`:
    две параллельные транзакции видят «чата нет» и обе вставляют.
    """
    sql = _sql(
        ChatRepository.build_ensure_statement(chat_uuid=CHAT_UUID, event_card_id=CARD_ID)
    ).lower()

    assert "insert into chats" in sql
    assert "select" not in sql


def test_get_by_card_statement_uses_event_card_id() -> None:
    """U6 (обратная сторона, §18.11): чтение идёт по `event_card_id`.

    Краснеет, если фильтр заменить на `uuid`: диагностический метод начал бы
    отвечать «нет чата» на существующей карточке.
    """
    sql = _sql(ChatRepository.build_get_by_card_statement(event_card_id=CARD_ID)).lower()

    assert "select" in sql
    assert "event_card_id" in sql


# ——— U7/U8/U8b: seat-CAS открытого набора ———


def test_join_seat_returning_contains_chat_uuid() -> None:
    """U7, AC-02, TD п.2 (риск R9): `RETURNING` отдаёт `chat_uuid`.

    Краснеет от мутации M9: без колонки сервис не узнаёт о сборке, и чат на
    последнем месте не создаётся — молчаливое «не создаём».
    """
    sql = _sql(
        EventCardRepository.build_join_seat_statement(card_id=CARD_ID, chat_uuid=CHAT_UUID)
    ).lower()

    assert "returning" in sql
    assert "chat_uuid" in sql.split("returning")[1]


def test_join_seat_case_sets_chat_uuid_only_on_full() -> None:
    """U8, AC-02, AC-05, D-6.2-1: `CASE` пишет чат только на последнем месте.

    Краснеет от мутации M2 (безусловная запись `chat_uuid`): чат появился бы
    на незаполненной карточке (FR30). Краснеет и от M3 (`>=` → `>`): на
    последнем месте чат остался бы `NULL`.
    """
    sql = _sql(
        EventCardRepository.build_join_seat_statement(card_id=CARD_ID, chat_uuid=CHAT_UUID)
    ).lower()

    # CR 6.2 (edge #12, auditor #9): проверки привязаны к ВЫРАЖЕНИЮ `chat_uuid`,
    # а не ко всему тексту запроса. Раньше `">=" in sql` удовлетворялось
    # условием из `status=CASE(...)`, и мутация `>=`→`>` в CASE чата оставалась
    # незамеченной unit-уровнем.
    # SQLAlchemy рендерит `chat_uuid=(CASE ...)` — БЕЗ пробела перед `=`.
    chat_case = sql.split("chat_uuid=")[1].split("returning")[0]
    assert "filled_count + 1 >= event_cards.capacity" in chat_case
    # `coalesce` сохраняет ранее записанный uuid (CR blind #1, HIGH):
    # без него повторная сборка подменила бы `chats.uuid`.
    assert "coalesce(event_cards.chat_uuid" in chat_case
    # `else_` — само поле, а не `NULL` (мутация M8 / риск R10).
    assert "else event_cards.chat_uuid" in chat_case


def test_join_seat_values_contain_three_keys_only() -> None:
    """U8b, AC-07: ОДИН `UPDATE` пишет `filled_count`, `status`, `chat_uuid`.

    Краснеет, если чат вынести во второй `UPDATE`: между инкрементом и записью
    чата существовала бы строка `published` + `filled_count = capacity`,
    запрещённая `ck_event_cards_full_capacity` (X-39, риск R4).
    """
    statement = EventCardRepository.build_join_seat_statement(card_id=CARD_ID, chat_uuid=CHAT_UUID)
    sql = _sql(statement).lower()
    # Ровно один `UPDATE event_cards` в тексте.
    assert sql.count("update event_cards") == 1

    set_part = sql.split("set")[1].split("where")[0]
    assert "filled_count" in set_part
    assert "status" in set_part
    assert "chat_uuid" in set_part
