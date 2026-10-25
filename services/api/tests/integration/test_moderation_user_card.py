"""Карточка пользователя для модератора (стори 8.5; AC-01…AC-10, AC-14).

**Главные утверждения:**

- четыре блока в ответе (AC-01);
- `403` до SQL для обычного пользователя (AC-02);
- `404 user_not_found` для несуществующего и `200` с пустыми списками для
  существующего без данных (AC-03, AC-04);
- `banned` отдаётся как есть (AC-09);
- списки усечены, счётчики полные (AC-10);
- порядок «новые сверху» (AC-08);
- пользователь без анкеты — `200` с пустыми полями, не `404` (AC-14).

**Почему в тесте усечения отдельный `other_id`.** Жалоба, где жалобщик и
обвиняемый — один человек, попала бы в ОБЕ оси, и `filed_total`/`against_total`
разошлись бы с ожиданием. Разные стороны делают оси независимыми.
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_user_card_helpers import (
    add_attendance,
    add_reports,
    add_sanctions,
    build_client,
    cookie_headers,
    get_card,
    make_moderator,
    make_target_user,
    set_profile,
    set_user_status,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _blocks(body: dict) -> set[str]:
    """Имена блоков ответа: `set` ловит и лишний, и потерянный."""
    return set(body)


async def test_card_returns_four_blocks(session: AsyncSession) -> None:
    """AC-01: `200` и ровно четыре блока, профиль заполнен из анкеты."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, name="Анна", city="Москва", age=28)
    moderator, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert _blocks(body) == {"profile", "reports", "sanctions", "attendance"}, _blocks(body)
    assert body["profile"]["uuid"] == str(target.uuid)
    assert body["profile"]["name"] == "Анна"
    assert body["profile"]["city"] == "Москва"
    assert body["profile"]["age"] == 28
    assert body["profile"]["status"] == "active"


async def test_plain_user_gets_403_before_any_sql(session: AsyncSession) -> None:
    """AC-02, D-65: обычный пользователь — `403`, и данные карточки не читаются.

    **«403 до SQL» не означает «ноль запросов»** — проверено прогоном: гард
    `ModeratorUser` опирается на `get_current_user`, который читает `users`
    по адресу из cookie. Это авторизация, а не карточка.

    Проверяется то, что действительно защищено: ни одна из таблиц данных
    (`profiles`, `reports`, `sanctions`, `attendance`) не читается, иначе
    отказ стоил бы столько же, сколько успех, и NFR39-ось «нет прав» текла бы
    через объём чтения.
    """
    from sqlalchemy import event

    target, _ = await make_target_user(session)
    plain, plain_token = await make_target_user(session)

    statements: list[str] = []

    def _count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    async with build_client(session) as client:
        bind = session.get_bind()
        sync_engine = getattr(bind, "sync_engine", bind)
        event.listen(sync_engine, "before_cursor_execute", _count)
        try:
            response = await get_card(client, token=plain_token, user_uuid=target.uuid)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)

    assert response.status_code == 403, response.text
    card_tables = ("FROM profiles", "FROM reports", "FROM sanctions", "FROM attendance")
    leaked = [sql for sql in statements if any(marker in sql for marker in card_tables)]
    assert leaked == [], f"гард пропустил чтение карточки: {leaked}"
    assert plain.id != target.id


async def test_unknown_user_returns_404(session: AsyncSession) -> None:
    """AC-03: несуществующий адрес — `404 user_not_found`."""
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=uuid_lib.uuid4())
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "user_not_found"


async def test_user_without_data_returns_empty_lists(session: AsyncSession) -> None:
    """AC-04: есть пользователь, нет данных — `200`, пустые списки, `total=0`."""
    target, _ = await make_target_user(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["reports"]["filed"] == []
    assert body["reports"]["against"] == []
    assert body["reports"]["filed_total"] == 0
    assert body["reports"]["against_total"] == 0
    assert body["sanctions"] == {"items": [], "total": 0}
    assert body["attendance"] == {"items": [], "total": 0}


async def test_banned_status_is_returned_as_is(session: AsyncSession) -> None:
    """AC-09, D-67: `banned` без переименования, история при этом видна."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    await set_user_status(session, user=target, status="banned")
    await add_sanctions(session, user=target, count=1)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["profile"]["status"] == "banned"
    # История санкций доступна и у забаненного: карточка не блокируется статусом.
    assert body["sanctions"]["total"] == 1


async def test_lists_truncated_but_counters_full(session: AsyncSession) -> None:
    """AC-10: 30 жалоб и 25 санкций → списки усечены, счётчики полные.

    `CARD_LIST_LIMIT = 20` (D-72). Усечённый счётчик занизил бы нагрузку на
    пользователя — тот же довод, что `total` очереди 8.3 (P-9).
    """
    from app.services.moderation_user_card_service import CARD_LIST_LIMIT

    target, _ = await make_target_user(session)
    other, _ = await make_target_user(session)
    await set_profile(session, user=target)
    await add_reports(session, user=target, filed=30, against=25, other_id=other.id)
    await add_sanctions(session, user=target, count=25, reporter_id=other.id)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert CARD_LIST_LIMIT == 20
    assert len(body["reports"]["filed"]) == 20
    assert len(body["reports"]["against"]) == 20
    assert len(body["sanctions"]["items"]) == 20
    # Полные числа — не длина списка.
    assert body["reports"]["filed_total"] == 30
    # 25 жалоб на пользователя + 25 жалоб-оснований его санкций: каждая
    # санкция выносится ПО жалобе (D-17), и эта жалоба подана на него же.
    # Это фактическое поведение домена, а не побочный эффект сида.
    assert body["reports"]["against_total"] == 50
    assert body["sanctions"]["total"] == 25


async def test_reports_ordered_newest_first(session: AsyncSession) -> None:
    """AC-08, D-70: новые сверху; последняя созданная — первая в ответе."""
    target, _ = await make_target_user(session)
    other, _ = await make_target_user(session)
    await set_profile(session, user=target)
    created = await add_reports(session, user=target, filed=3, against=2, other_id=other.id)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    filed_order = [item["uuid"] for item in body["reports"]["filed"]]
    against_order = [item["uuid"] for item in body["reports"]["against"]]
    # Создавались от старых к новым → ответ их реверсирует.
    assert filed_order == [str(u) for u in reversed(created["filed"])], filed_order
    assert against_order == [str(u) for u in reversed(created["against"])], against_order


async def test_sanctions_ordered_and_moderator_null(session: AsyncSession) -> None:
    """AC-07, AC-08: санкции новые сверху; `moderator_uuid=null` при удалённом."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    created = await add_sanctions(session, user=target, count=2, moderator_id=None)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    items = body["sanctions"]["items"]
    assert [item["uuid"] for item in items] == [str(u) for u in reversed(created)]
    # D-17: модератор удалён — санкция остаётся, автор `null`.
    assert all(item["moderator_uuid"] is None for item in items)
    assert all(item["reason"] == "Причина санкции" for item in items)


async def test_user_without_profile_gets_200_with_nulls(session: AsyncSession) -> None:
    """AC-14: пользователь без анкеты — `200`, поля `null`, НЕ `404`.

    Краснеет при `JOIN profiles` вместо `LEFT JOIN`: такой пользователь
    молча получил бы `404`, и модератор решил бы, что его нет.
    """
    target, _ = await make_target_user(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["profile"]["uuid"] == str(target.uuid)
    assert body["profile"]["name"] is None
    assert body["profile"]["city"] is None
    assert body["profile"]["age"] is None
    # `status` берётся из `users`, а не из анкеты — он есть всегда.
    assert body["profile"]["status"] == "active"


async def test_attendance_block_holds_last_ten(session: AsyncSession) -> None:
    """AC-01, D-69, D-72: явок 10 в списке при большем числе в `total`."""
    from app.services.moderation_user_card_service import CARD_ATTENDANCE_LIMIT

    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    total = await add_attendance(session, user=target, count=12)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert CARD_ATTENDANCE_LIMIT == 10
    assert len(body["attendance"]["items"]) == 10
    assert body["attendance"]["total"] == total == 12


async def test_attendance_route_still_works(session: AsyncSession) -> None:
    """AC-01, регресс 7.5: `/{uuid}/attendance` не перехвачен новым роутом.

    Проверяется не порядок объявления, а работа: после добавления
    `/{user_uuid}` старый двухсегментный путь обязан отвечать как прежде.
    """
    target, _ = await make_target_user(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await client.get(
            f"/api/v1/moderation/users/{target.uuid}/attendance",
            headers=cookie_headers(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["items"] == []
    # У 7.5 НЕТ `total`: keyset-пагинация выражает «есть ещё» присутствием
    # `next_cursor`, а `COUNT` добавил бы второй SQL в каждый запрос
    # (`AttendanceHistoryPage`, D-7.5-9). Проверяется фактический контракт, а
    # не ожидание — иначе тест требовал бы от 7.5 поля, которого там нет.
    assert "next_cursor" in body
    assert body["next_cursor"] is None


async def test_sanction_moderator_uuid_present_when_moderator_exists(
    session: AsyncSession,
) -> None:
    """AC-07: `moderator_uuid` заполнен, если аккаунт модератора ЖИВ.

    Второе значение признака (§18.11): тест с `moderator_id=None` доказывает
    только ветвь «автор удалён». Без этой половины подмена
    `moderator_uuid` на константу `None` осталась бы зелёной — проверено
    мутацией M10 в DS 8.5.
    """
    target, _ = await make_target_user(session)
    moderator, _ = await make_moderator(session)
    other, _ = await make_target_user(session)
    await set_profile(session, user=target)
    await add_sanctions(
        session, user=target, count=1, moderator_id=moderator.id, reporter_id=other.id
    )
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        items = response.json()["sanctions"]["items"]

    assert len(items) == 1
    assert items[0]["moderator_uuid"] == str(moderator.uuid), items[0]
