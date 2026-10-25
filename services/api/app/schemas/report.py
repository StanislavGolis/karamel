"""Схемы жалобы на неявку (стори 7.4; AC-03, AC-08).

**Ровно четыре поля в ответе** (NFR39, D-7.4-4). Нет ни `reporter_id`
(жалобщик не раскрывается обвиняемому), ни `accused_uuid` (обвиняемый не
подтверждается жалобщику: при `disputed` это выдало бы чужой флаг), ни
`attendance_id`/`card_id` (внутренние адреса наружу не выходят, D-2).

`extra="forbid"` на ОБЕИХ схемах:

- на запросе — лишнее поле (например, `reason`) даёт `422`, а не тихо
  игнорируется: клиент не должен думать, что передал текст жалобы, которого
  API не принимает (X-7.4-3);
- на ответе — защита от случайного расширения: если кто-то добавит поле в
  фабрику, Pydantic упадёт на сборке, а не отдаст лишнее в прод.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.datetime_format import iso_utc

__all__ = [
    "MessageReportRead",
    "MessageReportRequest",
    "NoShowReportRead",
    "NoShowReportRequest",
]


class NoShowReportRequest(BaseModel):
    """AC-03: тело `POST /cards/{uuid}/report-no-show`.

    Одно поле — публичный адрес обвиняемого. `reason` сознательно отсутствует
    (X-7.4-3): свободный ввод с ПД в 7.4 не нужен, причину пишет модератор в 8.4.
    """

    model_config = ConfigDict(extra="forbid")

    accused_uuid: uuid_lib.UUID = Field(description="Публичный адрес участника, на которого жалоба")


class NoShowReportRead(BaseModel):
    """AC-08 (NFR39): ответ `201` — РОВНО четыре поля.

    `status` и `target_type` — `Literal`, а не `str`: значение закреплено
    `CHECK` в схеме БД, и `Literal` дублирует это на уровне контракта —
    клиент видит в OpenAPI единственное возможное значение, а расширение
    домена (8.1) не пройдёт мимо типа.
    """

    model_config = ConfigDict(extra="forbid")

    uuid: uuid_lib.UUID
    target_type: Literal["no_show"]
    status: Literal["open"]
    reported_at: datetime

    @field_serializer("reported_at")
    def _serialize_reported_at(self, value: datetime) -> str:
        """project-context §4: ISO 8601 UTC с `Z` (образец — `schemas/photo.py`).

        `iso_utc` не None-safe по типу поля здесь не нужен: `reported_at`
        NOT NULL и всегда приходит из `RETURNING`.
        """
        return iso_utc(value) or ""

    @classmethod
    def from_created(cls, created: object) -> NoShowReportRead:
        """Собирает схему из `ReportCreated` — единственный путь наружу.

        Фабрика, а не `model_validate`: у доменного объекта нет `model_config`,
        и `model_validate` потребовал бы `from_attributes`. Фабрика заодно
        фиксирует список полей в одном месте: добавить поле можно только здесь,
        и `extra="forbid"` не даст протащить его мимо контракта.
        """
        return cls(
            uuid=created.uuid,  # type: ignore[attr-defined]
            target_type=created.target_type,  # type: ignore[attr-defined]
            status=created.status,  # type: ignore[attr-defined]
            reported_at=created.reported_at,  # type: ignore[attr-defined]
        )


class MessageReportRequest(BaseModel):
    """AC-02 стори 8.2: тело `POST /api/v1/reports` — жалоба на диалог.

    **`target_type` в теле, а не в пути.** Роут один (`/reports`), объект
    выбирает клиент: `no_show` здесь даёт `422`, потому что у неявки свой
    роут (7.4, D-23). `Literal["message"]` делает это отказом СХЕМЫ, а не
    проверкой в коде роута: значение закреплено `CHECK` в БД.

    `extra="forbid"`: лишнее поле (например, `reason`) даёт `422`, а не тихо
    игнорируется — клиент не должен думать, что передал текст жалобы,
    которого API не принимает (тот же довод, что в 7.4, X-7.4-3).
    """

    model_config = ConfigDict(extra="forbid")

    target_type: Literal["message"] = Field(
        description="Тип объекта жалобы; `no_show` подаётся через карточку (7.4)"
    )
    chat_uuid: uuid_lib.UUID = Field(description="Публичный адрес диалога")
    accused_uuid: uuid_lib.UUID = Field(description="Публичный адрес собеседника")


class MessageReportRead(BaseModel):
    """AC-02 (NFR39): ответ `201` — РОВНО четыре поля.

    Те же четыре, что у жалобы на неявку (7.4): адрес жалобы, тип, состояние
    и время. Нет ни `reporter_id` (жалобщик не раскрывается), ни
    `accused_uuid` (обвиняемый не подтверждается жалобщику), ни `chat_id`
    (внутренний адрес наружу не выходит, D-2). `extra="forbid"` защищает от
    расширения мимо контракта: фабрика упадёт на сборке, а не отдаст лишнее.
    """

    model_config = ConfigDict(extra="forbid")

    uuid: uuid_lib.UUID
    target_type: Literal["message"]
    status: Literal["open"]
    reported_at: datetime

    @field_serializer("reported_at")
    def _serialize_reported_at(self, value: datetime) -> str:
        """project-context §4: ISO 8601 UTC с `Z`."""
        return iso_utc(value) or ""

    @classmethod
    def from_created(cls, created: object) -> MessageReportRead:
        """Собирает схему из `ReportCreated` — единственный путь наружу."""
        return cls(
            uuid=created.uuid,  # type: ignore[attr-defined]
            target_type=created.target_type,  # type: ignore[attr-defined]
            status=created.status,  # type: ignore[attr-defined]
            reported_at=created.reported_at,  # type: ignore[attr-defined]
        )


# ——— очередь модератора (стори 8.3; AC-01…AC-05) ———


class ReportQueueAttendance(BaseModel):
    """AC-01, FR112: факты пары явки для очереди.

    Модератор видит ОБА подтверждения: без них «неявка» неотличима от
    «один сказал, второй молчал», а решение опиралось бы на пересказ.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(description="Статус пары attendance")
    author_confirmed: bool = Field(description="Подтвердил автор карточки")
    participant_confirmed: bool = Field(description="Подтвердил участник")
    card_uuid: uuid_lib.UUID = Field(description="Публичный адрес карточки встречи")


class ReportQueueItem(BaseModel):
    """AC-01, AC-05: одна жалоба в очереди.

    **Чего здесь НЕТ и почему.** Ни `reporter_id`, ни `chat_id`, ни текста
    сообщений: очередь показывает ПРЕДМЕТ разбора, а не переписку. Фрагмент
    (AC-02) запрашивается отдельным маршрутом — там же и аудит доступа
    (AC-03). Модератор для решения в очереди видит тип, возраст и факты явки.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: uuid_lib.UUID
    target_type: Literal["no_show", "message"]
    status: Literal["open"]
    reported_at: datetime
    overdue: bool = Field(description="Жалоба старше порога NFR52 (20 ч)")
    attendance: ReportQueueAttendance | None = Field(
        default=None, description="Факты пары; только для `target_type='no_show'`"
    )

    @field_serializer("reported_at")
    def _serialize_reported_at(self, value: datetime) -> str:
        """§4 project-context: время наружу — ISO-8601 UTC с `Z`."""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ReportQueuePage(BaseModel):
    """AC-04: конверт очереди `{items, total, cursor}` (как §7.1).

    `total` — «сколько всего под фильтром», не «сколько на странице»:
    модератор планирует работу по объёму, и усечённый счётчик занизил бы её.
    `cursor = null` — страница последняя (признак «есть ещё» выражается
    отсутствием курсора, второй флаг был бы вторым источником правды).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ReportQueueItem]
    total: int
    # DS 8.3, D-41: `serialization_alias` БЕЗ `validation_alias`. Пара
    # алиасов ломала КАЖДЫЙ вызов очереди: сервис собирает схему по имени
    # поля (`cursor=`), а `validation_alias="next_cursor"` делал `cursor`
    # «лишним входом» и при `extra="forbid"` валил сборку в `ValidationError`
    # → `500` на живом маршруте. Наружу по-прежнему `next_cursor`
    # (FastAPI сериализует `response_model` по алиасу).
    cursor: str | None = Field(default=None, serialization_alias="next_cursor")


class ReportFragmentMessage(BaseModel):
    """AC-02: одно сообщение фрагмента; `body` — ОТКРЫТЫЙ текст.

    Только в этом ответе и только модератору. В очередь и логи текст не
    попадает (NFR27): у очереди текста нет вовсе, у лога — только счётчик.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: uuid_lib.UUID
    sender_uuid: uuid_lib.UUID | None = Field(
        default=None, description="`null` у системного сообщения"
    )
    body: str
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ReportFragmentResponse(BaseModel):
    """AC-02: не более 10 последних сообщений, порядок — от старых к новым.

    `message_count` дублирует `len(items)` намеренно: это то же число, что
    ушло в лог доступа (AC-03), и клиент/аудит сверяются по одному полю.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_uuid: uuid_lib.UUID
    chat_uuid: uuid_lib.UUID
    message_count: int
    items: list[ReportFragmentMessage]


# ——— решение по жалобе и санкция (стори 8.4; AC-01…AC-10) ———

#: D-45, D-46: домен исхода разбора. `dismissed` — закрытие БЕЗ санкции (FR89).
#: Один алиас на запрос, ответ и `cast` в роутере: три копии `Literal`
#: разошлись бы при добавлении значения (класс дефекта 4.1).
Resolution = Literal["warning", "ban", "dismissed"]

#: D-46: домен статуса разобранной жалобы. Пара к `Resolution`
#: (`resolved_warning`↔`warning`, `resolved_ban`↔`ban`, `dismissed`↔`dismissed`).
ResolvedStatus = Literal["resolved_warning", "resolved_ban", "dismissed"]


class ResolveReportRequest(BaseModel):
    """AC-01, AC-10, D-45: тело решения по жалобе.

    **`Literal`, а не `StrEnum` в поле.** Мусор отбивает Pydantic ДО сервиса →
    `422` без новой ветки маппинга (тот же приём, что `target_type` в 8.3).

    **Поле называется `resolution`, а не `type`** (как в шарде). `type` в этом
    домене занят дважды: `sanctions.type` и `reports.target_type`; третье
    значение `type` в одном запросе сделало бы код нечитаемым.

    **`reason` обязательна и обрезается по краям.** Санкция без объяснения
    неотличима от ошибки модератора (FR39: обвиняемый видит ПРИЧИНУ).
    Минимум 3 символа — «ок»/«да» причиной не являются.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resolution: Resolution = Field(
        description="Исход разбора: предупреждение, бан или закрытие без санкции"
    )
    reason: str = Field(
        min_length=3,
        max_length=1000,
        description="Причина для обвиняемого; попадает в санкцию и в жалобу",
    )


class ResolveReportRead(BaseModel):
    """AC-01, AC-05, D-48: ответ на решение по жалобе.

    **Чего здесь НЕТ.** Ни `accused_uuid`, ни `reporter_uuid`, ни текста
    переписки: решение — факт о жалобе, а адресаты уведомляются отдельно.
    `sanction_uuid = null` при `dismissed` — санкции не существует, и `null`
    отличает её от «санкция есть, адрес неизвестен».
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_uuid: uuid_lib.UUID
    status: ResolvedStatus
    resolution: Resolution
    sanction_uuid: uuid_lib.UUID | None = Field(
        default=None, description="Адрес санкции; `null` при `dismissed`"
    )
    resolved_at: datetime

    @field_serializer("resolved_at")
    def _serialize_resolved_at(self, value: datetime) -> str:
        """§4 project-context: время наружу — ISO-8601 UTC с `Z`."""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
