"""AC-02: публичная схема не отдаёт `id` и персональные поля."""

import uuid as uuid_lib
from datetime import UTC, datetime

from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.schemas.user import UserPublic


def _user() -> User:
    return User(
        id=42,
        uuid=uuid_lib.uuid4(),
        provider="phone",
        phone="+79001234567",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        created_at=datetime(2026, 10, 20, 19, 34, tzinfo=UTC),
    )


def test_public_schema_has_no_internal_id() -> None:
    """AC-02: сериализация не содержит `id` — проверка тестом, а не глазами."""
    payload = UserPublic.model_validate(_user()).model_dump()

    assert "id" not in payload
    assert set(payload) == {"uuid", "status", "role", "created_at"}


def test_public_schema_hides_personal_fields() -> None:
    """AC-02: телефон, внешний id провайдера и согласие на ПД наружу не уходят."""
    payload = UserPublic.model_validate(_user()).model_dump()

    for hidden in ("phone", "provider_user_id", "consent_given_at", "provider"):
        assert hidden not in payload


def test_public_schema_uuid_is_present() -> None:
    """AC-02: публичный идентификатор — `uuid`."""
    user = _user()

    payload = UserPublic.model_validate(user).model_dump()

    assert payload["uuid"] == user.uuid


def test_created_at_serialized_as_iso_z() -> None:
    """project-context §4: даты — ISO 8601 UTC с `Z`."""
    payload_json = UserPublic.model_validate(_user()).model_dump(mode="json")

    assert payload_json["created_at"] == "2026-10-20T19:34:00Z"
    assert payload_json["created_at"].endswith("Z")
