"""Аудит доступа к карточке пользователя (стори 8.6; AC-08, D-86).

**Почему карточка тоже аудируется.** D-81 отложил это «если 8.6 введёт общий
механизм»; механизм введён, и добавить второй `action` дешевле, чем потом
искать, почему история доступов неполна. Карточка показывает историю
нарушений — персональные данные того же класса, что переписка.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_user_card_helpers import (
    build_client,
    get_card,
    make_moderator,
    make_target_user,
    set_profile,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def test_card_read_writes_card_action_with_subject(session: AsyncSession) -> None:
    """AC-08: `action='card_read'`, `subject_uuid` заполнен, остальные NULL."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    moderator, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text

    result = await session.execute(
        text(
            "SELECT action, moderator_uuid::text, subject_uuid::text, chat_uuid, "
            "       card_uuid, report_uuid, message_count, route "
            "  FROM access_logs"
        )
    )
    rows = result.all()
    assert len(rows) == 1, rows
    action, moderator_uuid, subject_uuid, chat_uuid, card_uuid, report_uuid, count, route = rows[0]
    assert action == "card_read"
    assert moderator_uuid == str(moderator.uuid)
    assert subject_uuid == str(target.uuid)
    # Карточка не раскрывает переписку — эти поля обязаны быть пустыми.
    assert chat_uuid is None and card_uuid is None and report_uuid is None
    assert count is None
    assert route is not None and route.endswith(str(target.uuid)), route
