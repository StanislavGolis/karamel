"""Кодек курсора списка заявившихся (стори 5а.4, Task 5; AC-05).

Курсор — позиция в отсортированном списке, а не права доступа: он несёт
только `submitted_at` и `uuid` заявки, оба уже публичны (`uuid` адресует
заявку, `submitted_at` отдаётся в элементе). Подпись не нужна: подделанное
значение двигает точку старта по данным, которые автор карточки и так
видит, а предикаты `card_id`, статуса и сообщества накладываются отдельно
и курсором не обходятся.

**Почему ключ `(submitted_at, uuid)`, хотя сортировка — `(submitted_at, id)`**
(D-2, D-5a.4-2): `id` — внутренний bigint, и он не выходит наружу даже в
query-параметре. Расхождение разрешено в репозитории: `uuid` из курсора
превращается обратно в `id` скалярным подзапросом
(`JoinRequestRepository._after_predicate`), поэтому порядок страниц ровно
тот, что требует ADR-05.

**Почему четвёртая копия формы, а не общий `core/keyset.py`** (долг X-12):
общий модуль с параметром «какие две колонки» уже дал бы формы, которые
никто не проверяет тестами; четвёртая не станет лучше. Класс ошибки
ПЕРЕИСПОЛЬЗУЕТСЯ (`place_cursor.InvalidCursorError`), а не копируется:
HTTP-слой отображает `422 invalid_cursor` по одному типу.

Формат: `base64url` без padding от `{"submitted_at": ISO-8601 UTC, "uuid": str}`.
Защиты взяты из CR 5а.3 (`join_request_cursor.py`) без ослабления:
`validate=True`, отказ на `RecursionError`, отказ на крайних `submitted_at`,
отказ на пустую строку.
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
    "decode_card_requests_cursor",
    "encode_card_requests_cursor",
]

# AC-05: ровно два ключа. Проверка НАБОРА, а не «нужные ключи присутствуют»:
# лишний ключ (например `id`) означал бы утечку внутренних данных в публичный
# конверт, и молчаливое игнорирование скрыло бы её навсегда.
_CURSOR_KEYS = frozenset({"submitted_at", "uuid"})

# AC-05, CR 5а.3: границы `datetime`. asyncpg отвергает значения вне диапазона
# `timestamptz` при биндинге → `DBAPIError` → `500` на ввод клиента.
# Валидируем здесь, где ещё можно ответить `422`.
_MIN_YEAR = 1000
_MAX_YEAR = 9000


def encode_card_requests_cursor(submitted_at: datetime, request_uuid: uuid_lib.UUID) -> str:
    """AC-05: непрозрачный курсор от последней строки страницы.

    `submitted_at` нормализуется в UTC и сериализуется с `Z`
    (project-context §4). naive-значение отвергается на входе: в БД колонка
    `timestamptz`, и naive дал бы строку без смещения, которую `decode`
    отверг бы уже как ошибку клиента — лучше падать там, где виден источник.

    Микросекунды сохраняются: PostgreSQL хранит их, а усечение сдвигало бы
    границу страницы — часть строк между страницами терялась бы там, где
    `submitted_at` совпадает у нескольких заявок (обычный случай: пачка
    подач в одну секунду).
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


def decode_card_requests_cursor(raw: str) -> tuple[datetime, uuid_lib.UUID]:
    """AC-05: разбор курсора; любое отклонение — `422 invalid_cursor`.

    Курсор приходит из query-параметра, то есть это вход пользователя:
    битое значение обязано давать `InvalidCursorError`, а не `500` из
    драйвера (`ValueError`/`KeyError` наружу не выпускаются).

    Пустая строка — тоже ошибка клиента, а не «нет курсора»: `/places`
    и `/cards` отвечают на неё `422`, и молчаливый сброс на первую страницу
    скрыл бы дефект листания на клиенте.
    """
    if raw == "":
        raise InvalidCursorError()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        # `validate=True`: без него `urlsafe_b64decode` МОЛЧА отбрасывает
        # символы вне алфавита, и искажённый курсор декодировался бы в другой
        # валидный JSON → тихо неверная страница вместо `422`.
        # `b64decode(altchars=b"-_", validate=True)`, а не `urlsafe_b64decode`:
        # только первый умеет `validate`.
        payload = json.loads(base64.b64decode(padded.encode(), altchars=b"-_", validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise InvalidCursorError() from error
    except RecursionError as error:
        # Глубоко вложенный JSON (`'[' * 100000`) — пользовательский ввод, и
        # `RecursionError` не наследник `ValueError`: без этой ветки он уходил
        # бы в общий обработчик → `500` вместо `422` (CR 5а.3, патч 2).
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
        normalized = submitted_at.astimezone(UTC)
    except (OverflowError, OSError) as error:
        # Приведение к UTC может выйти за границы `datetime`
        # (`0001-01-01+23:59` → OverflowError) — CR 5а.3, патч 3.
        raise InvalidCursorError() from error
    if not (_MIN_YEAR <= normalized.year <= _MAX_YEAR):
        raise InvalidCursorError()
    return normalized, request_uuid
