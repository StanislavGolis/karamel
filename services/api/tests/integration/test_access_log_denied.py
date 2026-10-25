"""`403` пишется в лог, но НЕ в таблицу (стори 8.6; AC-04, D-85).

**Почему отказ не идёт в `access_logs`.** Строка на отказ создала бы вектор
усиления записи: любой пользователь с валидной сессией набивал бы таблицу
запросами, которые никогда не раскроют данных. Плюс в записи нет
`chat_uuid`/`card_uuid` для неудавшегося запроса — она описывает доступ,
которого не было.

**Два утверждения в одном тесте.** (1) событие `moderation.access_denied`
есть — иначе AC-04 не выполнен; (2) строк в таблице ноль — иначе появился
бы вектор усиления. По отдельности каждое зелёное при нарушении другого.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_reports_helpers import (
    build_client,
    cookie_headers,
    make_message_report,
    make_plain_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _rows(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM access_logs"))
    return int(result.scalar_one())


async def test_denied_fragment_read_logs_but_does_not_write_row(session: AsyncSession) -> None:
    """AC-04, D-85: обычный пользователь — `403`, событие есть, строк нет."""
    from structlog.testing import capture_logs

    report = await make_message_report(session)
    plain, plain_token = await make_plain_user(session)

    with capture_logs() as captured:
        async with build_client(session) as client:
            response = await client.get(
                f"/api/v1/moderation/reports/{report['report_uuid']}/fragment",
                headers=cookie_headers(plain_token),
            )
            assert response.status_code == 403, response.text

    denied = [entry for entry in captured if entry.get("event") == "moderation.access_denied"]
    assert len(denied) == 1, captured
    assert denied[0]["user_uuid"] == str(plain.uuid)
    # Путь запроса — не пустой: по нему разбирают инцидент.
    assert denied[0]["route"].endswith("/fragment"), denied[0]
    # AC-04: в хранилище доступа НЕТ — отказа не было доступа.
    assert await _rows(session) == 0


async def test_denied_card_read_logs_but_does_not_write_row(session: AsyncSession) -> None:
    """AC-04, D-86: тот же контракт для карточки пользователя."""
    from structlog.testing import capture_logs

    target, _ = await make_plain_user(session)
    _, plain_token = await make_plain_user(session)

    with capture_logs() as captured:
        async with build_client(session) as client:
            response = await client.get(
                f"/api/v1/moderation/users/{target.uuid}", headers=cookie_headers(plain_token)
            )
            assert response.status_code == 403, response.text

    denied = [entry for entry in captured if entry.get("event") == "moderation.access_denied"]
    assert len(denied) == 1, captured
    assert await _rows(session) == 0
