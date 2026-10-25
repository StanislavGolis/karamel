"""Окно решения при публикации через API (стори 5б.1; AC-01…AC-04).

Реальный PostgreSQL, схема поднята Alembic. Карточки в нужном состоянии
кладутся СЫРЫМ SQL: `POST /cards` не даёт ни истёкшего `expires_at`, ни
`status='published'` без публикации — а ровно эти состояния и есть предмет
проверки (AC-02).

§18.1: каждый тест краснеет от снятия конкретной защиты (см. docstring).
§18.2: проверяется наблюдаемое следствие — строка в БД или код ответа.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1.join_requests import get_join_request_rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.card_join_service import CardNotAcceptingError, join_open_card
from app.services.event_card_service import get_card
from app.services.join_request_service import submit_join_request
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class _CardSettings:
    """Минимум настроек сервиса карточек: слаг сообщества без чтения `.env`.

    `community_slugs` обязателен: сервис сверяет с ним слаг (ADR-01) и на
    отсутствии поля падает `AttributeError` до всякой логики — заглушка
    обязана повторять протокол, а не «то, что показалось нужным».
    """

    default_community_slug = settings.default_community_slug
    community_slugs = [settings.default_community_slug]


class _JoinSettings:
    """Минимум настроек сервиса присоединения: протокол без чтения `.env`."""

    default_community_slug = settings.default_community_slug


class _ReqSettings:
    """Минимум настроек `submit_join_request`: протокол без чтения `.env`."""

    default_community_slug = settings.default_community_slug
    join_request_active_limit = 2
    s3_signed_url_ttl = 900


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _community() -> str:
    return settings.default_community_slug


def _future(**delta: int) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


class _NoRateLimit:
    """AC-10: технический лимит подач в этих тестах не проверяется.

    Без подмены `get_join_request_rate_limiter` реальный Redis-клиент
    (`app.core.redis._client`) создаётся на event loop первого теста и
    переиспользуется следующим: `RuntimeError: got Future attached to
    a different loop` (долг изоляции X-5a.6-3). Тот же приём, что в
    `test_card_requests_api.py`.
    """

    async def hit(self, applicant_id: int) -> int | None:
        return None


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменёнными зависимостями сессии и лимитера."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_join_request_rate_limiter] = _NoRateLimit
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_join_request_rate_limiter, None)


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _community()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    starts_at: datetime,
    expires_at: datetime,
    status: str = "published",
    capacity: int = 2,
    filled_count: int = 0,
    window_minutes: int = 60,
) -> str:
    """Карточка в нужном состоянии — сырым SQL, минуя правила сервиса."""
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, "
            ":window, :expires, :status, :filled, 'approved', :published_at) RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": _community(),
            "starts": starts_at,
            "capacity": capacity,
            "window": window_minutes,
            "expires": expires_at,
            "status": status,
            "filled": filled_count,
            "published_at": starts_at - timedelta(days=1),
        },
    )
    return str(row.scalar_one())


async def _card_expires_at(session: AsyncSession, card_uuid: str) -> datetime:
    row = await session.execute(
        text("SELECT expires_at FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    value = row.scalar_one()
    assert isinstance(value, datetime)
    return value


async def _create_draft(client: AsyncClient, token: str, place_id: int, **overrides: Any) -> str:
    """Черновик через API: публикация проверяется на состоянии, которое создал `POST`."""
    place_uuid = await _place_uuid(client, token, place_id)
    payload: dict[str, Any] = {
        "title": "Настольные игры",
        "category": "cafe",
        "place_uuid": place_uuid,
        "starts_at": _future(days=3).isoformat(),
        "capacity": 3,
    }
    payload.update(overrides)
    response = await client.post("/api/v1/cards", json=payload, cookies={ACCESS_COOKIE_NAME: token})
    assert response.status_code == 201, response.text
    return str(response.json()["uuid"])


async def _place_uuid(client: AsyncClient, token: str, place_id: int) -> str:
    """Публичный `uuid` уже вставленного места (карточка адресуется им, D-2)."""
    row = await client.get("/api/v1/places", cookies={ACCESS_COOKIE_NAME: token})
    assert row.status_code == 200, row.text
    for item in row.json()["items"]:
        if item["uuid"]:
            return str(item["uuid"])
    raise AssertionError("в сообществе нет ни одного места")


# --- AC-01: формула и «не пересчитывается» ------------------------------------


async def test_publish_sets_expires_from_window_once(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (ADR-02): `expires_at = now + окно` при публикации, один раз.

    Краснеет, если публикация не пересчитывает окно: осталось бы значение от
    создания карточки, и автор получил бы окно, начинающееся раньше задуманного.
    """
    user, token = await _new_user(session)
    card_uuid = await _create_draft(
        client, token, await _place(session), capacity=1, decision_window_minutes=120
    )

    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert published.status_code == 200, published.text
    expires = await _card_expires_at(session, card_uuid)
    delta = expires - datetime.now(UTC)
    assert timedelta(minutes=118) <= delta <= timedelta(minutes=121), delta


async def test_expires_at_is_not_recomputed_by_first_request(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (ADR-02): первая заявка НЕ сдвигает `expires_at`.

    Краснеет, если пересчитать окно в `submit_join_request` или в триггере:
    окно начиналось бы с первой заявки, и «до закрытия» росло бы от поступления
    заявившихся — то есть решение можно было бы откладывать бесконечно.
    """
    author, author_token = await _new_user(session)
    applicant, applicant_token = await _new_user(session)
    card_uuid = await _create_draft(
        client, author_token, await _place(session), capacity=1, decision_window_minutes=120
    )
    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: author_token}
    )
    assert published.status_code == 200, published.text
    before = await _card_expires_at(session, card_uuid)

    created = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "Хочу присоединиться"},
        cookies={ACCESS_COOKIE_NAME: applicant_token},
    )

    assert created.status_code == 201, created.text
    after = await _card_expires_at(session, card_uuid)
    assert after == before, "окно решения пересчиталось от первой заявки"
    assert applicant.id != author.id


# --- AC-03: усечение окна по `starts_at` --------------------------------------


async def test_window_longer_than_time_to_start_is_clamped(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03: окно больше времени до встречи → `expires_at == starts_at`.

    Краснеет от снятия `min` в `_compute_expires_at`: CHECK
    `ck_event_cards_expires_at` отверг бы строку, и публикация отдавала бы
    `500` вместо усечения.
    """
    _, token = await _new_user(session)
    starts_at = _future(minutes=30)
    card_uuid = await _create_draft(
        client,
        token,
        await _place(session),
        capacity=1,
        decision_window_minutes=120,
        starts_at=starts_at.isoformat(),
    )

    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert published.status_code == 200, published.text
    expires = await _card_expires_at(session, card_uuid)
    assert expires == starts_at, "окно обязано усечься по началу встречи"


# --- AC-02: лента ------------------------------------------------------------


async def test_feed_hides_published_card_with_expired_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (NFR20): истёкшее окно — карточки нет в ленте как открытой.

    Краснеет от снятия предиката окна из `build_feed_statement`: до прогона
    воркера (до 60 с) карточка висела бы в ленте, хотя решение уже не
    принимается.
    """
    _, token = await _new_user(session)
    expired_uuid = await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=2),
        expires_at=_future(minutes=-5),
    )

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    uuids = [item["uuid"] for item in response.json()["items"]]
    assert expired_uuid not in uuids


async def test_feed_keeps_full_card_with_expired_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: `full` с истёкшим окном ОСТАЁТСЯ в ленте.

    Краснеет, если привязать предикат окна ко всем статусам: `full` — это
    «места кончились сами» (ADR-10), участие возможно после отмены чужого
    участия (5а.6), и окно решения тут не при чём.
    """
    _, token = await _new_user(session)
    full_uuid = await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=2),
        expires_at=_future(minutes=-5),
        status="full",
        capacity=2,
        filled_count=2,
    )

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    uuids = [item["uuid"] for item in response.json()["items"]]
    assert full_uuid in uuids


async def test_feed_does_not_close_expired_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, D-4.5-4: лента только ЧИТАЕТ — статус в БД не меняется.

    Краснеет, если добавить ленивое закрытие в `list_cards`: один `GET` ленты
    писал бы столько строк, сколько карточек с истёкшим окном (регресс CR 4.5).
    """
    _, token = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=2),
        expires_at=_future(minutes=-5),
    )

    await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    row = await session.execute(
        text("SELECT status FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    assert str(row.scalar_one()) == "published"


# --- AC-02: заявка и присоединение -------------------------------------------


async def test_join_on_expired_window_is_not_accepting(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: присоединение на истёкшее окно → `409 CARD_NOT_ACCEPTING`.

    Краснеет от снятия гарда `expires_at <= now` в `join_open_card`: участник
    занял бы место в наборе, где решение уже не принимается, и место осталось
    бы занятым мёртвой карточкой.
    """
    _, token = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=2),
        expires_at=_future(minutes=-5),
        capacity=3,
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/join", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"
    row = await session.execute(
        text("SELECT filled_count FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    assert int(row.scalar_one()) == 0, "место не должно заниматься"


async def test_request_on_expired_window_is_not_accepting(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02: заявка на истёкшее окно → `409 CARD_NOT_ACCEPTING`.

    Краснеет от снятия гарда в `submit_join_request`: заявка попала бы в список
    автора после `expires_at`, и автовыбор 5б.3 рассматривал бы её как
    актуальную, хотя окно уже закрыто.
    """
    _, token = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=2),
        expires_at=_future(minutes=-5),
        capacity=1,
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "Я хочу прийти"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CARD_NOT_ACCEPTING"
    row = await session.execute(
        text(
            "SELECT count(*) FROM join_requests "
            "WHERE card_id = (SELECT id FROM event_cards WHERE uuid = :uuid)"
        ),
        {"uuid": card_uuid},
    )
    assert int(row.scalar_one()) == 0, "заявка не должна создаваться"


# --- AC-04: серверный якорь отсчёта ------------------------------------------


async def test_card_read_provides_server_time_close_to_expires(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (NFR14): `server_time` есть и согласован с `expires_at`.

    Краснеет, если отдавать `datetime.now()` из роутера при `moment`, взятом
    раньше (или если поле не заполнено вовсе — `model_validate` упал бы):
    клиент считал бы остаток от точки, расходящейся с сервером.
    """
    _, token = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=3),
        expires_at=_future(hours=1),
    )

    response = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "server_time" in body
    server_time = datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))
    assert abs((server_time - datetime.now(UTC)).total_seconds()) <= 5
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert expires > server_time, "окно открыто — якорь обязан быть раньше"


async def test_feed_items_carry_server_time(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: каждая карточка ленты несёт якорь — иначе `CountdownBadge`
    в ленте продолжал бы считать по часам устройства.

    Краснеет, если заполнять `server_time` только в детальном экране.
    """
    _, token = await _new_user(session)
    await _insert_card(
        session,
        author_id=(await _new_user(session))[0].id,
        place_id=await _place(session),
        starts_at=_future(hours=3),
        expires_at=_future(hours=1),
    )

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    items = response.json()["items"]
    assert items, "в ленте обязана быть хотя бы одна карточка"
    for item in items:
        assert item["server_time"], item


# --- Task 5.1: порядок блокировок не изменился (ADR-09) -----------------------


async def test_expired_window_guard_keeps_lock_order_and_writes_nothing() -> None:
    """AC-02, ADR-09 (паттерн 5а.6): гард окна НЕ добавляет блокировок и записей.

    Гонка порядок блокировок не доказывает (две транзакции на разных карточках
    не встретятся), поэтому последовательность операторов фиксируется по
    `before_cursor_execute` на СОБСТВЕННОМ движке: коммиты настоящие, соединение
    контролируемо.

    Краснеет от двух правок: (1) гард окна ПОСЛЕ `UPDATE` мест — тогда появился
    бы `UPDATE event_cards` при истёкшем окне и место утекло бы; (2) ленивое
    `close_if_due` внутри гарда — лишний `UPDATE event_cards` под блокировкой
    `users`, удлиняющий транзакцию присоединения.

    Проверяется ровно обещанное: первый `FOR UPDATE` — по `users`, а
    `event_cards`/`join_requests` не пишутся вовсе.
    """
    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    statements: list[str] = []
    # Собственный движок коммитит по-настоящему (иначе порядок блокировок не
    # наблюдать), поэтому строки убираются вручную: иначе следующий тест
    # увидит чужие места и пользователей в своей общине — это ровно долг
    # изоляции X-5a.6-3, который нельзя усугублять новым тестом.
    created_user_ids: list[int] = []

    def _capture(_conn: object, _cursor: object, statement: str, *args: object) -> None:
        statements.append(" ".join(statement.split()))

    try:
        async with factory() as seed:
            author = await UserRepository(seed).create(
                provider="phone",
                provider_user_id=f"+7906{uuid_lib.uuid4().int % 10**7:07d}",
                phone=f"+7906{uuid_lib.uuid4().int % 10**7:07d}",
                role="user",
                status="active",
            )
            actor = await UserRepository(seed).create(
                provider="phone",
                provider_user_id=f"+7906{uuid_lib.uuid4().int % 10**7:07d}",
                phone=f"+7906{uuid_lib.uuid4().int % 10**7:07d}",
                role="user",
                status="active",
            )
            created_user_ids.extend([author.id, actor.id])
            place_id = await _place(seed)
            card_uuid = await _insert_card(
                seed,
                author_id=author.id,
                place_id=place_id,
                starts_at=_future(hours=2),
                expires_at=_future(minutes=-5),
                capacity=3,
            )
            await seed.commit()
            actor_id = actor.id

        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            async with factory() as session:
                with pytest.raises(CardNotAcceptingError):
                    await join_open_card(
                        session,
                        actor_id=actor_id,
                        card_uuid=uuid_lib.UUID(card_uuid),
                        settings=_JoinSettings(),
                    )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)
    finally:
        async with factory() as cleanup:
            if created_user_ids:
                await cleanup.execute(
                    text(
                        "DELETE FROM join_requests WHERE card_id IN "
                        "(SELECT id FROM event_cards WHERE author_id = ANY(:users))"
                    ),
                    {"users": created_user_ids},
                )
                await cleanup.execute(
                    text("DELETE FROM event_cards WHERE author_id = ANY(:users)"),
                    {"users": created_user_ids},
                )
                await cleanup.execute(
                    text(
                        "DELETE FROM places WHERE id NOT IN "
                        "(SELECT place_id FROM event_cards) AND community_slug = :slug"
                    ),
                    {"slug": settings.default_community_slug},
                )
                await cleanup.execute(
                    text("DELETE FROM users WHERE id = ANY(:users)"), {"users": created_user_ids}
                )
                await cleanup.commit()
        await engine.dispose()

    upper = [statement.upper() for statement in statements]
    assert upper, "ни одного оператора не зафиксировано"
    assert any("FOR UPDATE" in s and "USERS" in s for s in upper), upper
    assert not [s for s in upper if "UPDATE EVENT_CARDS" in s], upper
    assert not [s for s in upper if "JOIN_REQUESTS" in s and "INSERT" in s], upper


async def test_server_time_comes_from_injected_moment(session: AsyncSession) -> None:
    """AC-04 (NFR14, §18.2): `server_time` — РОВНО переданный `moment`.

    Тест уровня сервиса с инъекцией времени: только так видно, что якорь не
    `datetime.now()`, а та же точка, по которой принимались решения о переходах.
    Толерантная проверка «в пределах 5 с» этот дефект НЕ ловила бы.

    Краснеет от возврата `datetime.now()` в сборке ответа (M7): якорь отличался
    бы от `expires_at` на реальный сдвиг, и клиент считал бы остаток от чужой
    точки отсчёта.
    """
    author, _ = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=author.id,
        place_id=await _place(session),
        starts_at=_future(hours=3),
        expires_at=_future(hours=1),
    )
    moment = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

    item = await get_card(
        session,
        card_uuid=uuid_lib.UUID(card_uuid),
        viewer_id=author.id,
        settings=_CardSettings(),
        now=moment,
    )

    assert item.server_time == moment


# --- Граница `expires_at == now` (CR 5б.1, мутации M10/M15) -------------------


async def test_join_at_exact_expiry_moment_is_not_accepting(
    session: AsyncSession,
) -> None:
    """AC-02: гард окна ВКЛЮЧИТЕЛЬНЫЙ — `expires_at == now` уже не принимает.

    Краснеет от подмены `<=` на `<` в `join_open_card`: в момент истечения
    окно считалось бы открытым, и участник занял бы место в карточке, где
    решение уже не принимается. Без инъекции `now` границу не выразить.
    """
    author, _ = await _new_user(session)
    actor, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        starts_at=_future(hours=2),
        expires_at=_future(hours=1),
        capacity=2,
    )
    stored = await _card_expires_at(session, card_uuid)

    with pytest.raises(CardNotAcceptingError):
        await join_open_card(
            session,
            actor_id=actor.id,
            card_uuid=uuid_lib.UUID(card_uuid),
            settings=_JoinSettings(),
            now=stored,
        )


async def test_request_at_exact_expiry_moment_is_not_accepting(
    session: AsyncSession,
) -> None:
    """AC-02: та же включительная граница для подачи заявки.

    Краснеет от подмены `<=` на `<` в `submit_join_request` (M15): заявка
    создавалась бы ровно в момент `expires_at` и попала бы в автовыбор 5б.3
    как актуальная. Тест требует инъекции `now` — без неё гард проверяется
    только «заведомо истёкшим» временем, где `<` и `<=` неразличимы.
    """
    author, _ = await _new_user(session)
    applicant, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        starts_at=_future(hours=2),
        expires_at=_future(hours=1),
        capacity=1,
    )
    stored = await _card_expires_at(session, card_uuid)

    with pytest.raises(CardNotAcceptingError):
        await submit_join_request(
            session,
            applicant_id=applicant.id,
            card_uuid=uuid_lib.UUID(card_uuid),
            message="Я хочу прийти",
            settings=_ReqSettings(),
            now=stored,
        )
