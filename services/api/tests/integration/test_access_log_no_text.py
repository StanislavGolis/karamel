"""Текста в аудите нет — по построению (стори 8.6; AC-02, D-91).

**Два независимых способа проверки.**

1. **Allowlist колонок** таблицы: сравнение МНОЖЕСТВА имён с ожидаемым.
   Поиск подстроки (`assert "body" not in columns`) не поймал бы лишнюю
   колонку `content`/`text`/`payload` — тест был бы зелёным, утечка есть.
2. **`capture_logs()`** на отсутствие тела сообщения и `reason` в событиях.

**Почему не `caplog`** (D-63): `structlog` не пишет в стандартный
`logging`-handler напрямую — в CR 8.4 `caplog.records` был ПУСТ, и тест
приватности оказался слепым зелёным. `capture_logs()` пишет до форматтеров.
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
    make_moderator,
    seed_messages,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-02, D-91: РОВНО эти колонки. Дополнение — утечка.
EXPECTED_COLUMNS = {
    "id",
    "uuid",
    "moderator_uuid",
    "action",
    "subject_uuid",
    "report_uuid",
    "chat_uuid",
    "card_uuid",
    "message_count",
    "created_at",
    "route",
}


async def test_access_logs_columns_allowlist(session: AsyncSession) -> None:
    """AC-02, D-91: в таблице НЕТ колонки для текста — allowlist."""
    result = await session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'access_logs'")
    )
    actual = {row[0] for row in result.all()}
    assert actual == EXPECTED_COLUMNS, actual ^ EXPECTED_COLUMNS


async def test_serialized_logs_have_no_message_text(session: AsyncSession) -> None:
    """AC-02, NFR27: в событиях лога нет тела сообщения и причины.

    Проверяется текст, введённый в диалог: он существует и не пуст, иначе
    проверка ниже прошла бы на пустой строке. **Контроль присутствия:**
    событие `moderation.fragment.read` обязано быть — иначе тест доказывал бы
    отсутствие текста в пустом списке событий.
    """
    from structlog.testing import capture_logs

    secret_body = "СекретноеТело42"
    report = await make_message_report(session)
    await seed_messages(
        session,
        chat_id=report["chat_id"],
        sender_id=report["chat"].author.id,
        bodies=[secret_body],
    )
    _, token = await make_moderator(session)

    with capture_logs() as captured:
        async with build_client(session) as client:
            response = await client.get(
                f"/api/v1/moderation/reports/{report['report_uuid']}/fragment",
                headers=cookie_headers(token),
            )
            assert response.status_code == 200, response.text
            # Тело реально ушло модератору — иначе секрет нечего было бы прятать.
            assert secret_body in response.text, response.text

    joined = " ".join(str(entry) for entry in captured)
    assert any(entry.get("event") == "moderation.fragment.read" for entry in captured), (
        "событие аудита не перехвачено — тест ничего не проверяет"
    )
    assert secret_body not in joined, joined
    assert "reason" not in joined, joined
