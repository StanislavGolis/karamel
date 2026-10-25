"""Приватность скрытого диалога (стори 8.2; AC-04, NFR33, ADR-13).

**Главное утверждение:** скрытый жалобой диалог для прямого запроса НЕ
существует — `404 chat_not_found`, не `403`. `403` подтвердил бы, что диалог
есть, и это раскрыло бы факт жалобы третьим лицам.

**Второе утверждение:** число SQL-запросов у «скрыт жалобой» и «не участник»
СОВПАДАЕТ. Разное число — тайминг-оракул (урок F-1 стори 6.3): по времени
ответа посторонний отличил бы «диалог есть, но скрыт» от «диалога нет».
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_message_helpers import (
    MessageChat,
    cookie_headers,
    make_client,
    make_message_chat,
    make_user,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


class _Counter:
    """Счётчик SQL через `before_cursor_execute` (образец — 6.4/6.6)."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1


async def _hidden_company(session: AsyncSession) -> MessageChat:
    """Компания, у которой диалог уже скрыт жалобой автора."""
    company = await make_message_chat(session)
    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )
        assert response.status_code == 201, response.text
    return company


@pytest.mark.parametrize("side", ["author", "member"])
async def test_hidden_chat_messages_give_404_for_both_sides(
    session: AsyncSession, side: str
) -> None:
    """AC-04: чтение истории скрытого диалога — `404` у ОБЕИХ сторон.

    Скрытие принадлежит чату (D-21), поэтому «у обоих» — свойство данных, а
    не двух проверок. Параметризация доказывает, что фильтр не привязан к
    роли зрителя.
    """
    company = await _hidden_company(session)
    token = company.author_token if side == "author" else company.member_token

    async with make_client(session) as client:
        response = await client.get(
            f"/api/v1/chats/{company.chat_uuid}/messages", headers=cookie_headers(token)
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "chat_not_found"


@pytest.mark.parametrize("side", ["author", "member"])
async def test_hidden_chat_send_message_gives_404(session: AsyncSession, side: str) -> None:
    """AC-04: отправка в скрытый диалог — `404`, сообщение не сохраняется."""
    company = await _hidden_company(session)
    token = company.author_token if side == "author" else company.member_token

    async with make_client(session) as client:
        response = await client.post(
            f"/api/v1/chats/{company.chat_uuid}/messages",
            json={"body": "ещё одно", "client_message_id": "00000000-0000-0000-0000-000000000001"},
            headers=cookie_headers(token),
        )

    assert response.status_code == 404, response.text
    count = await session.execute(text("SELECT count(*) FROM messages"))
    assert count.scalar_one() == 0, "сообщение сохранилось в скрытый диалог"


@pytest.mark.parametrize("side", ["author", "member"])
async def test_hidden_chat_mark_read_gives_404(session: AsyncSession, side: str) -> None:
    """AC-04: отметка прочтения скрытого диалога — `404`.

    Тот же гард (`_resolve_chat`), поэтому проверяется отдельно: три роута
    используют один вход видимости, и расхождение было бы регрессией
    (класс дефекта `_load_card`, CR 7.4 H-1).
    """
    company = await _hidden_company(session)
    token = company.author_token if side == "author" else company.member_token

    async with make_client(session) as client:
        response = await client.post(
            f"/api/v1/chats/{company.chat_uuid}/read", headers=cookie_headers(token)
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "chat_not_found"


async def test_hidden_and_not_participant_have_same_sql_count(session: AsyncSession) -> None:
    """AC-04, NFR33: число SQL у «скрыт» и «не участник» СОВПАДАЕТ.

    Анти-оракул: разное число запросов отличило бы по времени «диалог есть,
    но скрыт» от «диалога нет». Фильтр живёт в том же `WHERE`, а не вторым
    запросом (D-22) — именно это и проверяется счётчиком.
    """
    company = await _hidden_company(session)
    stranger, stranger_token = await make_user(session)

    async with make_client(session) as client:
        counter = _Counter()
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", counter)
        try:
            hidden = await client.get(
                f"/api/v1/chats/{company.chat_uuid}/messages",
                headers=cookie_headers(stranger_token),
            )
            hidden_count = counter.count

            counter.count = 0
            missing = await client.get(
                "/api/v1/chats/00000000-0000-0000-0000-00000000dead/messages",
                headers=cookie_headers(stranger_token),
            )
            missing_count = counter.count
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", counter)

    # Оба — `404`: посторонний не отличает скрытый чат от несуществующего.
    assert hidden.status_code == 404
    assert missing.status_code == 404
    assert hidden.json() == missing.json()
    # И число запросов одинаково — иначе различие видно по времени ответа.
    assert hidden_count == missing_count, (
        f"число SQL расходится: скрыт={hidden_count}, нет={missing_count} (тайминг-оракул)"
    )
