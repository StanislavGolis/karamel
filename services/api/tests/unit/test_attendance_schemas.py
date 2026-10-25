"""Unit-тесты схем и ключа дедупа подтверждения явки (стори 7.2).

TD 7.2: `U-BOOL-01…06` (StrictBool), `U-DEDUP-01…03` (ключ дедупа),
`U-ENV-01` (event_uuid только событиям-переходам).

**Почему без БД.** Всё здесь — контракт формы: тело запроса, форма ответа и
ключ дедупа. Ни одно из этих свойств не требует PostgreSQL, а ошибка в них
ломает клиента (или, в случае `arrived: 1`, создаёт ложную неявку) — то есть
ловить её надо за секунды, а не в интеграционном прогоне.

**Запрет тавтологий** (§18.2): каждый тест проверяет наблюдаемый результат
вызова, а не наличие константы в модуле.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.enums import AttendanceStatus
from app.schemas.attendance import AttendanceAnswer, AttendanceState
from app.schemas.ws import WS_TRANSITION_EVENT_TYPES, WsEnvelope
from app.services.ws_broker import _dedup_key

# ——— U-BOOL: `arrived` — StrictBool (D-7.2-9, X-7.1-5, R11) —————————————


@pytest.mark.parametrize("value", [True, False])
def test_bool_accepts_real_booleans(value: bool) -> None:
    """U-BOOL-01 (P-11): признак проходит в ОБОИХ значениях, не только `true`.

    Тест только на `True` прошёл бы и при константе `True` — тогда ответ
    «не пришёл» был бы невозможен, а `no_show` (санкция) не достигался бы
    вовсе.
    """
    assert AttendanceAnswer(arrived=value).arrived is value


@pytest.mark.parametrize("value", [1, 0])
def test_bool_rejects_integers(value: int) -> None:
    """U-BOOL-02 (X-7.1-5): `1`/`0` из JSON — `422`, а не тихое приведение.

    Мутация M8 (`StrictBool` → `bool`) делает этот тест красным: `0` стал бы
    `False`, то есть ответом «не пришёл», и односторонний ответ дал бы
    ложный `no_show` (FR109).
    """
    with pytest.raises(ValidationError):
        AttendanceAnswer(arrived=value)


@pytest.mark.parametrize("value", ["true", "false", "1", ""])
def test_bool_rejects_strings(value: str) -> None:
    """U-BOOL-03: строковые формы `true`/`false` — тоже ошибка клиента.

    Клиент, посылающий строку, должен получить `422` и исправить запрос, а не
    тихое согласие: `"false"` в нестрогом режиме стало бы `True`.
    """
    with pytest.raises(ValidationError):
        AttendanceAnswer(arrived=value)


def test_bool_rejects_null() -> None:
    """U-BOOL-04: `arrived: null` — ошибка: ответа «никак» у подтверждения нет.

    `None` в теле означал бы третий вариант ответа, которого в модели нет;
    приняв его как `False`, сервер записал бы неявку.
    """
    with pytest.raises(ValidationError):
        AttendanceAnswer(arrived=None)


def test_bool_rejects_missing_field() -> None:
    """U-BOOL-05: без `arrived` тело невалидно — подтверждения не было."""
    with pytest.raises(ValidationError):
        AttendanceAnswer()  # type: ignore[call-arg]


def test_bool_rejects_extra_fields() -> None:
    """U-BOOL-06: лишнее поле отбивается, а не игнорируется.

    `{"arrived": true, "participant_id": 5}` создал бы впечатление, что
    сторону можно выбрать телом запроса. Сторону определяет сервер по роли.
    """
    with pytest.raises(ValidationError):
        AttendanceAnswer.model_validate({"arrived": True, "participant_id": 5})


# ——— `my_answer`: `None` ≠ `False` (P-8, инвентаризация предикатов строка 13) —


@pytest.mark.parametrize(
    "my_answer",
    [True, False, None],
)
def test_state_keeps_three_valued_answer(my_answer: bool | None) -> None:
    """P-8: три состояния собственного ответа сохраняются как есть.

    Мутация M11 (`None` → `False` при сборке DTO) делает красным случай
    `None`: «не подтвердил» превратилось бы в «не пришёл», и сервер показал
    бы пользователю неявку, которой он не заявлял.
    """
    state = AttendanceState(my_answer=my_answer, status=AttendanceStatus.PENDING)

    assert state.my_answer is my_answer


def test_state_rejects_unknown_status() -> None:
    """U-ENUM-01 (инвентаризация, строка 17): неизвестный статус — `500`, не строка.

    Рассинхрон `CHECK` со `StrEnum` обязан быть виден как падение сборки
    ответа, а не уехать клиенту значением, которое он не умеет отображать.
    """
    with pytest.raises(ValidationError):
        AttendanceState.model_validate({"my_answer": True, "status": "maybe"})


def test_state_rejects_extra_fields() -> None:
    """D-7.2-4: третьего поля в ответе быть не может — даже полезного.

    `confirmed_at` выдал бы момент второго ответа, `author_confirmed` —
    чужой флаг. Схема обязана отвергнуть оба, иначе первая же правка
    сервиса протащит их наружу.
    """
    with pytest.raises(ValidationError):
        AttendanceState.model_validate(
            {"my_answer": True, "status": "pending", "confirmed_at": "2026-10-24T00:00:00Z"}
        )


# ——— U-DEDUP: ключ дедупа (D-7.2-6, R6) —————————————————————————————————


def test_dedup_key_prefers_event_uuid() -> None:
    """U-DEDUP-01: событие-переход дедупится по `(type, event_uuid)`.

    Мутация M6 (ключ → `(type, message_uuid)`) делает тест красным: у
    `attendance.done` нет `message_uuid`, ключ стал бы `None`, и двойная
    доставка кадра прошла бы как два события.
    """
    frame = {"type": "attendance.done", "event_uuid": "e-1", "payload": {}}

    assert _dedup_key(frame) == ("attendance.done", "e-1")


def test_dedup_key_falls_back_to_message_uuid() -> None:
    """U-DEDUP-02: кадр в полёте без `event_uuid` дедупится по `message_uuid`.

    Fallback нужен ровно на время деплоя: старый сервер не ставит
    `event_uuid`, а новый брокер обязан продолжать отбрасывать дубли
    сообщений (иначе 6.4/6.5 регрессируют молча).
    """
    frame = {
        "type": "message.created",
        "payload": {"message_uuid": "m-1", "body": "привет"},
    }

    assert _dedup_key(frame) == ("message.created", "m-1")


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "chat.subscribed", "payload": {"chat_uuid": "c-1"}},
        {"type": "chat.restored", "payload": {"chat_uuid": "c-1"}},
        {"type": "attendance.done", "event_uuid": 123, "payload": {}},
        {"type": "attendance.done", "event_uuid": "", "payload": {}},
        {"type": None, "payload": {}},
        {"type": "attendance.done", "payload": "not-a-dict"},
    ],
)
def test_dedup_key_returns_none_when_not_applicable(frame: dict[str, object]) -> None:
    """U-DEDUP-03: дедуп неприменим → `None`, НЕ исключение.

    Два случая в одном тесте, и оба обязательны:

    - `chat.subscribed`/`chat.restored` — легитимный повтор (D-2): дедуп
      склеил бы два подтверждения подписки (регрессия 6.5, `W-SUBSCRIBED-01`);
    - не-`str`/пустой ключ — испорченный кадр из Redis: исключение
      остановило бы фан-аут всем соединениям из-за одного кадра.
    """
    assert _dedup_key(frame) is None


# ——— U-ENV: `event_uuid` только событиям-переходам (D-2) ———————————————


def test_transition_event_gets_event_uuid() -> None:
    """U-ENV-01: событие-переход получает валидный uuid4 и он попадает в кадр."""
    frame = WsEnvelope.build("attendance.done", {"status": "confirmed"}).to_frame()

    event_uuid = frame.get("event_uuid")
    assert isinstance(event_uuid, str)
    # Валидность формы: иначе дедуп работал бы по произвольной строке и
    # столкновение ключей было бы возможно.
    assert str(__import__("uuid").UUID(event_uuid)) == event_uuid


def test_event_uuid_differs_between_transitions() -> None:
    """U-ENV-01: две сборки одного типа — разные `event_uuid`.

    Ключ должен быть новым на КАЖДЫЙ переход: одинаковый uuid склеил бы два
    реальных события в одно.
    """
    first = WsEnvelope.build("attendance.done", {"status": "confirmed"}).to_frame()
    second = WsEnvelope.build("attendance.done", {"status": "confirmed"}).to_frame()

    assert first["event_uuid"] != second["event_uuid"]


@pytest.mark.parametrize("type_", ["chat.subscribed", "chat.restored"])
def test_connection_state_event_has_no_event_uuid(type_: str) -> None:
    """U-ENV-01 / M12: события состояния соединения — БЕЗ `event_uuid`.

    Мутация M12 (ставить `event_uuid` всем) делает тест красным, а вместе с
    ним ломается `W-SUBSCRIBED-01`: повторная подписка потеряла бы
    подтверждение (регрессия 6.5, D-2).
    """
    frame = WsEnvelope.build(type_, {"chat_uuid": "c-1"}).to_frame()

    assert "event_uuid" not in frame
    assert frame["type"] == type_


def test_attendance_done_registered_as_transition() -> None:
    """U-ENV-01: `attendance.done` входит в реестр переходов — иначе нет uuid."""
    assert "attendance.done" in WS_TRANSITION_EVENT_TYPES
