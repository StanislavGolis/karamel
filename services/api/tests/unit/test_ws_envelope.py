"""Unit-тесты WS-контракта (стори 6.5; TD 6.5: U1–U4, U6–U7).

Без БД и без сети: здесь проверяется форма конверта (NFR60), реестр типов и
текст миграции `0017`. Всё, что требует PostgreSQL или сокета, живёт в
`tests/integration/test_ws_*.py`.

**Почему эти тесты важны отдельно.** Конверт — единственный контракт,
который делят бэкенд и фронт. Ошибка в нём (`+00:00` вместо `Z`, `type`
заглавными, `payload` массивом) не ломает серверные тесты доставки, но
ломает КЛИЕНТ, и обнаруживается уже в браузере. Здесь она ловится за
секунды.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.ws import WS_EVENT_TYPES, WsEnvelope, utc_timestamp

# ——— U1: `ts` c суффиксом Z —————————————————————————————————————————————


def test_envelope_ts_has_z() -> None:
    """U1 (NFR60): `ts` сериализуется с `Z`, а не с `+00:00`.

    Мутация, которая должна покраснеть: `utc_timestamp` без
    `.replace("+00:00", "Z")` → клиент получает `+00:00` и разбирает время
    иначе, чем курсор `(created_at, uuid)`, который уже умеет `Z`.
    """
    frame = WsEnvelope.build("message.created", {"a": 1}).to_frame()

    assert frame["ts"].endswith("Z")
    assert "+00:00" not in frame["ts"]


def test_utc_timestamp_normalizes_non_utc_moment() -> None:
    """U1: момент в другом поясе приводится к UTC, а не печатается как есть."""
    from datetime import timedelta, timezone

    tokyo = timezone(timedelta(hours=9))
    moment = datetime(2026, 10, 24, 23, 0, tzinfo=tokyo)

    assert utc_timestamp(moment) == "2026-10-24T14:00:00Z"


def test_utc_timestamp_keeps_microseconds() -> None:
    """U1: микросекунды не теряются — иначе два события в одну секунду равны."""
    moment = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)

    assert utc_timestamp(moment) == "2026-10-24T12:00:00.123456Z"


# ——— U2: форма `type` ————————————————————————————————————————————————————


@pytest.mark.parametrize(
    "bad_type",
    [
        "Message.Created",  # заглавные
        "MessageCreated",  # без точки
        "message_created",  # подчёркивание вместо точки
        "message.",  # пустая правая часть
        ".created",  # пустая левая часть
        "message.created.extra",  # две точки
        "message created",  # пробел
        "",  # пустая строка
    ],
)
def test_type_rejects_broken_forms(bad_type: str) -> None:
    """U2 (NFR60): `type` — только `сущность.действие` нижним регистром."""
    with pytest.raises(ValidationError):
        WsEnvelope(type=bad_type, payload={}, ts="2026-10-24T00:00:00Z")


@pytest.mark.parametrize(
    "good_type",
    ["message.created", "chat.restored", "card.full", "attendance.request", "message.delivered"],
)
def test_type_accepts_valid_forms(good_type: str) -> None:
    """U2, второе значение признака (§18.11): корректные типы принимаются."""
    envelope = WsEnvelope(type=good_type, payload={}, ts="2026-10-24T00:00:00Z")

    assert envelope.type == good_type


# ——— U3: `payload` — объект ————————————————————————————————————————————


@pytest.mark.parametrize("bad_payload", [[{"a": 1}], "строка", 42, None, True])
def test_payload_must_be_object(bad_payload: object) -> None:
    """U3 (NFR60): `payload` — объект, не массив и не скаляр."""
    with pytest.raises(ValidationError):
        WsEnvelope(type="message.created", payload=bad_payload, ts="2026-10-24T00:00:00Z")  # type: ignore[arg-type]


def test_payload_accepts_empty_object() -> None:
    """U3, второе значение: пустой объект допустим (`chat.restored` без данных)."""
    envelope = WsEnvelope(type="chat.restored", payload={}, ts="2026-10-24T00:00:00Z")

    assert envelope.payload == {}


def test_ts_without_z_rejected() -> None:
    """U3/AC-02: `ts` без `Z` отвергается на входе, а не «поправляется» молча."""
    with pytest.raises(ValidationError):
        WsEnvelope(type="message.created", payload={}, ts="2026-10-24T00:00:00+00:00")


def test_extra_fields_forbidden() -> None:
    """U3: лишнее поле в конверте — ошибка, а не тихо проигнорированное.

    Клиент разбирает ровно три поля: четвёртое означало бы, что сервер
    расширил контракт без согласования с фронтом (NFR60).
    """
    with pytest.raises(ValidationError):
        WsEnvelope(
            type="message.created",
            payload={},
            ts="2026-10-24T00:00:00Z",
            extra="лишнее",  # type: ignore[call-arg]
        )


def test_envelope_is_frozen() -> None:
    """U3: событие неизменяемо после сборки — мутация обошла бы валидацию."""
    envelope = WsEnvelope.build("message.created", {"a": 1})

    with pytest.raises(ValidationError):
        envelope.type = "card.full"  # type: ignore[misc]


# ——— U4: реестр типов ——————————————————————————————————————————————————


def test_event_types_registry_complete() -> None:
    """U4 (Task 1.3): реестр содержит типы, которые сервер реально отправляет."""
    for expected in ("message.created", "chat.restored", "card.full", "chat.subscribed"):
        assert expected in WS_EVENT_TYPES


def test_event_types_match_pattern() -> None:
    """U4: каждый тип реестра проходит тот же валидатор, что и событие."""
    for type_ in WS_EVENT_TYPES:
        WsEnvelope(type=type_, payload={}, ts="2026-10-24T00:00:00Z")


def test_message_created_payload_shape() -> None:
    """AC-01: payload `message.created` несёт ровно поля контракта.

    Проверяется СОСТАВ, а не только факт сборки: лишнее поле (`ciphertext`,
    `key`) утекло бы клиенту, а отсутствующее (`body`) сделало бы событие
    бесполезным.
    """
    message_uuid = uuid_lib.uuid4()
    chat_uuid = uuid_lib.uuid4()
    sender_uuid = uuid_lib.uuid4()

    frame = WsEnvelope.build(
        "message.created",
        {
            "message_uuid": str(message_uuid),
            "chat_uuid": str(chat_uuid),
            "sender_uuid": str(sender_uuid),
            "body": "привет",
            "created_at": "2026-10-24T00:00:00Z",
        },
    ).to_frame()

    assert set(frame["payload"]) == {
        "message_uuid",
        "chat_uuid",
        "sender_uuid",
        "body",
        "created_at",
    }
    assert "key" not in frame["payload"]
    assert "ciphertext" not in frame["payload"]


# ——— U6, U7: миграция 0017 ——————————————————————————————————————————————


_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0017_add_messages_delivered_at.py"
)


def test_migration_0017_adds_delivered_at_only() -> None:
    """U6 (Task 6.1): в `0017` есть `delivered_at` и НЕТ колонки `read_at`.

    `delivered ≠ read` (AC-06): `read_at` — стори 6.6. Если бы миграция
    добавила обе колонки, «доставлено» и «прочитано» стали бы одним фактом,
    и квитанции 6.6 пришлось бы выводить друг из друга.

    **Проверяется DDL, а не текст файла.** Первая версия теста искала
    подстроку `read_at` по всему исходнику и падала на докстринге, который
    как раз объясняет, почему `read_at` здесь НЕ появляется: тест ловил
    документацию вместо объявления колонки (тавтология наоборот, §18.2).
    """
    source = _MIGRATION.read_text(encoding="utf-8")

    assert 'sa.Column("delivered_at"' in source
    assert 'sa.Column("read_at"' not in source
    assert 'add_column("messages", sa.Column("read_at"' not in source
    # `read_at` не должен попадать ни в один `values`/`where` рабочего кода.
    assert "Message.read_at" not in source


def test_migration_0017_has_partial_index() -> None:
    """U7: частичный индекс по недоставленным строкам присутствует."""
    source = _MIGRATION.read_text(encoding="utf-8")

    assert "ix_messages_chat_undelivered" in source
    assert "delivered_at IS NULL" in source
    assert 'op.f("ix_messages_chat_undelivered")' in source


def test_migration_0017_reverts_column_and_index() -> None:
    """U7: `downgrade` снимает и индекс, и колонку — без «висящих» объектов."""
    source = _MIGRATION.read_text(encoding="utf-8")

    assert "op.drop_index" in source
    assert 'op.drop_column("messages", "delivered_at")' in source


# ——— U5: ключ дедупа ————————————————————————————————————————————————————


def test_dedup_key_uses_type_and_message_uuid() -> None:
    """U5: ключ дедупа — `(type, message_uuid)`, а не `type` целиком.

    Мутация M7 (дедуп по одному `type`) схлопнула бы РАЗНЫЕ сообщения
    одного типа и потеряла бы второе — тест обязан краснеть.
    """
    from app.services.ws_broker import _dedup_key

    first = {"type": "message.created", "payload": {"message_uuid": "m-1"}}
    second = {"type": "message.created", "payload": {"message_uuid": "m-2"}}

    assert _dedup_key(first) == ("message.created", "m-1")
    assert _dedup_key(first) != _dedup_key(second)


def test_dedup_key_absent_when_no_message_uuid() -> None:
    """U5 (D-6.5-6): события без `message_uuid` НЕ дедупятся.

    `chat.restored` не несёт идентификатора сообщения. Ключ по `None`
    схлопнул бы два легитимных события подряд — тест фиксирует, что ключа нет.
    """
    from app.services.ws_broker import _dedup_key

    assert _dedup_key({"type": "chat.restored", "payload": {}}) is None
    assert _dedup_key({"type": "chat.subscribed", "payload": {"chat_uuid": "c"}}) is None
    assert _dedup_key({"type": "message.created", "payload": {"message_uuid": ""}}) is None


def test_dedup_window_is_bounded() -> None:
    """U5: окно дедупа ограничено — память не растёт с числом событий.

    Неограниченный `set` — утечка под видом защиты от дубля: за сутки работы
    процесса он хранит все события. Проверяется ФАКТ вытеснения: после
    окна старый ключ считается новым.

    Ключ с 9.4 включает КАНАЛ (CR HIGH-1), поэтому он передаётся явно.
    """
    from app.services.ws_broker import WsBroker

    broker = WsBroker(dedup_window=2)
    channel = "ws:chat:11111111-1111-1111-1111-111111111111"

    assert broker._accept({"type": "message.created", "payload": {"message_uuid": "a"}}, channel)
    assert broker._accept({"type": "message.created", "payload": {"message_uuid": "b"}}, channel)
    assert broker._accept({"type": "message.created", "payload": {"message_uuid": "c"}}, channel)
    # `a` вытеснен окном из двух — он снова считается новым.
    assert broker._accept({"type": "message.created", "payload": {"message_uuid": "a"}}, channel)
    assert len(broker._dedup_order) == 2


def test_dedup_key_includes_channel() -> None:
    """CR 9.4 HIGH-1: один `event_uuid` в РАЗНЫХ каналах — не дубль.

    Дедуп существует против повторной доставки ОДНОГО сообщения из Redis
    Pub/Sub: повтор приходит в тот же канал. Намеренный фан-аут одного
    события (чат + личные каналы адресатов) — не дубль. Без канала в ключе
    первая публикация резервировала ключ, и `attendance.request` не доходил
    до личных каналов вовсе (FR111 не выполнялся).

    Внутри ОДНОГО канала дедуп обязан работать как раньше — проверяется
    обеими половинами.
    """
    from app.services.ws_broker import WsBroker

    broker = WsBroker()
    frame = {"type": "attendance.request", "event_uuid": "e-1", "payload": {}}
    chat_channel = "ws:chat:11111111-1111-1111-1111-111111111111"
    user_a = "ws:user:22222222-2222-2222-2222-222222222222"
    user_b = "ws:user:33333333-3333-3333-3333-333333333333"

    # Один кадр, три канала — все три доставки живы.
    assert broker._accept(frame, chat_channel) is True
    assert broker._accept(frame, user_a) is True
    assert broker._accept(frame, user_b) is True

    # Повтор в ТОТ ЖЕ канал — дубль, отброшен (защита работает).
    assert broker._accept(frame, chat_channel) is False
    assert broker._accept(frame, user_a) is False
