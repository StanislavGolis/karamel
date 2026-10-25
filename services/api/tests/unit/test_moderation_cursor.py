"""Кодек курсора очереди модерации (стори 4.3, Task 4; AC-03, AC-08).

§18.1: каждый тест краснеет от снятия конкретной проверки — в docstring
указано, какая правка делает его красным.
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.moderation_cursor import (
    INVALID_CURSOR_CODE,
    InvalidCursorError,
    decode_moderation_cursor,
    encode_moderation_cursor,
)

CARD_UUID = uuid_lib.UUID("44444444-4444-4444-8444-444444444444")
MOMENT = datetime(2026, 10, 23, 22, 0, 0, 123456, tzinfo=UTC)


def test_round_trip() -> None:
    """AC-08: кодирование и разбор возвращают исходную пару.

    Краснеет, если поменять имя ключа или формат даты: курсор перестал бы
    разбираться собственным декодером.
    """
    cursor = encode_moderation_cursor(MOMENT, CARD_UUID)

    decoded_at, decoded_uuid = decode_moderation_cursor(cursor)

    assert decoded_at == MOMENT
    assert decoded_uuid == CARD_UUID


def test_microseconds_preserved() -> None:
    """AC-08: микросекунды не теряются.

    Краснеет, если усечь метку до секунд: граница страницы сдвинулась бы,
    и строки с тем же `created_at` терялись бы между страницами.
    """
    cursor = encode_moderation_cursor(MOMENT, CARD_UUID)

    decoded_at, _ = decode_moderation_cursor(cursor)

    assert decoded_at.microsecond == MOMENT.microsecond


def test_cursor_is_url_safe_without_padding() -> None:
    """AC-08: курсор не содержит `+`, `/`, `=`.

    Краснеет, если использовать standard-base64: `+`/`/` в query-параметре
    требуют percent-кодирования, а `=` теряется прокси.
    """
    cursor = encode_moderation_cursor(MOMENT, CARD_UUID)

    assert "+" not in cursor
    assert "/" not in cursor
    assert "=" not in cursor


def test_naive_datetime_rejected_on_encode() -> None:
    """AC-08: naive-дата отвергается на входе.

    Краснеет, если снять проверку `tzinfo is None`: в БД `timestamptz`,
    и naive-строка вернулась бы из декодера как ошибка клиента, а не как
    ошибка вызывающего кода.
    """
    with pytest.raises(ValueError):
        encode_moderation_cursor(datetime(2026, 10, 23, 22, 0), CARD_UUID)


def test_timezone_normalized_to_utc() -> None:
    """AC-08: курсор с другим смещением приводится к UTC.

    Краснеет, если не вызывать `astimezone(UTC)`: одинаковый момент в разных
    зонах дал бы разные строки, и keyset-сравнение разошлось бы с БД.
    """
    local = MOMENT.astimezone(timezone(timedelta(hours=9)))

    cursor = encode_moderation_cursor(local, CARD_UUID)
    decoded_at, _ = decode_moderation_cursor(cursor)

    assert decoded_at == MOMENT
    assert decoded_at.tzinfo == UTC


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-base64!!!",
        "aGVsbG8",  # "hello" — JSON, но не объект
        base64.urlsafe_b64encode(b'{"created_at": "2026-10-23T22:00:00Z"}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"created_at": "2026-10-23T22:00:00Z", "uuid": "x"}')
        .decode()
        .rstrip("="),
    ],
)
def test_invalid_cursor_forms(raw: str) -> None:
    """AC-08: любая битая форма — `InvalidCursorError` (→ `422`).

    Краснеет, если добавить «мягкую» ветку: тихо проигнорированный курсор
    вернул бы первую страницу, и листание выглядело бы как «кончилось».
    """
    with pytest.raises(InvalidCursorError) as error:
        decode_moderation_cursor(raw)

    assert error.value.code == INVALID_CURSOR_CODE


def test_extra_key_rejected() -> None:
    """AC-08: лишний ключ в курсоре — ошибка, а не игнор.

    Краснеет, если проверять «нужные ключи есть» вместо «набор равен»: `id`,
    положенный в публичный конверт, остался бы незамеченным навсегда.
    """
    payload = {
        "created_at": "2026-10-23T22:00:00Z",
        "uuid": str(CARD_UUID),
        "id": 42,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_moderation_cursor(raw)


def test_naive_datetime_in_payload_rejected() -> None:
    """AC-08: naive-дата в payload — `422`, а не `500` из драйвера.

    Краснеет, если снять проверку `tzinfo is None` в декодере: значение ушло бы
    в `timestamptz`-сравнение, где asyncpg отвергает naive → `DataError` → `500`.
    """
    payload = {"created_at": "2026-10-23T22:00:00", "uuid": str(CARD_UUID)}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_moderation_cursor(raw)


def test_padding_tail_garbage_rejected() -> None:
    """AC-08: мусорный хвост после валидного курсора — ошибка.

    Краснеет, если убрать `validate=True`: `b64decode` молча выбрасывает
    символы вне алфавита, и `valid + "!!!"` прошёл бы разбор (CR 3.3, патч 3).
    """
    cursor = encode_moderation_cursor(MOMENT, CARD_UUID)

    with pytest.raises(InvalidCursorError):
        decode_moderation_cursor(cursor + "!!!")
