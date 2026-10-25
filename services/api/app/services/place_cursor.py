"""Кодек курсора выдачи мест (стори 3.3, Task 2.1–2.4; AC-04, AC-06).

Курсор — позиция в отсортированной ленте, а не права доступа (D-2): он несёт
только `created_at` и `uuid` места, оба уже публичны (попадают в ответ
`PlaceRead`). Подпись не нужна: подделанное значение двигает точку старта
по данным, которые участник и так видит, а предикат сообщества, активности
и категории накладывается отдельно и курсором не обходится.

Формат: `base64url` без padding от `{"created_at": ISO-8601 UTC, "uuid": str}`.
Ключ сортировки совпадает с ключом курсора — иначе `WHERE (created_at, uuid) >`
пропускал бы строки между страницами.

D-1, D-2 стори 3.1: внутренний `bigint` (`places.id`) в курсор не попадает
никогда. `base64` — кодирование, не шифрование: попавший туда `id`
вернулся бы из `decode` открытым и стал бы публичным адресом, то есть
разрешил бы перебор `/1..N` в обход лимитов.

Task 2.4: в общий `core/keyset.py` НЕ выносится. У карточек эпика 4 другой
ключ (`starts_at, id`), и преждевременная абстракция с параметром «какие
две колонки» дала бы третью форму, которую никто не проверяет тестами.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

# AC-04, AC-07: курсор — вход пользователя (query-параметр), поэтому битое
# значение обязано давать `422 invalid_cursor`, а не `500` из драйвера.
# Отдельный класс, а не `ValueError`: сервис различает «битый курсор» и
# «битые данные от собственного кода» и отображает в разные коды ответа.
INVALID_CURSOR_CODE = "invalid_cursor"


class InvalidCursorError(Exception):
    """AC-04, AC-07: курсор не разбирается — `422 invalid_cursor`.

    Ошибка поднимается на ЛЮБОМ шаге строгого разбора (base64, JSON, ключи,
    ISO-дата, UUID), но клиент получает один код: детали разбора не помогают
    ему исправить запрос — курсор непрозрачен и приходит от сервера.
    """

    code = INVALID_CURSOR_CODE


# AC-04: курсор обязан содержать ровно два ключа. Проверка набора, а не
# «нужные ключи присутствуют»: лишний ключ (например `id`) означал бы, что
# кто-то положил в публичный конверт внутренние данные, и молчаливое
# игнорирование скрыло бы это навсегда.
_CURSOR_KEYS = frozenset({"created_at", "uuid"})


def encode_place_cursor(created_at: datetime, place_uuid: uuid_lib.UUID) -> str:
    """AC-04: собирает непрозрачный курсор от последней строки страницы.

    `created_at` нормализуется в UTC и сериализуется с `+00:00` → `Z`
    (project-context §4: даты наружу — ISO 8601 UTC с `Z`).

    naive `datetime` отвергается на входе: `created_at` в БД — `timestamptz`,
    и naive-значение дало бы строку без смещения, которую `decode` отверг бы
    уже как ошибку клиента. Лучше падать здесь, где виден источник.

    Микросекунды сохраняются: PostgreSQL хранит их, и усечение до секунд
    сдвигало бы границу страницы — часть строк между страницами терялась бы
    (при равенстве `created_at` решает `uuid`, но усечённая метка отсекала бы
    строки с тем же `created_at` и меньшим `uuid`).
    """
    if created_at.tzinfo is None:
        raise ValueError("created_at должен быть aware (UTC), получено naive-значение")
    moment = created_at.astimezone(UTC)
    payload = {
        "created_at": moment.isoformat().replace("+00:00", "Z"),
        "uuid": str(place_uuid),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # `padding` снимается: `+`/`/` из standard-base64 в query-параметре требуют
    # percent-кодирования, а `=` в конце легко теряется прокси и клиентом.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_place_cursor(raw: str) -> tuple[datetime, uuid_lib.UUID]:
    """AC-04, AC-06: строгий разбор курсора в пару `(created_at, uuid)`.

    Разбор намеренно без «мягких» веток: нет `except Exception` с молчаливым
    `None` и нет подстановки текущего времени. Курсор — единственный признак
    позиции; тихо проигнорированный битый курсор вернул бы клиенту ПЕРВУЮ
    страницу, и бесконечное листание выглядело бы как «страницы кончились»
    только по совпадению.

    Проверки по шагам (каждая — отдельная причина `422 invalid_cursor`):
    1. base64url — включая неверный алфавит и «хвост» битых символов
       (`validate=True`: без него `b64decode` молча ВЫБРАСЫВАЕТ символы вне
       алфавита, и `valid_cursor + "!!!"` проходил разбор — CR 3.3, патч 3);
    2. UTF-8 + JSON-объект;
    3. ровно два ключа `created_at`/`uuid` (лишний ключ — признак утечки
       внутренних данных в публичный конверт);
    4. ISO-8601 с смещением и приведением к UTC. naive-дата отвергается:
       сравнение naive с `timestamptz` в asyncpg даёт `DataError` → `500`;
    5. `uuid` — валидный UUID.
    """
    try:
        # `padding` восстанавливается: `b64decode` требует кратности четырём,
        # а мы её сняли на кодировании.
        padded = raw + "=" * (-len(raw) % 4)
        # CR 3.3, патч 3: `validate=True` — иначе посторонние символы
        # отбрасываются молча и мусорный хвост принимается за валидный курсор.
        # Докстринг обещал строгость алфавита; проверка должна её давать.
        # `urlsafe_b64decode` не принимает `validate` — только обёртка над
        # `b64decode(..., altchars=b"-_", validate=True)`. Алфавит тот же
        # (`-`/`_` вместо `+`/`/`), но посторонние символы теперь ошибка.
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as error:
        # `binascii.Error` — неверный алфавит или длина (validate=True).
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    try:
        payload: Any = json.loads(decoded.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
        # CR 3.3, патч 2: глубоко вложенный JSON (курсор из `'['*20000`)
        # даёт `RecursionError`, а он не `ValueError` и не `JSONDecodeError`:
        # без этой ветки пользовательский query-параметр ронял запрос в `500`.
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
    # AC-04: aware-дата обязательна. `fromisoformat` принимает и naive-строку
    # (`2026-10-23T11:20:31`), и без этой проверки значение ушло бы
    # в `timestamptz`-сравнение, где asyncpg отвергает naive → `500`.
    if created_at.tzinfo is None:
        raise InvalidCursorError(INVALID_CURSOR_CODE)

    try:
        place_uuid = uuid_lib.UUID(uuid_raw)
    except (ValueError, AttributeError, TypeError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error

    # CR 3.3, патч 1: `astimezone(UTC)` на краю диапазона
    # (`9999-12-31T23:59:59-23:59`, `0001-01-01T00:00:00+23:59`) выходит
    # за границы `datetime` и бросает `OverflowError` — не `ValueError`,
    # поэтому не ловился и превращался в `500` на вводе клиента.
    try:
        return created_at.astimezone(UTC), place_uuid
    except (OverflowError, OSError) as error:
        raise InvalidCursorError(INVALID_CURSOR_CODE) from error
