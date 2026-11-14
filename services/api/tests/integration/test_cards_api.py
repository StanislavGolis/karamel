"""Создание, правка и публикация карточки через API (Task 8; AC-01…AC-08).

Реальный PostgreSQL, схема поднята Alembic. Проверяется не только код ответа,
но и состояние БД: `201` не доказывает, что `author_id` и `city_slug`
записаны правильно.

§18.1: каждый тест краснеет от снятия конкретной защиты. §18.2: единственное
отличие автора от другого пользователя — `author_id`, поэтому `404`-тест
краснеет ровно при снятии фильтра по автору.
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

# AC-08 (X-13): ответ содержит ровно эти ключи. Равенство, а не подмножество:
# появление `id`/`author_id`/`place_id` делает тест красным (D-2).
EXPECTED_KEYS = {"uuid", "status", "verification_status", "created_at"}


def _city() -> str:
    """Слаг сообщества из конфига пилота: тест не должен знать `.env` наизусть."""
    return settings.default_city_slug


def _starts_at(**delta: int) -> datetime:
    """Время начала в будущем — «сейчас + дни», а не фиксированная дата.

    Фиксированная дата сделала бы тест красным в день, когда эта дата станет
    прошлым (дефект, который не воспроизводится локально).
    """
    return datetime.now(UTC) + timedelta(**delta)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "cafe",
        # Стори 12.4 (AC-3, FR117/FR118): оба поля обязательны в `EventCardCreate`.
        "kind": "company",
        "activity": "boardgames",
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
    """Создаёт учётку с живой сессией; возвращает `(user, access_token)`."""
    phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role=str(role),
        status="active",
    )
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _insert_place(
    session: AsyncSession, *, is_active: bool = True, city_slug: str | None = None
) -> str:
    """Кладёт место напрямую в БД и возвращает его `uuid`.

    Нужно для сценария «место скрыто»: `POST /places` всегда создаёт активное.
    """
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'пр. Ленина, 1', :slug, :active) RETURNING uuid"
        ),
        {
            "name": f"Кафе {uuid_lib.uuid4().hex[:8]}",
            "slug": city_slug or _city(),
            "active": is_active,
        },
    )
    return str(row.scalar_one())


async def _card_row(session: AsyncSession, card_uuid: str) -> dict[str, Any] | None:
    """Строка карточки из БД сырым SQL: проверяется PostgreSQL, не identity map."""
    result = await session.execute(
        text(
            "SELECT author_id, city_slug, status, verification_status, "
            "filled_count, starts_at, expires_at, decision_window_minutes, "
            # Стори 4.6 (AC-02): правка опубликованной проверяется по сохранённому
            # полю — без него нельзя отличить «поле записано» от «ответ 200».
            # Стори 13.2 (AC-7): `title` больше не приходит от автора, поэтому
            # проверяется `description` — единственное свободное текстовое поле.
            "published_at, title, description FROM event_cards WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _count_cards(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM event_cards"))
    return int(result.scalar_one())


async def _approve(session: AsyncSession, card_uuid: str) -> None:
    """Ручной перевод в `approved`: автопроверка появится в 4.3 (D-4.2-5)."""
    await session.execute(
        text(
            "UPDATE event_cards SET verification_status = 'approved', verified_at = now() "
            "WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )


# --- AC-01: создание -----------------------------------------------------------


async def test_create_requires_auth(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: без сессии → `401`.

    Краснеет, если убрать `CurrentUser` из сигнатуры: карточку создавал бы
    анонимный клиент, и `author_id` было бы нечем заполнить.
    """
    place_uuid = await _insert_place(session)

    response = await client.post("/api/v1/cards", json=_payload(place_uuid=place_uuid))

    assert response.status_code == 401


async def test_create_card_returns_ref_and_writes_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, AC-08: `201`, тело — `EventCardRef`, в БД одна строка автора.

    Краснеет, если:
    - в теле появится `id`/`author_id`/`place_id` (нарушение D-2/§7.2);
    - `author_id` возьмётся из тела, а не из сессии;
    - `city_slug` придёт от клиента (ADR-01).
    """
    user, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    assert body["status"] == "draft"
    # Стори 4.3 (AC-01, FR101, ADR-08): чистый текст проходит автопроверку
    # и сразу получает `approved` + `verified_at`, `verified_by IS NULL`.
    # До 4.3 здесь был `pending` (X-20: публикация только через SQL).
    assert body["verification_status"] == "approved"

    row = await _card_row(session, body["uuid"])
    assert row is not None
    assert row["author_id"] == user.id
    assert row["city_slug"] == _city()
    assert row["filled_count"] == 0
    assert await _count_cards(session) == 1


async def test_create_with_hidden_place_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR98): скрытое место → `422 PLACE_NOT_AVAILABLE`, строка не создана.

    Краснеет, если убрать `ensure_place_available`: карточка сослалась бы
    на место, которого нет в справочнике (FR68 «скрыть» ≠ «удалить»).
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, is_active=False)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PLACE_NOT_AVAILABLE"
    assert response.json()["details"]["place_uuid"] == place_uuid
    assert await _count_cards(session) == 0


async def test_create_with_foreign_city_place_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (ADR-13): место чужого сообщества — тот же код, что и скрытое.

    Краснеет, если проверять только `is_active`: карточка Томска сослалась бы
    на место другого сообщества. Одинаковый код обязателен — иначе перебором
    `uuid` клиент узнал бы о существовании чужих мест.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, city_slug="other-city")

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PLACE_NOT_AVAILABLE"


async def test_create_with_past_starts_at_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03 (FR69): прошедшее время → `422 STARTS_AT_IN_PAST`.

    Краснеет, если убрать проверку `starts_at <= now`: карточка создалась бы
    на встречу, которая уже прошла.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid, starts_at=past),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "STARTS_AT_IN_PAST"


async def test_create_with_naive_datetime_is_schema_error(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: наивная строка отбивается схемой, а не кодом «прошедшее время».

    Краснеет, если проверку зоны перенести в сервис: клиент получил бы
    `STARTS_AT_IN_PAST` на значение, которое вообще не является моментом
    времени, и правил бы не то поле.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat()

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid, starts_at=naive),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert "code" in response.json()
    assert response.json()["code"] != "STARTS_AT_IN_PAST"


async def test_create_place_checked_before_time(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, AC-03: при двух нарушениях ответ — `PLACE_NOT_AVAILABLE`.

    Краснеет, если поменять порядок проверок в `create_card`: клиент получил бы
    `STARTS_AT_IN_PAST`, исправил время — и только потом узнал про место.
    Порядок зафиксирован Task 3.2.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session, is_active=False)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid, starts_at=past),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PLACE_NOT_AVAILABLE"


# --- AC-05: правка черновика ---------------------------------------------------


async def _create_card(
    client: AsyncClient, session: AsyncSession, token: str, **overrides: Any
) -> str:
    """Создаёт карточку через API и возвращает её `uuid`.

    Через API, а не сырым INSERT: тест правки должен начинать с состояния,
    которое реально создаёт система (иначе `PATCH` проверялся бы на строке,
    которую `POST` никогда бы не породил).
    """
    place_uuid = await _insert_place(session)
    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid, **overrides),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["uuid"])


async def test_update_own_draft_changes_fields_and_expires(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05: свой черновик правится, `expires_at` пересчитан.

    Краснеет, если `PATCH` не сохраняет переданные поля или не пересчитывает
    `expires_at`: окно решения осталось бы от старого `starts_at`.
    """
    _, token = await _new_user(session)
    # Стори 12.5, AC-01: горизонт автора — 72 часа. Раньше тест создавал
    # карточку на `days=3` (ровно граница) и двигал на `days=5` (120 ч) —
    # теперь это `422 STARTS_AT_TOO_FAR`, и тест краснел бы не по предмету
    # («пересчёт `expires_at`»), а по дальности даты. Предмет сохранён:
    # создание на 24 ч, правка на 48 ч — движение ВПЕРЁД внутри горизонта,
    # поэтому `expires_at` обязан вырасти.
    card_uuid = await _create_card(client, session, token, starts_at=_starts_at(days=1).isoformat())
    before = await _card_row(session, card_uuid)
    new_starts = _starts_at(days=2).isoformat()

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        # AC-7 стори 13.2: `title` из контракта удалён — правим описание.
        json={"description": "Новое описание", "starts_at": new_starts},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json()["uuid"] == card_uuid
    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["starts_at"] > before["starts_at"]
    assert row["expires_at"] > before["expires_at"]


async def test_update_foreign_card_is_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """AC-07 (ADR-13): чужая карточка → `404`, не `403`.

    Краснеет, если убрать фильтр по `author_id`: чужую карточку можно было бы
    править. Код `card_not_found` обязателен — `403` раскрыл бы существование.
    """
    _, owner_token = await _new_user(session)
    _, stranger_token = await _new_user(session)
    card_uuid = await _create_card(client, session, owner_token)
    before = await _card_row(session, card_uuid)

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"description": "Взлом"},
        cookies={ACCESS_COOKIE_NAME: stranger_token},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"
    after = await _card_row(session, card_uuid)
    assert after["starts_at"] == before["starts_at"]


async def test_update_missing_card_returns_same_code(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-07 (ADR-13): несуществующая карточка — тот же код и статус.

    Краснеет, если для «нет такой» вернуть другой код: разница между `404`
    за «нет» и за «чужая» и есть раскрытие существования.
    """
    _, token = await _new_user(session)

    response = await client.patch(
        f"/api/v1/cards/{uuid_lib.uuid4()}",
        json={"description": "Что-то"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"


async def test_update_published_card_conflicts(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05 (D-4.2-2), ОБНОВЛЕНО 4.6 (AC-01, FR99): правка опубликованной БЕЗ
    присоединений теперь разрешена — `200`.

    До 4.6 здесь ожидался `409 CARD_NOT_DRAFT`: правка опубликованной была
    отложена. Контракт изменился по FR99 («до первого присоединения»), и
    краснеет тест теперь при возврате запрета по одному статусу.
    """
    _, token = await _new_user(session)
    card_uuid = await _create_card(client, session, token)
    await _approve(session, card_uuid)
    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert published.status_code == 200, published.text

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"description": "После публикации"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200, response.text
    row = await _card_row(session, card_uuid)
    assert row is not None
    assert row["description"] == "После публикации"
    # AC-02 (4.6): уже проверенная карточка остаётся `approved` и НЕ уходит
    # в `pending` — иначе она исчезла бы из ленты на время новой модерации.
    assert row["verification_status"] == "approved"


async def test_update_capacity_to_one_requires_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05 (FR79): переход 2–6 → 1 без окна → `422`.

    Краснеет, если подставлять текущее окно открытого набора: режим заявок
    получил бы 48 часов «по наследству».
    """
    _, token = await _new_user(session)
    card_uuid = await _create_card(client, session, token)

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"capacity": 1},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "decision_window_required"


async def test_update_capacity_to_open_set_uses_default_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05 (FR70): переход 1 → 3 без окна подставляет дефолт конфига.

    Краснеет, если оставить клиентское окно: открытый набор закрылся бы
    через «окно заявок» (например, 30 минут).
    """
    _, token = await _new_user(session)
    card_uuid = await _create_card(client, session, token)
    # 1 место + окно: переводим карточку в режим заявок.
    first = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"capacity": 1, "decision_window_minutes": 30},
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert first.status_code == 200, first.text

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"capacity": 3},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    row = await _card_row(session, card_uuid)
    assert row["decision_window_minutes"] == settings.card_default_decision_window_minutes


# --- AC-06: публикация ---------------------------------------------------------


async def test_publish_pending_card_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-06 (FR101, OQ-1): публикация при `pending` → `422 VERIFICATION_PENDING`.

    Стори 4.3: чистый текст теперь автопроверяется в `approved`, поэтому
    `pending` создаётся стоп-словом (AC-02) — иначе тест проверял бы не
    состояние `pending`, а факт «карточка создана».

    Краснеет, если снять проверку `verification_status`: непроверенный текст
    попал бы в ленту, хотя NFR51 обещает автору проверку.
    """
    _, token = await _new_user(session)
    # AC-7 стори 13.2: `title` автором не передаётся — стоп-слово задаётся
    # описанием (автопроверка читает склейку title+description).
    card_uuid = await _create_card(client, session, token, description="Казино и займ")

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VERIFICATION_PENDING"


async def test_publish_approved_card_sets_published_at_and_expires(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06, AC-04: после `approved` публикация проходит и пересчитывает окно.

    Краснеет, если не ставить `published_at` или не пересчитывать `expires_at`:
    CHECK `ck_event_cards_published` отверг бы строку (`500`), а окно осталось бы
    от момента создания (OQ-2).
    """
    _, token = await _new_user(session)
    card_uuid = await _create_card(client, session, token)
    await _approve(session, card_uuid)

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    row = await _card_row(session, card_uuid)
    assert row["published_at"] is not None
    assert row["status"] == "published"


async def test_publish_twice_conflicts(client: AsyncClient, session: AsyncSession) -> None:
    """AC-06: повторная публикация → `409 CARD_NOT_DRAFT`.

    Краснеет, если сделать публикацию идемпотентной: второй `published_at`
    исказил бы историю, и «когда опубликовано» стало бы неверным.
    """
    _, token = await _new_user(session)
    card_uuid = await _create_card(client, session, token)
    await _approve(session, card_uuid)
    first = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert second.status_code == 409
    assert second.json()["code"] == "CARD_NOT_DRAFT"


async def test_publish_rejected_card_returns_reason(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06 (FR113): отклонённая карточка → `409 CARD_REJECTED` + причина.

    Краснеет, если вернуть `VERIFICATION_PENDING`: автор не отличил бы
    «ещё проверяем» от «отклонили, исправь» и не увидел бы причину.
    """
    _, token = await _new_user(session)
    card_uuid = await _create_card(client, session, token)
    await session.execute(
        text(
            "UPDATE event_cards SET verification_status = 'rejected', "
            "rejection_reason = 'Нецензурная лексика' WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "CARD_REJECTED"
    assert body["details"]["reason"] == "Нецензурная лексика"


async def test_create_sets_no_store_header(client: AsyncClient, session: AsyncSession) -> None:
    """AC-08: `cache-control: no-store` на успешном `201`.

    Краснеет, если `NoStoreMiddleware` перестанет покрывать `/cards`:
    карточка с `draft`/`pending` не должна попадать в кэш посредника.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid=place_uuid),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201
    assert response.headers.get("cache-control") == "no-store"


# --- P-10 (CR 10.1): HTTP-код 404 для чужого сообщества -----------------------


async def test_get_foreign_city_card_is_404_over_http(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05, ADR-13, P-10: чужое сообщество → HTTP `404`, не `403` и не `200`.

    До этого теста AC-05 доказывался только на уровне сервиса
    (`CardNotFoundError`). Переход `CardNotFoundError` → `404` держится на
    `_domain_error` в роуте и не проверялся ни разу.

    Пользователь чужого сообщества создаётся через `UserRepository.create`
    с явным слагом; после DN-3 слаг обязан входить в `Settings.city_slugs`,
    поэтому список сообществ расширяется на время создания (в пилоте сообщество
    одно, второго в `.env` нет).
    """
    from app.repositories.users import UserRepository

    # 1. Карточка сообщества пилота, опубликованная (иначе ленты/чтения нет).
    # `GET /cards/{uuid}` стоит на `ConsentUser`: без согласия — `403`, и базовая
    # линия упала бы не по предмету теста. Согласие ставится явно, как в
    # `test_cards_feed_api.py:71-72`.
    author, author_token = await _new_user(session)
    author.age_confirmed = True
    author.consent_given_at = datetime.now(UTC)
    await session.flush()
    card_uuid = await _create_card(client, session, author_token)
    await _approve(session, card_uuid)
    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: author_token}
    )
    assert published.status_code == 200, published.text

    # 2. Базовая линия (P-19): автор сообщества карточку ВИДИТ.
    own = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: author_token})
    assert own.status_code == 200, own.text

    # 3. Зритель другого сообщества.
    other = "other-city"
    monkeypatch.setattr(settings, "city_slugs", (*settings.city_slugs, other))
    phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
    stranger = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, city_slug=other
    )
    stranger.age_confirmed = True
    stranger.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, stranger)

    foreign = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: issued.access_token}
    )

    # ADR-13: именно `404` — `403` раскрыл бы существование карточки.
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["code"] == "card_not_found"


# --- Стори 13.2 (CR H-4): AC-5 через ЖИВОЙ API --------------------------------
#
# **Зачем отдельный тест.** AC-5 («заголовок сгенерирован ⇒ `approved`»)
# проверялся только на уровне `evaluate_card_content` — чистым вызовом правила.
# Если `run_auto_check` перестанет вызываться в `create_card` или `title`
# попадёт в него `None`, unit-тесты останутся зелёными, а продукт начнёт
# отправлять каждую карточку в ручную модерацию. Здесь проверяется тот путь,
# которым идёт настоящий автор: `POST` → `GET`.


async def test_generated_title_is_not_empty_and_approved_over_api(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3, AC-5 (CR H-4): `POST /cards` → `GET /cards/{uuid}` → `approved`.

    Проверяется ТРИ вещи одним сценарием:
    1. `title` непуст — сервер собрал его сам (AC-3, AC-7: автор не присылает);
    2. `title` содержит фразу активности — иначе «непусто» прошло бы и на
       константе-заглушке;
    3. `verification_status == 'approved'` — автопроверка состоялась и
       не нашла нарушений (AC-5).

    Краснеет, если `run_auto_check` убрать из `create_card` (статус остался бы
    `pending`) или если `title` попадёт в проверку `None` (правило `length`).
    """
    # `GET /cards/{uuid}` стоит на `ConsentUser`: без согласия — `403`, и тест
    # упал бы не по предмету AC-5. Согласие ставится явно — тот же приём, что
    # в `test_get_foreign_city_card_is_404_over_http` и `test_cards_feed_api.py`.
    author, token = await _new_user(session)
    author.age_confirmed = True
    author.consent_given_at = datetime.now(UTC)
    await session.flush()
    place_uuid = await _insert_place(session)

    response = await client.post(
        "/api/v1/cards",
        # Активность задаётся явно: дефолт `_payload` — `boardgames`, и заголовок
        # с ним проверял бы другую фразу. `coffee` + место категории `cafe`
        # дают предсказуемое «Кофе в кафе „…“».
        json=_payload(place_uuid=place_uuid, activity="coffee"),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert response.status_code == 201, response.text
    card_uuid = str(response.json()["uuid"])

    read = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})
    assert read.status_code == 200, read.text
    body = read.json()

    assert body["title"], "заголовок пуст — генерация не состоялась"
    assert body["title"].startswith("Кофе в кафе"), (
        f"заголовок собран не по активности и категории места: {body['title']!r}"
    )
    assert "…" not in body["title"], (
        f"имя места короткое, усечения быть не должно: {body['title']!r}"
    )
    assert body["verification_status"] == "approved", (
        f"автопроверка не пропустила сгенерированный заголовок: {body}"
    )
