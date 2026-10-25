"""Своя история явок (стори 7.5; AC-02, AC-04, D-7.5-3).

**Почему маршрут без `user_uuid` проверяется по сигнатуре.** D-7.5-3 требует,
чтобы подстановка чужого адреса была невозможна ПО КОНСТРУКЦИИ, а не по
проверке. Значит тест обязан упасть, если кто-то добавит параметр `user_uuid`
в этот маршрут: тогда «своя» история стала бы параметризуемой, и одна
проверка превратилась бы в две (своя + чужая) при одном гарде.

**Почему набор полей тот же.** Второй контракт для тех же данных разошёлся
бы при первой правке, а чужие флаги не раскрываются не «проверкой», а тем,
что они не читаются вовсе (P-8).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from tests.integration.attendance_history_helpers import (
    add_attendance,
    build_client,
    cookie_headers,
    make_card,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-02, AC-04: тот же набор из четырёх полей, что и у модератора.
EXPECTED_ITEM_KEYS = {"card_uuid", "card_title", "starts_at", "status"}


async def _own(client: AsyncClient, *, token: str | None) -> Any:
    """`GET /profiles/me/attendance` — параметров, кроме пагинации, нет."""
    headers = cookie_headers(token) if token is not None else {}
    return await client.get("/api/v1/profiles/me/attendance", headers=headers)


async def test_own_history_returns_only_own_records(session: AsyncSession) -> None:
    """AC-04: `200` и только СВОЯ история.

    Мутация: считать `participant_id` из запроса (или из `user_uuid`-параметра)
    — тогда вторая половина теста вернула бы чужую строку. Здесь два человека
    с записями по РАЗНЫМ карточкам, поэтому смешение видно по `card_uuid`.
    """
    owner, owner_token = await make_user(session)
    other, other_token = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=2)
    owner_card_uuid, owner_card_id = await make_card(session, author_id=other.id, starts_at=moment)
    _, other_card_id = await make_card(session, author_id=owner.id, starts_at=moment)
    await add_attendance(session, card_id=owner_card_id, participant_id=owner.id, created_at=moment)
    await add_attendance(session, card_id=other_card_id, participant_id=other.id, created_at=moment)

    async with build_client(session) as client:
        owner_response = await _own(client, token=owner_token)
        other_response = await _own(client, token=other_token)

    assert owner_response.status_code == 200, owner_response.text
    assert other_response.status_code == 200, other_response.text
    owner_items = owner_response.json()["items"]
    other_items = other_response.json()["items"]
    assert len(owner_items) == len(other_items) == 1
    assert owner_items[0]["card_uuid"] == str(owner_card_uuid)
    assert owner_items[0]["card_uuid"] != other_items[0]["card_uuid"]


async def test_own_history_has_same_four_fields(session: AsyncSession) -> None:
    """AC-02, AC-04: элемент своего маршрута — те же четыре поля."""
    owner, owner_token = await make_user(session)
    author, _ = await make_user(session)
    _, card_id = await make_card(session, author_id=author.id)
    await add_attendance(session, card_id=card_id, participant_id=owner.id)

    async with build_client(session) as client:
        response = await _own(client, token=owner_token)

    assert response.status_code == 200, response.text
    assert set(response.json()["items"][0]) == EXPECTED_ITEM_KEYS


async def test_own_history_without_records_is_empty_page(session: AsyncSession) -> None:
    """AC-04: нет записей → `200 {"items": [], "next_cursor": null}`."""
    lonely, lonely_token = await make_user(session)

    async with build_client(session) as client:
        response = await _own(client, token=lonely_token)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


async def test_own_history_requires_session(session: AsyncSession) -> None:
    """AC-04: без сессии → `401`, а не пустая страница."""
    async with build_client(session) as client:
        response = await _own(client, token=None)

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_own_route_does_not_accept_user_uuid(session: AsyncSession) -> None:
    """AC-04, D-7.5-3: маршрут НЕ принимает `user_uuid` — обход невозможен по конструкции.

    Проверяются два факта:
    (1) в параметрах маршрута нет `user_uuid` — добавление параметра покраснит
        тест ДО того, как появится способ читать чужое;
    (2) `?user_uuid=<чужой>` не меняет ответ: FastAPI игнорирует неизвестный
        query-параметр, но именно это и требуется — тихого «принял и учёл»
        быть не может.
    """
    owner, owner_token = await make_user(session)
    other, _ = await make_user(session)
    author, _ = await make_user(session)
    _, card_id = await make_card(session, author_id=author.id)
    await add_attendance(session, card_id=card_id, participant_id=owner.id)

    # Параметры берутся из OpenAPI (ПУБЛИЧНЫЙ контракт), а не из внутренних
    # `route.dependant`: путь роутера и путь в спецификации различаются
    # префиксами, и поиск по внутреннему пути молча находил бы не тот маршрут
    # (StopIteration вместо осмысленного отказа).
    spec = app.openapi()
    operation = spec["paths"]["/api/v1/profiles/me/attendance"]["get"]
    param_names = {parameter["name"] for parameter in operation.get("parameters", [])}
    assert "user_uuid" not in param_names, param_names
    assert "cursor" in param_names and "limit" in param_names, param_names

    async with build_client(session) as client:
        plain = await _own(client, token=owner_token)
        with_foreign = await client.get(
            "/api/v1/profiles/me/attendance",
            headers=cookie_headers(owner_token),
            params={"user_uuid": str(other.uuid)},
        )

    assert plain.status_code == with_foreign.status_code == 200
    assert plain.json() == with_foreign.json()


async def test_own_history_broken_cursor_gets_422(session: AsyncSession) -> None:
    """AC-06 на своём маршруте: битый курсор → `422 invalid_cursor`."""
    owner, owner_token = await make_user(session)

    async with build_client(session) as client:
        response = await client.get(
            "/api/v1/profiles/me/attendance",
            headers=cookie_headers(owner_token),
            params={"cursor": "не-курсор"},
        )

    assert response.status_code == 422, response.text
    assert response.json() == {"code": "invalid_cursor"}


async def test_own_and_moderator_serializers_agree(session: AsyncSession) -> None:
    """AC-02: один и тот же факт отдаётся ОДИНАКОВО двумя маршрутами.

    Мутация: собрать элемент своего маршрута вручную (своя фабрика) — поля
    разошлись бы при первой правке схемы, и клиент получил бы два контракта
    для одних данных.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    owner, owner_token = await make_user(session)
    author, _ = await make_user(session)
    _, card_id = await make_card(session, author_id=author.id, title="Одинаково")
    await add_attendance(session, card_id=card_id, participant_id=owner.id, status="confirmed")

    async with build_client(session) as client:
        own_response = await _own(client, token=owner_token)
        moderator_response = await client.get(
            f"/api/v1/moderation/users/{owner.uuid}/attendance",
            headers=cookie_headers(moderator_token),
        )

    assert own_response.status_code == moderator_response.status_code == 200
    assert own_response.json()["items"] == moderator_response.json()["items"]
    # Убеждаемся, что тест не «зелёный на пустом»: элемент действительно есть.
    assert own_response.json()["items"][0]["card_title"] == "Одинаково"


async def test_own_pagination_walks_all_pages(session: AsyncSession) -> None:
    """CR 7.5 L-6 (AC-04, AC-05): пагинация СВОЕГО маршрута, а не только модераторского.

    Свой маршрут проверялся лишь на непустой первой странице: `next_cursor`
    на нём не разу не передавался обратно. Дефект «курсор строится, но не
    применяется» (предикат keyset забыт на одной из ветвей) прошёл бы мимо.

    Мутация: убрать разбор `cursor` из своего маршрута — вторая страница
    вернула бы те же две строки, и `len(set(seen))` стал бы 2 вместо 3.
    """
    owner, owner_token = await make_user(session)
    author, _ = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=3)
    for index in range(3):
        _, card_id = await make_card(
            session, author_id=author.id, starts_at=moment, title=f"Своя {index}"
        )
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=owner.id,
            created_at=moment + timedelta(minutes=index),
        )

    seen: list[str] = []
    async with build_client(session) as client:
        cursor: str | None = None
        for _ in range(2):
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(
                "/api/v1/profiles/me/attendance",
                headers=cookie_headers(owner_token),
                params=params,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            seen.extend(item["card_uuid"] for item in body["items"])
            cursor = body["next_cursor"]

    assert cursor is None
    assert len(seen) == len(set(seen)) == 3


async def test_own_limit_one_and_max_are_accepted(session: AsyncSession) -> None:
    """CR 7.5 L-6 (AC-05): границы `limit=1` и `limit=50` — рабочие значения.

    `limit=1` — худший случай `limit + 1` и построения курсора из ОДНОЙ строки
    (любая ошибка в `rows[:limit]` или в `page_rows[-1]` видна именно здесь).
    `limit=50` совпадает с `MAX_LIMIT`: off-by-one в объявлении `le=MAX_LIMIT`
    отверг бы легальное значение. Проверялись только отказы (`0`/`51`).

    Мутация: `le=MAX_LIMIT - 1` — `limit=50` дал бы `422`.
    """
    owner, owner_token = await make_user(session)
    author, _ = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=4)
    for index in range(2):
        _, card_id = await make_card(session, author_id=author.id, starts_at=moment)
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=owner.id,
            created_at=moment + timedelta(minutes=index),
        )

    async with build_client(session) as client:
        one = await client.get(
            "/api/v1/profiles/me/attendance",
            headers=cookie_headers(owner_token),
            params={"limit": 1},
        )
        fifty = await client.get(
            "/api/v1/profiles/me/attendance",
            headers=cookie_headers(owner_token),
            params={"limit": 50},
        )

    assert one.status_code == 200, one.text
    assert len(one.json()["items"]) == 1
    # Одна строка выдана, ещё одна осталась — курсор обязан быть.
    assert one.json()["next_cursor"] is not None
    assert fifty.status_code == 200, fifty.text
    assert len(fifty.json()["items"]) == 2
    assert fifty.json()["next_cursor"] is None
