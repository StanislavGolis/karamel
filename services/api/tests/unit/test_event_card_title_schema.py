"""Стори 13.2 (AC-6, AC-7): `title` не вводится и не редактируется автором.

ADR-26: поле остаётся в БД и в ответе, но автор его не присылает. Проверяется
на уровне СХЕМЫ, а не сервиса: `422` обязан приходить от Pydantic, до
обращения к БД, иначе клиент получит `500` на середине транзакции.

`extra="forbid"` уже стоит на обеих моделях — отдельный доменный класс ошибки
не нужен. Это важно: класс дефекта 12.5 (доменная ошибка без ветки в
`_domain_error` → `500` вместо `422`) здесь не возникает вовсе.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.enums import Activity, CardKind, PlaceCategory
from app.schemas.event_card import EventCardCreate, EventCardUpdate


def _payload(**overrides: object) -> dict[str, object]:
    """Минимальный валидный вход создания карточки без `title`."""
    base: dict[str, object] = {
        "description": None,
        "category": PlaceCategory.CAFE,
        "place_uuid": uuid_lib.uuid4(),
        "starts_at": datetime.now(UTC) + timedelta(hours=5),
        "kind": CardKind.DATE,
        "activity": Activity.COFFEE,
        "capacity": 1,
        # Стори 12.4: при `capacity=1` окно решения обязательно (FR79) —
        # схема отвергает вход без него. Первая редакция payload его не
        # передавала, и тест падал на правиле, не связанном с `title`.
        "decision_window_minutes": 120,
    }
    base.update(overrides)
    return base


class TestCreateRejectsTitle:
    """AC-7: заголовок не вводится автором."""

    def test_title_absent_is_valid(self) -> None:
        model = EventCardCreate(**_payload())
        assert model.title is None

    def test_title_sent_is_rejected(self) -> None:
        """`422`, а не молчаливый выброс.

        Докстринг `EventCardUpdate` фиксирует конвенцию: лишнее поле — ошибка,
        чтобы клиент не считал, что опубликовал карточку. Здесь то же правило.
        """
        with pytest.raises(ValidationError) as exc:
            EventCardCreate(**_payload(title="Своё название"))
        assert "title" in str(exc.value)

    def test_title_none_explicitly_is_rejected(self) -> None:
        """Явный `title: null` — тоже попытка управлять полем."""
        with pytest.raises(ValidationError):
            EventCardCreate(**_payload(title=None))


class TestLegacyOther:
    """AC-6: `other` — легаси, но схема создания и так его отвергает.

    `Activity.OTHER` не проходит `_no_legacy_activity`. Значит ветка «`other`
    требует `title`» достижима только из сервиса (импорт/воркер), не из HTTP.
    Тест фиксирует это поведение, чтобы отказ не «потерялся» при правках.
    """

    def test_other_activity_rejected_by_schema(self) -> None:
        with pytest.raises(ValidationError):
            EventCardCreate(**_payload(activity=Activity.OTHER))


class TestUpdateRejectsTitle:
    """AC-7: заголовок не редактируется автором."""

    def test_title_field_absent_from_model(self) -> None:
        """Поля нет в модели вовсе — не «есть, но игнорируется»."""
        assert "title" not in EventCardUpdate.model_fields

    def test_title_in_patch_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventCardUpdate(title="Новое название")

    def test_other_fields_still_accepted(self) -> None:
        """Отсутствие `title` не должно сузить остальной PATCH."""
        model = EventCardUpdate(description="Новое описание")
        assert model.description == "Новое описание"
