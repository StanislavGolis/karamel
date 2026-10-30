"""Схемы карточки: allowlist полей и формат времени (стори 8.5; AC-05, §4).

**Почему unit, а не integration.** Здесь проверяется СХЕМА, а не маршрут:
`extra="forbid"` отбивает лишнее поле в конструкторе, и это свойство
Pydantic, не требующее БД.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.moderation_user import ModerationUserProfile


def test_profile_rejects_extra_field() -> None:
    """AC-05, D-66: лишнее поле в профиле — ошибка, а не расширение ответа.

    Без `extra="forbid"` поле, добавленное в конструктор, молча уехало бы в
    JSON: `NFR27` требует минимума, и «лишний ключ» это утечка.
    """
    with pytest.raises(ValidationError):
        ModerationUserProfile(
            uuid=uuid_lib.uuid4(),
            status="active",
            phone="+79001234567",
        )


def test_profile_accepts_null_optional_fields() -> None:
    """AC-14: пользователь без анкеты — четыре поля `None`, `status` есть.

    Стори 11.4 (CR P9) добавила седьмое поле `consent_version` **без дефолта**:
    пропуск поля в конструкторе — `TypeError`, а не молчаливый `null`,
    неотличимый от честного «версия неизвестна». Поэтому здесь оно передаётся
    явно: у учётки без согласия версия именно `None`.
    """
    profile = ModerationUserProfile(
        uuid=uuid_lib.uuid4(), status="banned", consent_version=None
    )
    assert profile.name is None
    assert profile.city is None
    assert profile.age is None
    assert profile.gender is None
    assert profile.status == "banned"
    assert profile.consent_version is None


def test_profile_rejects_unknown_status() -> None:
    """AC-09, D-67: домен статуса закрыт; `blocked` — не синоним `banned`."""
    with pytest.raises(ValidationError):
        ModerationUserProfile(uuid=uuid_lib.uuid4(), status="blocked")


def test_timestamps_are_serialized_with_z() -> None:
    """§4 project-context: время наружу — ISO-8601 UTC с `Z`.

    Контроль в обе стороны: суффикс `Z` есть, смещения `+00:00` нет. Проверка
    только на `Z` прошла бы и при строке с обоими суффиксами.
    """
    from app.schemas.moderation_user import AttendanceBrief

    moment = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    brief = AttendanceBrief(card_uuid=uuid_lib.uuid4(), status="confirmed", starts_at=moment)
    serialized = brief.model_dump(mode="json")["starts_at"]
    assert serialized.endswith("Z"), serialized
    assert "+00:00" not in serialized, serialized
    assert serialized == "2026-10-25T12:00:00Z", serialized
