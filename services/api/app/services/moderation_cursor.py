"""Кодек курсора очереди модерации карточек (стори 4.3, Task 4; AC-03, AC-08).

Курсор — позиция в отсортированной очереди, а не права доступа (D-2 стори 3.3):
он несёт только `created_at` и `uuid` карточки, оба уже публичны (попадают
в ответ очереди). Подпись не нужна: подделанное значение двигает точку старта
по данным, которые модератор и так видит, а фильтр `verification_status`
накладывается отдельно и курсором не обходится.

**Почему отдельный модуль, а не `place_cursor` и не общий `core/keyset.py`**
(D-4.3-8, X-12):

- ключ сортировки здесь `(created_at, uuid)`, у мест — `(created_at, uuid)`
  ТОЖЕ, но у ленты карточек 4.5 будет `(starts_at, id)`. Общий модуль
  с параметром «какие две колонки» дал бы третью форму, которую никто
  не проверяет тестами (тот же вывод, что в Task 2.4 стори 3.3);
- класс ошибки ПЕРЕИСПОЛЬЗУЕТСЯ (`place_cursor.InvalidCursorError`), а не
  копируется: HTTP-слой отображает `422 invalid_cursor` по одному типу,
  и вторая копия класса потребовала бы второй ветки маппинга.

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
    "decode_moderation_cursor",
    "encode_moderation_cursor",
]

# AC-08: ровно два ключа. Проверка набора, а не «нужные ключи присутствуют»:
# лишний ключ (например `id`) означал бы утечку внутренних данных в публичный
# конверт, и молчаливое игнорирование скрыло бы её навсегда.
_CURSOR_KEYS = frozenset({"created_at", "uuid"})


def encode_moderation_cursor(created_at: datetime, card_uuid: uuid_lib.UUID) -> str:
    """AC-03: непрозрачный курсор от последней строки страницы.

    `created_at` нормализуется в UTC и сериализуется с `Z` (project-context §4).
    naive-значение отвергается на входе: в БД колонка `timestamptz`, и naive
    дал бы строку без смещения, которую `decode` отверг бы уже как ошибку
    клиента — лучше падать там, где виден источник.

    Микросекунды сохраняются: PostgreSQL хранит их, а усечение сдвигало бы
    границу страницы (часть строк между страницами терялась бы).
    """
    if created_at.tzinfo is None:
        raise ValueError("created_at должен быть aware (UTC), получено naive-значение")
    moment = created_at.astimezone(UTC)
    payload = {
        "created_at": moment.isoformat().replace("+00:00", "Z"),
        "uuid": str(card_uuid),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # `padding` снимается: `+`/`/` из standard-base64 в query-параметре требуют
    # percent-кодирования, а `=` в конце легко теряется прокси и клиентом.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_moderation_cursor(raw: str) -> tuple[datetime, uuid_lib.UUID]:
    """AC-08: строгий разбор курсора в пару `(created_at, uuid)`.

    Проверки по шагам (каждая — отдельная причина `422 invalid_cursor`):
    base64url с `validate=True`; UTF-8 + JSON-объект; ровно два ключа;
    ISO-8601 с смещением; валидный UUID. Мягких веток нет: тихо
    проигнорированный битый курсор вернул бы первую страницу, и бесконечное
    листание выглядело бы как «страницы кончились».
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        # `validate=True`: без него `b64decode` молча ВЫБРАСЫВАЕТ символы вне
        # алфавита, и `valid_cursor + "!!!"` проходил разбор (CR 3.3, патч 3).
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    try:
        payload: Any = json.loads(decoded.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
        # `RecursionError` не является `ValueError`: глубоко вложенный JSON
        # (курсор из `'['*20000`) без этой ветки ронял запрос в `500`.
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    if not isinstance(payload, dict):
        raise InvalidCursorError(INVALID_CURSOR_CODE)
    if set(payload) != _CURSOR_KEYS:
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    created_raw = payload["created_at"]
    uuid_raw = payload["uuid"]
    if not isinstance(created_raw, str) or not isinstance(uuid_raw, str):
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    try:
        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error
    # aware-дата обязательна: `fromisoformat` принимает и naive-строку, и без
    # этой проверки значение ушло бы в `timestamptz`-сравнение, где asyncpg
    # отвергает naive → `500`.
    if created_at.tzinfo is None:
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    try:
        card_uuid = uuid_lib.UUID(uuid_raw)
    except (ValueError, AttributeError, TypeError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    # `astimezone(UTC)` на краю диапазона выходит за границы `datetime`
    # и бросает `OverflowError` — не `ValueError`.
    try:
        return created_at.astimezone(UTC), card_uuid
    except (OverflowError, OSError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error
