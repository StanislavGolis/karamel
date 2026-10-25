"""Кодек курсора ленты карточек (стори 4.5, Task 8.1; AC-08).

§18.1: каждый тест краснеет от снятия конкретной проверки в
`card_feed_cursor`. §18.2: проверяется наблюдаемое свойство (значение после
round-trip, класс исключения), а не константа в самом тесте.
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.card_feed_cursor import (
    InvalidCursorError,
    decode_feed_cursor,
    encode_feed_cursor,
)


def _encode_raw(payload: object) -> str:
    """Собирает курсор из произвольного JSON — для проверки строгости разбора."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_round_trip_preserves_microseconds() -> None:
    """AC-08: микросекунды сохраняются — иначе граница страницы сдвигается.

    Краснеет, если в `encode_feed_cursor` добавить `.replace(microsecond=0)`:
    две карточки с одинаковым `starts_at` до микросекунды и разными `uuid`
    перестали бы различаться по позиции, и keyset потерял бы строки.
    """
    moment = datetime(2026, 11, 1, 12, 30, 45, 123456, tzinfo=UTC)
    card_uuid = uuid_lib.uuid4()

    starts_at, decoded_uuid = decode_feed_cursor(encode_feed_cursor(moment, card_uuid))

    assert starts_at == moment
    assert starts_at.microsecond == 123456
    assert decoded_uuid == card_uuid


def test_encode_normalizes_to_utc_and_uses_z() -> None:
    """AC-08, project-context §4: даты наружу — ISO 8601 UTC с `Z`.

    Краснеет, если убрать `.astimezone(UTC)`: курсор с `+07:00` содержал бы
    смещение, и один и тот же момент давал бы два разных курсора — сервер
    не смог бы сравнивать позиции побайтово в логах и кэшах.
    """
    tomsk = timezone(timedelta(hours=7))
    moment = datetime(2026, 11, 1, 19, 0, 0, tzinfo=tomsk)

    cursor = encode_feed_cursor(moment, uuid_lib.uuid4())
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))

    assert payload["starts_at"].endswith("Z")
    assert "+07:00" not in payload["starts_at"]
    assert decode_feed_cursor(cursor)[0] == moment


def test_encode_rejects_naive_datetime() -> None:
    """AC-08: naive-значение отвергается на входе, а не на разборе.

    Краснеет, если снять проверку `tzinfo is None`: naive ушёл бы в курсор
    без смещения, и `decode` отверг бы его как ошибку КЛИЕНТА — сервер
    скрыл бы собственный дефект за `422`.
    """
    with pytest.raises(ValueError, match="aware"):
        encode_feed_cursor(datetime(2026, 11, 1, 12, 0, 0), uuid_lib.uuid4())


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "!!!",
        "not-base64-at-all",
        # валидный курсор с мусорным хвостом: `validate=True` обязан отбить
    ],
)
def test_decode_rejects_broken_base64(raw: str) -> None:
    """AC-08: неверный алфавит и длина — `422 invalid_cursor`.

    Краснеет, если убрать `validate=True`: `b64decode` молча выбрасывает
    символы вне алфавита, и `valid_cursor + "!!!"` проходил бы разбор
    (дефект CR 3.3, патч 3 — тот же кодек-класс).
    """
    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(raw)


def test_decode_rejects_valid_cursor_with_garbage_tail() -> None:
    """AC-08: хвост из символов вне алфавита — ошибка, а не тихий разбор."""
    valid = encode_feed_cursor(datetime.now(UTC), uuid_lib.uuid4())

    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(valid + "!!!")


@pytest.mark.parametrize(
    "payload",
    [
        ["a", "b"],
        "string",
        42,
        None,
        {"starts_at": "2026-11-01T12:00:00Z"},
        {"uuid": str(uuid_lib.uuid4())},
        {"starts_at": "2026-11-01T12:00:00Z", "uuid": str(uuid_lib.uuid4()), "id": 1},
        {"created_at": "2026-11-01T12:00:00Z", "uuid": str(uuid_lib.uuid4())},
    ],
)
def test_decode_rejects_wrong_shape(payload: object) -> None:
    """AC-08: не объект, не тот набор ключей — ошибка.

    Краснеет от снятия проверки `set(payload) != _CURSOR_KEYS`: лишний ключ
    (`id`) означал бы утечку внутреннего bigint в публичный конверт, а старое
    имя `created_at` — курсор другой ленты, подсунутый в эту.
    """
    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(_encode_raw(payload))


@pytest.mark.parametrize(
    "bad_value",
    [
        {"starts_at": 123, "uuid": str(uuid_lib.uuid4())},
        {"starts_at": None, "uuid": str(uuid_lib.uuid4())},
        {"starts_at": "2026-11-01T12:00:00Z", "uuid": 42},
        {"starts_at": "2026-11-01T12:00:00Z", "uuid": None},
    ],
)
def test_decode_rejects_non_string_values(bad_value: dict[str, object]) -> None:
    """AC-08: значения обязаны быть строками до разбора.

    Краснеет от снятия `isinstance(..., str)`: `json.loads` вернул бы `int`,
    `.replace("Z", ...)` упал бы `AttributeError` → `500` на вводе клиента.
    """
    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(_encode_raw(bad_value))


@pytest.mark.parametrize(
    "bad_date",
    [
        "not-a-date",
        "2026-13-01T12:00:00Z",
        "2026-11-01",
        # naive: `fromisoformat` принимает, а asyncpg отвергнет в сравнении
        "2026-11-01T12:00:00",
    ],
)
def test_decode_rejects_bad_or_naive_datetime(bad_date: str) -> None:
    """AC-08: битая и naive-дата — ошибка клиента.

    Краснеет от снятия проверки `starts_at.tzinfo is None`: naive-значение
    ушло бы в `timestamptz`-сравнение, где asyncpg бросает `DataError` → `500`.
    """
    raw = _encode_raw({"starts_at": bad_date, "uuid": str(uuid_lib.uuid4())})

    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(raw)


def test_decode_rejects_invalid_uuid() -> None:
    """AC-08: невалидный UUID — `422 invalid_cursor`."""
    raw = _encode_raw({"starts_at": "2026-11-01T12:00:00Z", "uuid": "not-a-uuid"})

    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(raw)


def test_decode_rejects_overflowing_timezone() -> None:
    """AC-08: смещение на краю диапазона — `OverflowError`, не `500`.

    `9999-12-31T23:59:59-23:59` при переводе в UTC выходит за границы
    `datetime`; `OverflowError` не является `ValueError`, и без отдельной
    ветки пользовательский параметр ронял бы запрос (дефект CR 3.3, патч 1).
    """
    raw = _encode_raw({"starts_at": "9999-12-31T23:59:59-23:59", "uuid": str(uuid_lib.uuid4())})

    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(raw)


def test_decode_rejects_deeply_nested_json() -> None:
    """AC-08: глубоко вложенный JSON — `RecursionError`, не `500`.

    `RecursionError` не наследник `ValueError`; без ветки курсор из
    `'[' * 20000` ронял запрос (CR 3.3, патч 2).
    """
    raw = base64.urlsafe_b64encode(b"[" * 20000).decode("ascii").rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_feed_cursor(raw)


def test_invalid_cursor_error_is_same_class_as_places() -> None:
    """AC-08, D-4.5-8: класс ошибки ПЕРЕИСПОЛЬЗОВАН, а не скопирован.

    Краснеет, если объявить `class InvalidCursorError(Exception)` в
    `card_feed_cursor`: `_domain_error` отображает один тип, и вторая копия
    дала бы `500` на битом курсоре ленты, пока места отвечают `422`.
    """
    from app.services.place_cursor import InvalidCursorError as PlacesError

    assert InvalidCursorError is PlacesError
