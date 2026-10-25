"""Кодек курсора очереди жалоб (стори 8.3; AC-04).

Курсор — позиция в отсортированной очереди, а не права доступа: он несёт
`reported_at` и `uuid`, оба уже присутствуют в ответе. Прав он не даёт: гард
`ModeratorUser` срабатывает ДО чтения, а подделанное значение лишь сдвигает
точку чтения внутри данных, которые модератор и так видит.

**Почему `uuid`, а не внутренний `id`.** `reports.id` — `bigint` без публичного
адреса; попав в query-параметр, он стал бы адресом для перебора `/1..N`.
`uuid` уже возвращается в элементе очереди, то есть курсор не раскрывает
нового поля (тот же довод, что в `card_feed_cursor` и `attendance_history_cursor`).

**Класс ошибки ПЕРЕИСПОЛЬЗУЕТСЯ** (`place_cursor.InvalidCursorError`):
HTTP-слой отображает `422 invalid_cursor` по одному типу, и второй класс дал
бы два источника правды для одного кода ответа.

Формат: `base64url` без padding от `{"reported_at": ISO-8601 UTC, "uuid": str}`.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

from app.services.place_cursor import INVALID_CURSOR_CODE, InvalidCursorError

__all__ = [
    "INVALID_CURSOR_CODE",
    "InvalidCursorError",
    "decode_queue_cursor",
    "encode_queue_cursor",
]

#: Проверка НАБОРА, а не «нужные ключи присутствуют»: лишний ключ означал бы,
#: что в публичный конверт попало внутреннее значение (`report_id`), и
#: молчаливое игнорирование скрыло бы утечку навсегда.
_CURSOR_KEYS = frozenset({"reported_at", "uuid"})

#: Верхняя граница длины входа: курсор формируется сервером и занимает ~120
#: символов. Ограничение в КОДЕКЕ, а не в `Query(max_length=...)`: иначе
#: слишком длинный курсор дал бы `422 invalid_request` (общий конверт
#: валидации) вместо `422 invalid_cursor` — второй код для одной ошибки ввода.
_MAX_CURSOR_LENGTH = 512


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON с дубликатом ключа — битый курсор, а не «последний победил»."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidCursorError(INVALID_CURSOR_CODE)
        result[key] = value
    return result


def encode_queue_cursor(reported_at: datetime, report_uuid: uuid_lib.UUID) -> str:
    """AC-04: непрозрачный курсор от последней строки страницы.

    `reported_at` нормализуется в UTC и сериализуется с `Z`: курсор, собранный
    в одном поясе и разобранный в другом, иначе дал бы сдвиг чтения.
    Микросекунды сохраняются — усечение до секунд отсекло бы строки с тем же
    `reported_at`, а сортировка идёт именно по нему.
    """
    if reported_at.tzinfo is None:
        # Наивное время внутри курсора недопустимо по той же причине, что и в
        # `decode`: сравнение с `timestamptz` стало бы зависимым от зоны.
        raise ValueError("reported_at должен быть aware (UTC), получено naive-значение")
    payload = {
        "reported_at": reported_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "uuid": str(report_uuid),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_queue_cursor(cursor: str) -> tuple[datetime, uuid_lib.UUID]:
    """AC-04: разбор курсора. Битое значение — `InvalidCursorError` (`422`).

    Курсор пришёл от клиента, поэтому ЛЮБАЯ ошибка разбора — ошибка ввода.
    `500` здесь был бы дефектом: он обвинил бы сервер в значении, которое
    сервер не формировал.

    `base64` дополняется padding-ом, а не отвергается: клиент мог пройти через
    посредника, который нормализовал `=`.
    """
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidCursorError(INVALID_CURSOR_CODE)
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        # `validate=True` через `b64decode(altchars=...)`: `urlsafe_b64decode`
        # не принимает `validate` и молча выбрасывает посторонние символы,
        # из-за чего `valid_cursor + "!!!"` проходил бы разбор.
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    try:
        payload: Any = json.loads(decoded.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
        # Глубоко вложенный JSON даёт `RecursionError`, а он не `ValueError`:
        # без этой ветки пользовательский query-параметр ронял бы запрос в `500`.
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    if not isinstance(payload, dict) or set(payload) != _CURSOR_KEYS:
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    reported_raw = payload["reported_at"]
    uuid_raw = payload["uuid"]
    if not isinstance(reported_raw, str) or not isinstance(uuid_raw, str):
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    try:
        reported_at = datetime.fromisoformat(reported_raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error
    # aware-дата обязательна: `fromisoformat` принимает и naive-строку, а
    # asyncpg отвергает naive в `timestamptz`-сравнении → `500`.
    if reported_at.tzinfo is None:
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    try:
        report_uuid = uuid_lib.UUID(uuid_raw)
    except (ValueError, AttributeError, TypeError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    return reported_at, report_uuid
