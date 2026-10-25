"""Кодек курсора выдачи мест (Task 7.1; AC-04, AC-06).

§18.1: каждый тест краснеет от конкретной правки. Ценность кодека — не
«encode → decode вернул то же», а строгость разбора: мягкие ветки (`except
Exception → None`, `datetime.fromisoformat` без проверки tzinfo, `dict.get`
вместо проверки набора ключей) не видны на happy path, но превращают `422
invalid_cursor` в `500` из asyncpg или в тихую первую страницу.

§18.2: нет ассертов вида `assert isinstance(error, Exception)`. Каждый тест
вызывает реальный сбой (битый base64, naive-дата, лишний ключ) и проверяет
класс ошибки.
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.place_cursor import (
    INVALID_CURSOR_CODE,
    InvalidCursorError,
    decode_place_cursor,
    encode_place_cursor,
)


def _encode_payload(payload: dict[str, object]) -> str:
    """Собирает курсор из ПРОИЗВОЛЬНОГО payload.

    Нужно, чтобы проверять разбор на формах, которые `encode_place_cursor`
    никогда не создаёт (лишний ключ, naive-дата, не-UUID). Тест не должен
    проходить через код, который эти формы отвергает.
    """
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "created_at": "2026-10-23T11:20:31.482913Z",
        "uuid": str(uuid_lib.UUID("0f3c1e6a-2b7d-4c8e-9a10-5b6d7e8f9012")),
    }
    payload.update(overrides)
    return payload


def test_round_trip_preserves_microseconds_and_uuid() -> None:
    """AC-04: микросекунды и `uuid` переживают `encode`/`decode` без потерь.

    Краснеет, если сериализовать `created_at` через `strftime` без `%f`
    или обрезать его до секунд: усечённая метка сдвигает границу страницы,
    и строки с тем же `created_at` (одна транзакция) теряются между страницами.
    """
    moment = datetime(2026, 10, 23, 11, 20, 31, 482913, tzinfo=UTC)
    place_uuid = uuid_lib.UUID("0f3c1e6a-2b7d-4c8e-9a10-5b6d7e8f9012")

    raw = encode_place_cursor(moment, place_uuid)
    decoded_moment, decoded_uuid = decode_place_cursor(raw)

    assert decoded_moment == moment
    assert decoded_moment.microsecond == 482913
    assert decoded_uuid == place_uuid


def test_encode_normalizes_foreign_timezone_to_utc() -> None:
    """AC-04, project-context §4: даты наружу — UTC.

    Краснеет, если убрать `astimezone(UTC)`: курсор нёс бы `+03:00`,
    а `decode` возвращал бы смещение, и row-value сравнение с `timestamptz`
    считалось бы по другому абсолютному времени только в тестах на другом
    часовом поясе.
    """
    kyiv = timezone(timedelta(hours=3))
    moment = datetime(2026, 10, 23, 14, 20, 31, tzinfo=kyiv)

    raw = encode_place_cursor(moment, uuid_lib.uuid4())
    decoded_moment, _ = decode_place_cursor(raw)

    assert decoded_moment.tzinfo is not None
    assert decoded_moment.utcoffset() == timedelta(0)
    assert decoded_moment == moment.astimezone(UTC)


def test_encode_rejects_naive_datetime() -> None:
    """AC-04: naive-дата отвергается на входе кодека.

    Краснеет, если убрать проверку `tzinfo is None`: naive-значение дало бы
    курсор без смещения, который `decode` отверг бы как ошибку КЛИЕНТА —
    то есть дефект собственного кода выглядел бы как битый запрос участника.
    """
    with pytest.raises(ValueError):
        encode_place_cursor(datetime(2026, 10, 23, 11, 20, 31), uuid_lib.uuid4())


def test_decoded_payload_has_exactly_two_keys() -> None:
    """AC-06: в курсоре нет внутреннего `id` — ровно два публичных ключа.

    Краснеет, если в payload добавить `id` (или любое третье поле):
    `base64` — кодирование, не шифрование, и добавленный `bigint` вернулся бы
    из `decode` открытым, став публичным адресом места (D-2 стори 3.1).
    """
    raw = encode_place_cursor(datetime(2026, 10, 23, tzinfo=UTC), uuid_lib.uuid4())
    padded = raw + "=" * (-len(raw) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))

    assert set(payload) == {"created_at", "uuid"}
    assert "id" not in payload


@pytest.mark.parametrize(
    "raw",
    [
        "zzz",  # не base64url
        "",  # пустая строка
        "!!!",  # недопустимый алфавит
    ],
)
def test_broken_base64_is_invalid_cursor(raw: str) -> None:
    """AC-04, AC-07: мусорный курсор — `invalid_cursor`, а не `500`.

    Краснеет, если обернуть разбор в `except Exception: return None`:
    тогда битый курсор молча вернул бы первую страницу, и клиент считал бы,
    что страницы кончились.
    """
    with pytest.raises(InvalidCursorError) as exc_info:
        decode_place_cursor(raw)

    assert exc_info.value.code == INVALID_CURSOR_CODE


def test_base64_of_non_json_is_invalid_cursor() -> None:
    """AC-04: base64 валиден, а внутри не JSON — та же доменная ошибка.

    Краснеет, если ловить только `binascii.Error`: `json.JSONDecodeError`
    не его подкласс и улетел бы наверх как `500`.
    """
    raw = base64.urlsafe_b64encode(b"not-a-json").decode("ascii").rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_missing_key_is_invalid_cursor() -> None:
    """AC-04: нет ключа `uuid` — курсор неполон.

    Краснеет, если читать поля через `payload.get(...)` и подставлять `None`:
    `Place.uuid == None` вернул бы пустую выдачу вместо `422`.
    """
    raw = _encode_payload({"created_at": "2026-10-23T11:20:31Z"})

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_extra_key_is_invalid_cursor() -> None:
    """AC-06: лишний ключ (например внутренний `id`) отвергается.

    Краснеет, если проверять только наличие нужных ключей (`issuperset`):
    курсор с `id` прошёл бы разбор, и внутренний `bigint` вернулся бы
    из публичного конверта.
    """
    raw = _encode_payload(_valid_payload(id=42))

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_naive_datetime_is_invalid_cursor() -> None:
    """AC-04: naive `created_at` в курсоре — `422`, а не `500` из asyncpg.

    Краснеет, если убрать проверку `tzinfo is None` после `fromisoformat`:
    `fromisoformat` принимает `2026-10-23T11:20:31`, и это значение ушло бы
    в сравнение с `timestamptz` (asyncpg отвергает naive → `DataError` → `500`).
    """
    raw = _encode_payload(_valid_payload(created_at="2026-10-23T11:20:31"))

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_not_iso_datetime_is_invalid_cursor() -> None:
    """AC-04: не-ISO строка в `created_at` — та же ошибка, не `500`.

    Краснеет, если не ловить `ValueError` от `fromisoformat`: он улетел бы
    наверх и превратился в `500` на пользовательском вводе.
    """
    raw = _encode_payload(_valid_payload(created_at="23.10.2026 11:20"))

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_non_uuid_value_is_invalid_cursor() -> None:
    """AC-04: `uuid` не UUID — `422`, а не падение драйвера.

    Краснеет, если не ловить `ValueError` от `uuid.UUID(...)`: строка
    `1..N` (перебор внутренних id) дала бы `500` вместо отказа.
    """
    raw = _encode_payload(_valid_payload(uuid="1"))

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_non_object_json_is_invalid_cursor() -> None:
    """AC-04: JSON-массив вместо объекта — та же ошибка.

    Краснеет, если убрать проверку `isinstance(payload, dict)`: индексация
    списка строкой дала бы `TypeError` → `500`.
    """
    raw = base64.urlsafe_b64encode(b'["2026-10-23T11:20:31Z", "x"]').decode("ascii").rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


def test_non_string_fields_are_invalid_cursor() -> None:
    """AC-04: `created_at` числом — `422`, не `TypeError`.

    Краснеет, если убрать проверку `isinstance(..., str)`: `float` ушёл бы
    в `fromisoformat` и поднял `TypeError`, который не ловится как `ValueError`.
    """
    raw = _encode_payload(_valid_payload(created_at=1234567890, uuid=42))

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


# --- CR 3.3, патчи 1–3: границы строгого разбора, найденные ревью -----------


@pytest.mark.parametrize(
    "created_at",
    [
        # Верхняя граница с отрицательным смещением и нижняя с положительным:
        # `astimezone(UTC)` выходит за границы `datetime`.
        "9999-12-31T23:59:59-23:59",
        "0001-01-01T00:00:00+23:59",
    ],
)
def test_datetime_overflowing_utc_range_is_invalid_cursor(created_at: str) -> None:
    """AC-04, AC-07 (CR 3.3, патч 1): дата на краю диапазона — `422`, не `500`.

    §18.1: краснеет, если убрать `except (OverflowError, OSError)` вокруг
    `astimezone(UTC)`. `datetime.fromisoformat` принимает
    `9999-12-31T23:59:59-23:59`, но приведение к UTC выходит за пределы
    `datetime` и бросает `OverflowError` — он не `ValueError`, не ловился
    и превращался в `500 internal_error` на пользовательском query-параметре
    (доказано исполнением: `LEAK -> OverflowError date value out of range`).
    """
    raw = _encode_payload(_valid_payload(created_at=created_at))

    with pytest.raises(InvalidCursorError) as exc_info:
        decode_place_cursor(raw)

    assert exc_info.value.code == INVALID_CURSOR_CODE


def test_deeply_nested_json_is_invalid_cursor() -> None:
    """AC-04, AC-07 (CR 3.3, патч 2): вложенный JSON — `422`, не `500`.

    §18.1: краснеет, если убрать `RecursionError` из `except` вокруг
    `json.loads`. Курсор из `'['*20000` (26 667 символов) валит разбор
    в `RecursionError` — он не `JSONDecodeError` и не `UnicodeDecodeError`,
    и запрос падал `500` без конверта `{code}` (доказано исполнением:
    `Recursion LEAK -> RecursionError`).
    """
    raw = base64.urlsafe_b64encode(b"[" * 20000).decode("ascii").rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(raw)


@pytest.mark.parametrize("suffix", ["!!!", "@@@@", "\x00", "$"])
def test_valid_cursor_with_garbage_suffix_is_invalid_cursor(suffix: str) -> None:
    """AC-04 (CR 3.3, патч 3): мусор в хвосте валидного курсора — `422`.

    §18.1: краснеет, если убрать `validate=True` из `b64decode`.

    Прежний параметризованный тест `test_broken_base64_is_invalid_cursor`
    краснел по ЧУЖОЙ причине: `"!!!"` целиком декодируется в пустые байты
    и падает на `json.loads`, то есть не проверял алфавит вообще. Здесь
    мусор дописан к ВАЛИДНОМУ курсору: без `validate=True` посторонние
    символы молча выбрасываются, и подделанный курсор принимается
    (доказано исполнением: `garbage accepted: '!!!'`).
    """
    valid = encode_place_cursor(
        datetime(2026, 10, 23, 11, 20, 31, 482913, tzinfo=UTC), uuid_lib.uuid4()
    )

    with pytest.raises(InvalidCursorError):
        decode_place_cursor(valid + suffix)
