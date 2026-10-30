"""`birth_date` не покидает сервер (стори 12.2; AC-3; ADR-18, NFR66).

**Что нашёл CR (H-2, класс 11.8 H-2).** Замер:

    grep -rn 'author_age\|birth_date' tests/integration/test_cards_feed_api.py
    (пусто)

То есть AC-3 в ленте не проверялся вовсе: замена `compute_age(...)` на прямой
`Profile.age` оставляла ВСЕ тесты зелёными, потому что до 12.2 все анкеты
имели число лет и разницы не было видно.

**Почему это не мелочь.** Новые учётки (`birth_date` есть, `age = NULL`)
получили бы в ленте `author.age = null` — карточка потеряла бы возраст
целиком, и ни один тест бы не покраснел. А `birth_date` в ответе нарушил бы
NFR66 (персональные данные в read-ответе).

Проверяются ДВЕ вещи на каждом ответе:

1. вычисленный `age` присутствует и равен ожидаемому числу;
2. ключа `birth_date` в JSON нет ни на одном уровне вложенности.

Краснеет при:
- удалении `compute_age` из `_feed_item_from_row` (M19) — `age` станет `None`;
- возврате `Profile.age` вместо вычисления (M20) — то же;
- добавлении `birth_date` в схему ответа (M21) — тест 2 покраснеет.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

ACCESS_COOKIE_NAME = "karamel_access"


def _city() -> str:
    return "tomsk"


def _birth_date_for_age(years: int) -> date:
    today = date.today()
    return date(today.year - years, today.month, today.day)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _viewer(session: AsyncSession) -> tuple[User, str]:
    """Зритель ленты: с согласием, без анкеты (анкета не нужна для чтения)."""
    phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    # Анкета связана составным FK `(user_id, gender) -> users(id, gender)`:
    # без пола у пользователя вставка профиля упадёт ForeignKeyViolation.
    user.gender = "female"
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _author_with_birth_date(session: AsyncSession, years: int) -> int:
    """Автор карточки «нового типа»: дата рождения есть, `age` — `NULL`.

    Именно такое состояние создаёт 12.2 у каждого нового пользователя.
    Возвращает внутренний `id` (нужен для FK карточки).
    """
    phone = f"+7904{uuid_lib.uuid4().int % 10**7:07d}"
    author = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    author.age_confirmed = True
    author.consent_given_at = datetime.now(UTC)
    author.gender = "female"  # составной FK анкеты (см. `_viewer`)
    await session.flush()
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
        ),
        {"user_id": author.id, "birth_date": _birth_date_for_age(years)},
    )
    await session.flush()
    return author.id


async def _published_card(session: AsyncSession, author_id: int) -> str:
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    place_id = int(place.scalar_one())
    starts = datetime.now(UTC) + timedelta(days=2)
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 2, 60, "
            ":expires, 'published', 0, 'approved', :published_at,"
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": _city(),
            "starts": starts,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
        },
    )
    await session.flush()
    return str(card.scalar_one())


def _no_birth_date_anywhere(payload: object) -> bool:
    """Рекурсивно проверяет, что ключа `birth_date` нет ни на одном уровне."""
    if isinstance(payload, dict):
        if "birth_date" in payload:
            return False
        return all(_no_birth_date_anywhere(value) for value in payload.values())
    if isinstance(payload, list):
        return all(_no_birth_date_anywhere(item) for item in payload)
    return True


async def test_feed_returns_computed_age_not_column(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3: в ленте — вычисленный возраст, а не колонка `age` (она `NULL`).

    Краснеет, если `compute_age` убрать: у автора `age = NULL`, и в ответе
    окажется `null` вместо 30.
    """
    _, token = await _viewer(session)
    author_id = await _author_with_birth_date(session, 30)
    await _published_card(session, author_id)

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "лента пуста — карточка не создалась, тест ничего не проверяет"
    assert items[0]["author"]["age"] == 30


async def test_feed_never_exposes_birth_date(client: AsyncClient, session: AsyncSession) -> None:
    """AC-3, NFR66: `birth_date` отсутствует в ответе на любом уровне.

    Рекурсивная проверка, а не поиск по строке: поле могло бы появиться
    во вложенном объекте автора, и плоский `"birth_date" not in text`
    на сериализованном JSON это поймал бы, но `json["items"][0]["author"]`
    с новым полем — нет, если бы проверяли только верхний уровень.
    """
    _, token = await _viewer(session)
    author_id = await _author_with_birth_date(session, 30)
    await _published_card(session, author_id)

    response = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    assert _no_birth_date_anywhere(response.json()), "birth_date утёк в read-ответ (NFR66, ADR-18)"


async def test_own_profile_never_exposes_birth_date(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3: даже свой профиль не отдаёт дату рождения — только возраст.

    Соблазн отдать владельцу «его же данные» велик, но NFR66 не делает
    исключения для собственного профиля: цель обработки — показ возраста,
    а не передача даты обратно в браузер.
    """
    phone = f"+7903{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    user.gender = "female"  # составной FK анкеты (см. `_viewer`)
    await session.flush()
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
        ),
        {"user_id": user.id, "birth_date": _birth_date_for_age(25)},
    )
    await session.flush()
    issued = await issue_session(session, user)

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: issued.access_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["age"] == 25, "возраст должен быть вычислен из даты рождения"
    assert "birth_date" not in body


# ============================================================================
# Стори 12.3, AC-3: гард расширен до 7 поверхностей.
#
# Реестр «поверхность → маршрут → схема с `age` → тест»:
#
# | # | Поверхность                | Маршрут                       | Схема                   |
# |---|----------------------------|-------------------------------|-------------------------|
# | 1 | Лента                      | `GET /cards`                  | `EventCardAuthor`       |
# | 2 | Свой профиль               | `GET /profiles/me`            | `ProfilePublic`         |
# | 3 | Карточка события           | `GET /cards/{uuid}`           | `EventCardAuthor`       |
# | 4 | Список заявителей          | `GET /cards/{uuid}/requests`  | `CardRequestsPerson`    |
# | 5 | Участники компании         | `GET /cards/{uuid}/participants` | `CardParticipant`    |
# | 6 | Мои заявки                 | `GET /my/requests`            | `MyRequestAuthor`       |
# | 7 | Карточка модератора        | `GET /moderation/users/{uuid}`| `ModerationUserProfile` |
# | 8 | Очередь модерации          | `GET /moderation/cards`       | `ModerationAuthor`      |
#
# Схем с полем `age` в `app/schemas/*.py` ровно СЕМЬ (замер VS, Task 5.1):
# `ProfilePublic`, `EventCardAuthor`, `CardParticipant`, `CardRequestsPerson`,
# `MyRequestAuthor`, `ModerationAuthor`, `ModerationUserProfile`.
#
# **Отклонение от Task 4.6 (P-31, с четырьмя следами).** Стори предполагала
# поверхность «чат» (`GET /api/v1/chats`, `MessagePage` → `CardParticipant.age`).
# Замер: `grep -rn 'CardParticipant' app/` → единственный потребитель
# `cards.py:734` (маршрут участников); в `ChatListItem`/`MessageRead` поля
# `age` НЕТ вообще. Чатовых поверхностей с возрастом не существует, и тест на
# них проверял бы отсутствующее поле — то есть текст, а не поведение (урок
# 11.10). Поверхность заменена на очередь модерации (Task 4.8), которая `age`
# действительно отдаёт.
# ============================================================================


def _today_shifted(years: int, *, months_ahead: int = 0) -> date:
    """Дата рождения, дающая ровно `years` лет на сдвинутом «сегодня».

    Используется тестом Task 10.4, где «сегодня» инъецируется: ожидаемое
    число считается от ТОГО ЖЕ значения, а не от реальной даты.
    """
    today = date.today()
    year = today.year - years
    month = today.month + months_ahead
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, today.day)


async def _moderator(session: AsyncSession) -> str:
    """Токен модератора: очередь и карточка пользователя закрыты `ModeratorUser`."""
    phone = f"+7902{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="moderator",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    user.gender = "female"
    await session.flush()
    issued = await issue_session(session, user)
    return issued.access_token


async def _card_id(session: AsyncSession, card_uuid: str) -> int:
    row = await session.execute(
        text("SELECT id FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
    )
    return int(row.scalar_one())


async def _select_participant(session: AsyncSession, *, card_uuid: str, user_id: int) -> None:
    """Заявка `selected` — единственный источник состава компании (ADR-04).

    Прямой INSERT, а не через сервис выбора: проверяется ЧТЕНИЕ состава,
    а автовыбор 5б.3 покрыт своими тестами; его падение валило бы гард не
    по делу.
    """
    card_id = await _card_id(session, card_uuid)
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
            "submitted_at, expires_at) "
            "VALUES (:uuid, :card, :applicant, 'привет', 'selected', :now, :expires)"
        ),
        {
            "uuid": uuid_lib.uuid4(),
            "card": card_id,
            "applicant": user_id,
            "now": now,
            "expires": now + timedelta(hours=1),
        },
    )
    await session.flush()


async def _pending_card(session: AsyncSession, author_id: int) -> str:
    """Карточка `draft` + `verification_status='pending'` — вход очереди модерации."""
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 2', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    place_id = int(place.scalar_one())
    card_uuid = uuid_lib.uuid4()
    await session.execute(
        text(
            "INSERT INTO event_cards (uuid, author_id, place_id, city_slug, title, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "verification_status, filled_count, kind, activity) "
            "VALUES (:uuid, :author, :place, :slug, 'На проверку', 'cafe', "
            "now() + interval '3 days', 2, 2880, now() + interval '1 day', 'draft', "
            "'pending', 0, 'company', 'coffee')"
        ),
        {"uuid": card_uuid, "author": author_id, "place": place_id, "slug": _city()},
    )
    await session.flush()
    return str(card_uuid)


async def test_card_detail_returns_computed_age(client: AsyncClient, session: AsyncSession) -> None:
    """AC-3, поверхность 3: `GET /cards/{uuid}` → `EventCardAuthor.age` вычислен.

    Краснеет, если в `get_card` вернуть колонку `Profile.age` вместо
    `compute_age`: у автора `age = NULL`, и в ответе будет `null`.
    Краснеет, если добавить `birth_date` в `EventCardAuthor` (M11).
    """
    viewer, token = await _viewer(session)
    author_id = await _author_with_birth_date(session, 30)
    card_uuid = await _published_card(session, author_id)

    response = await client.get(f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["author"]["age"] == 30
    assert _no_birth_date_anywhere(body), "birth_date утёк в карточку события"
    assert viewer.id  # зритель создан — ответ получен от его имени


async def test_card_requests_person_age_computed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3, поверхность 4: `GET /cards/{uuid}/requests` → `person.age` вычислен.

    Карточка `capacity=1` (режим заявок) и заявка `pending`: только в этом
    режиме маршрут отдаёт список. Краснеет, если заменить `compute_age` на
    колонку (M13) — у заявителя `age = NULL`, в ответе будет `null`.
    """
    author_phone = f"+7901{uuid_lib.uuid4().int % 10**7:07d}"
    author = await UserRepository(session).create(
        provider="phone",
        provider_user_id=author_phone,
        phone=author_phone,
        role="user",
        status="active",
    )
    author.age_confirmed = True
    author.consent_given_at = datetime.now(UTC)
    author.gender = "female"
    await session.flush()
    issued = await issue_session(session, author)
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
        ),
        {"user_id": author.id, "birth_date": _birth_date_for_age(30)},
    )
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 3', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    place_id = int(place.scalar_one())
    card_uuid = uuid_lib.uuid4()
    starts = datetime.now(UTC) + timedelta(days=2)
    card_row = await session.execute(
        text(
            "INSERT INTO event_cards (uuid, author_id, place_id, city_slug, title, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:uuid, :author, :place, :slug, 'Встреча', 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at, 'company', 'coffee') RETURNING id"
        ),
        {
            "uuid": card_uuid,
            "author": author.id,
            "place": place_id,
            "slug": _city(),
            "starts": starts,
            "expires": starts - timedelta(hours=1),
            "published_at": starts - timedelta(days=1),
        },
    )
    card_id = int(card_row.scalar_one())
    applicant_id = await _author_with_birth_date(session, 25)
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
            "submitted_at, expires_at) "
            "VALUES (:uuid, :card, :applicant, 'привет', 'pending', :now, :expires)"
        ),
        {
            "uuid": uuid_lib.uuid4(),
            "card": card_id,
            "applicant": applicant_id,
            "now": now,
            "expires": now + timedelta(hours=1),
        },
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        cookies={ACCESS_COOKIE_NAME: issued.access_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"], "список заявителей пуст — тест ничего не проверяет"
    assert body["items"][0]["person"]["age"] == 25
    assert _no_birth_date_anywhere(body), "birth_date утёк в список заявителей"


async def test_participants_age_computed(client: AsyncClient, session: AsyncSession) -> None:
    """AC-3, поверхность 5: `GET /cards/{uuid}/participants` → `items[].age`.

    Краснеет, если заменить `compute_age` на `participant.age` (M12):
    у выбранного участника колонка `age = NULL`, и элемент состава
    показал бы `null` вместо 30.
    """
    author_id = await _author_with_birth_date(session, 30)
    card_uuid = await _published_card(session, author_id)
    member_id = await _author_with_birth_date(session, 30)
    # Токен участника: состав видят только члены компании (AC-02 стори 6.3).
    member = await session.get(User, member_id)
    assert member is not None
    member.age_confirmed = True
    member.consent_given_at = datetime.now(UTC)
    await session.flush()
    await _select_participant(session, card_uuid=card_uuid, user_id=member_id)
    issued = await issue_session(session, member)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/participants",
        cookies={ACCESS_COOKIE_NAME: issued.access_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    ages = [item["age"] for item in body["items"]]
    assert ages and all(age == 30 for age in ages), f"возрасты: {ages}"
    assert _no_birth_date_anywhere(body), "birth_date утёк в состав компании"


async def test_my_requests_age_computed(client: AsyncClient, session: AsyncSession) -> None:
    """AC-3, поверхность 6: `GET /my/requests` → `items[].author.age`.

    Краснеет, если в `list_my_requests` вернуть колонку `author_age`:
    у автора с датой рождения она `NULL`.
    """
    applicant_phone = f"+7900{uuid_lib.uuid4().int % 10**7:07d}"
    applicant = await UserRepository(session).create(
        provider="phone",
        provider_user_id=applicant_phone,
        phone=applicant_phone,
        role="user",
        status="active",
    )
    applicant.age_confirmed = True
    applicant.consent_given_at = datetime.now(UTC)
    applicant.gender = "female"
    await session.flush()
    issued = await issue_session(session, applicant)
    author_id = await _author_with_birth_date(session, 25)
    card_uuid = await _published_card(session, author_id)
    card_id = await _card_id(session, card_uuid)
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
            "submitted_at, expires_at) "
            "VALUES (:uuid, :card, :applicant, 'привет', 'pending', :now, :expires)"
        ),
        {
            "uuid": uuid_lib.uuid4(),
            "card": card_id,
            "applicant": applicant.id,
            "now": now,
            "expires": now + timedelta(hours=1),
        },
    )
    await session.flush()

    response = await client.get(
        "/api/v1/my/requests", cookies={ACCESS_COOKIE_NAME: issued.access_token}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"], "список заявок пуст — тест ничего не проверяет"
    assert body["items"][0]["author"]["age"] == 25
    assert _no_birth_date_anywhere(body), "birth_date утёк в «Мои заявки»"


async def test_moderation_user_card_age_computed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3, поверхность 7: `GET /moderation/users/{uuid}` → `profile.age`.

    Краснеет, если в `moderation_user_card_service` вернуть `profile_row[3]`
    (legacy-колонку): у пользователя с датой рождения она `NULL`, и модератор
    увидел бы `null` вместо возраста.
    """
    target_id = await _author_with_birth_date(session, 30)
    moderator_token = await _moderator(session)
    target = await session.get(User, target_id)
    assert target is not None

    response = await client.get(
        f"/api/v1/moderation/users/{target.uuid}",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["age"] == 30
    assert _no_birth_date_anywhere(body), "birth_date утёк в карточку модератора"


async def test_moderation_queue_author_age_computed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3, поверхность 8 (Task 4.8): очередь модерации → `items[].author.age`.

    Краснеет, если в `moderation_service` вернуть колонку `author_age`:
    у автора с датой рождения она `NULL`. Краснеет, если добавить
    `birth_date` в `ModerationAuthor` (M17).
    """
    author_id = await _author_with_birth_date(session, 30)
    card_uuid = await _pending_card(session, author_id)
    moderator_token = await _moderator(session)

    response = await client.get(
        "/api/v1/moderation/cards?status=all",
        cookies={ACCESS_COOKIE_NAME: moderator_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    matched = [item for item in body["items"] if item["uuid"] == card_uuid]
    assert matched, "карточка не попала в очередь — тест ничего не проверяет"
    assert matched[0]["author"]["age"] == 30
    assert _no_birth_date_anywhere(body), "birth_date утёк в очередь модерации"


async def test_guard_and_serialization_share_one_today(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 10.4 (долг `W-12.2-b`): гард 18+ и `age` в ответе — ОДНО число.

    **Сценарий, который ловит расхождение.** «Сегодня» инъецируется в
    `TodayMiddleware` значением `2026-12-01`, а реальная дата — другая.
    Дата рождения подобрана так, что на инъецированном «сегодня» человеку
    РОВНО 18, а на реальном — 17.

    Если хоть одно место вызова осталось на `date.today()`:
    - гард (`deps.py`) посчитает 17 → ответ `403 underage`;
    - либо сериализация вернёт 17 в `age`, и это увидит ассерт.

    Оба исхода красные, поэтому тест ловит мутацию M16 (возврат
    `date.today()` в `deps.py`) и любую частичную правку Замера 6.
    """
    from app.core import today as today_module

    frozen = date(2026, 12, 1)
    # Ровно 18 на `frozen`: 2026 - 18 = 2008, тот же месяц и день.
    birth = date(2008, 12, 1)

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return frozen

    monkeypatch.setattr(today_module, "date", _FrozenDate)

    phone = f"+7899{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, role="user", status="active"
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    user.gender = "female"
    await session.flush()
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
        ),
        {"user_id": user.id, "birth_date": birth},
    )
    await session.flush()
    issued = await issue_session(session, user)

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: issued.access_token}
    )

    # Гард 18+ пропустил: на инъецированном «сегодня» человеку ровно 18.
    assert response.status_code == 200, (
        f"гард 18+ разошёлся с сериализацией: {response.status_code} {response.text}"
    )
    # Сериализация посчитала от ТОГО ЖЕ «сегодня» — иначе было бы 17.
    assert response.json()["age"] == 18


# --- Task 5.3: инвентарь схем с полем `age` -----------------------------------

#: Реестр поверхностей гарда. Новый класс с `age` обязан появиться здесь И в
#: гарде выше — иначе он останется без проверки на утечку `birth_date`.
#: Список зафиксирован замером (Task 5.1): `ast`-обход `app/schemas/*.py`
#: нашёл ровно эти семь классов.
AGE_BEARING_SCHEMAS = frozenset(
    {
        "ProfilePublic",
        "EventCardAuthor",
        # Стори 12.7 (AC-2, FR120): витрина анкеты автора. Покрытие —
        # `test_card_author_profile_api.py` (`FORBIDDEN_KEY` в СЫРОМ теле
        # каждого ответа + `age` вычисленный из `birth_date`).
        "EventCardAuthorProfile",
        "CardParticipant",
        "CardRequestsPerson",
        "MyRequestAuthor",
        "ModerationAuthor",
        "ModerationUserProfile",
    }
)


def test_every_schema_with_age_is_under_guard() -> None:
    """AC-3 (P-25): гард проверяет ЭФФЕКТ — число схем с `age` совпадает.

    Тест читает исходники схем, а не список в тесте: новый read-эндпоинт,
    отдающий `age`, уронит его, а не останется без гарда молча.

    Краснеет, если добавить поле `age` в новую схему (например в чатовую)
    и не покрыть её тестом.
    """
    import ast
    from pathlib import Path

    schemas_dir = Path(__file__).resolve().parents[2] / "app" / "schemas"
    found: set[str] = set()
    for path in sorted(schemas_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == "age"
                ):
                    found.add(node.name)
                    break

    assert found == AGE_BEARING_SCHEMAS, (
        "набор схем с `age` разошёлся с реестром гарда: "
        f"лишние={sorted(found - AGE_BEARING_SCHEMAS)}, "
        f"пропали={sorted(AGE_BEARING_SCHEMAS - found)}"
    )
