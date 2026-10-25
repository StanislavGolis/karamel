"""Схемы подтверждения явки (стори 7.2; AC-01, AC-04, AC-08, AC-11; CR 7.2 M-1, L-1).

**Контракт разделён по роли зрителя** (решение пользователя по CR 7.2 M-1):

- УЧАСТНИК получает статус СВОЕЙ пары: это адресный факт (ADR-04) и будущее
  основание санкции FR109. Чужой флаг не раскрывается (NFR39).
- АВТОР дополнительно получает `card_status` — агрегат по ВСЕМ парам карточки:
  он не принадлежит ни одной паре (D-7.1-2), и «его пары» не существует.

Агрегат ВЫЧИСЛЯЕТСЯ НА ЧТЕНИИ и в БД не хранится: второго источника правды
нет, инвариант строки (`ck_attendance_status_consistency`, F1–F12) не тронут.

**`my_answer`, а не `arrived`.** Имя поля не должно намекать, чья это сторона
в модели: `author_confirmed` читался бы как «флаг автора», и клиент вывел бы
из него чужое. `my_answer` — собственный ответ зрителя, и только он.

**`None` ≠ `False`** (P-8, инвентаризация предикатов строка 13): «ещё не
ответил» и «ответил: не пришёл» — разные состояния, и на этом различии
стоит `pending`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, model_serializer

from app.models.enums import AttendanceStatus

__all__ = [
    "ATTENDANCE_NOT_OPENED_CODE",
    "ATTENDANCE_NO_PAIR_CODE",
    "ATTENDANCE_SELF_CONFIRM_CODE",
    "AttendanceAnswer",
    "AttendanceState",
]

#: AC-07: окно подтверждения ещё не открылось (`now < starts_at`). `422`, не
#: `409`: значение пришло от клиента (он подтверждает раньше времени), а не
#: конфликт состояния сервера.
ATTENDANCE_NOT_OPENED_CODE = "ATTENDANCE_NOT_OPENED"

#: CR 7.2 H-3: подтверждать нечего — у зрителя НЕТ пары в этой карточке
#: (принятой заявки нет). `409`, а не тихий `200`: клиент иначе считал бы,
#: что подтверждение записано, и факт терялся бы молча.
#:
#: Отдельный код от `card_not_found` (D-7.2-8) здесь допустим: зритель уже
#: прошёл проверку участия, то есть существование карточки ему раскрыто
#: законно — он в её компании, просто без пары «автор ↔ он».
ATTENDANCE_NO_PAIR_CODE = "ATTENDANCE_NO_PAIR"

#: AC-10 (X-7.1-7): «участник» строки — сам автор карточки. Не `422`: тело
#: запроса корректно, запрещено ДЕЙСТВИЕ (подделка факта явки). Тот же класс
#: ответа, что у «своя карточка» (5а.2), — `403`.
ATTENDANCE_SELF_CONFIRM_CODE = "ATTENDANCE_SELF_CONFIRM"


class AttendanceAnswer(BaseModel):
    """AC-01, D-7.2-9: тело `POST /cards/{uuid}/attendance`.

    `StrictBool`, а не `bool` (X-7.1-5): Pydantic в обычном режиме приводит
    `1`/`0`/`"true"` к `True`/`False`, и `0` из JSON тихо стало бы ответом
    «не пришёл» — то есть ложный `no_show` без второго человека (FR109).
    Строгий тип отбивает всё, кроме `true`/`false`, кодом `422`.

    `extra="forbid"`: лишнее поле в теле — ошибка клиента, а не повод его
    игнорировать. Молчаливое принятие `{"arrived": true, "participant_id": 5}`
    создало бы впечатление, что сторону можно выбрать телом запроса.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    arrived: StrictBool


class AttendanceState(BaseModel):
    """AC-01, AC-04, AC-08: ответ `GET` и `POST /cards/{uuid}/attendance`.

    У участника — ДВА поля (`my_answer`, `status`), и это контракт анти-оракула
    (D-7.2-4): `status` его пары, `my_answer` его ответ. `confirmed_at` не
    отдаётся: момент второго ответа выдал бы поведение второй стороны.

    У автора появляется третье поле `card_status` — агрегат по всем парам
    (решение CR 7.2 M-1). Оно отсутствует в теле участника ВООБЩЕ, а не
    приходит `null`: `null` читался бы как значение и позволил бы отличить
    «агрегата нет» от «поле не для тебя» (см. `model_serializer` ниже).

    `status: AttendanceStatus` (а не `str`): неизвестное значение из БД даёт
    `500` на собственном дефекте (рассинхрон `CHECK` со `StrEnum`), а не
    уезжает клиенту строкой (инвентаризация предикатов, строка 17).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: AC-01, AC-04: `true`/`false` — ответ зрителя, `null` — он ещё не ответил.
    my_answer: bool | None
    #: AC-01…AC-04: для участника — статус ЕГО пары; для автора — агрегат
    #: карточки (у автора пары нет, D-7.1-2).
    status: AttendanceStatus
    #: CR 7.2 M-1: агрегат по всем парам. ТОЛЬКО для автора.
    card_status: AttendanceStatus | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_card_status(self, handler: Any) -> dict[str, Any]:
        """Убирает `card_status` из тела участника (контракт анти-оракула).

        `exclude_none=True` на маршруте не годится: он срезал бы и
        `my_answer: null`, который обязан присутствовать у неответившего
        (O-3, `A-NULL-ANSWER-01`). Поэтому поле удаляется точечно и только
        когда оно не задано.
        """
        data: dict[str, Any] = handler(self)
        if data.get("card_status") is None:
            data.pop("card_status", None)
        return data
