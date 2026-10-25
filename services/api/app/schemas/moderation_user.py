"""Схемы карточки пользователя для модератора (стори 8.5; AC-01…AC-10).

**`ModerationUserProfile` — allowlist, а не denylist** (D-66, NFR27).
Шесть полей, и ни одного больше. `phone`, `provider_user_id`, фото, `bio`,
`deletion_requested_at` в карточку не входят: модератору для разбора нужны
имя, город, возраст и история нарушений, а идентификаторы входа — это ключ
к аккаунту, а не основание для решения. `extra="forbid"` ставит запрет на
уровне схемы: лишний ключ в конструкторе — ошибка, а не молчаливое
расширение доступа.

**Почему `status` здесь, хотя он в `users`, а не в `profiles`.** Карточка —
агрегат двух таблиц; отдавать её двумя объектами заставило бы клиент
склеивать их по `uuid`, а модератору нужно одно поле в одном месте.

**`ModerationUserCard` — четыре блока, не плоский объект** (AC-01). Жалобы,
санкции и явки — разные домены с разными правилами усечения (`20` против
`10`, DS 8.5 D-72), и плоский ответ потерял бы это различие.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.schemas.report import Resolution, ResolvedStatus

__all__ = [
    "AttendanceBlock",
    "AttendanceBrief",
    "ModerationUserCard",
    "ModerationUserProfile",
    "ReportBrief",
    "ReportsBlock",
    "SanctionBrief",
    "SanctionsBlock",
]


class ModerationUserProfile(BaseModel):
    """AC-05, D-66: ровно шесть полей. Лишнее — ошибка схемы, не расширение.

    `name`/`city`/`age`/`gender` — `None` у пользователя без анкеты (AC-14),
    поэтому все четыре опциональны, а не только `name`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: uuid_lib.UUID = Field(description="Публичный адрес пользователя")
    name: str | None = Field(default=None, description="Имя из анкеты")
    city: str | None = Field(default=None, description="Город из анкеты")
    age: int | None = Field(default=None, description="Возраст из анкеты")
    gender: Literal["female", "male"] | None = Field(default=None, description="Пол из анкеты")
    status: Literal["active", "warned", "banned"] = Field(
        description="Статус учётной записи без переименований (D-67)"
    )


class ReportBrief(BaseModel):
    """AC-06: одна жалоба в карточке.

    **Чего здесь НЕТ.** Ни `reporter_uuid`, ни `accused_uuid`, ни `reason`:
    карточка показывает факт и исход жалобы, а участники и причина читаются
    в разборе (8.3/8.4). `reason` не входит и потому, что у `open`-жалобы его
    не существует, и разнобой «иногда есть, иногда нет» сбил бы клиент.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: uuid_lib.UUID = Field(description="Публичный адрес жалобы")
    target_type: Literal["no_show", "message"] = Field(description="Объект жалобы")
    status: Literal["open", "resolved_warning", "resolved_ban", "dismissed"] = Field(
        description="Состояние разбора"
    )
    resolution: Resolution | None = Field(
        default=None, description="Исход; `null` у `open`-жалобы (D-16)"
    )
    reported_at: datetime
    resolved_at: datetime | None = Field(default=None, description="`null` у `open`")

    @field_serializer("reported_at")
    def _serialize_reported_at(self, value: datetime) -> str:
        """§4 project-context: время наружу — ISO-8601 UTC с `Z`."""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_serializer("resolved_at")
    def _serialize_resolved_at(self, value: datetime | None) -> str | None:
        """`None` остаётся `None`: «разбор не начат» — не момент времени."""
        return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ReportsBlock(BaseModel):
    """AC-01, AC-06, AC-10: две оси жалоб + ПОЛНЫЕ счётчики.

    `filed` — жалобы пользователя, `against` — жалобы на него (FR51: «с
    результатом каждой» — результат это `status`+`resolution` в `ReportBrief`).
    Списки усечены `CARD_LIST_LIMIT`, `counts` — полные числа (AC-10):
    усечённый счётчик занизил бы нагрузку на пользователя, и модератор
    недооценил бы серию.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    filed: list[ReportBrief] = Field(description="Жалобы, поданные пользователем")
    against: list[ReportBrief] = Field(description="Жалобы на пользователя")
    filed_total: int = Field(description="Полное число поданных жалоб")
    against_total: int = Field(description="Полное число жалоб на пользователя")


class SanctionBrief(BaseModel):
    """AC-07: одна санкция.

    `moderator_uuid = null` — модератор удалён (`ON DELETE SET NULL`, D-17):
    санкция остаётся фактом, а её автор — уже нет. `null` здесь не «неизвестно»,
    а именно «аккаунта больше нет»; другого источника для этого значения нет.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: uuid_lib.UUID = Field(description="Публичный адрес санкции")
    type: Literal["warning", "ban"] = Field(description="Вид санкции")
    reason: str = Field(description="Причина, как её видит обвиняемый (FR39)")
    created_at: datetime
    moderator_uuid: uuid_lib.UUID | None = Field(
        default=None, description="`null`, если аккаунт модератора удалён"
    )

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        """§4 project-context: время наружу — ISO-8601 UTC с `Z`."""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class SanctionsBlock(BaseModel):
    """AC-01, AC-07, AC-10: санкции + полный счётчик."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[SanctionBrief]
    total: int = Field(description="Полное число санкций")


class AttendanceBrief(BaseModel):
    """AC-01, D-69: один факт явки в сводке.

    Только то, что нужно для «как человек ведёт себя на встречах»: статус пары
    и момент встречи. Флаги `author_confirmed`/`participant_confirmed` живут в
    очереди жалоб 8.3 — там они нужны для решения по КОНКРЕТНОЙ жалобе;
    в сводке они превратили бы десять строк в сорок полей.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_uuid: uuid_lib.UUID = Field(description="Публичный адрес карточки встречи")
    status: Literal["pending", "confirmed", "no_show", "disputed"] = Field(
        description="Состояние пары"
    )
    starts_at: datetime

    @field_serializer("starts_at")
    def _serialize_starts_at(self, value: datetime) -> str:
        """§4 project-context: время наружу — ISO-8601 UTC с `Z`."""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AttendanceBlock(BaseModel):
    """AC-01, D-69: последние 10 явок + полный счётчик.

    `total` — полное число, а не длина списка: пагинируемая история остаётся
    на `GET /moderation/users/{uuid}/attendance` (7.5), и `total` здесь —
    единственный способ увидеть объём, не открывая второй экран.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AttendanceBrief]
    total: int = Field(description="Полное число фактов явки")


class ModerationUserCard(BaseModel):
    """AC-01, FR54: сводка пользователя — четыре блока.

    Литерал `ResolvedStatus` импортирован, но не используется как поле: он
    нужен `ReportBrief.status`, и импорт держит одну форму домена на два
    модуля (8.4 и 8.5) вместо второй копии.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ModerationUserProfile
    reports: ReportsBlock
    sanctions: SanctionsBlock
    attendance: AttendanceBlock


#: Доменный алиас для сериализации статусов разбора в одном месте.
#: `ResolvedStatus` реэкспортируется, чтобы карточка и решение (8.4) не
#: разошлись в наборе значений при добавлении нового исхода.
CardReportStatus = ResolvedStatus
