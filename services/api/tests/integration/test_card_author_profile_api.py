"""Витрина анкеты автора через карточку (стори 12.7; AC-2, AC-4, AC-4a-c, AC-5).

Реальный PostgreSQL, схема поднята Alembic. Главные утверждения:

- **AC-4**: архивная (`finished`/`cancelled`) карточка даёт `404
  card_not_found` на анкету автора — то же тело, что «карточки нет»;
- **AC-4a**: `closed` анкету ОТКРЫВАЕТ (пара к AC-4, P-21) — без этой
  половины «всегда 404» прошло бы AC-4 незамеченным;
- **AC-4b**: чужой город и отсутствие карточки неотличимы от архива;
- **AC-4c**: автор видит анкету на своём `draft`, посторонний — нет;
- **AC-5**: TTL подписи не переживает `starts_at`; на архиве подписи НЕТ;
- **AC-2 (замок)**: `birth_date` не уходит, `age` вычисленный; в
  `EventCardRead.author` ровно три поля.

§18.1: каждый тест краснеет от снятия конкретной защиты. §18.2: проверяется
состояние ответа, а не только код. §18.11: пара «доступно/недоступно» есть
на каждое правило.
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

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import event_card_service, media_service
from app.services.event_card_service import _author_photo_ttl
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

FAKE_URL = (
    "https://storage.example.test/bucket/photos/fake.jpg?X-Amz-Signature=fake&X-Amz-Expires=300"
)

# AC-2, 12.3: `birth_date` не покидает сервер НИКОГДА. Ключ ищется в сыром
# теле ответа, а не в разобранном JSON: поле могло бы приехать вложенным.
FORBIDDEN_KEY = "birth_date"


@dataclass
class FakeMedia:
    """Подмена хранилища: проверяется и результат, и СЧЁТЧИК вызовов подписи.

    Счётчик — это доказательство U15 (AC-5): на архивной карточке подписи
    быть не должно, и «0 вызовов» отличимо от «подписали и не отдали».
    """

    signed_url_calls: list[str] = field(default_factory=list)
    ttls: list[int] = field(default_factory=list)
    fail_sign_for: set[str] = field(default_factory=set)

    def get_signed_url(self, storage_key: str, *, ttl: int | None = None) -> str:
        self.signed_url_calls.append(storage_key)
        if ttl is not None:
            self.ttls.append(ttl)
        if storage_key in self.fail_sign_for:
            raise media_service.MediaStorageError("S3 generate_presigned_url failed: AccessDenied")
        return FAKE_URL


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def fake_media(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeMedia]:
    """S3 не поднимается.

    Подмена В ДВУХ местах: сервис импортирует `get_signed_url` по имени, и
    подмена атрибута модуля его не задевает (долг CR 5а.3). `reset_client()`
    обязателен: иначе кэш boto3 вернул бы настоящий клиент.
    """
    media = FakeMedia()
    monkeypatch.setattr(media_service, "get_signed_url", media.get_signed_url)
    monkeypatch.setattr(event_card_service, "get_signed_url", media.get_signed_url)
    media_service.reset_client()
    yield media
    media_service.reset_client()


# ——— посев —————————————————————————————————————————————————————————————————


def _auth(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


async def _new_user(
    session: AsyncSession, *, city_slug: str | None = None
) -> tuple[User, str]:
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
        city_slug=city_slug,
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {
            "name": f"Кафе {uuid_lib.uuid4().hex[:8]}",
            "slug": settings.default_city_slug,
        },
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    status: str = "published",
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    city_slug: str | None = None,
    capacity: int = 3,
    filled_count: int = 0,
) -> str:
    """Карточка в нужном состоянии — сырым SQL, минуя правила сервиса.

    `filled_count`/`capacity` — параметры, а не константы: CHECK
    `ck_event_cards_full_capacity` требует `filled_count = capacity` при
    `status='full'`, и «полная» карточка без этого инварианта физически
    не вставляется.
    """
    starts = starts_at or (datetime.now(UTC) + timedelta(days=3))
    # Окно решения — ДО встречи и в БУДУЩЕМ: иначе `get_card` выполнит
    # легальное ленивое закрытие, и тест проверял бы `closed` вместо
    # запрошенного состояния.
    expires = expires_at or (starts - timedelta(minutes=1))
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', 'описание', 'cafe', :starts, "
            ":capacity, 60, :expires, :status, :filled, 'approved', :published_at, "
            "'company', 'coffee') RETURNING uuid"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": city_slug or settings.default_city_slug,
            "starts": starts,
            "expires": expires,
            "status": status,
            "capacity": capacity,
            "filled": filled_count,
            "published_at": starts - timedelta(days=1) if status != "draft" else None,
        },
    )
    return str(row.scalar_one())


async def _profile(
    session: AsyncSession,
    *,
    user: User,
    name: str = "Анна",
    city: str = "Томск",
    bio: str = "Люблю выставки и кофе без спешки",
    birth_date: Any = None,
    # Стори 12.2: CHECK `ck_profiles_completeness` требует
    # `age IS NOT NULL OR birth_date IS NOT NULL` — профиль без обоих полей
    # физически не существует. Дефолт `28` (не `None`) держит посев валидным;
    # тесты, которым нужен именно legacy-`age`, передают его явно.
    age: int | None = 28,
) -> int:
    row = await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, birth_date, gender, bio, status) "
            "VALUES (:user, :name, :city, :age, :birth, :gender, :bio, 'complete') RETURNING id"
        ),
        {
            "user": user.id,
            "name": name,
            "city": city,
            "age": age,
            "birth": birth_date,
            "gender": user.gender,
            "bio": bio,
        },
    )
    return int(row.scalar_one())


async def _photo(session: AsyncSession, *, profile_id: int, storage_key: str) -> None:
    await session.execute(
        text(
            "INSERT INTO profile_photos (profile_id, storage_key, position, status, content_type, "
            "size_bytes, consent_given_at, moderated_at) VALUES (:profile, :key, 1, 'approved', "
            "'image/jpeg', 1000, now(), now())"
        ),
        {"profile": profile_id, "key": storage_key},
    )


async def _participation(session: AsyncSession, *, card_id: int, applicant_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, "
            "submitted_at, expires_at, decided_at) VALUES (:card, :applicant, '', "
            "'selected', now(), now() + interval '1 day', now())"
        ),
        {"card": card_id, "applicant": applicant_id},
    )
    await session.execute(
        text("UPDATE event_cards SET filled_count = filled_count + 1 WHERE id = :id"),
        {"id": card_id},
    )


# ——— AC-4, AC-4a: архив закрыт, closed открыт —————————————————————————————


async def test_published_card_shows_author_profile(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-2, AC-5: `published` → анкета доступна, фото подписано.

    Мутация U3 (всегда `CardNotFoundError`) делает тест красным.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    profile_id = await _profile(session, user=author)
    await _photo(session, profile_id=profile_id, storage_key="photos/a.jpg")
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Анна"
    assert body["city"] == "Томск"
    assert body["bio"] == "Люблю выставки и кофе без спешки"
    assert body["photo_url"] == FAKE_URL
    assert body["photo_expires_in"] is not None
    # AC-2, 12.3: дата рождения не уходит НИКОГДА.
    assert FORBIDDEN_KEY not in response.text


async def test_full_card_shows_author_profile(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-4: `full` → анкета доступна, как `published`.

    Мутация U4 (выпадение `full` из набора видимых) делает тест красным.
    Пара к `published` обязательна: два статуса — две ветки набора, и
    «работает только для published» иначе прошло бы незамеченным.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    # CHECK `ck_event_cards_full_capacity`: `full` без `filled_count == capacity`
    # физически не вставляется.
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="full", filled_count=3
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Анна"


async def test_closed_card_still_shows_author_profile(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-4a (D-12.7-3): `closed` анкету ОТКРЫВАЕТ — набор закрыт, встреча будет.

    Пара к AC-4 (P-21): без этой половины мутация «всегда 404» прошла бы
    незамеченной. Мутация U7 (включить `closed` в запрет) делает тест красным.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="closed"
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200


@pytest.mark.parametrize("status", ["finished", "cancelled"])
async def test_archived_card_hides_author_profile(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia, status: str
) -> None:
    """AC-4, П11: `finished`/`cancelled` → `404 card_not_found`, НЕ `403`.

    Мутации U5 (убрать `finished` из запрета) и U6 (`cancelled`) красят тест.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    profile_id = await _profile(session, user=author)
    await _photo(session, profile_id=profile_id, storage_key="photos/a.jpg")
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status=status
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}
    # AC-5, U15: на архиве подписи НЕТ — статус проверяется ДО чтения фото.
    assert fake_media.signed_url_calls == []


async def test_three_reasons_share_one_body(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-4b, D-12.7-2: архив · чужой город · нет карточки — ОДНО тело.

    Сравниваются тела, а не «оба 404»: отдельный код `card_archived`
    (мутация U9) раскрыл бы статус и сделал бы тест красным.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    archived_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="finished"
    )
    # Чужой город: карточка в другом сообществе. Слаг НЕ берётся из
    # `Settings.city_slugs` (в конфиге один город `tomsk` — тест ушёл бы в
    # skip, а `skipped > 0` ломает baseline по P-30). CHECK на
    # `event_cards.city_slug` держит только ФОРМУ (`^[a-z0-9_-]{3,64}$`),
    # поэтому второй город вставляется сырым SQL и проверяется ровно то,
    # что нужно: зритель с `tomsk` чужую карточку не видит.
    foreign_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, city_slug="othercity"
    )
    missing_uuid = str(uuid_lib.uuid4())

    archived = await client.get(
        f"/api/v1/cards/{archived_uuid}/author", headers=_auth(viewer_token)
    )
    foreign = await client.get(
        f"/api/v1/cards/{foreign_uuid}/author", headers=_auth(viewer_token)
    )
    missing = await client.get(
        f"/api/v1/cards/{missing_uuid}/author", headers=_auth(viewer_token)
    )

    assert archived.status_code == foreign.status_code == missing.status_code == 404
    assert archived.json() == foreign.json() == missing.json() == {"code": "card_not_found"}


# ——— AC-4c: черновик ———————————————————————————————————————————————————


async def test_author_sees_own_profile_on_draft(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-4c (D-12.7-3): автор видит свою анкету на своём черновике.

    Без этой ветки автор получал бы `404` от собственного профиля —
    поведение, которого никто не проектировал.
    """
    author, author_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="draft"
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(author_token)
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Анна"


async def test_stranger_gets_404_on_draft(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-4c: посторонний на черновик — `404`, как на несуществующую карточку."""
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="draft"
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


# ——— AC-2: возраст вычисленный, birth_date не уходит ———————————————————


async def test_age_is_computed_from_birth_date(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-2 (U10, U11): `age` — вычисленный из `birth_date`, не legacy-`age`.

    Legacy-`age` намеренно врёт (`40`), `birth_date` даёт 22. Мутация U10
    (отдавать `profile.age`) делает тест красным.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    today = datetime.now(UTC).date()
    birth = today.replace(year=today.year - 22)
    await _profile(session, user=author, birth_date=birth, age=40)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200
    assert response.json()["age"] == 22
    assert FORBIDDEN_KEY not in response.text


async def test_empty_profile_gives_nulls_not_error(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-2: анкеты нет → `name`/`age`/`city`/`bio`/`photo_url` = `null`, `200`.

    Пустая анкета не должна ломать экран: «есть встреча, есть место, есть
    время» — карточка полезна и без анкеты.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] is None
    assert body["age"] is None
    assert body["city"] is None
    assert body["bio"] is None
    assert body["photo_url"] is None
    assert body["photo_expires_in"] is None


async def test_photo_url_and_expiry_are_a_pair(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-5, Task 3.3, U16: ссылка и срок — вместе или никак.

    Нет фото → оба `null`. Есть фото → оба заполнены. Мутация U16
    (`photo_expires_in` при `photo_url is None`) делает тест красным.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    with_photo_uuid = await _insert_card(session, author_id=author.id, place_id=place)
    without_photo_uuid = await _insert_card(session, author_id=author.id, place_id=place)

    with_photo = await client.get(
        f"/api/v1/cards/{with_photo_uuid}/author", headers=_auth(viewer_token)
    )
    without_photo = await client.get(
        f"/api/v1/cards/{without_photo_uuid}/author", headers=_auth(viewer_token)
    )

    assert with_photo.json()["photo_url"] is None
    assert with_photo.json()["photo_expires_in"] is None
    assert without_photo.json()["photo_url"] is None
    assert without_photo.json()["photo_expires_in"] is None

    profile_id = (
        await session.execute(
            text("SELECT id FROM profiles WHERE user_id = :u"), {"u": author.id}
        )
    ).scalar_one()
    await _photo(session, profile_id=int(profile_id), storage_key="photos/p.jpg")

    signed = await client.get(
        f"/api/v1/cards/{with_photo_uuid}/author", headers=_auth(viewer_token)
    )

    assert signed.json()["photo_url"] == FAKE_URL
    assert signed.json()["photo_expires_in"] is not None


# ——— AC-5: TTL не переживает карточку —————————————————————————————————


def test_ttl_unit_boundaries() -> None:
    """AC-5, Task 3.4, U12-U14: границы TTL считаются без БД.

    Три ветки: остаток меньше TTL хранилища, больше и отрицательный.
    Мутации: U12 (`ttl = settings.s3_signed_url_ttl` всегда) красит первую
    ветку; U14 (`max(0, ...)`) красит третью.
    """
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

    class _S:
        s3_signed_url_ttl = 900

    # Через 5 минут встреча → ссылка живёт 300 с, не 900.
    assert _author_photo_ttl(
        starts_at=now + timedelta(minutes=5), now=now, settings=_S()
    ) == 300
    # Через 40 минут → ограничение хранилища (900) сильнее остатка карточки.
    assert _author_photo_ttl(
        starts_at=now + timedelta(minutes=40), now=now, settings=_S()
    ) == 900
    # `starts_at` в прошлом (гонка воркера) → 1, не 0 и не отрицательное.
    assert _author_photo_ttl(
        starts_at=now - timedelta(minutes=10), now=now, settings=_S()
    ) == 1


async def test_ttl_never_outlives_card(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-5, U12, U13: подпись и `expires_in` — ОДНО число, ≤ остатка до встречи.

    Мутация U12 (подписать дефолтом) красит сверку с `fake_media.ttls`;
    мутация U13 (вернуть clamp, подписав дефолтом) — сверку двух чисел.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    profile_id = await _profile(session, user=author)
    await _photo(session, profile_id=profile_id, storage_key="photos/t.jpg")
    starts = datetime.now(UTC) + timedelta(minutes=5)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, starts_at=starts
    )

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200
    expires_in = response.json()["photo_expires_in"]
    # ≤ остатка до встречи (допуск на время выполнения запроса).
    assert expires_in <= 300
    assert expires_in > 0
    # D-12.7-4: то же число ушло в подпись — одно значение, не два.
    assert fake_media.ttls == [expires_in]


# ——— анти-оракул: три пути 404 стоят одинаково ——————————————————————


@dataclass
class QueryCounter:
    count: int = 0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1


async def _count_queries(
    client: AsyncClient, session: AsyncSession, *, card_uuid: str, token: str
) -> tuple[int, int]:
    """Считает SQL-запросы одного вызова: (число, код ответа)."""
    counter = QueryCounter()
    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", counter)
    try:
        response = await client.get(
            f"/api/v1/cards/{card_uuid}/author", headers=_auth(token)
        )
    finally:
        event.remove(listen_engine, "before_cursor_execute", counter)
    return counter.count, response.status_code


async def test_query_count_is_equal_for_all_404_reasons(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-4b, D-12.7-2: тайминг-оракул закрыт — три причины стоят ОДИНАКОВО.

    Приём CR 6.3 F-1: тело и заголовки совпадали бы и без выравнивания, но
    посторонний отличал бы существование карточки по времени ответа.
    Выравнивание — запрос признаков зрителя с заведомо несуществующим
    `card_id` (`_MISSING_CARD_ID`).

    Проверяется РАВЕНСТВО, а не абсолютное число: добавление общего запроса
    не должно красить тест, а зави��имость от существования карточки — должна.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    archived_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="finished"
    )
    missing_uuid = str(uuid_lib.uuid4())

    archived_count, archived_status = await _count_queries(
        client, session, card_uuid=archived_uuid, token=viewer_token
    )
    missing_count, missing_status = await _count_queries(
        client, session, card_uuid=missing_uuid, token=viewer_token
    )

    assert archived_status == missing_status == 404
    assert archived_count == missing_count


# ——— замки AC-2 (Task 5.1): author — ровно три поля —————————————————


async def test_feed_author_has_exactly_three_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-2, D-12.7-5, U17: в `EventCardRead.author` ровно `uuid`/`name`/`age`.

    Краснеет при добавлении `photo_url`/`city` в `EventCardAuthor`: лента
    подписывала бы URL на каждый элемент страницы (N+1 подписей).
    """
    author, author_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place)

    response = await client.get(f"/api/v1/cards/{card_uuid}", headers=_auth(author_token))

    assert response.status_code == 200
    assert set(response.json()["author"]) == {"uuid", "name", "age"}
    assert FORBIDDEN_KEY not in response.text


async def test_company_of_archived_card_is_still_available(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-3 (FR85), U18: состав компании на АРХИВНОЙ карточке не отнимается.

    Анкета автора закрыта (AC-4), а состав — история встречи (FR85) и
    остаётся доступен участнику. Мутация U18 (закрыть `participants` для
    архивных) делает тест красным.
    """
    author, author_token = await _new_user(session)
    member, member_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    card_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="finished"
    )
    card_id = (
        await session.execute(
            text("SELECT id FROM event_cards WHERE uuid = :u"), {"u": card_uuid}
        )
    ).scalar_one()
    await _participation(session, card_id=int(card_id), applicant_id=member.id)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/participants", headers=_auth(member_token)
    )

    assert response.status_code == 200
    assert response.json()["total"] >= 1


async def test_archived_profile_404_matches_missing_for_stranger(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-3, Task 5.4: посторонний на архивную — тело идентично «карточки нет».

    Сравнивается СЫРОЕ тело (`json()`), а не только код: разные `details`
    раскрыли бы существование карточки.
    """
    author, author_token = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    archived_uuid = await _insert_card(
        session, author_id=author.id, place_id=place, status="finished"
    )
    missing_uuid = str(uuid_lib.uuid4())

    archived = await client.get(
        f"/api/v1/cards/{archived_uuid}/participants", headers=_auth(viewer_token)
    )
    missing = await client.get(
        f"/api/v1/cards/{missing_uuid}/participants", headers=_auth(viewer_token)
    )

    assert archived.status_code == missing.status_code == 404
    assert archived.json() == missing.json()


# ——— замок состава ответа (CR 12.7, находка M22) ——————————————————————


SHOWCASE_FIELDS = {
    "uuid",
    "name",
    "age",
    "city",
    "bio",
    "photo_url",
    "photo_expires_in",
}


async def test_showcase_response_has_exactly_declared_fields(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-2, AC-4, NFR38: витрина отдаёт РОВНО объявленные поля.

    Найдено мутацией CR M22 (`within_bounds: bool | None = None` в
    `EventCardAuthorProfile`): ни один тест не краснел. Тела сравнивались
    по КЛЮЧАМ значений (`body["name"]` и т.п.), и лишнее поле просто ехало
    к клиенту.

    Почему это не косметика: `within_bounds` — признак «прошёл ли зритель
    рамки аудитории» (12.6, AC-3). Он существует для АВТОРА карточки;
    в витрине он отдал бы постороннему результат проверки его собственной
    анкеты по чужим рамкам, то есть условие отбора (П11, NFR38).
    `age_confirmed` — тот же класс.

    Мутация M22 красит тест: набор ключей перестаёт совпадать. Пропавшее
    поле краснеет так же — сравнение на равенство, не на вхождение.
    """
    author, _ = await _new_user(session)
    _, viewer_token = await _new_user(session)
    place = await _place(session)
    await _profile(session, user=author)
    card_uuid = await _insert_card(session, author_id=author.id, place_id=place)

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/author", headers=_auth(viewer_token)
    )

    assert response.status_code == 200
    assert set(response.json()) == SHOWCASE_FIELDS
    assert FORBIDDEN_KEY not in response.text
    assert "within_bounds" not in response.text
    assert "age_confirmed" not in response.text

