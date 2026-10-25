"""Кодек курсора истории явок (стори 7.5; AC-05, AC-06, AC-08).

Курсор — позиция в отсортированной истории, а не права доступа: он несёт
`created_at` и `card_uuid`, оба уже присутствуют в ответе (AC-02). Прав он не
даёт: гард (`ModeratorUser` / `CurrentUser`) срабатывает ДО истории,
`participant_id` берётся из маршрута, а не из курсора, поэтому подделанное
значение лишь сдвигает точку чтения внутри данных, которые клиент и так видит.

**Почему `card_uuid`, а не `attendance.id`** (D-7.1-4, D-7.5-5). У `attendance`
нет публичного `uuid`, а внутренний `bigint` в query-параметре стал бы
публичным адресом и разрешил бы перебор `/1..N`. `card_uuid` уже возвращается
в элементе истории, то есть курсор не раскрывает нового поля. Второй ключ
сортировки (`card_id`) восстанавливается из `card_uuid` скалярным подзапросом
ВНУТРИ запроса истории — отдельный `SELECT` рос бы вместе со страницей.

**Класс ошибки ПЕРЕИСПОЛЬЗУЕТСЯ** (`place_cursor.InvalidCursorError`), а не
копируется: HTTP-слой отображает `422 invalid_cursor` по одному типу, и
второй класс дал бы два источника правды для одного кода ответа.

Формат: `base64url` без padding от `{"created_at": ISO-8601 UTC, "card_uuid": str}`.
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
    "decode_history_cursor",
    "encode_history_cursor",
]

#: AC-05: курсор обязан содержать ровно два ключа. Проверка НАБОРА, а не
#: «нужные ключи присутствуют»: лишний ключ означал бы, что в публичный
#: конверт попало внутреннее значение (`card_id`, `participant_id`), и
#: молчаливое игнорирование скрыло бы утечку навсегда.
_CURSOR_KEYS = frozenset({"created_at", "card_uuid"})

#: CR 7.5 L-4: верхняя граница длины входа. Курсор формируется сервером и
#: занимает ~120 символов; всё, что длиннее, — перебор или мусор. Ограничение
#: стоит В КОДЕКЕ, а не в `Query(max_length=...)`: иначе слишком длинный курсор
#: дал бы `422 invalid_request` (общий конверт валидации) вместо `422
#: invalid_cursor`, то есть второй код для одной и той же ошибки ввода.
_MAX_CURSOR_LENGTH = 512


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """CR 7.5 L-5: JSON с дубликатом ключа — битый курсор, а не «последний победил».

    `json.loads` молча берёт последнее значение, поэтому `{"created_at":A,
    "created_at":B,"card_uuid":...}` проходил проверку НАБОРА ключей и
    канонизировал неоднозначный ввод. Курсор формирует сервер, дубликата в
    нём быть не может — значит повтор ключа означает подделку.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise InvalidCursorError()
        seen[key] = value
    return seen


def encode_history_cursor(created_at: datetime, card_uuid: uuid_lib.UUID) -> str:
    """AC-05: непрозрачный курсор от последней строки страницы.

    `created_at` нормализуется в UTC и сериализуется с `Z` (project-context §4):
    курсор, собранный в одном поясе и разобранный в другом, иначе дал бы сдвиг
    чтения. Микросекунды сохраняются: PostgreSQL хранит их, а усечение до секунд
    отсекло бы строки с тем же `created_at` (СОРТИРОВКА идёт по `created_at`,
    поэтому потеря была бы не «на границе tie-брейка», а целой страницей).
    """
    if created_at.tzinfo is None:
        # Наивное время внутри курсора недопустимо по той же причине, что и в
        # `decode`: сравнение с `timestamptz` стало бы зависимым от зоны процесса.
        raise ValueError("created_at должен быть aware (UTC), получено naive-значение")
    payload = {
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "card_uuid": str(card_uuid),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_history_cursor(cursor: str) -> tuple[datetime, uuid_lib.UUID]:
    """AC-06: разбор курсора. Битое значение — `InvalidCursorError` (`422`).

    Курсор пришёл от клиента, поэтому ЛЮБАЯ ошибка разбора — ошибка ввода.
    `500` здесь был бы дефектом: он обвинил бы сервер в значении, которое
    сервер не формировал.

    `base64` дополняется padding-ом, а не отвергается: клиент мог пройти через
    посредника, который нормализовал `=` (round-trip-тест с `=` — Task 2.3).
    """
    # CR 7.5 L-4: длина проверяется ДО декодирования, иначе мегабайтный ввод
    # целиком попадал бы в память и в base64-декодер под контролем клиента.
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidCursorError()
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload: Any = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        # Набор ключей — ДО чтения значений: `{"created_at": ..., "card_id": 5}`
        # обязан дать `422`, а не тихо прочитаться как курсор без `card_uuid`.
        if not isinstance(payload, dict) or set(payload) != _CURSOR_KEYS:
            raise InvalidCursorError()
        created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        card_uuid = uuid_lib.UUID(str(payload["card_uuid"]))
        # CR 7.5 H-1: нормализация в UTC стоит ВНУТРИ `try`. Смещение, уводящее
        # за границы `datetime` (`0001-01-01T00:00:00+14:00`), поднимает
        # `OverflowError` уже здесь, и он обязан стать `422 invalid_cursor`:
        # значение пришло от клиента. Вне `try` ошибка вылетала как `500`.
        if created_at.tzinfo is None:
            # Наивное время — тоже битое значение: сравнение с `timestamptz` в
            # PostgreSQL дало бы неопределённый результат (см. `place_cursor`).
            raise InvalidCursorError()
        created_at = created_at.astimezone(UTC)
    except (
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        binascii.Error,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidCursorError() from exc
    return created_at, card_uuid
