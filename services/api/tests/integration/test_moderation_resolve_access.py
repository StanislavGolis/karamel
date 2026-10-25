"""Ограничение доступа после санкции (стори 8.4; AC-02, AC-03, AC-06, D-49).

**Главные утверждения:**

- `ban` → `403 user_blocked` на записи И на чтении (AC-02, AC-03, D-51:
  бан глобальный, AC-03 шарда «чтение не блокируется» снят);
- `warning` → доступ не ограничен, но запись остаётся в истории (AC-06, FR41);
- понижения нет: `banned` не становится `warned` (D-49).

**Почему проверяются ДВА маршрута, а не один.** Глобальность бана — это
свойство `get_current_user`, а не конкретного роута: если проверка стояла бы
только на создании карточки, второй маршрут остался бы открытым, и тест на
одном маршруте этого не показал бы.

**Почему `warning` проверяется в ОБОИХ значениях признака** (§18.11). Тест
только на «доступ есть» прошёл бы и при полном отсутствии записи `warned`:
он не доказывает, что статус вообще читается кодом.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_resolve_helpers import (
    build_client,
    cookie_headers,
    get_user_status,
    make_moderator,
    make_no_show_report,
    make_plain_user,
    post_resolve,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _card_body(place_uuid: str, starts_at: str) -> dict[str, object]:
    """Минимальное тело создания карточки — только обязательные поля."""
    return {
        "title": "Встреча",
        "category": "cafe",
        "place_uuid": place_uuid,
        "starts_at": starts_at,
    }


async def _place_uuid(session: AsyncSession) -> str:
    """Существующее место: без него `422` пришёл бы от валидации места,
    а не от бана, и тест AC-02 стал бы тавтологией."""
    result = await session.execute(text("SELECT uuid FROM places LIMIT 1"))
    return str(result.scalar_one())


async def test_ban_blocks_card_creation(session: AsyncSession) -> None:
    """AC-02: `ban` → `403 user_blocked` на `POST /cards` (FR42)."""
    report = await make_no_show_report(session)
    _, moderator_token = await make_moderator(session)

    async with build_client(session) as client:
        resolved = await post_resolve(
            client, token=moderator_token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert resolved.status_code == 200, resolved.text

        banned = report["accused"]
        banned_token = await _token_of(session, banned.id)
        place_uuid = await _place_uuid(session)
        response = await client.post(
            "/api/v1/cards",
            json=_card_body(place_uuid, "2030-01-01T12:00:00Z"),
            headers=cookie_headers(banned_token),
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "user_blocked", response.text


async def test_ban_blocks_join_request(session: AsyncSession) -> None:
    """AC-02: `ban` → `403` и на подаче заявки (FR42)."""
    report = await make_no_show_report(session)
    _, moderator_token = await make_moderator(session)

    async with build_client(session) as client:
        resolved = await post_resolve(
            client, token=moderator_token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert resolved.status_code == 200, resolved.text

        accused_token = await _token_of(session, report["accused"].id)
        response = await client.post(
            f"/api/v1/cards/{report['card_uuid']}/requests",
            json={"message": "хочу присоединиться"},
            headers=cookie_headers(accused_token),
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "user_blocked", response.text


async def test_ban_blocks_reading_too(session: AsyncSession) -> None:
    """AC-02, AC-03, D-51: бан глобальный — чтение ленты тоже `403`.

    Шард 8.4 AC-03 предполагал «чтение не блокируется», но оговорка
    «уточняется на CU» реализована решением D-51: выборочный доступ требует
    правки единственного контура авторизации и отдельной стори. Тест
    фиксирует ФАКТИЧЕСКОЕ поведение, а не пожелание шарда.
    """
    report = await make_no_show_report(session)
    _, moderator_token = await make_moderator(session)

    async with build_client(session) as client:
        resolved = await post_resolve(
            client, token=moderator_token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert resolved.status_code == 200, resolved.text

        accused_token = await _token_of(session, report["accused"].id)
        response = await client.get("/api/v1/cards", headers=cookie_headers(accused_token))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "user_blocked", response.text


async def test_warning_keeps_access_and_writes_status(session: AsyncSession) -> None:
    """AC-06, FR41: `warning` не ограничивает, но статус `warned` записан.

    Проверяются ОБА значения (§18.11): запись в БД (`warned`) и отсутствие
    ограничения (лента отвечает `200`). Тест только на доступе прошёл бы и
    при неработающем `UPDATE users`.
    """
    report = await make_no_show_report(session)
    _, moderator_token = await make_moderator(session)

    async with build_client(session) as client:
        resolved = await post_resolve(
            client,
            token=moderator_token,
            report_uuid=report["report_uuid"],
            resolution="warning",
        )
        assert resolved.status_code == 200, resolved.text

        assert await get_user_status(session, user_id=report["accused"].id) == "warned"

        accused_token = await _token_of(session, report["accused"].id)
        response = await client.get("/api/v1/cards", headers=cookie_headers(accused_token))
        assert response.status_code == 200, response.text


async def test_ban_is_not_downgraded_by_warning(session: AsyncSession) -> None:
    """D-49: `banned` не понижается до `warned` новой санкцией.

    Понижение отменило бы действующую санкцию фактом новой жалобы. Тест
    краснеет при снятии предиката `status <> 'banned'` из `UPDATE users`.
    """
    first = await make_no_show_report(session)
    _, moderator_token = await make_moderator(session)

    async with build_client(session) as client:
        banned = await post_resolve(
            client, token=moderator_token, report_uuid=first["report_uuid"], resolution="ban"
        )
        assert banned.status_code == 200, banned.text

    # Вторая жалоба на ДРУГОЙ паре, но обвиняемый — тот же пользователь:
    # понижение проверяется на нём, а не на новом.
    second = await make_no_show_report(session, accused_id=first["accused"].id)
    async with build_client(session) as client:
        warned = await post_resolve(
            client, token=moderator_token, report_uuid=second["report_uuid"], resolution="warning"
        )
        assert warned.status_code == 200, warned.text

    assert await get_user_status(session, user_id=first["accused"].id) == "banned"


async def test_plain_user_gets_403_on_resolve(session: AsyncSession) -> None:
    """AC-10: обычный пользователь — `403`, санкция не создаётся."""
    report = await make_no_show_report(session)
    _, token = await make_plain_user(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="warning"
        )
        assert response.status_code == 403, response.text

    result = await session.execute(text("SELECT count(*) FROM sanctions"))
    assert int(result.scalar_one()) == 0


async def _token_of(session: AsyncSession, user_id: int) -> str:
    """Новый access-токен для уже созданного пользователя.

    **`expire_all()` обязателен.** `resolve_report` пишет статус СЫРЫМ SQL
    (`UPDATE users ...`), поэтому ORM-объект `User`, загруженный в этой сессии
    раньше, остался бы в identity map со старым `status='active'` — и
    `get_current_user` (он читает через `get_by_uuid`) вернул бы кэш вместо
    строки БД. Без сброса тест был бы зелёным при снятом `UPDATE`: проверял бы
    кэш, а не поведение API. Тот же класс, что §18.3 (`populate_existing`).
    """
    from app.repositories.users import UserRepository
    from app.services.session_service import issue_session

    session.expire_all()
    user = await UserRepository(session).get_by_id(user_id)
    assert user is not None
    issued = await issue_session(session, user)
    return issued.access_token
