"""Текст билдеров сообщений (стори 6.4; AC-01, AC-02, AC-04).

§18.2: проверяется наблюдаемое свойство SQL, а не факт объявления метода.
Гард участия и анти-оракул доказываются интеграционными тестами.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from app.repositories.messages import MessageRepository

CHAT_ID = 11
SENDER_ID = 22
MESSAGE_UUID = uuid_lib.UUID("77777777-7777-4777-8777-777777777777")
CLIENT_ID = uuid_lib.UUID("88888888-8888-4888-8888-888888888888")
CREATED_AT = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами (приём 4.1)."""
    return str(
        statement.compile(  # type: ignore[attr-defined]
            compile_kwargs={"literal_binds": True}
        )
    ).lower()


def _insert_sql() -> str:
    return _sql(
        MessageRepository.build_insert_statement(
            chat_id=CHAT_ID,
            sender_id=SENDER_ID,
            body=b"nonce-and-ciphertext-and-tag",
            message_uuid=MESSAGE_UUID,
            client_message_id=CLIENT_ID,
        )
    )


# ——— стори 6.6 (AC-02, AC-06): отметка прочтения ———


def test_mark_read_statement_updates_only_unread_others_messages() -> None:
    """Стори 6.6 (AC-02, AC-06): UPDATE трогает только непрочитанное ЧУЖОЕ.

    Краснеет от трёх мутаций:
    - снять `read_at IS NULL` → повторное прочтение переписывало бы время
      ПЕРВОГО прочтения, и «прочитано» перестало бы быть неизменным фактом;
    - снять `sender_id <> viewer` → собственные сообщения считались бы
      непрочитанными, и счётчик у автора всегда был бы ≥ 1;
    - снять `chat_id` в `WHERE` → отметка затронула бы чужие чаты.
    """
    sql = _sql(MessageRepository.build_mark_read_statement(chat_id=CHAT_ID, viewer_id=SENDER_ID))
    lowered = sql.lower()

    assert lowered.startswith("update messages")
    assert "read_at=now()" in lowered.replace(" ", "")
    where_clause = lowered.split("where")[1]
    assert f"chat_id = {CHAT_ID}" in where_clause
    assert "read_at is null" in where_clause
    assert (
        f"sender_id != {SENDER_ID}" in where_clause or f"sender_id <> {SENDER_ID}" in where_clause
    )


# ——— AC-04: идемпотентность по (chat_id, client_message_id) ———


def test_insert_statement_conflicts_on_chat_and_client_id() -> None:
    """AC-04, D-6.4-2: `ON CONFLICT (chat_id, client_message_id) DO NOTHING`.

    Краснеет от мутации M1 (снять `ON CONFLICT`): ретрай после обрыва давал бы
    `IntegrityError` → `500` на легальном действии пользователя (NFR18).
    """
    sql = _insert_sql()

    lowered = sql.lower()
    assert "on conflict" in lowered
    conflict_clause = lowered.split("on conflict")[1].split("do nothing")[0]
    assert "chat_id" in conflict_clause
    assert "client_message_id" in conflict_clause
    assert "do nothing" in lowered
    # §18.11: конфликт НЕ по `uuid` сообщения — иначе коллизия генератора
    # маскировалась бы под повторную отправку.
    assert "uuid" not in conflict_clause.replace("client_message_id", "")


def test_insert_statement_has_no_plaintext_column() -> None:
    """AC-01, NFR31: в `values` нет колонки с открытым текстом.

    Краснеет, если кто-то добавит `body_text`/`plaintext`: шифрование стало бы
    необязательным, и открытый текст попал бы в БД на первом же рефакторинге.
    """
    sql = _insert_sql()
    values_part = sql.split("values")[1].split("on conflict")[0]

    for forbidden in ("plaintext", "body_text", "text_body", "message_text"):
        assert forbidden not in values_part, forbidden


# ——— AC-02: порядок истории ———


def test_history_statement_orders_by_created_at_then_uuid() -> None:
    """AC-02: `ORDER BY created_at ASC, uuid ASC` — ключ `ix_messages_chat_created_uuid`.

    Краснеет от мутации M5 (снять `ORDER BY`): страницы истории шли бы в
    физическом порядке строк, и keyset-курсор терял бы сообщения.
    """
    sql = _sql(MessageRepository.build_history_statement(chat_id=CHAT_ID, after=None, limit=31))

    assert "order by" in sql
    order_clause = sql.split("order by", 1)[1]
    assert "created_at" in order_clause
    assert "messages.uuid" in order_clause
    assert order_clause.index("created_at") < order_clause.index("messages.uuid")
    # D-2: внутренний `id` в публичном порядке не участвует — иначе он стал бы
    # адресом в курсоре.
    assert "messages.id" not in order_clause


def test_history_statement_keyset_predicate_is_tuple_comparison() -> None:
    """AC-02: курсор — кортежное сравнение `(created_at, id) > (:a, :b)`.

    Краснеет, если предикат разложат на `created_at > :a OR (created_at = :a
    AND id > :b)` с ошибкой приоритетов: страница теряла бы сообщения с тем
    же `created_at`.
    """
    sql = _sql(
        MessageRepository.build_history_statement(
            chat_id=CHAT_ID,
            after=(CREATED_AT, MESSAGE_UUID),
            limit=31,
        )
    )

    assert "created_at, messages.uuid) >" in sql
    # Фильтр по чату присутствует всегда — иначе страница вернула бы чужую переписку.
    assert "messages.chat_id = 11" in sql


def test_history_statement_filters_by_chat_without_cursor() -> None:
    """AC-02: без курсора фильтр по чату есть, keyset-предиката нет."""
    sql = _sql(MessageRepository.build_history_statement(chat_id=CHAT_ID, after=None, limit=31))

    assert "messages.chat_id = 11" in sql
    assert "created_at, messages.uuid) >" not in sql
    assert "limit 31" in sql
