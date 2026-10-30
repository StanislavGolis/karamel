"""Метрики пилота считаются по ЖИВЫМ данным (стори 10.5, Task 5; AC-02).

AC-02 — не про формулу, а про источник: NFR5 запрещает таблицу агрегатов,
`MATERIALIZED VIEW` и колонку-счётчик, а ADR-14 запрещает кэш. Три теста
здесь доказывают три части этого требования по отдельности, потому что
«данные живые» — утверждение, которое легко принять на веру:

1. в миграциях нет материализованных представлений (структурная проверка);
2. изменение базовых таблиц меняет ответ БЕЗ инвалидации (поведение);
3. ответ равен прямому SQL (уже доказано `test_pilot_metrics_api.py`, R1).

**Почему проверка миграций идёт по ИСХОДНИКАМ, а не по `pg_matviews`.**
`pg_matviews` показал бы только применённые объекты текущей базы: миграция,
добавляющая вьюху, но не применённая в тестовой БД, прошла бы незамеченной,
а именно такой дефект и надо поймать — он войдёт в следующую выкатку.
"""

from __future__ import annotations

import os
import re
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

_URL = "/api/v1/metrics"
FROM = datetime(2026, 6, 1, tzinfo=UTC)
TO = datetime(2026, 6, 15, tzinfo=UTC)

_MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _card(session: AsyncSession, *, author_id: int, filled: int) -> int:
    """Карточка в окне метрик (`published → published_at IS NOT NULL`).

    `filled_count == capacity` требует статус `full` — это CHECK
    `ck_event_cards_full_capacity` (ADR-10), двусторонний: `published` с
    полной занятостью запрещён. Без этой ветки фикстура падала
    `IntegrityError` до измеряемого `assert`, и красный тест ничего бы не
    доказывал (P-21).
    """
    status = "full" if filled >= 2 else "published"
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_id = int(place.scalar_one())
    created = FROM + timedelta(days=2)
    starts = created + timedelta(hours=2)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, created_at, kind, activity) "
            "VALUES (:a, :p, :s, :t, NULL, 'cafe', :st, 2, 60, :e, :status, :f, "
            "'approved', :pub, :created, 'company', 'coffee') RETURNING id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_city_slug,
            "t": f"Встреча {uuid_lib.uuid4().hex[:6]}",
            "st": starts,
            "e": created + timedelta(hours=1),
            "f": filled,
            "status": status,
            "pub": created - timedelta(days=1),
            "created": created,
        },
    )
    return int(row.scalar_one())


async def test_no_materialized_view_or_aggregate_table_in_migrations() -> None:
    """AC-02.1: ни одна миграция не создаёт `MATERIALIZED VIEW` или таблицу агрегатов.

    Проверяются ИСХОДНИКИ миграций: неприменённая миграция с вьюхой — тот же
    дефект, только отложенный до следующей выкатки.
    """
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"MATERIALIZED\s+VIEW", source, re.IGNORECASE):
            offenders.append(f"{path.name}: MATERIALIZED VIEW")
        if re.search(
            r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(pilot_)?metrics", source, re.IGNORECASE
        ):
            offenders.append(f"{path.name}: таблица агрегатов метрик")
    assert offenders == [], f"NFR5/ADR-14 нарушены: {offenders}"


async def test_new_data_changes_answer_without_cache(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02.3: добавили карточку → следующий запрос показывает другое число.

    Кэш (TTL, `lru_cache`, ISR) удержал бы прежний ответ, и администратор
    принимал бы решение по устаревшим числам. Тест делает ДВА запроса подряд
    и сравнивает их: единственное отличие между ними — вставленная строка.
    """
    author, token = await make_user(session, role="moderator")
    await _card(session, author_id=author.id, filled=0)

    first = (
        await client.get(
            _URL,
            params={"from": FROM.isoformat(), "to": TO.isoformat()},
            headers=cookie_headers(token),
        )
    ).json()
    assert first["metrics"][0]["denominator"] == 1
    assert first["metrics"][0]["value"] == 0.0

    await _card(session, author_id=author.id, filled=2)

    second = (
        await client.get(
            _URL,
            params={"from": FROM.isoformat(), "to": TO.isoformat()},
            headers=cookie_headers(token),
        )
    ).json()
    nfr1 = second["metrics"][0]
    assert nfr1["denominator"] == 2
    assert nfr1["value"] == pytest.approx(0.5)
    assert (nfr1["numerator"], nfr1["denominator"]) != (
        first["metrics"][0]["numerator"],
        first["metrics"][0]["denominator"],
    )


async def test_response_has_no_store_cache_control(
    client: AsyncClient, session: AsyncSession
) -> None:
    """ADR-14, AC-02.3: заголовок `cache-control: no-store` на ответе метрик.

    Без него промежуточный кэш (браузер, прокси) вернул бы вчерашние числа,
    и «метрика на любую дату» перестала бы быть правдой при неизменном коде.
    """
    _, token = await make_user(session, role="moderator")
    response = await client.get(
        _URL,
        params={"from": FROM.isoformat(), "to": TO.isoformat()},
        headers=cookie_headers(token),
    )
    assert "no-store" in response.headers.get("cache-control", "")
