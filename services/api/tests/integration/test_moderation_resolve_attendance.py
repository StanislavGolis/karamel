"""Применимость санкции за неявку (стори 8.4; AC-04, FR109, FR89).

**Главное утверждение:** состояние пары читается СЕЙЧАС, под блокировкой жалобы,
а не берётся из `reports`. Между подачей и решением пару могли до-подтвердить
(7.2), и санкция за `confirmed` была бы ложной.

**Почему `disputed` разрешает только `dismissed`.** FR89: расхождение — не
основание санкции. Модератор обязан закрыть такую жалобу без последствий, но
сама жалоба существует и должна уйти из очереди.

**Почему `confirmed` и `pending` дают `422`, а не `409`.** Значение состояния
пришло от клиента (он нажал «бан» на паре, которую оба подтвердили); сервер
корректен — та же граница, что `ATTENDANCE_PENDING` в 7.4 (D-11).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_resolve_helpers import (
    REASON,
    build_client,
    make_moderator,
    make_no_show_report,
    post_resolve,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _sanctions_count(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM sanctions"))
    return int(result.scalar_one())


async def _report_status(session: AsyncSession, report_uuid: object) -> str:
    result = await session.execute(
        text("SELECT status FROM reports WHERE uuid = :u"), {"u": report_uuid}
    )
    return str(result.scalar_one())


async def test_no_show_allows_sanction(session: AsyncSession) -> None:
    """AC-04, FR109: `no_show` — единственное основание санкции за неявку."""
    report = await make_no_show_report(session, pair_status="no_show")
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert response.status_code == 200, response.text

    assert await _sanctions_count(session) == 1


async def test_disputed_blocks_sanction_with_422(session: AsyncSession) -> None:
    """AC-04, FR89: `disputed` + `warning` — `422 SANCTION_NOT_APPLICABLE`."""
    report = await make_no_show_report(session, pair_status="disputed")
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="warning"
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "SANCTION_NOT_APPLICABLE"

    # Ни санкции, ни изменений в жалобе: отказ не оставляет следов.
    assert await _sanctions_count(session) == 0
    assert await _report_status(session, report["report_uuid"]) == "open"


async def test_disputed_allows_dismissed(session: AsyncSession) -> None:
    """AC-04, AC-07, FR89: `disputed` + `dismissed` — закрытие законно."""
    report = await make_no_show_report(session, pair_status="disputed")
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="dismissed"
        )
        assert response.status_code == 200, response.text

    assert await _sanctions_count(session) == 0
    assert await _report_status(session, report["report_uuid"]) == "dismissed"


@pytest.mark.parametrize("pair_status", ["confirmed", "pending"])
async def test_confirmed_and_pending_are_not_applicable(
    session: AsyncSession, pair_status: str
) -> None:
    """AC-04: `confirmed`/`pending` — санкция недопустима ни в каком виде.

    `confirmed` — оба подтвердили, что пришли: жалоба «на неявку» ложна по
    построению. `pending` — подтверждение не завершено, судить нечего.
    Разница кодов с `disputed` намеренная: там закрытие разрешено, здесь нет.
    """
    report = await make_no_show_report(session, pair_status=pair_status)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "SANCTION_NOT_APPLICABLE"

    assert await _sanctions_count(session) == 0
    assert await _report_status(session, report["report_uuid"]) == "open"


async def test_confirmed_blocks_dismissed_too(session: AsyncSession) -> None:
    """AC-04, D-52: `confirmed` — `422` и для закрытия.

    Отличие от `disputed` — контракт, а не недосмотр: `disputed` фиксирует
    расхождение и требует закрытия (FR89), а `confirmed` означает, что
    нарушения не было вовсе, и жалоба должна разбираться отдельно
    (модератор видит оба флага в очереди 8.3).
    """
    report = await make_no_show_report(session, pair_status="confirmed")
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="dismissed"
        )
        assert response.status_code == 422, response.text

    assert await _report_status(session, report["report_uuid"]) == "open"


async def test_message_report_skips_attendance_check(session: AsyncSession) -> None:
    """AC-04: у жалобы на переписку пары нет — проверка не применяется.

    Обратная сторона AC-04: если бы проверка стояла вне `target_type`,
    `message`-жалоба падала бы на `attendance_id IS NULL`.
    """
    from tests.integration.moderation_resolve_helpers import make_message_report

    report = await make_message_report(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="warning"
        )
        assert response.status_code == 200, response.text

    assert await _sanctions_count(session) == 1
    # Причина дошла до санкции и в этой ветви.
    result = await session.execute(text("SELECT reason FROM sanctions"))
    assert result.scalar_one() == REASON
