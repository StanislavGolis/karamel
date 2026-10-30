"""Состав компании через API (стори 6.3; AC-01…AC-05).

Реальный PostgreSQL, схема поднята Alembic. Главные утверждения:

- **AC-01**: участник получает карточку события и профили участников, фото —
  подписанным URL (в БД ссылки нет, D-11);
- **AC-02**: посторонний и несуществующая карточка дают ПОБАЙТНО равный `404`
  (ADR-13) — анти-оракул проверяется сравнением тел, а не «оба 404»;
- **AC-03**: состав 2–6 включает автора, он первый и помечен `is_author`;
  порядок остальных — `submitted_at ASC, id ASC`;
- **AC-05**: число запросов к БД не зависит от размера компании (NFR7).

§18.11: булевы признаки (`is_author`, наличие `photo_url`) проверяются в
ОБОИХ значениях; «посторонний не видит состав» — тем же ответом, что
«карточки нет».
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

from app.api.v1.cards import router as cards_router  # noqa: F401 - поверхность API
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import chat_participants_service, media_service
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

FAKE_URL = (
    "https://storage.example.test/bucket/photos/fake.jpg?X-Amz-Signature=fake&X-Amz-Expires=900"
)


@dataclass
class FakeMedia:
    """Подмена хранилища: проверяются и результат, и СЧЁТЧИК вызовов подписи."""

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
    monkeypatch.setattr(chat_participants_service, "get_signed_url", media.get_signed_url)
    media_service.reset_client()
    yield media
    media_service.reset_client()


# ——— посев —————————————————————————————————————————————————————————————————


def _city() -> str:
    return settings.default_city_slug


async def _new_user(session: AsyncSession) -> tuple[User, str]:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
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


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": _city()},
    )
    return int(row.scalar_one())


async def _insert_card(
    session: AsyncSession, *, author_id: int, place_id: int, capacity: int = 2
) -> tuple[str, int]:
    starts = datetime.now(UTC) + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, 'full', :filled, 'approved', :published_at, 'company', 'coffee') "
            "RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": _city(),
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            # `ck_event_cards_full_capacity`: `full` требует `filled_count = capacity`.
            "filled": capacity,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return str(card_uuid), int(card_id)


async def _profile(session: AsyncSession, *, user: User, name: str, age: int = 28) -> int:
    row = await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, gender, bio, status) "
            "VALUES (:user, :name, 'Томск', :age, :gender, 'о себе', 'complete') RETURNING id"
        ),
        {"user": user.id, "name": name, "age": age, "gender": user.gender},
    )
    return int(row.scalar_one())


async def _profile_id(session: AsyncSession, *, user: User) -> int:
    """`id` уже созданной анкеты: повторный `INSERT` нарушил бы `uq_profiles_user_id`."""
    row = await session.execute(
        text("SELECT id FROM profiles WHERE user_id = :user"), {"user": user.id}
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


async def _participation(
    session: AsyncSession,
    *,
    card_id: int,
    applicant_id: int,
    status: str = "selected",
    submitted_at: datetime | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:card, :applicant, '', :status, :submitted_at, "
            "now() + interval '1 day', :decided_at)"
        ),
        {
            "card": card_id,
            "applicant": applicant_id,
            "status": status,
            "submitted_at": submitted_at or datetime.now(UTC),
            "decided_at": None if status == "pending" else datetime.now(UTC),
        },
    )


@dataclass
class Company:
    """Собранная компания: автор + участники с профилями."""

    author: User
    author_token: str
    card_uuid: str
    card_id: int
    members: list[User] = field(default_factory=list)
    member_tokens: list[str] = field(default_factory=list)


async def _company(session: AsyncSession, *, size: int) -> Company:
    """Компания размера `size` (2..6): автор + `size-1` выбранных участников."""
    author, author_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=max(size, 2)
    )
    company = Company(
        author=author, author_token=author_token, card_uuid=card_uuid, card_id=card_id
    )
    base = datetime.now(UTC) - timedelta(hours=size)
    for index in range(size - 1):
        member, token = await _new_user(session)
        await _profile(session, user=member, name=f"Участник {index}", age=20 + index)
        await _participation(
            session,
            card_id=card_id,
            applicant_id=member.id,
            submitted_at=base + timedelta(minutes=index),
        )
        company.members.append(member)
        company.member_tokens.append(token)
    await session.flush()
    return company


def _auth(token: str) -> dict[str, str]:
    """Заголовок аутентификации: имя cookie — из единственного источника.

    Литерал `access_token` не совпадает с `ACCESS_COOKIE_NAME`
    (`karamel_access`), и запрос уходил бы без сессии (`401`) — тест
    «посторонний получает 404» проходил бы по неверной причине.
    """
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


# ——— AC-01, AC-03: состав виден участнику, автор первый ———


async def test_member_sees_company_with_author_first(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-01, AC-03: компания из 3 — все видны, автор первый и помечен.

    Краснеет от мутации M2 (снять проверку авторства): автор перестал бы
    попадать в список или потерял бы `is_author`; от M5 (снять `ORDER BY`) —
    порядок участников стал бы физическим.
    """
    company = await _company(session, size=3)

    response = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants",
        headers=_auth(company.member_tokens[0]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["card"]["uuid"] == company.card_uuid
    assert [item["is_author"] for item in body["items"]] == [True, False, False]
    assert body["items"][0]["uuid"] == str(company.author.uuid)
    # §18.11: порядок участников — по времени присоединения (ADR-05).
    assert [item["uuid"] for item in body["items"][1:]] == [
        str(member.uuid) for member in company.members
    ]
    # AC-04: внутренние идентификаторы наружу не выходят (D-2, D-13).
    for forbidden in ("applicant_id", "user_id", "profile_id", "storage_key"):
        assert forbidden not in response.text


async def test_author_sees_company(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-03: автор — участник компании и видит состав наравне со всеми.

    §18.11: проверяется ДРУГОЕ значение признака (автор), а не только
    участник: гард, случайно запрещающий автору, прошёл бы тест на участнике.
    """
    company = await _company(session, size=2)

    response = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants",
        headers=_auth(company.author_token),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2


# ——— AC-02: анти-оракул ———


async def test_outsider_gets_identical_404_to_missing_card(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-02, ADR-13: посторонний и несуществующая карточка — ОДИН ответ.

    Сравниваются тела целиком, а не «оба 404»: разный `code`/`details`
    раскрыл бы существование карточки так же, как `403`.

    Краснеет от мутации M3 (снять проверку участия) и M4 (`404` → `403`).
    """
    company = await _company(session, size=2)
    outsider, outsider_token = await _new_user(session)
    await session.flush()
    assert outsider.id != company.author.id

    existing = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants", headers=_auth(outsider_token)
    )
    missing = await client.get(
        f"/api/v1/cards/{uuid_lib.uuid4()}/participants", headers=_auth(outsider_token)
    )

    assert existing.status_code == 404
    assert existing.status_code == missing.status_code
    assert existing.json() == missing.json()
    assert existing.headers["content-type"] == missing.headers["content-type"]


async def test_pending_applicant_is_not_a_company_member(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-02, D-6.3-2: `pending`-заявитель состав не видит.

    Заявка ещё не компания (FR30), и состав ему не положен. Краснеет от
    мутации M1 (инверсия предиката: `status <> 'not_selected'`).
    """
    company = await _company(session, size=2)
    applicant, applicant_token = await _new_user(session)
    await _participation(
        session, card_id=company.card_id, applicant_id=applicant.id, status="pending"
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants", headers=_auth(applicant_token)
    )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


# ——— AC-03: режим заявок (`capacity=1`) ———


async def test_requests_mode_company_has_two_members(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-03: `capacity=1` — компания из автора и выбранного заявителя.

    Краснеет, если состав начнут считать по `filled_count` или потеряют
    автора: в режиме заявок участник ровно один.
    """
    author, author_token = await _new_user(session)
    place_id = await _place(session)
    card_uuid, card_id = await _insert_card(
        session, author_id=author.id, place_id=place_id, capacity=1
    )
    chosen, chosen_token = await _new_user(session)
    await _profile(session, user=chosen, name="Выбранная")
    await _participation(session, card_id=card_id, applicant_id=chosen.id)
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{card_uuid}/participants", headers=_auth(chosen_token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["uuid"] == str(author.uuid)
    assert body["items"][1]["uuid"] == str(chosen.uuid)


# ——— AC-03: компания 6 человек ———


async def test_full_company_of_six(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-03: верхняя граница 2–6 — все шесть видны.

    Краснеет, если состав ограничат «парой» (наследие `user_a_id`/`user_b_id`
    из v1) или обрежут `limit`.
    """
    company = await _company(session, size=6)

    response = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants",
        headers=_auth(company.member_tokens[-1]),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 6
    assert len(response.json()["items"]) == 6


# ——— AC-01: подпись фото ———


async def test_photo_url_signed_only_for_approved(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-01, D-11: фото по подписанному URL; нет фото — оба поля `None`.

    §18.11: признак проверяется в ОБОИХ значениях — у одного участника фото
    есть, у другого нет. TTL приходит из настроек и равен `s3_signed_url_ttl`.
    """
    company = await _company(session, size=3)
    first, second = company.members[0], company.members[1]
    # Анкета участника создана в `_company`; второй `INSERT` нарушил бы
    # `uq_profiles_user_id` — берём существующий профиль (так же, как в
    # `test_author_photo_is_signed_too`).
    profile_id = await _profile_id(session, user=first)
    await _photo(session, profile_id=profile_id, storage_key="photos/one.jpg")
    # §18.11: у второго участника фото ЕСТЬ, но не одобрено — `pending` не
    # покидает сервер (`list_primary_approved_by_user_ids`). Без этой строки
    # снятие фильтра `approved` (мутация M5) не наблюдалось бы: у второго
    # участника фото просто нет, и тест остался бы зелёным.
    second_profile = await _profile_id(session, user=second)
    await session.execute(
        text(
            "INSERT INTO profile_photos (profile_id, storage_key, position, status, "
            "content_type, size_bytes, consent_given_at) VALUES (:profile, 'photos/pending.jpg', "
            "1, 'pending', 'image/jpeg', 1000, now())"
        ),
        {"profile": second_profile},
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants",
        headers=_auth(company.member_tokens[0]),
    )

    assert response.status_code == 200
    by_uuid = {item["uuid"]: item for item in response.json()["items"]}
    with_photo = by_uuid[str(first.uuid)]
    without_photo = by_uuid[str(second.uuid)]

    assert with_photo["photo_url"] == FAKE_URL
    assert with_photo["photo_expires_in"] == settings.s3_signed_url_ttl
    assert "photos/one.jpg" not in response.text
    assert without_photo["photo_url"] is None
    assert without_photo["photo_expires_in"] is None


async def test_author_photo_is_signed_too(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-01, AC-03: у автора тоже есть фото в составе.

    Краснеет, если фото подписываются только для `selected`-участников:
    автор идёт отдельной строкой (D-6.3-3) и легко выпадает из пакетного
    запроса.
    """
    company = await _company(session, size=2)
    author_profile = await _profile(session, user=company.author, name="Автор")
    await _photo(session, profile_id=author_profile, storage_key="photos/author.jpg")
    await session.flush()

    response = await client.get(
        f"/api/v1/cards/{company.card_uuid}/participants",
        headers=_auth(company.member_tokens[0]),
    )

    body = response.json()
    assert body["items"][0]["photo_url"] == FAKE_URL
    assert fake_media.signed_url_calls == ["photos/author.jpg"]


# ——— AC-05 (NFR7): число запросов не зависит от размера компании ———


@dataclass
class QueryCounter:
    """Счётчик `execute` на сессии: доказывает отсутствие N+1."""

    count: int = 0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1


async def _count_queries(
    client: AsyncClient, session: AsyncSession, *, card_uuid: str, token: str
) -> int:
    """Считает SQL-запросы одного запроса API через `before_cursor_execute`."""
    counter = QueryCounter()
    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", counter)
    try:
        response = await client.get(f"/api/v1/cards/{card_uuid}/participants", headers=_auth(token))
        assert response.status_code == 200
    finally:
        event.remove(listen_engine, "before_cursor_execute", counter)
    return counter.count


async def test_query_count_is_independent_of_company_size(
    client: AsyncClient, session: AsyncSession, fake_media: FakeMedia
) -> None:
    """AC-05 (NFR7): 2 человека и 6 человек — ОДИНАКОВОЕ число запросов.

    Краснеет, если фото или профили начнут читать по одному на участника:
    на шести участниках счётчик вырос бы. Проверяется равенство, а не
    абсолютное число: добавление одного общего запроса (например, карточки)
    не должно красить тест, а рост от размера компании — должен.
    """
    small = await _company(session, size=2)
    big = await _company(session, size=6)

    small_count = await _count_queries(
        client, session, card_uuid=small.card_uuid, token=small.member_tokens[0]
    )
    big_count = await _count_queries(
        client, session, card_uuid=big.card_uuid, token=big.member_tokens[0]
    )

    assert small_count == big_count
    assert small_count <= 6
