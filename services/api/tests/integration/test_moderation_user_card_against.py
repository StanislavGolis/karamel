"""Ось «жалобы на пользователя» через `accused_id` (стори 8.5; AC-06, AC-13).

**Почему обе ветви в одном файле.** `filed` и `against` — две РАЗНЫЕ оси
одного домена, и тест только на одной прошёл бы при перепутанном предикате
(§18.11: признак проверяется в обоих значениях).

**Почему проверяется `accused_id`, а не вычисление на лету.** D-64 ввёл
колонку; M1 (мутация) меняет `WHERE accused_id` на `reporter_id`, и без
этого файла такая подмена осталась бы незамеченной — обе оси вернули бы
непустые списки, и тесты счётчиков остались бы зелёными.
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_user_card_helpers import (
    add_reports,
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


async def test_axes_are_disjoint(session: AsyncSession) -> None:
    """AC-06: поданные пользователем и на него — РАЗНЫЕ списки, без пересечения."""
    target, _ = await make_target_user(session)
    other, _ = await make_target_user(session)
    await set_profile(session, user=target)
    created = await add_reports(session, user=target, filed=2, against=3, other_id=other.id)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    filed = {item["uuid"] for item in body["reports"]["filed"]}
    against = {item["uuid"] for item in body["reports"]["against"]}
    assert filed == {str(u) for u in created["filed"]}, filed
    assert against == {str(u) for u in created["against"]}, against
    # Пересечение пусто: жалоба не может быть одновременно поданной и предъявленной
    # одним и тем же пользователем — в этих данных стороны разные.
    assert filed & against == set()


async def test_against_uses_accused_id_not_reporter(session: AsyncSession) -> None:
    """AC-13: `against` читает `accused_id` — проверка прямым SQL.

    Тест не верит сервису: он смотрит в БД, что у жалобы `against`
    `accused_id` = целевой пользователь, а `reporter_id` — другой человек.
    Если бы `accused_id` не заполнялся при подаче, оба значения совпали бы
    или `accused_id` был бы `NULL`.
    """
    target, _ = await make_target_user(session)
    other, _ = await make_target_user(session)
    await set_profile(session, user=target)
    created = await add_reports(session, user=target, filed=1, against=1, other_id=other.id)

    result = await session.execute(
        text("SELECT r.reporter_id, r.accused_id FROM reports AS r WHERE r.uuid = :u"),
        {"u": created["against"][0]},
    )
    reporter_id, accused_id = result.one()
    assert int(accused_id) == int(target.id)
    assert int(reporter_id) == int(other.id)
    assert int(reporter_id) != int(accused_id)


async def test_new_report_via_service_appears_in_against(session: AsyncSession) -> None:
    """AC-13: жалоба, поданная СЕРВИСОМ 8.2, сразу видна в карточке обвиняемого.

    Это и есть BLOCKER из VS. **Идёт через `report_message`, а не прямым
    `INSERT`** — иначе тест не проверяет AC-13 вообще: прямой `INSERT`
    пишет `accused_id` сам, и мутация «убрать колонку из `_CREATE_MESSAGE_REPORT_SQL`»
    остаётся зелёной (проверено прогоном в DS 8.5, мутация M11).
    """
    from app.services.report_service import report_message
    from tests.integration.report_message_helpers import make_message_chat

    company = await make_message_chat(session)
    _, token = await make_moderator(session)

    created = await report_message(
        session,
        chat_uuid=uuid_lib.UUID(company.chat_uuid),
        viewer_id=company.author.id,
        accused_uuid=company.member.uuid,
    )
    assert created.uuid is not None

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=company.member.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["reports"]["against_total"] == 1, body["reports"]
    assert len(body["reports"]["against"]) == 1
    # Ось «поданные» у обвиняемого пуста: жалобу подал автор.
    assert body["reports"]["filed_total"] == 0, body["reports"]
