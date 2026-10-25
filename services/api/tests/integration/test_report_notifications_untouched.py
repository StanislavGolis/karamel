"""Жалоба НЕ пишет в `notifications` (стори 7.4; AC-13, D-7.4-6).

**Почему это проверяется тестом, а не комментарием.** Соблазн «уведомить
модератора» велик и технически близок: `notifications` уже существует (7.3),
и достаточно добавить один `INSERT`. Но `notifications` — АДРЕСНЫЙ аутбокс с
`user_id NOT NULL`: рассылка всем модераторам создала бы второй механизм
адресации, запрещённый границами стори, а адресация «по роли» — третий.
Очередь модератора читает САМУ таблицу `reports` (8.3), и это и есть канал.

**Почему `count(*)` до и после, а не «нет строк с типом».** Проверка по типу
(`type = 'report.created'`) прошла бы при уведомлении с ДРУГИМ типом — то
есть при любом обходном имени. Счётчик ловит любую запись.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_helpers import (
    build_client,
    make_company,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _notifications_count(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM notifications"))
    return int(result.scalar_one())


async def test_report_does_not_touch_notifications(session: AsyncSession) -> None:
    """AC-13 (D-7.4-6): после успешной жалобы в `notifications` ноль новых строк.

    Мутация M13 (добавить `INSERT INTO notifications` в сервис) делает тест
    красным. Проверяется и успешный путь (`201`), и повтор (`409`): второй
    тоже не должен «досылать» уведомление.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]
    before = await _notifications_count(session)

    async with build_client(session) as client:
        created = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )
        repeated = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert created.status_code == 201
    assert repeated.status_code == 409
    assert await _notifications_count(session) == before


async def test_rejected_report_does_not_touch_notifications(session: AsyncSession) -> None:
    """AC-13: отказ (`404`/`422`) тоже не пишет уведомлений.

    Отказная ветка короче, но проверять надо обе: уведомление в ней было бы
    не менее вредным — оно сообщило бы модератору о жалобе, которой нет.
    """
    company = await make_company(session, pair_status="pending", members=2)
    accused = company["members"][0]["user"]
    before = await _notifications_count(session)

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert response.status_code == 422
    assert await _notifications_count(session) == before
