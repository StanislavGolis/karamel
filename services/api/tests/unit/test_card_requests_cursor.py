"""Кодек курсора списка заявившихся (стори 5а.4, Task 10.2; AC-05).

Курсор приходит из query-параметра, то есть это ВХОД ПОЛЬЗОВАТЕЛЯ. Каждый
тест здесь защищает `422 invalid_cursor` от превращения в `500`: битый
base64, лишний ключ, naive-метка и крайние годы не должны доходить до
драйвера (CR 5а.3 — три из этих дефектов были реальными).
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.card_requests_cursor import (
    decode_card_requests_cursor,
    encode_card_requests_cursor,
)
from app.services.place_cursor import InvalidCursorError


def _raw(payload: dict[str, object]) -> str:
    """Курсор из произвольного JSON — чтобы проверять отказы, а не только roundtrip."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


class TestRoundtrip:
    def test_roundtrip_preserves_microseconds(self) -> None:
        """AC-05: микросекунды сохраняются — иначе границы страниц «плывут».

        Краснеет, если усечь до секунд: у заявок, поданных в одну секунду,
        страница потеряла бы или повторила строки.
        """
        moment = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
        request_uuid = uuid_lib.uuid4()

        decoded_at, decoded_uuid = decode_card_requests_cursor(
            encode_card_requests_cursor(moment, request_uuid)
        )

        assert decoded_at == moment
        assert decoded_uuid == request_uuid

    def test_roundtrip_normalizes_to_utc(self) -> None:
        """AC-05, project-context §4: значение в ответе всегда UTC.

        Краснеет, если оставить локальное смещение: сравнение в SQL шло бы
        с другим моментом, чем показывает клиент.
        """
        local = datetime(2026, 10, 24, 21, 0, tzinfo=timezone(timedelta(hours=9)))

        decoded_at, _ = decode_card_requests_cursor(
            encode_card_requests_cursor(local, uuid_lib.uuid4())
        )

        assert decoded_at == datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        assert decoded_at.tzinfo is UTC


class TestEncodeRejects:
    def test_naive_datetime_is_rejected(self) -> None:
        """AC-05: naive-метка при кодировании — ошибка программиста, не клиента.

        Краснеет, если пропустить naive дальше: `decode` отверг бы её уже как
        ошибку клиента, и источник дефекта (сервер) стал бы невидим.
        """
        with pytest.raises(ValueError):
            encode_card_requests_cursor(datetime(2026, 10, 24, 12, 0), uuid_lib.uuid4())


class TestDecodeRejects:
    def test_empty_string_is_rejected(self) -> None:
        """AC-05 (CR 5а.3, патч 4): `?cursor=` — ошибка клиента, не «нет курсора».

        Краснеет, если вернуть `None`: клиент получил бы первую страницу
        вместо `422` и молча зациклился бы на листании.
        """
        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor("")

    def test_broken_base64_is_rejected(self) -> None:
        """AC-05: битый base64 — `422`, а не падение.

        `validate=True` (CR 5а.3, патч 5): без него символы вне алфавита
        отбрасываются МОЛЧА, и курсор «чинится» в другой валидный JSON.
        """
        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor("!!!not-base64!!!")

    def test_extra_key_is_rejected(self) -> None:
        """AC-05: набор ключей проверяется, а не «нужные присутствуют».

        Краснеет, если проверять только наличие: лишний `id` означал бы
        утечку внутреннего bigint в публичный конверт (D-2).
        """
        raw = _raw(
            {
                "submitted_at": "2026-10-24T12:00:00+00:00",
                "uuid": str(uuid_lib.uuid4()),
                "id": 42,
            }
        )

        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor(raw)

    def test_naive_timestamp_is_rejected(self) -> None:
        """AC-05: подделанная naive-метка не доходит до SQL.

        Краснеет, если пропустить: PostgreSQL применил бы временную зону
        сессии, и страница «уехала» бы молча.
        """
        raw = _raw({"submitted_at": "2026-10-24T12:00:00", "uuid": str(uuid_lib.uuid4())})

        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor(raw)

    @pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00"])
    def test_extreme_years_are_rejected(self, stamp: str) -> None:
        """AC-05 (CR 5а.3, патч 3): крайние `submitted_at` → `422`, не `500`.

        asyncpg отвергает такие значения при биндинге в `timestamptz`, и без
        проверки границ клиент получал бы `500` на собственную опечатку.
        """
        raw = _raw({"submitted_at": stamp, "uuid": str(uuid_lib.uuid4())})

        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor(raw)

    def test_bad_uuid_is_rejected(self) -> None:
        """AC-05: не-UUID в курсоре — `422`, а не `500` из конструктора."""
        raw = _raw({"submitted_at": "2026-10-24T12:00:00+00:00", "uuid": "not-a-uuid"})

        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor(raw)

    def test_non_dict_payload_is_rejected(self) -> None:
        """AC-05: JSON-массив вместо объекта — `422`.

        Краснеет, если убрать `isinstance(payload, dict)`: `set(payload)` на
        списке дал бы `TypeError` наружу.
        """
        raw = base64.urlsafe_b64encode(b'["a","b"]').decode().rstrip("=")

        with pytest.raises(InvalidCursorError):
            decode_card_requests_cursor(raw)
