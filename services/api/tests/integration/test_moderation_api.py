"""Очередь модерации карточек через API (стори 4.3, Task 11; AC-03…AC-09).

Реальный PostgreSQL, схема поднята Alembic. Проверяется не только код ответа,
но и состояние БД: `200` не доказывает, что `verified_by` записан из сессии.

§18.1: каждый тест краснеет от снятия конкретной защиты.
§18.2: единственное отличие модератора от обычного пользователя — `role`,
поэтому `403`-тест краснеет ровно при снятии `require_moderator`.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _community() -> str:
    return settings.default_community_slug


def _starts_at(**delta: int) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Настольные игры",
        "category": "cafe",
        "starts_at": _starts_at(days=3).isoformat(),
        "capacity": 3,
    }
    base.update(overrides)
    return base


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой зависимостью сессии."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _new_user(session: AsyncSession, *, role: str = UserRole.USER) -> tuple[User, str]:
    phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role=str(role),
        status="active",
    )
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _insert_place(session: AsyncSession) -> str:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:name, 'cafe', 'пр. Ленина, 1', :slug, true) RETURNING uuid"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _community()},
    )
    return str(row.scalar_one())


async def _insert_profile(session: AsyncSession, *, user_id: int, name: str, age: int) -> None:
    """Анкета нужна, чтобы проверить `author.name`/`author.age` в очереди."""
    await session.execute(
        text("UPDATE users SET gender = 'male' WHERE id = :id"),
        {"id": user_id},
    )
    # CHECK `ck_profiles_completeness`: `complete` требует ВСЕ четыре поля
    # (name, city, age, bio) — без `bio` вставка падает `IntegrityError`.
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, bio, gender, status) "
            "VALUES (:uid, :name, 'Томск', :age, 'Люблю кино', 'male', 'complete')"
        ),
        {"uid": user_id, "name": name, "age": age},
    )


async def _card_row(session: AsyncSession, card_uuid: str) -> dict[str, Any] | None:
    """Строка карточки сырым SQL: проверяется PostgreSQL, не identity map."""
    result = await session.execute(
        text(
            "SELECT status, verification_status, rejection_reason, verified_at, verified_by, "
            "title FROM event_cards WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _create_card(
    client: AsyncClient, *, token: str, session: AsyncSession, **overrides: Any
) -> str:
    """Создаёт карточку через API и возвращает `uuid`."""
    place_uuid = await _insert_place(session)
    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid, **overrides),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["uuid"])


# --- AC-07: гард --------------------------------------------------------------


async def test_queue_requires_auth(client: AsyncClient) -> None:
    """AC-07: без сессии — `401 unauthenticated`.

    Краснеет, если убрать зависимость авторизации: очередь открылась бы всем.
    """
    response = await client.get("/api/v1/moderation/cards")

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


async def test_queue_forbidden_for_user(client: AsyncClient, session: AsyncSession) -> None:
    """AC-07 (R-01, R-06): обычный `user` получает `403 forbidden`.

    Краснеет, если заменить `ModeratorUser` на `CurrentUser`: очередь
    с текстами и авторами стала бы доступна любому участнику.
    """
    _, token = await _new_user(session)

    response = await client.get("/api/v1/moderation/cards", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden"}


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_decisions_forbidden_for_user(
    client: AsyncClient, session: AsyncSession, action: str
) -> None:
    """AC-07: `user` не может ни одобрить, ни отклонить карточку.

    Краснеет, если снять гард хотя бы с одного POST: участник получил бы
    право менять `verification_status` чужой карточки.
    """
    _, token = await _new_user(session)
    author, author_token = await _new_user(session)
    card_uuid = await _create_card(client, token=author_token, session=session)

    response = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/{action}",
        json={"rejection_reason": "причина"} if action == "reject" else None,
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden"}


# --- AC-01/AC-02: автопроверка при создании -----------------------------------


async def test_clean_card_is_auto_approved(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01 (FR101, ADR-08): чистый текст получает `approved` БЕЗ SQL-правки.

    Краснеет, если убрать `run_auto_check` из `create_card`: до 4.3 карточка
    оставалась `pending` и публиковать её можно было только руками (X-20).
    """
    author, token = await _new_user(session)
    card_uuid = await _create_card(client, token=token, session=session)

    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["verification_status"] == "approved"
    # ADR-08: NULL = автоматическая проверка, не человек.
    assert row["verified_by"] is None
    assert row["verified_at"] is not None


async def test_stop_word_card_stays_pending(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01/AC-02: стоп-слово оставляет карточку `pending` — она идёт в очередь.

    Краснеет, если автопроверка ставит `approved` всегда или ставит `rejected`:
    авто-`rejected` без человека нарушил бы FR113 и NFR54.
    """
    author, token = await _new_user(session)
    card_uuid = await _create_card(
        client, token=token, session=session, title="Казино и займ вечером"
    )

    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["verification_status"] == "pending"
    assert row["rejection_reason"] is None


async def test_publish_allowed_right_after_creation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: после автопроверки публикация проходит без SQL (закрывает X-20).

    Краснеет, если `create_card` не выставляет `approved`: публикация
    вернула бы `422 VERIFICATION_PENDING`.
    """
    author, token = await _new_user(session)
    card_uuid = await _create_card(client, token=token, session=session)

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"


# --- AC-03: очередь -----------------------------------------------------------


async def test_queue_lists_pending_with_author_and_place(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: очередь отдаёт `pending` с автором, местом и `auto_check`.

    Краснеет, если убрать фильтр `verification_status='pending'` (одобренные
    карточки попали бы в очередь) или не заполнить `auto_check.failed_rules`
    (модератор не увидел бы причину попадания).
    """
    author, author_token = await _new_user(session)
    await _insert_profile(session, user_id=author.id, name="Анна", age=29)
    moderator, moderator_token = await _new_user(session, role=UserRole.MODERATOR)

    pending_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )
    approved_uuid = await _create_card(
        client, token=author_token, session=session, title="Кино в пятницу"
    )

    response = await client.get(
        "/api/v1/moderation/cards", cookies={ACCESS_COOKIE_NAME: moderator_token}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    uuids = [item["uuid"] for item in body["items"]]
    assert pending_uuid in uuids
    assert approved_uuid not in uuids

    item = next(entry for entry in body["items"] if entry["uuid"] == pending_uuid)
    assert item["author"]["name"] == "Анна"
    assert item["author"]["age"] == 29
    assert item["place"]["name"].startswith("Кафе")
    assert "stop_word" in item["auto_check"]["failed_rules"]
    assert item["auto_check"]["passed"] is False
    # §7.2: внутренние id наружу не выходят.
    assert "id" not in item
    assert "author_id" not in item
    assert "place_id" not in item
    assert "verified_by" not in item


async def test_queue_total_respects_status_filter(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, D-4.3-7: `total` считается по ТОМУ ЖЕ фильтру, что и страница.

    Краснеет, если в `build_queue_count_statement` потерять
    `verification_status`: счётчик показывал бы всю таблицу, и «работы на 3
    карточки» не совпадало бы с очередью из одной. Мутация M8 (снятый фильтр
    в count) выжила до этого теста.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    # Одна `pending` (стоп-слово) и одна `approved` (чистый текст).
    await _create_card(client, token=author_token, session=session, title="Эскорт на вечер")
    await _create_card(client, token=author_token, session=session, title="Кино в пятницу")

    pending = await client.get(
        "/api/v1/moderation/cards?status=pending",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )
    approved = await client.get(
        "/api/v1/moderation/cards?status=approved",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert pending.status_code == 200, pending.text
    assert approved.status_code == 200, approved.text
    assert pending.json()["total"] == 1
    assert approved.json()["total"] == 1


async def test_queue_all_shows_approved_too(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (D-4.3-4): `status=all` снимает фильтр.

    Краснеет, если трактовать `all` как значение `verification_status`:
    выдача была бы пустой, хотя карточки есть.
    """
    author, author_token = await _new_user(session)
    moderator, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(client, token=author_token, session=session)

    response = await client.get(
        "/api/v1/moderation/cards?status=all", cookies={ACCESS_COOKIE_NAME: moderator_token}
    )

    assert response.status_code == 200, response.text
    assert card_uuid in [item["uuid"] for item in response.json()["items"]]


async def test_queue_invalid_status_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03: недопустимое значение `status` — `422` схемы.

    Краснеет, если принимать `status` строкой: мусор ушёл бы в SQL-параметр
    и дал бы пустую выдачу вместо внятной ошибки.
    """
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)

    response = await client.get(
        "/api/v1/moderation/cards?status=whatever",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 422


async def test_queue_invalid_cursor(client: AsyncClient, session: AsyncSession) -> None:
    """AC-08: битый курсор — `422 invalid_cursor`, а не `500`.

    Краснеет, если пропустить `InvalidCursorError` наверх: пользовательский
    query-параметр дал бы `500`.
    """
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)

    response = await client.get(
        "/api/v1/moderation/cards?cursor=not-a-cursor",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


async def test_queue_pagination_is_stable(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03/AC-08: две страницы покрывают очередь без потерь и дублей.

    Краснеет, если курсор строится от выброшенной строки или ключ курсора
    не совпадает с ключом сортировки: строки терялись бы или повторялись.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    created: list[str] = []
    for index in range(3):
        created.append(
            await _create_card(client, token=author_token, session=session, title=f"Казино {index}")
        )

    # `limit` страницы задаётся константой сервиса (20), поэтому пагинация
    # проверяется курсором напрямую: берём первую страницу `all` и идём дальше.
    first = await client.get(
        "/api/v1/moderation/cards?status=all", cookies={ACCESS_COOKIE_NAME: moderator_token}
    )
    assert first.status_code == 200
    assert first.json()["total"] >= 3

    cursor = first.json()["cursor"]
    if cursor is not None:
        second = await client.get(
            f"/api/v1/moderation/cards?status=all&cursor={cursor}",
            cookies={ACCESS_COOKIE_NAME: moderator_token},
        )
        assert second.status_code == 200
        first_ids = {item["uuid"] for item in first.json()["items"]}
        second_ids = {item["uuid"] for item in second.json()["items"]}
        assert not (first_ids & second_ids)

    listed = {
        item["uuid"]
        for item in (
            await client.get(
                "/api/v1/moderation/cards?status=all",
                cookies={ACCESS_COOKIE_NAME: moderator_token},
            )
        ).json()["items"]
    }
    assert set(created) & listed


# --- AC-04: approve -----------------------------------------------------------


async def test_approve_sets_moderator_from_session(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: `approve` пишет `verified_by` из СЕССИИ, не из тела.

    Краснеет, если брать модератора из запроса: клиент выбирал бы, кто
    проверил, и история решений была бы подделываемой.
    """
    author, author_token = await _new_user(session)
    moderator, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )

    response = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/approve",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["verification_status"] == "approved"

    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["verified_by"] == moderator.id
    assert row["verified_at"] is not None


async def test_approve_twice_conflicts(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04 (OQ-3): повторный `approve` — `409 CARD_ALREADY_VERIFIED`.

    Краснеет, если вернуть `200`: второе решение переписало бы `verified_by`
    и `verified_at`, и история проверки стала бы недостоверной.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )
    first = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/approve",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/approve",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert second.status_code == 409
    assert second.json() == {"code": "CARD_ALREADY_VERIFIED"}


async def test_approve_published_conflicts(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04 (OQ-5): решение по опубликованной карточке — `409`.

    Краснеет, если убрать проверку статуса: модератор мог бы перевести
    опубликованную карточку в `rejected`, и она выпала бы из ленты в обход
    процедуры жалоб (6.x).
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(client, token=author_token, session=session)
    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: author_token}
    )
    assert published.status_code == 200, published.text

    response = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/approve",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 409
    assert response.json() == {"code": "CARD_ALREADY_PUBLISHED"}


async def test_approve_unknown_card_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: неизвестный `uuid` — `404 card_not_found`."""
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    unknown = uuid_lib.uuid4()

    response = await client.post(
        f"/api/v1/moderation/cards/{unknown}/approve",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


# --- AC-05: reject ------------------------------------------------------------


async def test_reject_stores_reason_and_author_sees_it(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05/AC-06 (FR113): причина сохранена и видна автору в `publish`.

    Краснеет, если не сохранять причину или не отдавать её в `details.reason`:
    автор не поймёт, что исправлять.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )

    response = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/reject",
        json={"rejection_reason": "Стоп-слово в заголовке"},
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["verification_status"] == "rejected"

    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["rejection_reason"] == "Стоп-слово в заголовке"

    publish = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: author_token}
    )
    assert publish.status_code == 409
    assert publish.json()["code"] == "CARD_REJECTED"
    assert publish.json()["details"]["reason"] == "Стоп-слово в заголовке"


@pytest.mark.parametrize("body", [{}, {"rejection_reason": ""}, {"rejection_reason": "   "}])
async def test_reject_without_reason_rejected(
    client: AsyncClient, session: AsyncSession, body: dict[str, Any]
) -> None:
    """AC-05 (FR113): отклонение без причины — `422`.

    Краснеет, если снять `min_length`/нормализацию причины: карточка получила бы
    `rejected` с пустой причиной, что нарушает FR113 и CHECK в БД.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )

    response = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/reject",
        json=body,
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 422
    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["verification_status"] == "pending"


async def test_reject_twice_replaces_reason(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05 (OQ-4): повторный `reject` — `200` с заменой причины.

    Краснеет, если отвечать `409`: модератор не смог бы уточнить формулировку.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )
    await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/reject",
        json={"rejection_reason": "Первая"},
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    response = await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/reject",
        json={"rejection_reason": "Вторая"},
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 200
    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["rejection_reason"] == "Вторая"


# --- AC-06: правка снимает отклонение -----------------------------------------


async def test_edit_rejected_card_runs_auto_check_again(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06 (D-4.3-3): правка отклонённого черновика снимает `rejected`.

    Краснеет, если `update_card` не вызывает `run_auto_check`: отклонённая
    карточка осталась бы отклонённой навсегда, и лимит 4.4 наказал бы автора
    дважды за одну ошибку.
    """
    author, author_token = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    card_uuid = await _create_card(
        client, token=author_token, session=session, title="Эскорт на вечер"
    )
    await client.post(
        f"/api/v1/moderation/cards/{card_uuid}/reject",
        json={"rejection_reason": "Стоп-слово"},
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"title": "Кино в пятницу"},
        cookies={ACCESS_COOKIE_NAME: author_token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["verification_status"] == "approved"
    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["rejection_reason"] is None
    assert row["verified_by"] is None


# --- AC-08: no-store ----------------------------------------------------------


async def test_queue_no_store_header(client: AsyncClient, session: AsyncSession) -> None:
    """AC-08: `cache-control: no-store` на очереди.

    Краснеет, если отключить `NoStoreMiddleware`: очередь с текстами авторов
    осела бы в кэшах прокси.
    """
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)

    response = await client.get(
        "/api/v1/moderation/cards", cookies={ACCESS_COOKIE_NAME: moderator_token}
    )

    assert response.headers.get("cache-control") == "no-store"


# --- CR 4.3, патчи 1–2: срез сообщества ---------------------------------------


async def _insert_foreign_card(session: AsyncSession, *, author_id: int) -> str:
    """Карточка ЧУЖОГО сообщества напрямую в БД.

    Через API её не создать: `community_slug` всегда берётся из настроек
    (ADR-01). Сырой INSERT — единственный способ воспроизвести сценарий
    «модератор другого города открыл очередь».
    """
    place_id = (
        await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug, is_active) "
                "VALUES (:name, 'cafe', 'ул. Чужая, 1', 'other-city', true) RETURNING id"
            ),
            {"name": f"Чужое {uuid_lib.uuid4().hex[:8]}"},
        )
    ).scalar_one()
    card_uuid = uuid_lib.uuid4()
    await session.execute(
        text(
            "INSERT INTO event_cards (uuid, author_id, place_id, community_slug, title, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "verification_status, filled_count) "
            "VALUES (:uuid, :author, :place, 'other-city', 'Чужая карточка', 'cafe', "
            "now() + interval '3 days', 2, 2880, now() + interval '1 day', 'draft', "
            "'pending', 0)"
        ),
        {"uuid": card_uuid, "author": author_id, "place": place_id},
    )
    return str(card_uuid)


async def test_queue_hides_other_community(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, ADR-01/ADR-13 (CR 4.3, патч 1): очередь — только своё сообщество.

    Краснеет, если убрать `WHERE community_slug = ...` из `build_queue_statement`
    и `build_queue_count_statement`: модератор «Томска» видел (и мог отклонить)
    карточку другого города, а `total` считал всю таблицу.
    """
    author, _ = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    foreign_uuid = await _insert_foreign_card(session, author_id=author.id)

    response = await client.get(
        "/api/v1/moderation/cards?status=all",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 200, response.text
    uuids = [item["uuid"] for item in response.json()["items"]]
    assert foreign_uuid not in uuids
    # `total` тоже считается по своему сообществу.
    assert response.json()["total"] == len(uuids)


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_decision_on_other_community_is_not_found(
    client: AsyncClient, session: AsyncSession, action: str
) -> None:
    """AC-04/AC-05, ADR-13 (CR 4.3, патч 2): решение по чужой карточке — `404`.

    Краснеет, если убрать срез сообщества из `get_for_moderation`: зная `uuid`
    (он публичен), модератор мог бы одобрить или отклонить карточку другого
    города — то есть управлять лентой чужого сообщества.
    """
    author, _ = await _new_user(session)
    _, moderator_token = await _new_user(session, role=UserRole.MODERATOR)
    foreign_uuid = await _insert_foreign_card(session, author_id=author.id)

    response = await client.post(
        f"/api/v1/moderation/cards/{foreign_uuid}/{action}",
        json={"rejection_reason": "причина"} if action == "reject" else None,
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}
    # Состояние карточки не изменилось.
    row = await _card_row(session, foreign_uuid)
    assert row is not None
    assert row["verification_status"] == "pending"
