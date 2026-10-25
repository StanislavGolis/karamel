"""Кодек курсора «моих заявок» (стори 5а.3, Task 4; AC-06).

Курсор — позиция в отсортированном списке, а не права доступа (D-2 стори 3.3):
он несёт только `submitted_at` и `uuid` заявки, оба уже публичны (`uuid`
адресует заявку в `DELETE`, `submitted_at` отдаётся в элементе списка).
Подпись не нужна: подделанное значение двигает точку старта по данным,
которые заявитель и так видит, а предикаты `applicant_id`, статуса и
сообщества накладываются отдельно и курсором не обходятся.

**Почему ключ `(submitted_at, uuid)`, а не `(submitted_at, id)`** (D-5a.3-6):
`id` — внутренний bigint (D-2). Tie-breaker попадает в курсор, курсор —
в query-параметр, то есть `id` стал бы публичным адресом и разрешил бы
перебор `/1..N` в обход лимитов (тот же довод, что в `place_cursor`
и `card_feed_cursor`). `uuid` даёт тот же детерминированный порядок.

**Почему третья копия формы, а не общий `core/keyset.py`** (долг X-12):
общий модуль с параметром «какие две колонки» уже дал бы две формы,
которые никто не проверяет тестами; третья не станет лучше. Класс ошибки
ПЕРЕИСПОЛЬЗУЕТСЯ (`place_cursor.InvalidCursorError`), а не копируется:
HTTP-слой отображает `422 invalid_cursor` по одному типу, и вторая копия
потребовала бы второй ветки маппинга.

Формат: `base64url` без padding от `{"submitted_at": ISO-8601 UTC, "uuid": str}`.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid as uuid_lib
from datetime import UTC, datetime

from app.services.place_cursor import INVALID_CURSOR_CODE, InvalidCursorError

__all__ = [
    "INVALID_CURSOR_CODE",
    "InvalidCursorError",
    "decode_my_requests_cursor",
    "encode_my_requests_cursor",
]

# AC-06: ровно два ключа. Проверка НАБОРА, а не «нужные ключи присутствуют»:
# лишний ключ (например `id`) означал бы утечку внутренних данных в публичный
# конверт, и молчаливое игнорирование скрыло бы её навсегда.
_CURSOR_KEYS = frozenset({"submitted_at", "uuid"})


def encode_my_requests_cursor(submitted_at: datetime, request_uuid: uuid_lib.UUID) -> str:
    """AC-06: непрозрачный курсор от последней строки страницы.

    `submitted_at` нормализуется в UTC и сериализуется с `Z`
    (project-context §4). naive-значение отвергается на входе: в БД колонка
    `timestamptz`, и naive дал бы строку без смещения, которую `decode`
    отверг бы уже как ошибку клиента — лучше падать там, где виден источник.

    Микросекунды сохраняются: PostgreSQL хранит их, а усечение сдвигало бы
    границу страницы — часть строк между страницами терялась бы там, где
    `submitted_at` совпадает у нескольких заявок (обычный случай: одна
    пачка подач в одну секунду).
    """
    if submitted_at.tzinfo is None:
        raise ValueError("submitted_at должен быть timezone-aware")
    payload = json.dumps(
        {
            "submitted_at": submitted_at.astimezone(UTC).isoformat(),
            "uuid": str(request_uuid),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_my_requests_cursor(raw: str) -> tuple[datetime, uuid_lib.UUID]:
    """AC-06: разбор курсора; любое отклонение — `422 invalid_cursor`.

    Курсор приходит из query-параметра, то есть это вход пользователя:
    битое значение обязано давать `InvalidCursorError`, а не `500` из
    драйвера (`ValueError`/`KeyError` наружу не выпускаются).

    `sort_keys=True` при кодировании и проверка НАБОРА ключей при разборе —
    пара: без проверки набора лишний ключ проходил бы молча.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        # `validate=True`: без него `urlsafe_b64decode` МОЛЧА отбрасывает
        # символы вне алфавита, и искажённый курсор декодировался бы в другой
        # валидный JSON → тихо неверная страница вместо `422`.
        # `b64decode(altchars=b"-_", validate=True)`, а не `urlsafe_b64decode`:
        # только первый умеет `validate`, и без него символы вне алфавита
        # отбрасываются МОЛЧА.
        payload = json.loads(base64.b64decode(padded.encode(), altchars=b"-_", validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise InvalidCursorError() from error
    except RecursionError as error:
        # Глубоко вложенный JSON (`'[' * 100000`) — пользовательский ввод, и
        # `RecursionError` не наследник `ValueError`: без этой ветки он уходил
        # бы в общий обработчик → `500` вместо `422`.
        raise InvalidCursorError() from error
    if not isinstance(payload, dict) or set(payload) != _CURSOR_KEYS:
        raise InvalidCursorError()
    try:
        submitted_at = datetime.fromisoformat(str(payload["submitted_at"]))
        request_uuid = uuid_lib.UUID(str(payload["uuid"]))
    except (ValueError, TypeError) as error:
        raise InvalidCursorError() from error
    if submitted_at.tzinfo is None:
        # naive-метка из подделанного курсора: сравнивать её в SQL с
        # `timestamptz` бессмысленно, и PostgreSQL применил бы временную зону
        # сессии — страница «уехала» бы молча.
        raise InvalidCursorError()
    try:
        # Приведение к UTC может выйти за границы `datetime` (`0001-01-01
        # +23:59` → OverflowError), а asyncpg отвергает такие значения при
        # биндинге в `timestamptz` → `DBAPIError` → `500` на ввод клиента.
        # Валидируем границы здесь, где ещё можно ответить `422`.
        return submitted_at.astimezone(UTC), request_uuid
    except (OverflowError, OSError) as error:
        raise InvalidCursorError() from error
