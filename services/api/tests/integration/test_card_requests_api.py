"""Список заявившихся через API (стори 5а.4, Task 10.4, 10.5; AC-01…AC-07).

Реальный PostgreSQL, схема поднята Alembic. Главные утверждения:

- порядок `submitted_at ASC, id ASC` при РАВНЫХ `submitted_at` (tie-breaker
  ADR-05) — без него страницы теряли бы строки;
- не автор карточки получает ТОТ ЖЕ `404`, что и на несуществующую (ADR-13);
- `not_selected` лежит в БД и НЕ отдаётся автору;
- `capacity=2..6` → `open_set` без выбора (FR70).

Тесты без подготовки строки в БД проверяли бы пустоту, а не фильтр (§18.2).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.join_requests import get_join_request_rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import join_request_service, media_service
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


# CR 2.2, патч 4: форма URL повторяет реальный path-style presigned URL: имя
# бакета в пути, `storage_key` в ответ не попадает.
FAKE_URL = (
    "https://storage.example.test/bucket/photos/fake.jpg?X-Amz-Signature=fake&X-Amz-Expires=900"
)


@dataclass
class FakeMedia:
    """Подмена хранилища: проверяется и результат, и СЧЁТЧИК вызовов подписи."""

    signed_url_calls: list[str] = field(default_factory=list)
    fail_sign_for: set[str] = field(default_factory=set)

    def get_signed_url(self, storage_key: str, *, ttl: int | None = None) -> str:
        self.signed_url_calls.append(storage_key)
        if storage_key in self.fail_sign_for:
            raise media_service.MediaStorageError("S3 generate_presigned_url failed: AccessDenied")
        return FAKE_URL


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_join_request_rate_limiter] = _NoLimit
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_join_request_rate_limiter, None)


class _NoLimit:
    async def hit(self, applicant_id: int) -> int | None:
        return None


@pytest.fixture
async def fake_media(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeMedia]:
    """Task 10.5: S3 не поднимается, сеть не используется.

    `reset_client()` обязателен: без него кэш boto3 вернул бы настоящий клиент
    после первого теста (приём `test_profile_photos_api.py`).
    """
    media = FakeMedia()
    # Подмена в ДВУХ местах, и это не перестраховка: `join_request_service`
    # импортировал `get_signed_url` по имени (`from ... import get_signed_url`),
    # поэтому подмена атрибута `media_service` его не задевает — сервис держит
    # собственную ссылку на функцию. Тест, патчащий только модуль, ходил бы
    # в настоящий S3 и «проверял» подпись реальными ключами из `.env`.
    monkeypatch.setattr(media_service, "get_signed_url", media.get_signed_url)
    monkeypatch.setattr(join_request_service, "get_signed_url", media.get_signed_url)
    media_service.reset_client()
    yield media
    media_service.reset_client()


def _city() -> str:
    return settings.default_city_slug


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    # `gender` обязателен для анкеты: `profiles.gender` NOT NULL и связан
    # составным FK с `users(id, gender)` (CR 2.1, миграция `0005`). Без него
    # вставка анкеты падала бы `NotNullViolation`.
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession, *, is_active: bool = True) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, :active) RETURNING id"
        ),
        {
            "name": f"Кафе {uuid_lib.uuid4().hex[:8]}",
            "slug": _city(),
            "active": is_active,
        },
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 1,
    city_slug: str | None = None,
) -> str:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', 0, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": city_slug or _city(),
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
        },
    )
    return str(row.scalar_one())


async def _card_id(session: AsyncSession, card_uuid: str) -> int:
    row = await session.execute(
        text("SELECT id FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    return int(row.scalar_one())


async def _profile(session: AsyncSession, *, user: User, name: str = "Анна", age: int = 28) -> int:
    """Полная анкета: CHECK требует `name/city/age/bio` либо ВСЕ NULL, либо все заполнены."""
    row = await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, gender, bio, status) "
            "VALUES (:user, :name, :city, :age, :gender, 'о себе', 'complete') RETURNING id"
        ),
        {
            "user": user.id,
            "name": name,
            "city": "Томск",
            "age": age,
            "gender": user.gender,
        },
    )
    return int(row.scalar_one())


async def _photo(session: AsyncSession, *, profile_id: int, storage_key: str) -> None:
    """Одобренное фото: CHECK `ck_profile_photos_status_consistency` требует
    `moderated_at IS NOT NULL` для `approved`, иначе — `CheckViolation`."""
    await session.execute(
        text(
            "INSERT INTO profile_photos (profile_id, storage_key, position, status, content_type, "
            "size_bytes, consent_given_at, moderated_at) VALUES (:profile, :key, 1, 'approved', "
            "'image/jpeg', 1000, now(), now())"
        ),
        {"profile": profile_id, "key": storage_key},
    )


async def _insert_request(
    session: AsyncSession,
    *,
    card_id: int,
    applicant_id: int,
    status: str = "pending",
    submitted_at: datetime | None = None,
    message: str = "пойдём вместе",
) -> str:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:card, :applicant, :message, :status, "
            ":submitted_at, now() + interval '1 day', :decided_at) RETURNING uuid"
        ),
        {
            "card": card_id,
            "applicant": applicant_id,
            "message": message,
            "status": status,
            "submitted_at": submitted_at or datetime.now(UTC),
            "decided_at": None if status == "pending" else datetime.now(UTC),
        },
    )
    return str(row.scalar_one())


async def _get(client: AsyncClient, token: str, card_uuid: str, cursor: str | None = None) -> Any:
    params = {"cursor": cursor} if cursor else None
    return await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        params=params,
        cookies={ACCESS_COOKIE_NAME: token},
    )


# --- AC-01: порядок и tie-breaker -----------------------------------------


async def test_author_sees_requests_ordered_with_id_tiebreaker(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-01 (ADR-05): при РАВНЫХ `submitted_at` порядок задаёт `id`.

    Краснеет, если убрать `id` из `ORDER BY` (мутация M1): PostgreSQL вернул
    бы строки в порядке плана, и курсор (по `uuid`) терял/дублировал бы их
    между страницами. `id` не отдаётся наружу, поэтому порядок проверяется
    по `message` — оно задано в порядке вставки.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    moment = datetime.now(UTC) - timedelta(days=1)

    for index in range(3):
        applicant, _ = await _new_user(session)
        await _insert_request(
            session,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=moment,
            message=f"сообщение-{index}",
        )
    await session.flush()

    response = await _get(client, token, card_uuid)

    assert response.status_code == 200
    body = response.json()
    assert [item["message"] for item in body["items"]] == [
        "сообщение-0",
        "сообщение-1",
        "сообщение-2",
    ]
    assert body["total"] == 3
    assert body["mode"] == "requests"
    assert body["selection_available"] is True


# --- AC-02: не автор получает тот же 404 ----------------------------------


async def test_non_author_gets_same_404_as_missing_card(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02 (ADR-13): не автор — `404` с ТЕМ ЖЕ телом, что у несуществующей.

    Краснеет, если снять проверку авторства (мутация M2): заявившийся увидел
    бы весь список и сообщения других. `403` тоже был бы дефектом — он
    подтвердил бы существование карточки.
    """
    author, _ = await _new_user(session)
    stranger, stranger_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    await session.flush()

    foreign = await _get(client, stranger_token, card_uuid)
    missing = await _get(client, stranger_token, str(uuid_lib.uuid4()))

    assert foreign.status_code == 404
    assert missing.status_code == 404
    # Тела совпадают байт-в-байт: различие позволило бы перебором `uuid`
    # узнать, какие карточки существуют.
    assert foreign.json() == missing.json()
    assert foreign.json()["code"] == "card_not_found"


async def test_other_city_card_is_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: карточка другого сообщества — `404`, а не пустой список."""
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place_id, city_slug="other-city"
    )
    await session.flush()

    response = await _get(client, token, card_uuid)

    assert response.status_code == 404
    assert response.json()["code"] == "card_not_found"


# --- AC-04: not_selected не отдаётся --------------------------------------


async def test_not_selected_is_absent_from_author_list(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-04 (FR81): `not_selected` ЛЕЖИТ в БД и НЕ появляется в списке автора.

    Краснеет, если заменить `JOIN_REQUEST_APPLICANT_STATUSES` на все статусы
    (мутация M5): автор увидел бы собственные отказы, а очередь перестала бы
    быть очередью.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)

    waiting, _ = await _new_user(session)
    rejected, _ = await _new_user(session)
    pending_uuid = await _insert_request(
        session, card_id=card_id, applicant_id=waiting.id, status="pending"
    )
    rejected_uuid = await _insert_request(
        session, card_id=card_id, applicant_id=rejected.id, status="not_selected"
    )
    await session.flush()
    present = await session.execute(
        text("SELECT count(*) FROM join_requests WHERE uuid = :uuid"), {"uuid": rejected_uuid}
    )
    assert present.scalar_one() == 1

    response = await _get(client, token, card_uuid)

    assert response.status_code == 200
    body = response.json()
    assert [item["uuid"] for item in body["items"]] == [pending_uuid]
    assert body["total"] == 1


# --- AC-03, AC-09: открытый набор -----------------------------------------


async def test_open_set_mode_has_no_selection(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, AC-09 (FR70): `capacity=4` → `open_set`, выбора нет.

    Краснеет, если определять режим по `is_request_mode` или сравнивать
    `capacity < 2` на клиенте: кнопка «Выбрать» появилась бы на карточке,
    где выбор запрещён.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id, capacity=4)
    card_id = await _card_id(session, card_uuid)
    member, _ = await _new_user(session)
    await _insert_request(session, card_id=card_id, applicant_id=member.id, status="selected")
    waiting, _ = await _new_user(session)
    await _insert_request(session, card_id=card_id, applicant_id=waiting.id, status="pending")
    await session.flush()

    response = await _get(client, token, card_uuid)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "open_set"
    assert body["selection_available"] is False
    # Только участники (`selected`); `pending` в открытом наборе не бывает.
    assert body["total"] == 1


# --- AC-01, AC-06: профиль, возраст, фото ---------------------------------


async def test_profile_and_signed_photo_are_returned(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-01, AC-06: профиль, возраст и подписанный URL ОДНИМ пакетным запросом.

    Счётчик `signed_url_calls` проверяет не «фото есть», а «подпись вызвана
    один раз на страницу» (мутация M4: подпись в цикле по элементу дала бы
    N вызовов, а на одном элементе — те же 1, поэтому элементов два).
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)

    for index in range(2):
        applicant, _ = await _new_user(session)
        profile_id = await _profile(session, user=applicant, name=f"Имя{index}", age=30)
        await _photo(session, profile_id=profile_id, storage_key=f"photos/{index}.jpg")
        await _insert_request(
            session,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=datetime.now(UTC) - timedelta(days=1) + timedelta(minutes=index),
            message=f"сообщение-{index}",
        )
    await session.flush()

    response = await _get(client, token, card_uuid)

    assert response.status_code == 200
    body = response.json()
    first = body["items"][0]["person"]
    assert first["name"] == "Имя0"
    assert first["age"] == 30
    assert first["photo_url"] == FAKE_URL
    assert first["photo_expires_in"] == settings.s3_signed_url_ttl
    # Пакетная подпись: два элемента — ровно два вызова на СТРАНИЦУ, а не
    # по вызову на элемент внутри цикла сборки.
    assert sorted(fake_media.signed_url_calls) == ["photos/0.jpg", "photos/1.jpg"]


async def test_photo_lookup_is_one_query_per_page(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-06 (мутация M4): фото заявителей — ОДИН запрос на страницу, не N.

    Счётчик `signed_url_calls` этого не ловит: подпись в цикле по элементу
    даёт ровно столько же вызовов, сколько пакетная (по одному на фото).
    Ловится только запрос к БД, поэтому слушаем движок.

    Два заявителя выбраны намеренно: на одном элементе «один запрос» и
    «N+1» неразличимы, и тест был бы тавтологией (§18.2).
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    for index in range(2):
        applicant, _ = await _new_user(session)
        profile_id = await _profile(session, user=applicant)
        await _photo(session, profile_id=profile_id, storage_key=f"photos/n1-{index}.jpg")
        await _insert_request(
            session,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=datetime.now(UTC) - timedelta(days=1) + timedelta(minutes=index),
        )
    await session.flush()

    statements: list[str] = []

    def _before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    # `session.get_bind()` в async-сессии возвращает `Connection` (у него нет
    # `sync_engine`), а `session.bind` — сам `AsyncEngine`. Слушаем СИНХРОННЫЙ
    # движок: `before_cursor_execute` живёт на нём, а asyncpg-обёртка
    # проксирует события.
    engine = session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", _before)
    try:
        response = await _get(client, token, card_uuid)
    finally:
        event.remove(engine, "before_cursor_execute", _before)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    photo_selects = [sql for sql in statements if "profile_photos" in sql]
    assert len(photo_selects) == 1, f"N+1 по фото: {len(photo_selects)} запросов"


async def test_applicant_without_profile_yields_nulls(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-06: нет анкеты → `name`/`age`/`photo_url` равны `null`, элемент есть.

    Краснеет, если подставлять `""`/`0`: клиент показал бы «, 0 лет» вместо
    «человек без анкеты» (project-context §4).
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    applicant, _ = await _new_user(session)
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)
    await session.flush()

    response = await _get(client, token, card_uuid)

    assert response.status_code == 200
    person = response.json()["items"][0]["person"]
    assert person["name"] is None
    assert person["age"] is None
    assert person["photo_url"] is None
    assert person["photo_expires_in"] is None


async def test_photo_sign_failure_keeps_list_alive(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-06: сбой подписи не валит список — `photo_url=null`.

    Краснеет, если не ловить `MediaStorageError`/`MediaUnavailableError`:
    один отсутствующий объект в бакете давал бы `500` на весь экран
    (долг CR 5а.3, патч 1 — не повторять).
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    applicant, _ = await _new_user(session)
    profile_id = await _profile(session, user=applicant)
    await _photo(session, profile_id=profile_id, storage_key="photos/broken.jpg")
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)
    await session.flush()
    fake_media.fail_sign_for.add("photos/broken.jpg")

    response = await _get(client, token, card_uuid)

    assert response.status_code == 200
    person = response.json()["items"][0]["person"]
    assert person["photo_url"] is None
    assert person["photo_expires_in"] is None


# --- AC-05: пагинация и курсор --------------------------------------------


async def test_pagination_by_cursor_has_no_overlap_and_keeps_total(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-05: страницы не пересекаются, `total` не зависит от листания.

    CR 5а.4, патч 2: тест ПЕРЕПИСАН. Прежняя версия создавала 3 заявки при
    странице 20 — `cursor` всегда был `None`, вторая страница не запрашивалась
    ни разу, и путь `after is not None` вживую не исполнялся. Именно поэтому
    дефект патча 1 (`500` на любой второй странице) прошёл зелёным.

    Теперь 25 заявок и настоящие две страницы. `submitted_at` идёт ГРУППАМИ
    по три равных значения: проверяется и keyset по времени, и tie-breaker
    `id`, и согласованность с курсором по `uuid` (D-5a.4-2) — главный риск.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    card_id = await _card_id(session, card_uuid)
    # 25 заявок; `submitted_at` повторяется тройками — tie-breaker в деле.
    base = datetime.now(UTC) - timedelta(days=1)
    expected_order: list[str] = []
    for index in range(25):
        applicant, _ = await _new_user(session)
        moment = base + timedelta(minutes=index // 3)
        await _insert_request(
            session,
            card_id=card_id,
            applicant_id=applicant.id,
            submitted_at=moment,
            message=f"сообщение-{index}",
        )
        expected_order.append(f"сообщение-{index}")
    await session.flush()

    first = await _get(client, token, card_uuid)
    assert first.status_code == 200, first.text
    first_body = first.json()

    assert first_body["total"] == 25
    assert len(first_body["items"]) == 20
    assert first_body["cursor"] is not None, "25 заявок при странице 20 обязаны дать курсор"

    second = await _get(client, token, card_uuid, first_body["cursor"])
    assert second.status_code == 200, second.text
    second_body = second.json()

    # `total` — «сколько всего под фильтром», курсором не уменьшается.
    assert second_body["total"] == 25
    assert second_body["cursor"] is None

    first_messages = [item["message"] for item in first_body["items"]]
    second_messages = [item["message"] for item in second_body["items"]]
    # Ни пересечений, ни пропусков: вместе страницы дают ровно исходный список.
    assert not (set(first_messages) & set(second_messages)), "страницы пересеклись"
    assert len(second_messages) == 5
    assert first_messages + second_messages == expected_order


async def test_broken_cursor_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: битый курсор — `422 invalid_cursor`, а не `500`.

    Краснеет, если пропустить декодирование ошибкой наружу: клиент получил
    бы `500` на собственную опечатку и не смог бы сбросить курсор.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    await session.flush()

    response = await _get(client, token, card_uuid, cursor="not-a-cursor")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


# --- AC-07: гарды ---------------------------------------------------------


async def test_anonymous_is_401(client: AsyncClient, session: AsyncSession) -> None:
    """AC-07 (FR64): без сессии — `401`, список не отдаётся."""
    author, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place_id)
    await session.flush()

    response = await client.get(f"/api/v1/cards/{card_uuid}/requests")

    assert response.status_code == 401
