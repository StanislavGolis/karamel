"""Публичные схемы пользователя (Task 5.3, 5.4).

AC-02: наружу отдаётся только `uuid`. `id`, `phone`, `provider_user_id`,
`consent_given_at` — внутренние или персональные поля, они не уходят в ответ.
Тест `tests/unit/test_user_schema.py` проверяет отсутствие ключа `id` в сериализации,
чтобы регрессия ловилась автоматически, а не на ревью.
"""

import uuid as uuid_lib
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.enums import UserRole, UserStatus


class UserPublic(BaseModel):
    """AC-02: публичное представление учётной записи. Поля `id` здесь нет и не будет."""

    model_config = ConfigDict(from_attributes=True)

    uuid: uuid_lib.UUID
    # Типизированы enum-ами, а не str: строка приняла бы `activ` и отдала бы клиенту
    # невалидное значение, которое прошло сериализацию и упало бы на фронте.
    status: UserStatus
    role: UserRole
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        """project-context §4: даты — ISO 8601 UTC с `Z`, например `2026-10-20T19:34:00Z`.

        Naive datetime трактуется как UTC: `astimezone` без tzinfo взял бы зону процесса,
        и один и тот же объект сериализовался бы по-разному на сервере и в тестах.
        """
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
