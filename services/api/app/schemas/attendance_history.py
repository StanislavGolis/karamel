"""Схемы истории явок (стори 7.5; AC-02, AC-05, AC-09 O-5).

**Ровно четыре поля в элементе** (D-7.5-4, NFR39): `card_uuid`, `card_title`,
`starts_at`, `status`.

Чего нет и почему:

- `my_answer` — у модератора нет своей стороны в чужой паре, а своя история
  уже отдаётся `GET /cards/{uuid}/attendance` (D-7.2-4);
- `confirmed_at` — момент второго ответа выдал бы поведение второй стороны
  (класс D-7.2-4);
- `attendance_id` — внутренний адрес наружу не выходит (D-2);
- `participant_uuid` — у модератора он есть в URL, в своём маршруте избыточен.

`extra="forbid"` на ОБЕИХ схемах — защита от случайного расширения: если
кто-то добавит поле в фабрику, Pydantic упадёт на сборке, а не отдаст лишнее
в прод (та же логика, что в `schemas/report.py`).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer

from app.core.datetime_format import iso_utc
from app.models.enums import AttendanceStatus

__all__ = ["AttendanceHistoryItem", "AttendanceHistoryPage"]


class AttendanceHistoryItem(BaseModel):
    """AC-02 (NFR39): РОВНО четыре поля — точка.

    `status: AttendanceStatus`, а не `str`: неизвестное значение из БД —
    рассинхрон `CHECK`/`StrEnum`, то есть дефект сервера (`500`), а не строка,
    уехавшая клиенту (инвентаризация предикатов, строка 17).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_uuid: uuid_lib.UUID
    card_title: str
    starts_at: datetime
    status: AttendanceStatus

    @field_serializer("starts_at")
    def _serialize_starts_at(self, value: datetime) -> str:
        """project-context §4: ISO 8601 UTC с `Z` (образец — `schemas/report.py`).

        CR 7.5 L-1: `iso_utc(...) or ""` подменял отсутствующее время ПУСТОЙ
        строкой. `event_cards.starts_at` — `NOT NULL`, поэтому `None` здесь
        означал бы рассинхрон модели и БД, то есть дефект сервера; пустая
        строка отдала бы его клиенту как `Invalid Date` и сломала бы
        сортировку в списке молча. Поэтому рассинхрон — исключение, а не
        подстановка.
        """
        serialized = iso_utc(value)
        if serialized is None:
            raise ValueError("starts_at обязателен: None означает рассинхрон модели и БД")
        return serialized


class AttendanceHistoryPage(BaseModel):
    """AC-05: страница keyset-пагинации.

    `next_cursor = null` — страница ПОСЛЕДНЯЯ (D-7.5-9). Отдельного
    `last_cursor`/`has_more` нет: признак «есть ещё» выражается самим
    отсутствием курсора, второй флаг был бы вторым источником правды.

    `items` — список без обёртки-объекта и без `total`: `total` требовал бы
    `COUNT`, а `COUNT(*) OVER ()` в 6.6 усекался курсором и лечение добавило
    второй SQL (D-7.5-9).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AttendanceHistoryItem]
    next_cursor: str | None
