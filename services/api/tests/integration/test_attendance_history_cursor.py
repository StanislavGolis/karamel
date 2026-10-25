"""Кодек курсора истории явок (стори 7.5; AC-05, AC-06).

**Почему каждая ветка разбора имеет свой тест.** Курсор — единственное поле,
которое приходит от клиента в этой стори. Любая непокрытая ветка `decode`
превращает ошибку ввода в `500` (AC-06 прямо называет это дефектом), поэтому
проверяются ВСЕ шаги строгого разбора: base64 → JSON → набор ключей →
ISO-дата → UUID → наличие таймзоны (P-13: мутация обязана краснеть).

**Почему round-trip проверяет микросекунды.** Ключ сортировки истории —
`attendance.created_at`, а не `starts_at`. Усечение до секунд отсекло бы
строки с тем же `created_at`: потеря была бы не на границе tie-break, а
целой страницей.
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest

from app.services.attendance_history_cursor import (
    INVALID_CURSOR_CODE,
    InvalidCursorError,
    decode_history_cursor,
    encode_history_cursor,
)
from app.services.place_cursor import INVALID_CURSOR_CODE as PLACE_CODE

#: Курсор — вход пользователя, а не состояние БД: тесты не требуют PostgreSQL
#: и не помечены `integration` (маркер нужен только тем, кто ходит в базу).


def test_round_trip_preserves_microseconds() -> None:
    """AC-05: `encode → decode` возвращает ту же метку с микросекундами."""
    moment = datetime(2026, 9, 1, 18, 0, 0, 123456, tzinfo=UTC)
    card_uuid = uuid_lib.uuid4()

    restored_at, restored_uuid = decode_history_cursor(encode_history_cursor(moment, card_uuid))

    assert restored_at == moment
    assert restored_uuid == card_uuid


def test_cursor_has_no_padding_and_is_urlsafe() -> None:
    """AC-05: `base64url` БЕЗ padding — `=` в query-параметре требует экранирования."""
    cursor = encode_history_cursor(datetime(2026, 9, 1, tzinfo=UTC), uuid_lib.uuid4())

    assert "=" not in cursor
    assert "+" not in cursor
    assert "/" not in cursor


def test_cursor_payload_has_exactly_two_keys() -> None:
    """AC-05: в конверте ровно `created_at` и `card_uuid` — внутренних id нет."""
    cursor = encode_history_cursor(datetime(2026, 9, 1, tzinfo=UTC), uuid_lib.uuid4())
    raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))

    assert set(raw) == {"created_at", "card_uuid"}
    assert "card_id" not in raw
    assert "participant_id" not in raw
    assert raw["created_at"].endswith("Z")


def test_padded_cursor_is_accepted() -> None:
    """AC-06: курсор с восстановленным padding-ом разбирается, а не падает.

    Посредник (прокси, ручной вызов) может вернуть `=`; отвергать такой ввод
    значило бы наказывать клиента за нормализацию, которую он не выбирал.
    """
    moment = datetime(2026, 9, 1, tzinfo=UTC)
    card_uuid = uuid_lib.uuid4()
    cursor = encode_history_cursor(moment, card_uuid)
    padded = cursor + "=" * (-len(cursor) % 4)

    assert decode_history_cursor(padded) == (moment, card_uuid)


def test_naive_datetime_in_cursor_is_rejected() -> None:
    """AC-06: наивное время — `422`, а не неопределённое сравнение в БД."""
    payload = json.dumps({"created_at": "2026-09-01T18:00:00", "card_uuid": str(uuid_lib.uuid4())})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_history_cursor(cursor)


def test_not_base64_is_rejected() -> None:
    """AC-06: не-base64 — `422`."""
    with pytest.raises(InvalidCursorError):
        decode_history_cursor("это точно не курсор!!!")


def test_valid_base64_but_not_json_is_rejected() -> None:
    """AC-06: base64-валидное, но не-JSON — `422`."""
    cursor = base64.urlsafe_b64encode(b"not json at all").decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_history_cursor(cursor)


def test_non_uuid_card_uuid_is_rejected() -> None:
    """AC-06: `card_uuid` не-UUID — `422`."""
    payload = json.dumps({"created_at": "2026-09-01T18:00:00Z", "card_uuid": "42"})
    cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_history_cursor(cursor)


def test_extra_key_is_rejected() -> None:
    """AC-05: лишний ключ (`card_id`) — `422`.

    Проверка НАБОРА, а не «нужные ключи есть»: иначе внутренний `card_id`
    попал бы в публичный конверт и был бы молча проигнорирован.
    """
    payload = json.dumps(
        {
            "created_at": "2026-09-01T18:00:00Z",
            "card_uuid": str(uuid_lib.uuid4()),
            "card_id": 5,
        }
    )
    cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    with pytest.raises(InvalidCursorError):
        decode_history_cursor(cursor)


def test_error_class_is_reused_from_place_cursor() -> None:
    """AC-06: класс ошибки и код ПЕРЕИСПОЛЬЗУЮТСЯ, второй не создаётся.

    Два класса дали бы два источника правды для одного кода ответа: `422`
    ставился бы по одному из них, а второй уезжал бы в `500`.
    """
    assert INVALID_CURSOR_CODE == PLACE_CODE == "invalid_cursor"
    assert InvalidCursorError.code == "invalid_cursor"


def test_encode_rejects_naive_datetime() -> None:
    """AC-05: сервер не должен уметь собрать неоднозначный курсор."""
    with pytest.raises(ValueError):
        encode_history_cursor(datetime(2026, 9, 1), uuid_lib.uuid4())


def test_cursor_of_other_timezone_normalizes_to_utc() -> None:
    """AC-05: пояс нормализуется в UTC — иначе чтение сдвигалось бы."""
    moment = datetime(2026, 9, 1, 18, 0, tzinfo=UTC) + timedelta(hours=3)
    card_uuid = uuid_lib.uuid4()
    restored_at, _ = decode_history_cursor(encode_history_cursor(moment, card_uuid))

    assert restored_at == moment
    assert restored_at.utcoffset() == timedelta(0)


def test_history_sql_sort_and_keyset_are_both_keys() -> None:
    """AC-05, AC-07, AC-08: SQL истории сортирует по ДВУМ ключам и сравнивает пару.

    **Почему проверяется текст запроса, а не поведение** (testing-rules §18.3:
    для операторов это штатный приём). Поведенческий тест
    (`test_tie_break_by_card_id_across_pages`) НЕ краснеет от удаления
    `card_id DESC` из `ORDER BY`: индекс `ix_attendance_participant_created`
    сам отдаёт строки в порядке `(created_at DESC, card_id DESC)`, и
    PostgreSQL возвращает их уже отсортированными. То есть мутация
    эквивалентна по наблюдаемому результату — ровно тот случай, когда
    контракт держится текстом запроса.

    Проверяются три свойства, каждое из которых ломает пагинацию:
    (1) `ORDER BY` содержит ОБА ключа (иначе порядок при равных `created_at`
        недетерминирован и страницы могут пересечься);
    (2) keyset — row-comparison `(created_at, card_id) < (...)`, а не
        `OR`-цепочка (иначе планировщик теряет индексный порядок и получает
        `Sort`);
    (3) флагов подтверждения в `SELECT` нет (P-8).
    """
    from app.repositories.attendance import _HISTORY_AFTER_CURSOR_SQL, _HISTORY_SQL

    for sql in (_HISTORY_SQL, _HISTORY_AFTER_CURSOR_SQL):
        normalized = " ".join(sql.split())
        assert "ORDER BY a.created_at DESC, a.card_id DESC" in normalized, normalized
        assert "OR (" not in normalized, normalized
        for flag in ("author_confirmed", "participant_confirmed", "confirmed_at"):
            assert flag not in normalized, (flag, normalized)
    assert "(a.created_at, a.card_id) <" in " ".join(_HISTORY_AFTER_CURSOR_SQL.split())
