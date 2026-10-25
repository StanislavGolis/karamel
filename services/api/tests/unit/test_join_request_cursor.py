"""Кодек курсора «моих заявок» (стори 5а.3, Task 9.3; AC-06).

§18.2: проверяется поведение разбора, а не «функция вызвалась». Ключевой
тест — отвержение лишнего ключа: `base64` не шифрование, и `id`, попавший
в курсор, вернулся бы из `decode` открытым (D-2).
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.join_request_cursor import (
    decode_my_requests_cursor,
    encode_my_requests_cursor,
)
from app.services.place_cursor import InvalidCursorError


def _raw(payload: dict[str, object]) -> str:
    """Курсор из произвольного payload — как его собрал бы подделыватель."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_roundtrip_preserves_microseconds_and_uuid() -> None:
    """AC-06: микросекунды и `uuid` переживают кодек.

    Краснеет, если усечь микросекунды: граница страницы сдвинулась бы, и
    строки с одинаковым `submitted_at` терялись бы между страницами.
    """
    submitted_at = datetime(2026, 10, 24, 3, 9, 12, 345678, tzinfo=UTC)
    request_uuid = uuid_lib.uuid4()

    decoded_at, decoded_uuid = decode_my_requests_cursor(
        encode_my_requests_cursor(submitted_at, request_uuid)
    )

    assert decoded_at == submitted_at
    assert decoded_uuid == request_uuid


def test_encode_normalizes_to_utc() -> None:
    """AC-06: смещение приводится к UTC — сравнение в SQL не зависит от зоны."""
    local = datetime(2026, 10, 24, 12, 0, tzinfo=timezone(timedelta(hours=9)))

    decoded_at, _ = decode_my_requests_cursor(encode_my_requests_cursor(local, uuid_lib.uuid4()))

    assert decoded_at == local
    assert decoded_at.utcoffset() == timedelta(0)


def test_encode_rejects_naive_datetime() -> None:
    """AC-06: naive-метка отвергается на входе, а не молча сериализуется.

    Краснеет, если снять проверку: в курсор попала бы строка без смещения,
    и PostgreSQL применил бы зону сессии — страница «уехала» бы молча.
    """
    with pytest.raises(ValueError):
        encode_my_requests_cursor(datetime(2026, 10, 24, 3, 0), uuid_lib.uuid4())  # noqa: DTZ001


def test_extra_key_is_rejected() -> None:
    """AC-06 (D-2): лишний ключ `id` отвергается, а не игнорируется.

    Краснеет, если заменить проверку набора на «нужные ключи присутствуют»:
    внутренний bigint вернулся бы из `decode` и стал бы публичным адресом.
    """
    raw = _raw(
        {
            "submitted_at": datetime.now(UTC).isoformat(),
            "uuid": str(uuid_lib.uuid4()),
            "id": 42,
        }
    )

    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)


def test_missing_key_is_rejected() -> None:
    """AC-06: без `uuid` tie-breaker нет — курсор невалиден."""
    raw = _raw({"submitted_at": datetime.now(UTC).isoformat()})

    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)


@pytest.mark.parametrize("raw", ["", "!!!", "eyJhIjoxfQ"])
def test_broken_base64_is_invalid_cursor(raw: str) -> None:
    """AC-06: мусор в query-параметре — `InvalidCursorError`, не `500`.

    Краснеет, если пробросить `binascii.Error`/`JSONDecodeError` наружу:
    клиент получил бы `500` на собственной опечатке.
    """
    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)


def test_naive_timestamp_in_payload_is_rejected() -> None:
    """AC-06: naive-метка из ПОДДЕЛАННОГО курсора тоже отвергается."""
    raw = _raw({"submitted_at": "2026-10-24T03:00:00", "uuid": str(uuid_lib.uuid4())})

    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)


def test_deeply_nested_json_is_invalid_cursor() -> None:
    """AC-06 (CR 5а.3, патч P2): глубокая вложенность — `422`, не `500`.

    Краснеет, если убрать ветку `RecursionError`: `json.loads('['*100000)`
    поднимает `RecursionError`, который НЕ наследник `ValueError`, и он
    уходил бы в общий обработчик — клиент получал бы `500` на собственном
    вводе вместо `422 invalid_cursor`.
    """
    raw = base64.urlsafe_b64encode(b"[" * 100_000).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ],
)
def test_out_of_range_timestamp_is_invalid_cursor(timestamp: str) -> None:
    """AC-06 (CR 5а.3, патч P3): крайние метки — `422`, не `500`.

    Краснеет, если убрать `astimezone(UTC)` в `try`: приведение к UTC
    выходит за границы `datetime` (`0001-01-01 +23:59`), а asyncpg отвергает
    такое значение при биндинге в `timestamptz` → `DBAPIError` → `500`.
    """
    raw = _raw({"submitted_at": timestamp, "uuid": str(uuid_lib.uuid4())})

    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)


def test_decode_normalizes_valid_offset_to_utc() -> None:
    """AC-06: корректное смещение приводится к UTC, а не отвергается.

    Защита от переусердствования: `+03:00` — валидный ввод, и он обязан
    дать тот же инстант, иначе клиент с законным курсором получил бы `422`.
    """
    raw = _raw({"submitted_at": "2026-10-24T12:00:00+03:00", "uuid": str(uuid_lib.uuid4())})

    decoded_at, _ = decode_my_requests_cursor(raw)

    assert decoded_at == datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    assert decoded_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("raw", ["abc$def", "YWJj ZGVm", "YWJj!ZGVm"])
def test_out_of_alphabet_chars_are_invalid_cursor(raw: str) -> None:
    """AC-06 (CR 5а.3, патч P5): символы вне алфавита — `422`.

    Краснеет, если убрать `validate=True`: `urlsafe_b64decode` МОЛЧА
    отбрасывает такие символы, и искажённый курсор мог бы декодироваться в
    другой валидный JSON — тихо неверная страница вместо ошибки клиента.
    """
    with pytest.raises(InvalidCursorError):
        decode_my_requests_cursor(raw)
