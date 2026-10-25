"""Модели `chats`/`messages` (стори 6.1; AC-01, AC-02, AC-03, AC-09).

§18.2: проверяется наблюдаемое свойство (состав колонок, имена ограничений,
порядок индекса), а не факт объявления класса. Имена — часть контракта:
`alembic check` и `downgrade` опираются на них (§18.7).

§18.11: булев признак «у чата есть колонки участников» проверяется в ОБОИХ
значениях — отсутствие `user*` в `chats` И наличие `sender_id` в `messages`
(участник сообщения существует, участник чата — нет).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Table, UniqueConstraint

from app.models.chat import Chat
from app.models.message import Message

_CHATS = cast(Table, Chat.__table__)
_MESSAGES = cast(Table, Message.__table__)


def test_chats_columns_are_exactly_expected() -> None:
    """AC-01, AC-02: состав колонок `chats` фиксирован.

    Краснеет, если добавить `user_a_id`/`user_b_id` или «временную» nullable
    колонку: состав компании — вычисляемый (ADR-04), а не хранимый.
    """
    assert list(_CHATS.columns.keys()) == [
        "id",
        "uuid",
        "event_card_id",
        # Стори 8.2 (AC-01, D-21): состояние ДИАЛОГА. Отдельная колонка, а не
        # персональный флаг: FR37 требует скрытия у ОБЕИХ сторон.
        "status",
        "created_at",
        "updated_at",
    ]


def test_chats_has_no_participant_columns() -> None:
    """AC-02: ни одно имя колонки чата не упоминает участника.

    Краснеет, если участники вернутся под другим именем (`member_a_id`).
    """
    forbidden = ("user", "member", "participant", "applicant")

    # Итерация по ОБЪЕКТАМ колонок, а не по `.keys()` (ruff SIM118): сравнивать
    # нужно ИМЯ, а не `Column`, иначе `token in name` падает на типе.
    for column in _CHATS.columns:
        assert not any(token in column.name for token in forbidden), column.name


def test_chats_event_card_id_is_unique() -> None:
    """AC-01: «один чат на карточку» — инвариант БД, а не соглашение кода.

    Краснеет, если `UniqueConstraint` убрать: два чата на одну встречу
    разошлись бы по составу компании.
    """
    unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in _CHATS.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("event_card_id",) in unique_sets
    assert ("uuid",) in unique_sets


def test_chats_event_card_fk_cascades() -> None:
    """AC-01: FK на карточку с `ON DELETE CASCADE`.

    Краснеет, если `ondelete` убрать: удаление карточки оставило бы висячий
    чат, а `DELETE` карточки падал бы на FK-барьере.
    """
    (fk,) = [c for c in _CHATS.constraints if isinstance(c, ForeignKeyConstraint)]

    assert fk.ondelete == "CASCADE"
    assert [element.target_fullname for element in fk.elements] == ["event_cards.id"]


def test_messages_columns_are_exactly_expected() -> None:
    """AC-03: состав колонок `messages` фиксирован, `body` — `bytea`.

    Краснеет, если `body` станет `Text`: тогда шифртекст пришлось бы хранить
    в base64 (потеря 33% объёма и лишний шаг кодирования).
    """
    assert list(_MESSAGES.columns.keys()) == [
        "id",
        # Стори 6.4 (`0016`): публичный адрес (D-2) и клиентский
        # идентификатор идемпотентной отправки (AC-04, NFR18).
        "uuid",
        "client_message_id",
        "chat_id",
        "sender_id",
        # Стори 6.7 (`0019`, AC-04, D-6.7-1): признак системного сообщения —
        # рядом с автором, потому что инвариант связывает ИМЕННО эти две
        # колонки («is_system ⟺ sender_id IS NULL»).
        "is_system",
        "body",
        "created_at",
        # Стори 6.5 (`0017`): момент доставки события участнику.
        "delivered_at",
        # Стори 6.6 (`0018`, AC-02, AC-06): ПРОЧТЕНИЕ — отдельный факт.
        # `delivered ≠ read`: два поля, два смысла; слияние сделало бы
        # невозможным ответ «доставлено, но не прочитано» (NFR12).
        "read_at",
    ]
    assert _MESSAGES.columns["body"].type.__class__.__name__ == "LargeBinary"


def test_messages_fks_cascade() -> None:
    """AC-03: оба FK `CASCADE` (карточка→чат→сообщения, пользователь→сообщения).

    Краснеет, если поставить `RESTRICT`: чистка PII стала бы невозможной.
    """
    targets = {
        element.target_fullname: fk.ondelete
        for fk in _MESSAGES.constraints
        if isinstance(fk, ForeignKeyConstraint)
        for element in fk.elements
    }

    assert targets == {"chats.id": "CASCADE", "users.id": "CASCADE"}


def test_messages_history_index_order_matches_cursor() -> None:
    """AC-03: `ix_messages_chat_created (chat_id, created_at, id)` — точный порядок.

    Краснеет, если порядок изменить (например `(chat_id, id)`): курсор
    `(created_at, id)` перестаёт покрываться, и каждая страница сортируется
    в памяти.
    """
    (index,) = [i for i in _MESSAGES.indexes if i.name == "ix_messages_chat_created"]

    assert isinstance(index, Index)
    assert [column.name for column in index.columns] == ["chat_id", "created_at", "id"]
    assert index.unique is False


def test_messages_body_length_check_exists() -> None:
    """AC-03: `CHECK octet_length(body) > 28` — минимальный инвариант БД.

    Краснеет, если `CheckConstraint` убрать: в БД попал бы блоб короче
    nonce+tag, который невозможно расшифровать.
    """
    checks = [
        str(constraint.sqltext)
        for constraint in _MESSAGES.constraints
        if isinstance(constraint, CheckConstraint)
    ]

    assert any("octet_length(body) > 28" in text for text in checks)
