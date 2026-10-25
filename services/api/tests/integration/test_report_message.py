"""Жалоба на собеседника в диалоге (стори 8.2; AC-02, AC-03, AC-05).

Реальный PostgreSQL, схема поднята Alembic (`0026` добавила `chats.status`).
Главные утверждения:

- **AC-02**: `201` и РОВНО четыре поля ответа — по ключам, а не по «не упало»;
- **AC-03**: скрытие происходит ОДНОЙ строкой, поэтому у ОБЕИХ сторон: проверяется
  состояние в БД и отсутствие диалога в списках у автора и участника;
- **AC-05**: повтор даёт `409 ALREADY_REPORTED`, и код ОТЛИЧАЕТСЯ от кода 7.4:
  клиенту нужен разный текст про разные объекты.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_message_helpers import (
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


async def _status(session: AsyncSession, chat_uuid: str) -> str:
    result = await session.execute(
        text("SELECT status FROM chats WHERE uuid = :u"), {"u": chat_uuid}
    )
    return str(result.scalar_one())


async def _report_row(session: AsyncSession) -> tuple[str, str, object, object]:
    """`(target_type, status, chat_id, attendance_id)`. Ровно одна строка."""
    result = await session.execute(
        text("SELECT target_type, status, chat_id, attendance_id FROM reports")
    )
    rows = result.all()
    assert len(rows) == 1, f"ожидалась одна жалоба, получено {len(rows)}"
    return str(rows[0][0]), str(rows[0][1]), rows[0][2], rows[0][3]


async def test_author_reports_member_creates_report_and_hides_chat(session: AsyncSession) -> None:
    """AC-02, AC-03: `201`, четыре поля, диалог скрыт у обеих сторон."""
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    assert response.status_code == 201, response.text
    body = response.json()
    # AC-02: РОВНО четыре поля — равенство ключей, а не подмножество.
    assert set(body) == {"uuid", "target_type", "status", "reported_at"}
    assert body["target_type"] == "message"
    assert body["status"] == "open"

    # AC-03: скрытие — одна строка чата, поэтому у ОБЕИХ сторон.
    assert await _status(session, company.chat_uuid) == "hidden"

    target_type, status, chat_id, attendance_id = await _report_row(session)
    assert target_type == "message"
    assert status == "open"
    # D-15: у жалобы на диалог `attendance_id` и `event_card_id` пусты.
    assert attendance_id is None
    assert chat_id is not None


async def test_member_reports_author(session: AsyncSession) -> None:
    """AC-02: вторая сторона тоже может жаловаться (симметрия FR37)."""
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.member_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.author.uuid,
        )

    assert response.status_code == 201, response.text
    assert await _status(session, company.chat_uuid) == "hidden"


async def test_hidden_chat_disappears_from_both_lists(session: AsyncSession) -> None:
    """AC-03: список чатов не содержит скрытый диалог НИ У ОДНОЙ стороны.

    Проверка идёт через HTTP, а не через предикат напрямую: смысл AC — что
    участник не видит диалог, а не что SQL содержит условие.
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        # До жалобы диалог есть у обеих сторон.
        for token in (company.author_token, company.member_token):
            listing = await client.get("/api/v1/chats", headers=cookie_headers(token))
            assert listing.status_code == 200
            assert company.chat_uuid in [i["chat_uuid"] for i in listing.json()["items"]]

        await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

        # После жалобы — ни у автора, ни у участника.
        for token in (company.author_token, company.member_token):
            listing = await client.get("/api/v1/chats", headers=cookie_headers(token))
            assert listing.status_code == 200
            items = [i["chat_uuid"] for i in listing.json()["items"]]
            assert company.chat_uuid not in items, "скрытый диалог остался в списке"


async def test_repeat_report_gives_409_and_one_row(session: AsyncSession) -> None:
    """AC-05: повтор — `409 ALREADY_REPORTED`, жалоба ОДНА.

    Код отличается от `ATTENDANCE_ALREADY_REPORTED` (7.4): разные объекты,
    и клиенту нужен разный текст. Проверяется именно значение кода.
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        first = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )
        assert first.status_code == 201

        second = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "ALREADY_REPORTED"
    assert second.json()["code"] != "ATTENDANCE_ALREADY_REPORTED"

    count = await session.execute(text("SELECT count(*) FROM reports"))
    assert count.scalar_one() == 1, "повтор создал вторую жалобу"


async def test_other_side_can_still_report_after_first(session: AsyncSession) -> None:
    """AC-05: `UNIQUE (chat_id, reporter_id)` — на ЖАЛОБЩИКА, не на диалог.

    Вторая сторона жалуется своей строкой: `409` у одного не блокирует
    другого. Проверяется, что ограничение не «одна жалоба на диалог».
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        first = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )
        assert first.status_code == 201

        second = await post_report(
            client,
            token=company.member_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.author.uuid,
        )

    assert second.status_code == 201, second.text
    count = await session.execute(text("SELECT count(*) FROM reports"))
    assert count.scalar_one() == 2


async def test_no_show_target_type_rejected_with_422(session: AsyncSession) -> None:
    """AC-02: `target_type='no_show'` на `/reports` — `422` (D-23).

    У неявки свой роут (7.4), и `Literal["message"]` делает это отказом
    СХЕМЫ, до всякой работы с БД.
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
            target_type="no_show",
        )

    assert response.status_code == 422, response.text
    # Скрытия не произошло: запрос отвергнут до записи.
    assert await _status(session, company.chat_uuid) == "active"


async def test_extra_field_in_body_rejected(session: AsyncSession) -> None:
    """AC-02: лишнее поле — `422`, а не тихое игнорирование.

    Клиент не должен думать, что передал текст жалобы, которого API не
    принимает (тот же довод, что X-7.4-3 в 7.4).
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await client.post(
            "/api/v1/reports",
            json={
                "target_type": "message",
                "chat_uuid": company.chat_uuid,
                "accused_uuid": str(company.member.uuid),
                "reason": "он грубил",
            },
            headers=cookie_headers(company.author_token),
        )

    assert response.status_code == 422, response.text


async def test_stranger_cannot_report(session: AsyncSession) -> None:
    """AC-02, ADR-13: посторонний — `404 card_not_found`, не `403`.

    `403` подтвердил бы существование диалога.
    """
    company = await make_message_chat(session)
    stranger, stranger_token = await make_user(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=stranger_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"
    assert await _status(session, company.chat_uuid) == "active"
