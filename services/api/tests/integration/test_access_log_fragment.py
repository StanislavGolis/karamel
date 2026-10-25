"""Аудит доступа к переписке (стори 8.6; AC-01, AC-03, AC-09).

**Тест идёт через СЕРВИС** (`read_fragment` → роут), не прямым `INSERT` в
`access_logs`: иначе мутация «убрать запись аудита» осталась бы зелёной —
урок M11 из DS 8.5 (D-77), где тест на прямой вставке не проверял AC.

**Регресс 8.3 входит сюда же:** строка `logger.info("moderation.fragment.read")`
остаётся, тесты 8.3 (`test_access_log_has_ids_and_no_message_text`,
`test_no_access_log_when_decryption_fails`) обязаны остаться зелёными.
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


async def _access_logs(
    session: AsyncSession,
) -> list[tuple[str, str, int | None, str | None, str | None]]:
    """Все строки аудита: `(action, moderator_uuid, message_count, chat_uuid, report_uuid)`."""
    result = await session.execute(
        text(
            "SELECT action, moderator_uuid::text, message_count, chat_uuid::text, "
            "       report_uuid::text "
            "  FROM access_logs ORDER BY id"
        )
    )
    return [(r[0], r[1], r[2], r[3], r[4]) for r in result.all()]


async def _read_fragment(client, *, token: str, report_uuid: object):
    return await client.get(
        f"/api/v1/moderation/reports/{report_uuid}/fragment",
        headers=cookie_headers(token),
    )


async def test_fragment_read_creates_one_audit_row(session: AsyncSession) -> None:
    """AC-01: одно открытие фрагмента → ровно одна строка `fragment_read`."""
    report = await make_message_report(session)
    await seed_messages(
        session,
        chat_id=report["chat_id"],
        sender_id=report["chat"].author.id,
        bodies=["a", "b", "c"],
    )
    moderator, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await _read_fragment(client, token=token, report_uuid=report["report_uuid"])
        assert response.status_code == 200, response.text

    rows = await _access_logs(session)
    assert len(rows) == 1, rows
    action, moderator_uuid, message_count, chat_uuid, report_uuid = rows[0]
    assert action == "fragment_read"
    assert moderator_uuid == str(moderator.uuid)
    assert chat_uuid == str(report["chat_uuid"])
    assert report_uuid == str(report["report_uuid"])
    # AC-03: ФАКТИЧЕСКИ отданное число, не FRAGMENT_LIMIT.
    assert message_count == 3, message_count


async def test_message_count_is_capped_by_fragment_limit(session: AsyncSession) -> None:
    """AC-03: 12 сообщений → в аудите 10 (реально отданное), не 12 и не лимит.

    Проверяются ОБА значения (§18.11): 3 (меньше лимита) и 10 (ровно лимит).
    Тест только на «меньше» прошёл бы и при `min(len, 10)` вместо `len`.
    """
    report = await make_message_report(session)
    await seed_messages(
        session,
        chat_id=report["chat_id"],
        sender_id=report["chat"].author.id,
        bodies=[f"msg{i}" for i in range(12)],
    )
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await _read_fragment(client, token=token, report_uuid=report["report_uuid"])
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["message_count"] == 10
    rows = await _access_logs(session)
    assert rows[0][2] == 10, rows


async def test_two_reads_create_two_rows(session: AsyncSession) -> None:
    """AC-09, D-93: два открытия — ДВЕ строки. Идемпотентности нет.

    Бизнес-ключа у аудита нет: законное второе открытие неотличимо от
    ретрая. Дедупликация склеила бы два реальных доступа в один факт.
    """
    report = await make_message_report(session)
    await seed_messages(
        session, chat_id=report["chat_id"], sender_id=report["chat"].author.id, bodies=["a"]
    )
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        first = await _read_fragment(client, token=token, report_uuid=report["report_uuid"])
        assert first.status_code == 200, first.text
        second = await _read_fragment(client, token=token, report_uuid=report["report_uuid"])
        assert second.status_code == 200, second.text

    rows = await _access_logs(session)
    assert len(rows) == 2, rows
    assert all(row[0] == "fragment_read" for row in rows)


async def test_audit_row_survives_chat_deletion(session: AsyncSession) -> None:
    """AC-01, D-92: `chat_uuid` НЕ обнуляется при удалении чата.

    **Это и есть смысл «ни одного FK»** — и до CR 8.6 следствие решения не
    проверялось ни одним тестом: `test_upgrade_creates_table_and_indexes`
    ловил наличие FK, но не проверял, что запись остаётся полноценной после
    удаления объекта. С `ON DELETE SET NULL` строка пережила бы удаление, но
    потеряла бы `chat_uuid`, и на вопрос «к какому диалогу был доступ»
    ответить стало бы нельзя (NFR32).

    **Почему проверка через сервис, а не прямым INSERT:** строка создаётся
    реальным чтением фрагмента — иначе тест не доказывал бы, что именно
    ПИШЕТСЯ при доступе.
    """
    report = await make_message_report(session)
    await seed_messages(
        session, chat_id=report["chat_id"], sender_id=report["chat"].author.id, bodies=["a"]
    )
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await _read_fragment(client, token=token, report_uuid=report["report_uuid"])
        assert response.status_code == 200, response.text

    before = await _access_logs(session)
    assert len(before) == 1, before
    assert before[0][3] == str(report["chat_uuid"])

    # Удаляем диалог — ровно то, что с `ON DELETE SET NULL` обнулило бы ссылку.
    await session.execute(text("DELETE FROM chats WHERE id = :id"), {"id": report["chat_id"]})
    await session.flush()

    after = await _access_logs(session)
    assert len(after) == 1, "строка аудита исчезла вместе с чатом"
    assert after[0][3] == str(report["chat_uuid"]), (
        f"chat_uuid обнулён при удалении чата: было {before[0][3]}, стало {after[0][3]}"
    )
