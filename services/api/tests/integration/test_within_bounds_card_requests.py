"""Рамки аудитории в списке заявившихся (стори 12.6; AC-2, AC-3, AC-4).

Реальный PostgreSQL, схема поднята Alembic. Главные утверждения:

- **AC-3:** автор видит `within_bounds` у КАЖДОГО заявившегося, и значение
  соответствует рамкам карточки (возраст — по `birth_date`, приоритет над
  legacy `age`; пол — из `users.gender`);
- **AC-4:** заявитель НЕ видит этого поля ни в `GET /my/requests`, ни в
  ответе на подачу — отказ безличен (FR81);
- **AC-2:** карточка с рамками видна и заявку подать МОЖНО (мягкий фильтр);
  рамки видны до подачи (`EventCardRead`).

**Почему данные готовятся, а не берутся готовыми** (§18.2): тест на пустом
списке проверял бы пустоту, а не предикат. Каждый заявитель — с ЯВНЫМ
возрастом, чтобы отличие `true`/`false` было следствием рамки, а не
случайности.
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

FAKE_URL = (
    "https://storage.example.test/bucket/photos/fake.jpg?X-Amz-Signature=fake&X-Amz-Expires=900"
)


class _NoLimit:
    async def hit(self, applicant_id: int) -> int | None:
        return None


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


@pytest.fixture(autouse=True)
def _stub_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 не поднимается: подпись фото не должна ходить в сеть.

    Подмена в ДВУХ местах — сервис держит собственную ссылку на функцию
    (`from ... import get_signed_url`), и патч модуля его не задевает.
    """

    def _fake(storage_key: str, *, ttl: int | None = None) -> str:
        return FAKE_URL

    monkeypatch.setattr(media_service, "get_signed_url", _fake)
    monkeypatch.setattr(join_request_service, "get_signed_url", _fake)
    media_service.reset_client()
    yield
    media_service.reset_client()


async def _new_user(session: AsyncSession, *, gender: str = "female") -> tuple[User, str]:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender=gender,
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _profile(
    session: AsyncSession,
    *,
    user: User,
    name: str = "Анна",
    birth_date: Any = None,
    age: int | None = None,
) -> int:
    """Анкета с датой рождения, legacy-числом или обоими.

    CHECK `completeness` требует заполненных `name/city/bio` и **возраста
    либо даты рождения** (миграция `0036`). Пустая анкета здесь не нужна:
    проверяется возраст как ИСТОЧНИК рамки.
    """
    row = await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, birth_date, gender, bio, status) "
            "VALUES (:user, :name, 'Томск', :age, :birth, :gender, 'о себе', 'complete') "
            "RETURNING id"
        ),
        {
            "user": user.id,
            "name": name,
            "age": age,
            "birth": birth_date,
            "gender": user.gender,
        },
    )
    return int(row.scalar_one())


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 1,
    gender_filter: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    expires_in_minutes: int = 60,
) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    expires = datetime.now(UTC) + timedelta(minutes=expires_in_minutes)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity, "
            "gender_filter, age_min, age_max) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'published', 0, 'approved', :published_at, 'company', 'coffee', "
            ":gender, :age_min, :age_max) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": expires,
            "published_at": starts - timedelta(days=1),
            "gender": gender_filter,
            "age_min": age_min,
            "age_max": age_max,
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, message: str = "пойдём вместе"
) -> str:
    row = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:card, :applicant, :message, 'pending', now(), "
            "now() + interval '1 day') RETURNING uuid"
        ),
        {"card": card_id, "applicant": applicant_id, "message": message},
    )
    return str(row.scalar_one())


def _birth_date_for_age(age: int) -> Any:
    """Дата рождения, дающая РОВНО `age` полных лет на сегодня.

    Считается от сегодняшней даты, а не литералом: литерал `2000-01-01`
    через полгода дал бы 26 лет вместо 25, и тест покраснел бы без единой
    правки кода (класс `W-12.2-b`).
    """
    today = datetime.now(UTC).date()
    return today.replace(year=today.year - age) - timedelta(days=1)


# ——— AC-3: within_bounds для автора ———————————————————————————————————————


async def test_author_sees_within_bounds_for_each_applicant(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3: у каждого элемента есть `within_bounds`, и он верен по возрасту.
    П4 (FR119): автор видит заявившихся и пометку «вне рамок» у каждого.

    Рамка `age_min=30`: 35-летний проходит, 22-летний — нет. Значение
    считается SQL-предикатом (тот же, что фильтрует автовыбор), поэтому
    краснеет при любой поломке предиката.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, age_min=30
    )

    adult, _ = await _new_user(session)
    await _profile(session, user=adult, name="Взрослый", birth_date=_birth_date_for_age(35))
    await _insert_request(session, card_id=card_id, applicant_id=adult.id, message="мне 35")

    young, _ = await _new_user(session)
    await _profile(session, user=young, name="Молодой", birth_date=_birth_date_for_age(22))
    await _insert_request(session, card_id=card_id, applicant_id=young.id, message="мне 22")

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.json()
    by_message = {item["message"]: item["within_bounds"] for item in body["items"]}
    assert by_message == {"мне 35": True, "мне 22": False}, (
        f"рамка `age_min=30` применена неверно: {by_message}"
    )


async def test_gender_filter_marks_outsider(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-1b, AC-3: рамка по полу отсекает по `users.gender`, не по анкете.

    Оба заявителя без даты рождения и без числа — их возраст НЕИЗВЕСТЕН,
    поэтому предикат обязан дать `False` у обоих: «не доказано, что в рамках»
    (мутация U11: `COALESCE(..., 0)` сделала бы 0 и `age_min` отсекал бы
    всех, а без `IS NOT NULL` неизвестный возраст проходил бы).
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, gender_filter="male"
    )

    man, _ = await _new_user(session, gender="male")
    male_profile = await _profile(session, user=man, name="Мужчина", age=30)
    await _insert_request(session, card_id=card_id, applicant_id=man.id, message="мужчина")

    woman, _ = await _new_user(session, gender="female")
    await _profile(session, user=woman, name="Женщина", age=30)
    await _insert_request(session, card_id=card_id, applicant_id=woman.id, message="женщина")
    assert male_profile > 0  # анкета создана — иначе тест проверял бы пустоту

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    by_message = {item["message"]: item["within_bounds"] for item in response.json()["items"]}
    assert by_message == {"мужчина": True, "женщина": False}, (
        f"рамка `gender_filter=male` применена неверно: {by_message}"
    )


async def test_birth_date_wins_over_legacy_age(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3, U10: `birth_date` приоритетнее legacy `age`.

    У заявителя `birth_date` = 22 года и legacy `age` = 40. Рамка `age_min=30`.
    Если предикат возьмёт legacy-число, он пройдёт — но число устарело
    (решение 12.2: `birth_date` приоритетнее, `compute_age`).
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, age_min=30
    )

    applicant, _ = await _new_user(session)
    await _profile(
        session,
        user=applicant,
        name="Дата важнее",
        birth_date=_birth_date_for_age(22),
        age=40,
    )
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id, message="22 и 40")

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["within_bounds"] is False, (
        "приоритет `birth_date` нарушен: legacy `age=40` пропустил 22-летнего "
        f"через рамку `age_min=30` (within_bounds={item['within_bounds']})"
    )


async def test_unknown_age_is_out_of_bounds(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3, U11: нет ни даты, ни числа ⇒ `within_bounds = false`.

    Анкеты может не быть вовсе (онбординг не пройден). «Неизвестно» — это
    «не доказано, что в рамках», а не «проходит».
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, age_min=30
    )

    applicant, _ = await _new_user(session)
    # Анкеты НЕТ вообще — `LEFT JOIN` в проекции даст NULL.
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id, message="без анкеты")

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["within_bounds"] is False, (
        f"неизвестный возраст не должен проходить рамку, получено {item['within_bounds']}"
    )


async def test_card_without_bounds_reports_null(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-3: карточка без рамок — `within_bounds` = `null` (проверять нечего).

    Отличие от `false` принципиально: `false` сказал бы автору «вне рамок»
    на карточке, где рамок нет вовсе, и он отклонил бы заявку по причине,
    которой не существует.
    """
    author, token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)

    applicant, _ = await _new_user(session)
    await _profile(session, user=applicant, name="Любой", birth_date=_birth_date_for_age(28))
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/requests",
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["within_bounds"] is None, (
        f"у карточки без рамок ожидался `null`, получено {item['within_bounds']}"
    )


# ——— AC-4: заявитель не видит поля ———————————————————————————————————————


async def test_applicant_my_requests_has_no_within_bounds(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-4: `GET /my/requests` не содержит `within_bounds` ни в одном элементе.

    Даже когда рамки заданы и заявитель ВНЕ их: он видит только статус
    заявки, а не причину (FR81, ADR-19).
    """
    author, _ = await _new_user(session)
    place_id = await _place(session)
    _, card_id = await _insert_card(session, author_id=author.id, place_id=place_id, age_min=30)

    applicant, applicant_token = await _new_user(session)
    await _profile(
        session, user=applicant, name="Молодой", birth_date=_birth_date_for_age(22)
    )
    await _insert_request(session, card_id=card_id, applicant_id=applicant.id)

    response = await client.get(
        "/api/v1/my/requests", cookies={ACCESS_COOKIE_NAME: applicant_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"], "список своих заявок пуст — тест проверял бы пустоту"
    for item in body["items"]:
        assert "within_bounds" not in item, (
            f"AC-4 нарушен: заявитель увидел рамки в своих заявках: {sorted(item)}"
        )


async def test_applicant_join_response_has_no_within_bounds(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-4: ответ на подачу заявки тоже не раскрывает рамки.

    Подача при этом УСПЕШНА — мягкий фильтр (AC-2): карточка видна и заявку
    подать можно, даже если заявитель вне рамок.
    """
    author, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _ = await _insert_card(
        session, author_id=author.id, place_id=place_id, age_min=30
    )

    applicant, applicant_token = await _new_user(session)
    await _profile(
        session, user=applicant, name="Молодой", birth_date=_birth_date_for_age(22)
    )
    await session.flush()

    response = await client.post(
        f"/api/v1/cards/{card_uuid}/requests",
        json={"message": "пойдём вместе"},
        cookies={ACCESS_COOKIE_NAME: applicant_token},
    )

    assert response.status_code == 201, (
        f"AC-2: мягкий фильтр — подача должна проходить, получено {response.status_code}: "
        f"{response.text}"
    )
    assert "within_bounds" not in response.json(), (
        f"AC-4: ответ на подачу раскрыл рамки: {sorted(response.json())}"
    )


# ——— AC-2a: рамки видны в карточке до подачи ——————————————————————————————


async def test_card_read_exposes_bounds(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-2a: `EventCardRead` отдаёт три поля рамок — заявитель знает заранее.

    Без этого «рамки показаны до подачи» выполнялось бы только на словах:
    заявитель узнавал бы о них после отказа.
    """
    author, _ = await _new_user(session)
    place_id = await _place(session)
    card_uuid, _ = await _insert_card(
        session,
        author_id=author.id,
        place_id=place_id,
        gender_filter="female",
        age_min=25,
        age_max=35,
    )

    viewer, viewer_token = await _new_user(session)
    await _profile(session, user=viewer, name="Зритель", birth_date=_birth_date_for_age(28))
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{card_uuid}", cookies={ACCESS_COOKIE_NAME: viewer_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["gender_filter"] == "female"
    assert body["age_min"] == 25
    assert body["age_max"] == 35
