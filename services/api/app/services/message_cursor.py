"""Кодек курсора истории чата (стори 6.4; AC-02).

Курсор — позиция в отсортированной истории, а не права доступа: он несёт
`created_at` и `uuid` сообщения, оба уже публичны (попадают в ответ). Прав
на чат он не даёт: гард участия проверяется ДО истории и курсором не
обходится — подделанное значение лишь сдвигает точку чтения по данным,
которые клиент и так видит.

**Почему `(created_at, uuid)`, а не `(created_at, id)`** (D-2). Внутренний
`bigint` в query-параметре стал бы публичным адресом и разрешил бы перебор
`/1..N` в обход состава компании. `uuid` даёт тот же детерминированный
порядок (он уникален), а `id` остаётся внутренним ключом.

**Почему отдельный модуль, а не общий `core/keyset.py`** (X-12): общий модуль
с параметром «какие две колонки» дал бы третью форму, которую никто не
проверяет тестами. Класс ошибки ПЕРЕИСПОЛЬЗУЕТСЯ (`place_cursor`), а не
копируется: HTTP-слой отображает `422 invalid_cursor` по одному типу.

Формат: `base64url` без padding от `{"created_at": ISO-8601 UTC, "uuid": str}`.
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
    "decode_message_cursor",
    "encode_message_cursor",
]


def encode_message_cursor(created_at: datetime, message_uuid: uuid_lib.UUID) -> str:
    """Собирает непрозрачный курсор из позиции последнего сообщения страницы.

    Время нормализуется в UTC и сериализуется с `Z`: курсор, собранный в
    одном часовом поясе и разобранный в другом, иначе дал бы сдвиг чтения.
    """
    payload = {
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "uuid": str(message_uuid),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_message_cursor(cursor: str) -> tuple[datetime, uuid_lib.UUID]:
    """Разбирает курсор. Битое значение — `InvalidCursorError` (`422`), не `500`.

    Курсор пришёл от клиента, поэтому любая ошибка разбора — ошибка ВВОДА.
    `500` здесь был бы дефектом: он обвинил бы сервер в значении, которое
    сервер не формировал.
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload: Any = json.loads(raw.decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        message_uuid = uuid_lib.UUID(str(payload["uuid"]))
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError() from exc
    if created_at.tzinfo is None:
        # Наивное время в курсоре — тоже битое значение: сравнение с
        # `timestamptz` в PostgreSQL дало бы неопределённый результат.
        raise InvalidCursorError()
    return created_at.astimezone(UTC), message_uuid
