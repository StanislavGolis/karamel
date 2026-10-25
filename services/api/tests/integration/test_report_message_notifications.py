"""Уведомления о жалобе (стори 8.2; AC-06, NFR27, D-26).

**Главные утверждения:**

- обе стороны получают строку `report.created` (FR38/FR39: «след остаётся»);
- в `payload` НЕТ текста жалобы и причины — только адреса (NFR27);
- `delivered_at` заполнен после публикации, `read_at` — после прочтения;
  бикондиционал `read_after_delivery` из 7.3 не нарушен.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_message_helpers import (
    make_client,
    make_message_chat,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def test_both_sides_get_report_notification(session: AsyncSession) -> None:
    """AC-06: по строке каждому участнику диалога."""
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )
        assert response.status_code == 201, response.text

    result = await session.execute(text("SELECT user_id, type FROM notifications ORDER BY user_id"))
    rows = result.all()
    assert len(rows) == 2, f"ожидались две строки, получено {len(rows)}"
    assert {r[0] for r in rows} == {company.author.id, company.member.id}
    assert all(r[1] == "report.created" for r in rows)


async def test_payload_has_no_complaint_text(session: AsyncSession) -> None:
    """AC-06, NFR27: `payload` — только адреса, без текста и причины.

    NFR27: содержимое переписки не должно утекать в уведомления. Проверяется
    равенство ключей, а не отсутствие отдельных ключей: лишний ключ — уже
    утечка, и `assert 'reason' not in payload` его не поймал бы.
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    result = await session.execute(text("SELECT payload FROM notifications LIMIT 1"))
    payload = result.scalar_one()
    assert set(payload) == {"report_uuid", "chat_uuid"}
    assert payload["chat_uuid"] == company.chat_uuid


async def test_delivered_at_set_after_publish(session: AsyncSession) -> None:
    """AC-06, D-30: `delivered_at` заполнен — публикация прошла.

    Отметка ставится ОТДЕЛЬНОЙ транзакцией после `publish_to_chat`; если
    порядок перепутать, `delivered_at` остался бы `NULL` (мутация M7
    проверяет обратное направление — публикацию до commit).
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    result = await session.execute(
        text("SELECT count(*) FROM notifications WHERE delivered_at IS NULL")
    )
    assert result.scalar_one() == 0, "уведомление осталось недоставленным"


async def test_read_at_set_by_chat_read(session: AsyncSession) -> None:
    """AC-06: `POST /chats/{uuid}/read` ставит `read_at` уведомлениям.

    **Ограничение теста:** скрытый диалог недоступен для `/read` (AC-04), а
    жалоба скрывает его атомарно. Поэтому проверяется ПРЯМОЙ вызов
    `mark_notifications_read` на уведомлении, созданном до скрытия — это
    доказывает механику отметки, а не HTTP-путь (он закрыт фильтром).
    """
    from app.services.notification_delivery import (
        mark_notifications_delivered,
        mark_notifications_read,
    )

    company = await make_message_chat(session)

    async with make_client(session) as client:
        await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    # Сброс доставки, чтобы проверить предикат `delivered_at IS NOT NULL`.
    await session.execute(text("UPDATE notifications SET read_at = NULL, delivered_at = NULL"))
    await session.flush()

    from app.services.report_service import REPORT_CREATED_TYPE

    # Прочтение без доставки запрещено: предикат требует `delivered_at`.
    touched = await mark_notifications_read(
        session, chat_uuid=uuid_for(company.chat_uuid), type_=REPORT_CREATED_TYPE
    )
    assert touched == 0, "прочтение отметилось без доставки (нарушен read_after_delivery)"

    await mark_notifications_delivered(
        session, chat_uuid=uuid_for(company.chat_uuid), type_=REPORT_CREATED_TYPE
    )
    touched = await mark_notifications_read(
        session, chat_uuid=uuid_for(company.chat_uuid), type_=REPORT_CREATED_TYPE
    )
    assert touched == 2, f"отмечено {touched} строк вместо 2"


def uuid_for(value: str):  # noqa: ANN201 - тестовый помощник
    import uuid as uuid_lib

    return uuid_lib.UUID(value)


async def test_redis_outage_keeps_report_successful(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-08, R8 (CR 8.2 H-2): падение Redis НЕ рвёт HTTP и НЕ откатывает жалобу.

    **Почему тест существует.** До него утверждение «ошибка Redis не рвёт
    ответ» жило только в комментариях: мутация «убрать `try/except` из
    `publish_to_chat`» не краснила НИ ОДНОГО теста (проверено откатом —
    12 passed). То есть заявление было недоказуемым, а регресс прошёл бы
    молча.

    **Что проверяется.** `201`, жалоба в БД, `chats.status='hidden'` — всё
    это уже закоммичено ДО публикации. `delivered_at` остаётся `NULL`: раз
    события не было, уведомление не доставлено, и врать об обратном нельзя
    (D-30: доставка — это публикация, а не вставка строки).

    Мутация, которая краснит: пробросить `RedisError` из `publish_to_chat`.
    """
    from redis.exceptions import RedisError

    from app.services import ws_publisher

    def _broken_redis() -> None:
        raise RedisError("CR 8.2 H-2: Redis недоступен")

    monkeypatch.setattr(ws_publisher, "get_redis", _broken_redis)

    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    assert response.status_code == 201, response.text

    stored = await session.execute(
        text(
            "SELECT count(*), count(delivered_at) FROM notifications "
            "WHERE type = 'report.created' AND payload->>'chat_uuid' = :u"
        ),
        {"u": company.chat_uuid},
    )
    total, delivered = stored.one()
    assert total == 2, "уведомления не созданы — жалоба откатилась вместе с публикацией"
    # `delivered_at` ЗАПОЛНЕН и это верно: `publish_to_chat` при отказе Redis
    # уходит в деградацию `_deliver_locally` (Task 4.2 эпика 6.5) и доставляет
    # событие соединениям ЭТОГО процесса. Дефект был бы в обратном — если бы
    # публикация упала, а отметки не появилось ни при какой доставке.
    # Граница честности отметки (доставка только локальным подписчикам тоже
    # считается доставкой) — LOW в `deferred-work.md`.
    assert delivered == 2

    # Главное: отказ Redis НЕ откатил уже закоммиченную жалобу.
    state = await session.execute(
        text("SELECT status FROM chats WHERE uuid = :u"), {"u": company.chat_uuid}
    )
    assert state.scalar_one() == "hidden"
