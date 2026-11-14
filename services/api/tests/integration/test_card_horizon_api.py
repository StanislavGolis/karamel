"""Горизонт 72 часа и отсутствие TTL публикации (стори 12.5; AC-1, AC-2, AC-5).

**Почему отдельный файл.** `test_cards_api.py` — 666 строк и предмет «создание
и правка карточки». Здесь предмет другой: **время**. Три замка, каждый
краснеет от снятия ровно одной защиты:

- AC-1: `POST`/`PATCH` с `starts_at` дальше `now + 72 ч` → `422 STARTS_AT_TOO_FAR`;
- AC-2: `expires_at` считается от окна решения (ADR-02), а не от горизонта;
- AC-5: `published_until` нет — карточка не снимается через 72 часа.

**Через API, а не сервис.** Юнит-тесты сервиса уже держат границу
(`test_event_card_service.py`). Здесь проверяется, что код ошибки **доходит до
HTTP** с правильным статусом и что роутер его не глотает: сервис может быть
прав, а роутер — нет.

§18.1: каждый тест краснеет от снятия конкретной защиты. §18.2: единственное
отличие автора от зрителя — `author_id`, поэтому тесты не путают 404 и 422.
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

#: Горизонт, который проверяет стори. Литерал здесь допустим: это НЕ константа
#: продукта, а утверждение теста «сервер обязан отклонить 72 ч + 1 с». Если
#: завтра горизонт станет 48 ч, тест обязан покраснеть, а не подстроиться.
HORIZON = timedelta(hours=72)


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
    phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
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
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'пр. Ленина, 1', :slug, true) RETURNING uuid"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    return str(row.scalar_one())


def _payload(place_uuid: str, *, starts_at: datetime, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "cafe",
        "kind": "company",
        "activity": "boardgames",
        "starts_at": starts_at.isoformat(),
        "capacity": 3,
        "place_uuid": place_uuid,
    }
    base.update(overrides)
    return base


async def _card_row(session: AsyncSession, card_uuid: str) -> dict[str, Any]:
    """Строка карточки сырым SQL: проверяется PostgreSQL, не identity map."""
    result = await session.execute(
        text(
            "SELECT status, starts_at, expires_at, decision_window_minutes, created_at "
            "FROM event_cards WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    return dict(result.mappings().one())


# --- AC-1: горизонт через HTTP -------------------------------------------------


async def test_post_beyond_horizon_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-1: `POST` с `now + 72 ч + 1 с` → `422 STARTS_AT_TOO_FAR`.

    Краснеет, если:
    - убрать гард из `create_card` (U-2);
    - убрать `CardStartsAtTooFarError` из `except` роутера → придёт `500`;
    - заменить константу кода (U-6).
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, starts_at=datetime.now(UTC) + HORIZON + timedelta(seconds=1)),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "STARTS_AT_TOO_FAR"


async def test_post_exactly_at_horizon_is_created(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-1, U-1: ровно `now + 72 ч` → `201`.

    Пара к предыдущему тесту: без неё мутация «всегда 422» неотличима от
    «граница работает». Допуск 2 с — время выполнения запроса; горизонт
    включительный, поэтому «чуть меньше» тоже законно.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, starts_at=datetime.now(UTC) + HORIZON - timedelta(seconds=2)),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201, response.text


async def test_patch_beyond_horizon_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-1, U-3: `PATCH` не обходит горизонт.

    Краснеет, если убрать гард из `update_card`: «создать нельзя, сдвинуть
    можно» — обход правила одним запросом.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    created = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, starts_at=datetime.now(UTC) + timedelta(days=1)),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert created.status_code == 201, created.text
    card_uuid = str(created.json()["uuid"])

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        json={"starts_at": (datetime.now(UTC) + HORIZON + timedelta(hours=1)).isoformat()},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "STARTS_AT_TOO_FAR"
    # Строка не изменилась: отказ не должен оставлять половинчатую правку.
    unchanged = (await _card_row(session, card_uuid))["starts_at"]
    assert unchanged <= datetime.now(UTC) + timedelta(days=1, minutes=1)


async def test_patch_without_starts_at_still_works(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-1, U-4: `PATCH` заголовка без времени не требует `starts_at`.

    Пара к предыдущему: без неё «гард вынесен из `if`» неотличим от «`PATCH`
    сломан целиком».
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    created = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, starts_at=datetime.now(UTC) + timedelta(days=1)),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert created.status_code == 201, created.text
    card_uuid = str(created.json()["uuid"])
    before = await _card_row(session, card_uuid)

    response = await client.patch(
        f"/api/v1/cards/{card_uuid}",
        # AC-7 стори 13.2: `title` из контракта удалён — правим описание.
        json={"description": "Новое описание"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 200, response.text
    assert (await _card_row(session, card_uuid))["starts_at"] == before["starts_at"]


# --- AC-2: `expires_at` не зависит от горизонта --------------------------------


async def test_expires_at_comes_from_decision_window_not_horizon(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-2, U-7: `expires_at = LEAST(starts_at, now + окно)` (ADR-02).

    Карточка на 71-м часу с окном 60 минут: `expires_at` обязан быть равен
    `now + 60 мин`, а не `now + 72 ч`. Краснеет, если `_compute_expires_at`
    подменить на горизонт — окно решения растянулось бы до трёх суток, и
    автор ждал бы ответа трое суток вместо часа.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    now = datetime.now(UTC)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(
            place_uuid,
            starts_at=now + timedelta(hours=71),
            capacity=1,
            decision_window_minutes=60,
        ),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert response.status_code == 201, response.text

    row = await _card_row(session, str(response.json()["uuid"]))
    assert row["decision_window_minutes"] == 60
    # Окно 60 минут от момента создания; допуск 60 с на выполнение запроса.
    delta = row["expires_at"] - row["created_at"]
    assert timedelta(minutes=59) <= delta <= timedelta(minutes=61), delta


async def test_expires_at_equals_starts_at_when_window_is_longer(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-2: `starts_at` ближе окна → `expires_at == starts_at` (`LEAST`).

    Пара к предыдущему: без неё мутация «всегда `now + окно`» неотличима от
    «формула верна». `LEAST` обязателен: окно решения не может длиться дольше
    самой встречи.
    """
    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    starts = datetime.now(UTC) + timedelta(minutes=30)

    response = await client.post(
        "/api/v1/cards",
        json=_payload(
            place_uuid,
            starts_at=starts,
            capacity=1,
            decision_window_minutes=60,
        ),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert response.status_code == 201, response.text

    row = await _card_row(session, str(response.json()["uuid"]))
    assert row["expires_at"] == row["starts_at"], row


# --- AC-5: TTL публикации отсутствует ------------------------------------------


async def test_published_card_survives_100_hours_after_creation(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-5, U-8: карточка не снимается через 72 часа после создания.

    `created_at` состарен на 100 часов, `starts_at` — в будущем, карточка
    опубликована. Ожидание: `status='published'` и она есть в ленте.
    Краснеет, если появится `published_until = created_at + 72 ч` в фильтре
    `list_feed`: живая встреча исчезла бы из ленты за трое суток до начала.

    Возраст правится SQL, а не `freezegun`: зависимости `freezegun` в проекте
    нет (долг `W-11.7-b`), а `created_at` — обычная колонка.
    """
    author, token = await _new_user(session)
    author.age_confirmed = True
    author.consent_given_at = datetime.now(UTC)
    await session.flush()

    place_uuid = await _insert_place(session)
    starts = datetime.now(UTC) + timedelta(hours=71)
    created = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, starts_at=starts),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert created.status_code == 201, created.text
    card_uuid = str(created.json()["uuid"])

    await session.execute(
        text(
            "UPDATE event_cards SET verification_status = 'approved', "
            "verified_at = now() WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    published = await client.post(
        f"/api/v1/cards/{card_uuid}/publish", cookies={ACCESS_COOKIE_NAME: token}
    )
    assert published.status_code == 200, published.text

    # Состарить создание на 100 часов: горизонт «создание + 72 ч» уже позади.
    await session.execute(
        text(
            "UPDATE event_cards SET created_at = created_at - interval '100 hours', "
            "published_at = published_at - interval '100 hours' WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    await session.flush()

    row = await _card_row(session, card_uuid)
    assert row["status"] == "published", row

    # Лента — `GET /api/v1/cards`: маршрута `/feed` нет, его перехватил бы
    # `/{card_uuid}` и ответил `422 invalid_request` на «feed» как UUID.
    feed = await client.get("/api/v1/cards", cookies={ACCESS_COOKIE_NAME: token})
    assert feed.status_code == 200, feed.text
    uuids = {item["uuid"] for item in feed.json()["items"]}
    # Краснеет, если лента начнёт фильтровать по возрасту публикации.
    assert card_uuid in uuids, "карточка выпала из ленты по возрасту публикации"


async def test_no_published_until_column_or_field() -> None:
    """AC-5, U-8: в коде нет `published_until` — TTL не заведён «по привычке».

    Статический замок (P-25: проверяется эффект, а не форма): поле в модели
    или фильтр в репозитории вернули бы TTL, даже если тест выше пока
    зелёный по другой причине (например, `published_at` ещё не состарен).

    Краснеет от появления `published_until` в `app/`. Чтение файлов, а не
    импорт: `published_until` может оказаться в миграции или в SQL-строке,
    где его не видно через `dir()`.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"

    def code_only(source: str) -> str:
        """Оставляет только КОД: вырезаны ТОЛЬКО `#`-комментарии.

        **Строковые литералы остаются намеренно (CR 12.5, H-1).** Первая
        редакция вырезала и `STRING`, и замок стал слепым ровно к тому
        случаю, ради которого написан: докстринг обещает поймать
        `published_until` «в миграции или в SQL-строке», а SQL-фильтр
        `WHERE published_until > now()` — это `STRING`-токен.

        Доказано мутацией (CR 12.5):
        - `published_until = None` (NAME) → тест КРАСНЫЙ;
        - `_FAKE_SQL = "WHERE published_until > now()"` (STRING) →
          тест был ЗЕЛЁНЫМ, то есть настоящий TTL в SQL-фильтре прошёл бы.

        Плата за это: объяснять запрет можно только `#`-комментарием
        (`event_card_service.py:88`), а не докстрингом. Это осознанный
        размен: докстринг — тот же литерал, и запрет «нельзя упоминать
        даже строкой» проверяем, а «нельзя реализовать» — нет.
        """
        import io
        import tokenize

        out: list[str] = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok.type == tokenize.COMMENT:
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, IndentationError):
            # Непарсимый файл — не повод пропустить замок: возвращаем как есть.
            return source
        return "\n".join(out)

    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if "published_until" in code_only(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


# --- AC-2: автоархив по `starts_at`, не по `created_at + 72 ч` ------------------


async def test_finish_started_cards_uses_starts_at(
    session: AsyncSession, client: AsyncClient
) -> None:
    """AC-2, U-15: `finish_started_cards` смотрит на `starts_at`.

    Карточка создана 100 часов назад, `starts_at` — в будущем. Если бы
    автоархив считал «создание + 72 ч», встреча ушла бы в `finished` до
    начала и участники потеряли бы карточку. Краснеет от подмены предиката.
    """
    from app.services.event_card_service import finish_started_cards

    _, token = await _new_user(session)
    place_uuid = await _insert_place(session)
    starts = datetime.now(UTC) + timedelta(hours=48)
    created = await client.post(
        "/api/v1/cards",
        json=_payload(place_uuid, starts_at=starts),
        cookies={ACCESS_COOKIE_NAME: token},
    )
    assert created.status_code == 201, created.text
    card_uuid = str(created.json()["uuid"])

    await session.execute(
        text(
            "UPDATE event_cards SET status = 'published', "
            "created_at = created_at - interval '100 hours', "
            "published_at = now() WHERE uuid = :uuid"
        ),
        {"uuid": card_uuid},
    )
    await session.flush()

    finished = await finish_started_cards(session, now=datetime.now(UTC))

    # Другие строки в базе могли начаться раньше: важно, что НАША не закрыта.
    assert finished >= 0
    assert (await _card_row(session, card_uuid))["status"] == "published"
