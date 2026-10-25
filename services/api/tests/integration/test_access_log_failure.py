"""Упавшее чтение не оставляет записи аудита (стори 8.6; AC-05, D-90).

**Почему это критично.** Аудит фиксирует СОСТОЯВШИЙСЯ доступ. Строка,
записанная до расшифровки, утверждала бы доступ, которого не было, — и при
разборе инцидента модератор «оказывался» читавшим переписку, которую он не
видел.

**Приём из 8.3** (`test_no_access_log_when_decryption_fails`): блоб
шифруется ЧУЖИМ ключом, `InvalidTag` рвёт чтение. `ASGITransport`
пробрасывает необработанное исключение — это и проверяется: ложный `200` с
мусором в теле был бы хуже падения (NFR31).
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
import structlog
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_message_body
from tests.integration.moderation_reports_helpers import (
    build_client,
    cookie_headers,
    make_message_report,
    make_moderator,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def test_failed_decryption_leaves_no_audit_row(session: AsyncSession) -> None:
    """AC-05: расшифровка упала → `access_logs` пуста, события нет."""
    _, token = await make_moderator(session)
    seeded = await make_message_report(session)
    sender = seeded["chat"].author.id
    blob = encrypt_message_body(
        "чужой ключ", chat_id=seeded["chat_id"], sender_id=sender, key="0" * 64
    )
    await session.execute(
        text("INSERT INTO messages (uuid, chat_id, sender_id, body) VALUES (:u, :c, :s, :b)"),
        {"u": uuid_lib.uuid4(), "c": seeded["chat_id"], "s": sender, "b": blob},
    )
    await session.flush()

    with structlog.testing.capture_logs() as logs:
        async with build_client(session) as client:
            with pytest.raises(InvalidTag):
                await client.get(
                    f"/api/v1/moderation/reports/{seeded['report_uuid']}/fragment",
                    headers=cookie_headers(token),
                )

    events = [entry for entry in logs if entry.get("event") == "moderation.fragment.read"]
    assert events == [], f"лог записан при упавшей расшифровке: {events}"
    # AC-05: главное — в ХРАНИЛИЩЕ строки нет, а не только в логе.
    count = await session.execute(text("SELECT count(*) FROM access_logs"))
    assert int(count.scalar_one()) == 0
