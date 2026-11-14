"""Состояния карточки через API: правка, закрытие, отмена, архив (стори 4.6, Task 9).

AC-01…AC-05, AC-09. Реальный PostgreSQL, схема поднята Alembic. Карточки в
нужных состояниях кладутся СЫРЫМ SQL там, где правила сервиса до этого
состояния не доводят (`finished`, `filled_count = capacity`): через API
такое состояние не получить, а проверять нужно именно его.

§18.1: каждый тест краснеет от снятия конкретной защиты. §18.2: `cancel`
проверяется по строке в БД, а не только по коду ответа.
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


async def _new_user(session: AsyncSession, *, role: str = "user") -> tuple[User, str]:
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role=role, status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> int:
    """Место сообщества и его внутренний `id` (нужен для FK карточки)."""
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    status: str = "published",
    filled_count: int = 0,
    capacity: int = 3,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    description: str | None = None,
    title: str = "Встреча",
    verification_status: str = "approved",
) -> str:
    """Карточка в нужном состоянии — сырым SQL, минуя правила сервиса."""
    starts = starts_at or _future(days=3)
    expires = expires_at or (starts - timedelta(hours=1))
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, :title, :description, 'cafe', :starts, :capacity, "
            "60, :expires, :status, :filled, :verification, :published_at,"
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": _city(),
            "title": title,
            "description": description,
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


async def _card_row(session: AsyncSession, card_uuid: str) -> dict[str, Any]:
    """Текущее состояние строки: по нему проверяются переходы (§18.2)."""
    result = await session.execute(
        text(
            "SELECT status, closed_at, filled_count, description, verification_status, title "
            "FROM event_cards WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    return dict(result.mappings().one())


# --- AC-01: правка опубликованной ---------------------------------------------


async def test_patch_published_without_joins_succeeds(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (FR99): правка опубликованной до первого присоединения — `200`.

    Краснеет, если вернуть запрет по статусу (контракт 4.2): FR99 разрешает
    правку до присоединения, а не только в черновике.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=user.id, place_id=await _place(session), filled_count=0
    )

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        # AC-7 стори 13.2: `title` из контракта удалён, поэтому правка
        # проверяется на `description` — единственном свободном тексте.
        json={"description": "Уточнил описание"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200, response.text
    row = await _card_row(session, card_uuid)
    assert row["description"] == "Уточнил описание"
    assert row["status"] == "published"


async def test_patch_published_after_join_is_conflict(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01 (FR99): после присоединения — `409 CARD_ALREADY_JOINED`.

    Краснеет, если снять проверку `filled_count`: автор менял бы встречу,
    на которую уже согласились.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=user.id, place_id=await _place(session), filled_count=1
    )

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"description": "Поздно"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_ALREADY_JOINED"
    row = await _card_row(session, card_uuid)
    assert row["title"] == "Встреча"


@pytest.mark.parametrize("status", ["closed", "finished", "cancelled"])
async def test_patch_terminal_card_is_not_editable(
    client: AsyncClient, session: AsyncSession, status: str
) -> None:
    """AC-01: терминальные состояния не правятся — `409 CARD_NOT_EDITABLE`.

    Краснеет, если пустить правку любого не-`draft` статуса: закрытую карточку
    можно было бы переписать и вернуть в ленту.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=user.id, place_id=await _place(session), status=status
    )

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"description": "Правка"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_NOT_EDITABLE"


async def test_patch_published_with_stop_word_changes_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (FR101): провал автопроверки на опубликованной — `409`, БЕЗ мутаций.

    Краснеет, если проверка идёт ПОСЛЕ присвоения: в БД остался бы
    запрещённый текст, а ответ сообщал бы об отказе.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=user.id, place_id=await _place(session), title="Спокойно"
    )

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        # AC-7 стори 13.2: стоп-слово проверяется через `description` —
        # автопроверка читает склейку title+description, а title от автора
        # больше не приходит.
        json={"description": "Казино на районе"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_EDIT_REQUIRES_REVIEW"
    row = await _card_row(session, card_uuid)
    assert row["title"] == "Спокойно"
    assert row["verification_status"] == "approved"


# --- AC-03: закрытие набора ---------------------------------------------------


async def test_close_published_by_author(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (FR74): автор закрывает набор — `200`, `closed`, `closed_at` заполнен.

    Краснеет, если CAS не пишет `closed_at`: клиент не отличит «закрыто сейчас»
    от «закрыто по времени» — а это разные факты.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=user.id, place_id=await _place(session))

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/close", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"
    row = await _card_row(session, card_uuid)
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


async def test_close_twice_conflicts(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03: повторное закрытие — `409 CARD_NOT_PUBLISHED`.

    Краснеет, если после первого закрытия второй вызов снова отвечает `200`:
    получилось бы «закрыто дважды» с перезаписью `closed_at`.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=user.id, place_id=await _place(session))
    first = await client.post(
        f"/api/v1/cards/{card_uuid}/close", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/cards/{card_uuid}/close", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert second.status_code == 409
    assert second.json()["code"] == "CARD_NOT_PUBLISHED"


async def test_close_full_card_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (D-4.6-4): `close` на `full` запрещён — набор уже остановлен.

    Краснеет, если разрешить закрытие из `full`: `closed` исказил бы причину
    остановки (места кончились) и участник увидел бы неверную подпись.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=user.id,
        place_id=await _place(session),
        status="full",
        filled_count=3,
        capacity=3,
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/close", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_NOT_PUBLISHED"


async def test_close_by_stranger_is_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03 (ADR-13): чужая карточка — `404`, не `403`.

    Краснеет, если закрывать по `card_id` без проверки автора.
    """
    owner, _ = await _new_user(session)
    _, stranger_token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=owner.id, place_id=await _place(session))

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/close", cookies={ACCESS_COOKIE_NAME: stranger_token}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"
    row = await _card_row(session, card_uuid)
    assert row["status"] == "published"


async def test_moderator_cannot_close_foreign_card(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03 (D-4.6-5): модератор НЕ закрывает чужие карточки — `404`.

    Краснеет, если добавить модератору обход `get_owned`: снятие с ленты —
    предмет жалоб (6.x), а не ручного закрытия набора автором.
    """
    owner, _ = await _new_user(session)
    _, mod_token = await _new_user(session, role="moderator")
    card_uuid = await _insert_card(session, author_id=owner.id, place_id=await _place(session))

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/close", cookies={ACCESS_COOKIE_NAME: mod_token}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"


# --- AC-04: отмена ------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "published", "full", "closed"])
async def test_cancel_from_non_terminal(
    client: AsyncClient, session: AsyncSession, status: str
) -> None:
    """AC-04 (FR100): отмена разрешена из всех не терминальных состояний.

    `full` требует `filled_count = capacity` (двусторонний CHECK `0011`).
    Краснеет, если предикат перечисляет только `published`: автор не смог бы
    отменить черновик или уже закрытый набор.
    """
    user, token = await _new_user(session)
    filled = 3 if status == "full" else 0
    card_uuid = await _insert_card(
        session,
        author_id=user.id,
        place_id=await _place(session),
        status=status,
        filled_count=filled,
        capacity=3,
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/cancel", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    row = await _card_row(session, card_uuid)
    assert row["status"] == "cancelled"


async def test_cancel_finished_card_is_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: архивную карточку отменить нельзя — `409 CARD_NOT_CANCELLABLE`.

    Краснеет, если разрешить отмену из `finished`: прошедшая встреча стала бы
    отменённой, и история явки (5.x) потеряла бы смысл.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=user.id, place_id=await _place(session), status="finished"
    )

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/cancel", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CARD_NOT_CANCELLABLE"


async def test_delete_cancels_instead_of_removing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04 (R-04): `DELETE` — мягкая отмена, строка остаётся в БД.

    Краснеет, если `DELETE` физически удаляет строку: история заявок и чатов
    эпика 5 ссылалась бы на несуществующую карточку.
    """
    user, token = await _new_user(session)
    card_uuid = await _insert_card(session, author_id=user.id, place_id=await _place(session))

    response = await client.delete(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    row = await _card_row(session, card_uuid)
    # Строка на месте (R-04), статус изменён.
    assert row["status"] == "cancelled"


async def test_cancel_frees_author_slot(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04 (FR71): отменённая карточка освобождает слот автора.

    Краснеет, если `cancelled` попадёт в `ACTIVE_STATUSES`: автор не смог бы
    опубликовать новую встречу после отмены, хотя лимит это разрешает.
    """
    user, token = await _new_user(session)
    place = await _place(session)
    first = await _insert_card(session, author_id=user.id, place_id=place)
    second = await _insert_card(session, author_id=user.id, place_id=place)

    # При лимите 2 обе карточки занимают слот — третья не публикуется.
    blocked = await _insert_card(
        session, author_id=user.id, place_id=place, status="draft", title="Третья"
    )
    await session.execute(
        text("UPDATE event_cards SET verification_status = 'approved' WHERE uuid = :uuid"),
        {"uuid": blocked},
    )
    over_limit = await client.post(
        f"/api/v1/cards/{blocked}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert over_limit.status_code == 429

    cancelled = await client.post(
        f"/api/v1/cards/{first}/cancel", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert second

    after = await client.post(
        f"/api/v1/cards/{blocked}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert after.status_code == 200, after.text


# --- AC-05: ленивый архив -----------------------------------------------------


async def test_get_started_card_finishes_lazily(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05 (FR73, NFR21): карточка после `starts_at` лениво уходит в архив.

    Краснеет, если убрать ленивый `finished` из `get_card`: участник видел бы
    «опубликована» у встречи, которая уже прошла, пока воркер не отработает.
    """
    author, author_token = await _new_user(session)
    _, reader_token = await _new_user(session)
    card_uuid = await _insert_card(
        session,
        author_id=author.id,
        place_id=await _place(session),
        starts_at=_future(minutes=-5),
        expires_at=_future(minutes=-10),
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: reader_token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "finished"
    row = await _card_row(session, card_uuid)
    assert row["status"] == "finished"
    assert author
    assert author_token


async def test_finished_card_still_readable_as_history(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05 (FR73): архивная карточка доступна по `uuid` как история.

    Краснеет, если `finished` исчезнет из видимости: ссылка из чата (эпик 6)
    вела бы в `404`, и участники теряли бы историю встречи.
    """
    author, _ = await _new_user(session)
    _, reader_token = await _new_user(session)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=await _place(session), status="finished"
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: reader_token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "finished"


async def test_finished_card_not_in_feed(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05 (FR73): архив не попадает в ленту (только `FEED_STATUSES`).

    Краснеет, если добавить `finished` в набор ленты: прошедшие встречи
    вытеснили бы актуальные.
    """
    author, _ = await _new_user(session)
    _, reader_token = await _new_user(session)
    place = await _place(session)
    await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        status="finished",
        title="Прошедшая",
        starts_at=_future(days=-1),
        expires_at=_future(days=-2),
    )

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: reader_token})

    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["items"]]
    assert "Прошедшая" not in titles


# --- AC-09: публикация при полной вместимости ---------------------------------
#
# Через API и даже сырым INSERT состояние «черновик с `filled_count = capacity`»
# НЕДОСТИЖИМО: CHECK `ck_event_cards_full_capacity` запрещает его и в редакции
# 4.1, и в новой. Значит проверка `CARD_NO_SEATS` — defense-in-depth уровня
# сервиса (§18.1: доказать откатом через БД нельзя, поэтому она покрыта
# юнит-тестом `test_publish_without_seats_is_rejected` в
# `tests/unit/test_card_state_transitions.py`). Здесь фиксируется сам факт
# недостижимости — чтобы будущая правка CHECK не сделала защиту мёртвой молча.


async def test_draft_with_full_capacity_is_impossible_in_db(
    session: AsyncSession,
) -> None:
    """AC-09 (X-22): «черновик без мест» отвергается базой — состояние недостижимо.

    Краснеет, если ослабить CHECK: тогда `publish` действительно упёрся бы
    в заполненную вместимость, и `CARD_NO_SEATS` стал бы обязательным путём,
    а не страховкой.
    """
    user, _ = await _new_user(session)

    with pytest.raises(Exception) as error:
        await _insert_card(
            session,
            author_id=user.id,
            place_id=await _place(session),
            status="draft",
            filled_count=3,
            capacity=3,
        )
    assert "ck_event_cards_full_capacity" in str(error.value)


# --- AC-07: двусторонний CHECK (X-35) -----------------------------------------


async def test_full_status_requires_full_capacity(session: AsyncSession) -> None:
    """AC-07 (X-35): `full` при свободном месте отвергается базой.

    Краснеет, если вернуть односторонний CHECK из 4.1: `full` с `filled_count =
    1` при `capacity = 3` прошёл бы, и лента отдавала бы «есть места» со
    статусом «собрана».
    """
    user, _ = await _new_user(session)
    place = await _place(session)

    with pytest.raises(Exception) as error:
        await _insert_card(
            session, author_id=user.id, place_id=place, status="full", filled_count=1, capacity=3
        )
    assert "ck_event_cards_full_capacity" in str(error.value)


async def test_published_with_full_capacity_is_rejected(session: AsyncSession) -> None:
    """AC-07 (X-35): обратная сторона — `published` при полной вместимости.

    Краснеет, если заменить CHECK на `(status <> 'full') OR ...` без первой
    половины: 4.1 ослабла бы, и «опубликована, мест нет» стала бы возможной.
    """
    user, _ = await _new_user(session)

    with pytest.raises(Exception) as error:
        await _insert_card(
            session,
            author_id=user.id,
            place_id=await _place(session),
            status="published",
            filled_count=3,
            capacity=3,
        )
    assert "ck_event_cards_full_capacity" in str(error.value)
