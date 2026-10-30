"""Лента карточек и детальный экран через API (стори 4.5, Task 9; AC-01…AC-08).

Реальный PostgreSQL, схема поднята Alembic. Карточки кладутся СЫРЫМ SQL:
через `POST /cards` нельзя получить ни прошедшее `starts_at`, ни `closed`,
ни общий `starts_at` у нескольких карточек — а ровно эти состояния и есть
предмет проверки (AC-01, AC-03, AC-08).

§18.1: каждый тест краснеет от снятия конкретной защиты. §18.2: `has_seats`
проверяется на карточке с `filled_count == capacity`, а не на константе.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _city() -> str:
    return settings.default_city_slug


def _future(**delta: int) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


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


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    # FR9 (CR 2.1, Решение 2): лента отдаётся через `ConsentUser` — показ имён
    # и возрастов других людей требует согласия на обработку ПД. Без этих двух
    # полей зависимость отвечает `403 consent_required` до всякой логики.
    #
    # Ставится на ORM-объекте, а не сырым `UPDATE`: `get_current_user` читает
    # того же пользователя в ТОЙ ЖЕ сессии и вернул бы снимок из identity map
    # со старыми полями (сырой `UPDATE` мимо ORM его не обновляет).
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession, *, slug: str | None = None) -> int:
    """Место и его внутренний `id` (нужен для FK карточки)."""
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id, uuid"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": slug or _city()},
    )
    mapping = row.mappings().one()
    return int(mapping["id"])


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    status: str = "published",
    category: str = "cafe",
    capacity: int = 2,
    filled_count: int = 0,
    slug: str | None = None,
    verification_status: str = "approved",
    title: str = "Встреча",
) -> str:
    """Карточка в нужном состоянии — сырым SQL, минуя правила сервиса."""
    starts = starts_at or _future(days=2)
    expires = expires_at or (starts - timedelta(hours=1))
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, :title, NULL, :category, :starts, :capacity, 60, "
            ":expires, :status, :filled, :verification, :published_at,"
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": slug or _city(),
            "title": title,
            "category": category,
            "starts": starts,
            "capacity": capacity,
            "expires": expires,
            "status": status,
            "filled": filled_count,
            "verification": verification_status,
            "published_at": starts - timedelta(days=1) if status != "draft" else None,
        },
    )
    return str(row.scalar_one())


async def _card_status(session: AsyncSession, card_uuid: str) -> str:
    row = await session.execute(
        text("SELECT status FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    return str(row.scalar_one())


# --- AC-01: состав и порядок ленты --------------------------------------------


async def test_feed_returns_only_published_and_full_future(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: только `published`/`full`, только будущие, только своё сообщество.
    П1 (FR79, видимость): в ленту попадают только `published`/`full` и только будущие.

    Краснеет от снятия любого из трёх условий билдера: `status IN`, `starts_at >=
    now`, `city_slug =`. Каждая «лишняя» карточка здесь — отдельное
    нарушение, а не вариант одного.
    """
    user, token = await _new_user(session)
    place = await _place(session)

    visible_published = await _insert_card(session, author_id=user.id, place_id=place)
    visible_full = await _insert_card(
        session, author_id=user.id, place_id=place, status="full", filled_count=2
    )
    # Невидимые по статусу.
    for bad_status in ("draft", "closed", "finished", "cancelled"):
        await _insert_card(
            session, author_id=user.id, place_id=place, status=bad_status, filled_count=0
        )
    # Невидимая по времени: встреча уже прошла.
    await _insert_card(
        session,
        author_id=user.id,
        place_id=place,
        starts_at=_future(days=-1),
        expires_at=_future(days=-2),
    )
    # Невидимая по сообществу.
    other_place = await _place(session, slug="other_city")
    await _insert_card(session, author_id=user.id, place_id=other_place, slug="other_city")

    response = await client.get(
        "/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["uuid"] for item in body["items"]} == {visible_published, visible_full}
    assert body["total"] == 2
    assert body["cursor"] is None


async def test_feed_orders_by_starts_at(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: сортировка `starts_at ASC` — ближайшая встреча первой.

    Краснеет при `ORDER BY starts_at DESC` или при отсутствии сортировки:
    лента показывала бы дальние встречи раньше ближних.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    far = await _insert_card(session, author_id=user.id, place_id=place, starts_at=_future(days=5))
    near = await _insert_card(session, author_id=user.id, place_id=place, starts_at=_future(days=1))

    response = await client.get(
        "/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert [item["uuid"] for item in response.json()["items"]] == [near, far]


# --- AC-08: пагинация без дублей и пропусков -----------------------------------


async def test_pagination_has_no_gaps_or_duplicates_on_equal_starts_at(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08: keyset по `(starts_at, uuid)` — 5 карточек с РАВНЫМ `starts_at`.

    Главный тест стори. Равные `starts_at` — не редкость: «встречи в 19:00».
    Краснеет от двух правок:
    - курсор по одному `starts_at` без `uuid` → строки с равной меткой
      теряются или дублируются между страницами;
    - курсор от выброшенной (лишней) строки → сдвиг на одну карточку.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    # Одинаковый `starts_at` до микросекунды у всех — не редкость («в 19:00»).
    # Карточек БОЛЬШЕ страницы (22 при `CARDS_PAGE_SIZE = 20`), иначе курсора
    # не будет вовсе и проверять keyset не на чем.
    shared_start = _future(days=3)
    created = [
        await _insert_card(
            session, author_id=user.id, place_id=place, starts_at=shared_start, title=f"В{i}"
        )
        for i in range(22)
    ]

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(5):  # страховка от бесконечного цикла при дефекте курсора
        url = "/api/v1/cards" + (f"?cursor={cursor}" if cursor else "")
        response = await client.get(url, headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"})
        assert response.status_code == 200, response.text
        body = response.json()
        collected.extend(item["uuid"] for item in body["items"])
        cursor = body["cursor"]
        if cursor is None:
            break

    assert sorted(collected) == sorted(created)
    assert len(collected) == len(set(collected)) == 22


async def test_pagination_stops_with_null_cursor(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-08: последняя страница отдаёт `cursor=null`.

    Краснеет, если курсор собирать всегда: клиент бесконечно ходил бы за
    пустыми страницами.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    await _insert_card(session, author_id=user.id, place_id=place)

    response = await client.get(
        "/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.json()["cursor"] is None


async def test_invalid_cursor_is_422_not_500(client: AsyncClient, session: AsyncSession) -> None:
    """AC-08: битый курсор — `422 invalid_cursor`.

    Краснеет, если пропустить `InvalidCursorError` наверх: пользовательский
    query-параметр давал бы `500`, и клиент не смог бы отличить свою ошибку
    от сбоя сервера.
    """
    _user, token = await _new_user(session)

    response = await client.get(
        "/api/v1/cards?cursor=zzz", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_cursor"}


async def test_total_does_not_shrink_between_pages(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07 (X-07): `total` — «сколько всего под фильтром», не «сколько осталось».

    Краснеет, если `COUNT` начнёт учитывать курсор: при листании счётчик
    уменьшался бы, и клиент показывал бы «всего встреч» всё меньше.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    # Больше страницы (`CARDS_PAGE_SIZE = 20`): иначе первая страница содержит
    # всё, `cursor` равен `null`, и сравнивать нечего.
    for i in range(22):
        await _insert_card(
            session, author_id=user.id, place_id=place, starts_at=_future(days=i + 1)
        )

    first = await client.get("/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"})
    cursor = first.json()["cursor"]
    assert cursor is not None

    second = await client.get(
        f"/api/v1/cards?cursor={cursor}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert first.json()["total"] == second.json()["total"]


# --- AC-04, AC-05: фильтры -----------------------------------------------------


async def test_has_seats_excludes_full_cards(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: `has_seats=true` отсекает `filled_count == capacity`.

    Краснеет от замены на `<=` (пропустил бы заполненную) или на `status='full'`
    (зависел бы от статуса, а не от арифметики AC).
    """
    user, token = await _new_user(session)
    place = await _place(session)
    # СТОРИ 4.6 (AC-07, X-35): различающий кейс «`status='full'` при свободном
    # месте» БОЛЬШЕ НЕ СУЩЕСТВУЕТ — двусторонний CHECK `0011` его запрещает.
    # Раньше тест опирался на этот дефект схемы, чтобы отличить фильтр по
    # арифметике от фильтра по статусу. Теперь различитель другой: карточка
    # со свободным местом в `published` (единственный статус ленты, где места
    # ещё есть) против карточки в `full` (мест нет по определению CHECK).
    has_free_seat = await _insert_card(
        session, author_id=user.id, place_id=place, status="published", capacity=3, filled_count=1
    )
    # Мест нет: `filled_count == capacity`.
    await _insert_card(
        session, author_id=user.id, place_id=place, status="full", capacity=2, filled_count=2
    )

    response = await client.get(
        "/api/v1/cards?has_seats=true", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert [item["uuid"] for item in response.json()["items"]] == [has_free_seat]


async def test_without_has_seats_full_cards_are_returned(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04: без флага заполненная карточка остаётся в ленте.

    Краснеет, если условие добавить всегда: фильтр стал бы поведением по
    умолчанию, и лента молча скрывала бы часть встреч.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    await _insert_card(
        session, author_id=user.id, place_id=place, status="full", capacity=2, filled_count=2
    )

    response = await client.get(
        "/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert len(response.json()["items"]) == 1


async def test_category_filter(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: `category=cinema` — только кино.

    Краснеет, если фильтр не применить: в выдаче оказались бы обе категории.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    cinema = await _insert_card(session, author_id=user.id, place_id=place, category="cinema")
    await _insert_card(session, author_id=user.id, place_id=place, category="quiz")

    response = await client.get(
        "/api/v1/cards?category=cinema", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert [item["uuid"] for item in response.json()["items"]] == [cinema]


async def test_unknown_category_is_422_with_allowed_list(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05: мусорная категория — `422 category_invalid` + `details.allowed`.

    Краснеет, если ветка `category_invalid` в `main.py` не покрывает
    `/api/v1/cards`: клиент получил бы `invalid_request` без перечня, и
    выпадашка фильтра не смогла бы его показать.
    """
    _user, token = await _new_user(session)

    response = await client.get(
        "/api/v1/cards?category=movies", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "category_invalid"
    assert "cinema" in body["details"]["allowed"]


async def test_empty_feed(client: AsyncClient, session: AsyncSession) -> None:
    """AC-09: пустая лента — `items=[]`, `total=0`, `cursor=null`.

    Краснеет, если сервис вернёт `None` вместо пустого списка: клиент не смог
    бы отличить «нет карточек» от «ошибка» и не показал бы `EmptyState`.
    """
    _user, token = await _new_user(session)

    response = await client.get(
        "/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.json() == {"items": [], "total": 0, "cursor": None}


# --- AC-02: видимость карточки -------------------------------------------------


async def test_get_card_by_uuid_returns_full_form(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: форма §7.2 — вложенные `place`/`author`, без внутренних bigint.

    Краснеет при появлении `place_id`/`author_id`/`verified_by` в схеме
    (утечка D-2) или при плоских `place_name`/`author_name` вместо объектов.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(session, author_id=user.id, place_id=place)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "uuid",
        "status",
        "verification_status",
        "created_at",
        "title",
        "description",
        "category",
        # Стори 12.4 (AC-7, FR117/FR118): тип и активность — публичные поля
        # контракта, а не внутренние адреса.
        "kind",
        "activity",
        # Стори 12.6 (AC-2a, FR119): рамки аудитории видны ДО подачи —
        # публичные поля контракта.
        "gender_filter",
        "age_min",
        "age_max",
        "starts_at",
        "expires_at",
        # Стори 5б.1 (AC-04, NFR14): серверный якорь отсчёта окна.
        "server_time",
        "capacity",
        "filled_count",
        "place",
        "author",
        "chat_uuid",
        # Стори 5б.2 (AC-09, D-5b.2-7, FR104): признак участия ЗРИТЕЛЯ.
        # Публичное поле контракта; у постороннего всегда `false`, поэтому
        # оно не различает проигравшего и наблюдателя (AC-03).
        "viewer_participates",
        # Стори 11.5 (AC-2, AC-5, FR82): «моя заявка» и её `uuid`. Публичные
        # поля контракта: у постороннего `false`/`null`, поэтому состав
        # заявившихся ими не раскрывается (FR81).
        "viewer_requested",
        "viewer_request_uuid",
    }
    assert set(body["place"]) == {"uuid", "name", "address_text"}
    assert set(body["author"]) == {"uuid", "name", "age"}
    assert body["chat_uuid"] is None


async def test_get_card_from_other_city_is_404(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02 (ADR-13): карточка другого сообщества — `404`, не `403`.

    Краснеет от снятия среза `city_slug` в билдере: участник получал бы
    карточку чужого города по прямому `uuid`, а `403` раскрыл бы её
    существование.
    """
    user, token = await _new_user(session)
    other_place = await _place(session, slug="other_city")
    card_uuid = await _insert_card(
        session, author_id=user.id, place_id=other_place, slug="other_city"
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


async def test_draft_is_visible_only_to_author(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: `draft` — `200` автору и `404` другому участнику.

    Краснеет в двух направлениях:
    - без проверки `author_id != viewer_id` любой участник читал бы чужой
      черновик (FR99: он невидим в ленте, но `uuid` мог утечь);
    - если скрыть черновик и от автора, он не смог бы открыть свою карточку.
    """
    author, author_token = await _new_user(session)
    _other, other_token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place, status="draft")

    own = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={author_token}"}
    )
    foreign = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={other_token}"}
    )

    assert own.status_code == 200
    assert own.json()["status"] == "draft"
    assert foreign.status_code == 404


async def test_unknown_card_is_404(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: несуществующая карточка — тот же `404 card_not_found`."""
    _user, token = await _new_user(session)

    response = await client.get(
        f"/api/v1/cards/{uuid_lib.uuid4()}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


# --- AC-03: ленивое закрытие окна ----------------------------------------------


async def test_get_card_closes_expired_window_lazily(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: истёкшее окно закрывается при чтении, без воркера.

    Краснеет от снятия ленивого закрытия: карточка оставалась бы `published`
    с прошедшим `expires_at`, и автор ждал бы воркера (NFR21 требует ≤5 мин,
    а воркера в 4.5 нет вовсе).
    """
    user, token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(
        session,
        author_id=user.id,
        place_id=place,
        starts_at=_future(hours=2),
        expires_at=_future(hours=-1),
        status="published",
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"
    assert await _card_status(session, card_uuid) == "closed"


async def test_lazy_close_is_idempotent(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (ADR-03): повторное чтение не меняет ничего и не падает.

    Краснеет, если CAS не содержит `status='published'`: второй `UPDATE`
    перезаписывал бы `closed_at` и «продлевал» закрытие.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(
        session,
        author_id=user.id,
        place_id=place,
        starts_at=_future(hours=2),
        expires_at=_future(hours=-1),
    )

    first = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )
    second = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert first.json()["status"] == "closed"
    assert second.json()["status"] == "closed"
    assert await _card_status(session, card_uuid) == "closed"


async def test_published_card_with_open_window_stays_published(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: окно в будущем — карточка остаётся `published`.

    Краснеет, если снять условие `expires_at <= now` из CAS: чтение закрывало
    бы карточку раньше срока, пока автор ещё выбирает.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(
        session,
        author_id=user.id,
        place_id=place,
        starts_at=_future(days=2),
        expires_at=_future(days=1),
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"}
    )

    assert response.json()["status"] == "published"


async def test_feed_does_not_close_windows(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, D-4.5-4: лента НЕ закрывает окна (иначе N `UPDATE` на один `GET`).

    Краснеет, если добавить ленивое закрытие в `list_cards`: один запрос
    ленты писал бы столько строк, сколько карточек с истёкшим окном, — то есть
    `GET` превращался бы в пакетный `UPDATE`.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(
        session,
        author_id=user.id,
        place_id=place,
        starts_at=_future(hours=2),
        expires_at=_future(hours=-1),
    )

    await client.get("/api/v1/cards", headers={"Cookie": f"{ACCESS_COOKIE_NAME}={token}"})

    assert await _card_status(session, card_uuid) == "published"
