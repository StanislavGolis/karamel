"""AC-04: ретенция не переписывает историю метрик (стори 10.6; Task 6.1–6.4).

**Что именно проверяется.** NFR3 — фильтр `created_at ∈ [period_from,
period_to)` по `event_cards` (`pilot_metrics.py:75-80`). Удаляемая карточка
имеет `created_at < starts_at < moment - 6 мес` (инвариант `created_at <
starts_at` держит `event_card_service.py:392,541,669,731`). Значит:

- при `period_from >= moment - 6 мес` её в окне НЕТ → значение не меняется
  (Task 6.1, 6.2 — AC-04 ВЫПОЛНЯЕТСЯ);
- при более раннем начале окна она В окне → значение МЕНЯЕТСЯ
  (Task 6.4 — разрыв W-10.6-a, зафиксирован, не замолчан).

**Почему окно задаётся от `moment`, а не от `now()`.** Весь тест идёт на ОДНОМ
моменте: прогон и оба замера метрики видят одну и ту же границу шести
месяцев. Иначе тест зависел бы от разницы в миллисекунды.

**Почему в окне обязана быть неудаляемая карточка.** Иначе тест тривиален:
пустое окно даёт `null` и до, и после прогона — «значение не изменилось»
было бы правдой без всякой ретенции (D-10.6-1, требование к тестам).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.services.attendance_retention_service import purge_expired_attendance
from app.services.pilot_metrics_service import build_metrics
from app.services.session_service import ACCESS_COOKIE_NAME
from tests.integration.attendance_history_helpers import add_attendance, make_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _boundary(session: AsyncSession, moment: datetime) -> datetime:
    """Граница «6 календарных месяцев назад» по часам PostgreSQL (D-10.6-6)."""
    row = await session.execute(
        text("SELECT CAST(:m AS timestamptz) - interval '6 months'"), {"m": moment}
    )
    return row.scalar_one()


async def _card(
    session: AsyncSession,
    *,
    author_id: int,
    participant_id: int,
    starts_at: datetime,
    created_at: datetime,
    pair_status: str = "pending",
) -> int:
    """Карточка `full` с ЯВНЫМ `created_at` и строкой `attendance`.

    `created_at` задаётся данными, а не `now()`: метрика фильтрует именно по
    нему, и без управления этим полем окно теста не построить.
    """
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_id = int(place.scalar_one())
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, created_at, kind, activity) "
            "VALUES (:a, :p, :s, 'Встреча', NULL, 'cafe', :st, 2, 60, :e, 'full', 2, "
            "'approved', :pub, :created, 'company', 'coffee') RETURNING id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": settings.default_city_slug,
            "st": starts_at,
            "e": starts_at - timedelta(hours=1),
            "pub": starts_at - timedelta(days=1),
            "created": created_at,
        },
    )
    card_id = int(card.scalar_one())
    await add_attendance(
        session, card_id=card_id, participant_id=participant_id, status=pair_status
    )
    await session.flush()
    return card_id


async def _nfr3(
    session: AsyncSession, *, period_from: datetime, period_to: datetime
) -> tuple[int, int, float | None]:
    """NFR3 за окно: `(numerator, denominator, value)` — как их отдаёт сервис."""
    metrics = await build_metrics(session, period_from=period_from, period_to=period_to)
    nfr3 = next(metric for metric in metrics.metrics if metric.code == "nfr3")
    return nfr3.numerator, nfr3.denominator, nfr3.value


# --- Task 6.1, 6.2: окно внутри срока хранения — значение не меняется ---


async def test_metric_unchanged_when_window_starts_after_boundary(session: AsyncSession) -> None:
    """AC-04 (Task 6.1): окно с `period_from ≥ moment − 6 мес` → NFR3 идентичен.

    Прогон удаляет карточку, созданную 7 месяцев назад (`created_at` вне окна),
    и не трогает карточку внутри срока хранения. Окно [`moment − 5 мес`,
    `moment`) содержит неудаляемую карточку — значит замер не тривиален.
    """
    moment = datetime.now(UTC)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)

    # Удаляемая: встреча и создание — 7 месяцев назад (вне окна метрики).
    await _card(
        session,
        author_id=author.id,
        participant_id=participant.id,
        starts_at=moment - timedelta(days=210),
        created_at=moment - timedelta(days=210),
        pair_status="pending",
    )
    # Неудаляемая: встреча 10 дней назад, создана 20 дней назад (В окне).
    kept_card_id = await _card(
        session,
        author_id=author.id,
        participant_id=participant.id,
        starts_at=moment - timedelta(days=10),
        created_at=moment - timedelta(days=20),
        pair_status="confirmed",
    )

    period_from = moment - timedelta(days=150)
    before = await _nfr3(session, period_from=period_from, period_to=moment)
    result = await purge_expired_attendance(session, moment=moment, limit=200)
    after = await _nfr3(session, period_from=period_from, period_to=moment)

    assert result.deleted == 1, "прогон обязан удалить ровно одну карточку"
    assert before == after, (
        f"NFR3 изменился после прогона: {before} → {after}. "
        "AC-04 требует, чтобы при period_from ≥ moment − 6 мес значение оставалось прежним"
    )
    # Замер не тривиален: в окне была и осталась карточка.
    assert after[1] == 1
    assert await _count_attendance(session, kept_card_id) == 1


async def test_kept_card_contribution_unchanged(session: AsyncSession) -> None:
    """AC-04 (Task 6.2): карточка внутри срока хранения осталась, вклад тот же.

    Контроль к предыдущему тесту: значение могло «не измениться» и потому,
    что обе карточки исчезли. Здесь проверяется, что неудаляемая карточка
    на месте и её строка `attendance` цела.
    """
    moment = datetime.now(UTC)
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    kept_card_id = await _card(
        session,
        author_id=author.id,
        participant_id=participant.id,
        starts_at=moment - timedelta(days=5),
        created_at=moment - timedelta(days=6),
        pair_status="confirmed",
    )

    await purge_expired_attendance(session, moment=moment, limit=200)

    assert await _count_attendance(session, kept_card_id) == 1
    numerator, denominator, value = await _nfr3(
        session, period_from=moment - timedelta(days=30), period_to=moment
    )
    assert (numerator, denominator, value) == (1, 1, 1.0)


async def _count_attendance(session: AsyncSession, card_id: int) -> int:
    """Число строк `attendance` у карточки — без ORM."""
    row = await session.execute(
        text("SELECT count(*) FROM attendance WHERE card_id = :c"), {"c": card_id}
    )
    return int(row.scalar_one())


# --- Task 6.3: инвариант created_at < starts_at ---


async def test_created_before_starts_invariant(session: AsyncSession) -> None:
    """D-10.6-1 (Task 6.3): `created_at < starts_at` — основание вывода об AC-04.

    Проверяются обе половины утверждения:
    1. карточка, созданная через API, имеет `created_at < starts_at`;
    2. карточку со `starts_at` в прошлом создать НЕЛЬЗЯ (`CardStartsAtInPastError`
       → 4xx), поэтому `created_at < starts_at` нельзя обойти.

    Без второй половины первая была бы верна только для добросовестных
    данных, и вывод «удаляемая карточка не попадает в окно» стал бы догадкой.
    """
    author, token = await make_user(session)
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING uuid"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_uuid = place.scalar_one()

    async def _override() -> Any:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            future = await client.post(
                "/api/v1/cards",
                json={
                    "title": "Настольные игры",
                    "category": "cafe",
                    # Стори 12.4 (AC-3): тип и активность обязательны.
                    "kind": "company",
                    "activity": "boardgames",
                    "place_uuid": str(place_uuid),
                    "starts_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                    "capacity": 3,
                },
                cookies={ACCESS_COOKIE_NAME: token},
            )
            past = await client.post(
                "/api/v1/cards",
                json={
                    "title": "Прошлая встреча",
                    "category": "cafe",
                    # Стори 12.4 (AC-3): тип и активность обязательны.
                    "kind": "company",
                    "activity": "boardgames",
                    "place_uuid": str(place_uuid),
                    "starts_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    "capacity": 3,
                },
                cookies={ACCESS_COOKIE_NAME: token},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert future.status_code == 201
    assert past.status_code == 422, (
        f"карточку в прошлом создать нельзя, ответ: {past.status_code} {past.text}"
    )

    row = await session.execute(
        text(
            "SELECT created_at < starts_at FROM event_cards "
            "WHERE author_id = :a ORDER BY id DESC LIMIT 1"
        ),
        {"a": author.id},
    )
    assert row.scalar_one() is True


# --- Task 6.4 → стори 11.7, Task 3: запрос за нижней границей отвергается ---


async def _http_metrics(
    session: AsyncSession,
    token: str,
    *,
    period_from: datetime,
    period_to: datetime,
) -> Any:
    """`GET /api/v1/metrics` настоящим HTTP-клиентом на переданной сессии.

    Образец — `test_created_before_starts_invariant:224-251`: подмена
    `get_session` и `ASGITransport` без поднятия сервера. Роутер обязан
    отвечать `422`, а `422` — HTTP-семантика; `build_metrics` о кодах не
    знает (project-context §6), поэтому проверка идёт через клиент, а не
    через сервис.
    """
    async def _override() -> Any:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/api/v1/metrics",
                params={"from": period_from.isoformat(), "to": period_to.isoformat()},
                cookies={ACCESS_COOKIE_NAME: token},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_request_before_boundary_is_rejected(session: AsyncSession) -> None:
    """AC-1, AC-5 (стори 11.7): окно с `period_from < now − 6 мес` → `422`.

    **Тест переписан, а не удалён.** До 11.7 он назывался
    `test_metric_changes_when_window_starts_before_boundary` и фиксировал
    разрыв `W-10.6-a`: значение NFR3 МЕНЯЛОСЬ после прогона ретенции.
    Теперь такой запрос отвергается, поэтому утверждение другое — окно
    НЕДОСТУПНО, и значения, которое могло бы измениться, не существует
    вовсе. Это и есть `AC-04` стори 10.6, закрытый кодом, а не документом.

    **Подготовка сохранена намеренно.** Карточка, созданная 210 дней назад,
    и прогон `purge_expired_attendance` остаются: без них тест перестал бы
    быть про ретенцию и стал бы тестом разбора даты. Проверяются оба
    состояния — ДО прогона (карточка и её строка `attendance` ещё живы) и
    ПОСЛЕ (ретенция их удалила): контракт держится на границе окна, а не на
    содержимом базы.
    """
    moment = datetime.now(UTC)
    author, token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    await _card(
        session,
        author_id=author.id,
        participant_id=participant.id,
        starts_at=moment - timedelta(days=210),
        created_at=moment - timedelta(days=210),
        pair_status="pending",
    )

    period_from = moment - timedelta(days=240)
    period_to = moment - timedelta(days=1)

    # Task 3.4: отказ ДО прогона ретенции — замок на контракте, не на данных.
    before_purge = await _http_metrics(
        session, token, period_from=period_from, period_to=period_to
    )
    assert before_purge.status_code == 422, before_purge.text
    assert before_purge.json() == {"code": "period_too_old"}

    result = await purge_expired_attendance(session, moment=moment, limit=200)
    assert result.deleted == 1

    # Task 3.3: тот же запрос ПОСЛЕ прогона — тот же отказ, а не `200`.
    after_purge = await _http_metrics(
        session, token, period_from=period_from, period_to=period_to
    )
    assert after_purge.status_code == 422, after_purge.text
    assert after_purge.json() == {"code": "period_too_old"}


async def test_period_starting_inside_six_months_is_allowed(session: AsyncSession) -> None:
    """AC-1, Task 3.6: окно, начинающееся ВНУТРИ шести месяцев, → `200`.

    **Почему не «ровно шесть месяцев».** Роутер берёт `datetime.now(UTC)` в
    момент запроса, а тест захватил `moment` раньше. Разница в микросекунды
    делала бы `moment − 6 мес` на волосок СТАРШЕ серверной границы, и тест
    флейкал бы на часах (`freezegun` в проекте нет, и тянуть его ради одной
    проверки дороже, чем отступ в сутки). Поэтому проверяются две стороны
    с отступом: `− 6 мес + 1 сут` (внутри) здесь и `− 6 мес − 1 сут`
    (снаружи) в тесте выше — его окно начинается за 240 сут.

    Замок на границу `>=`/`>` проверяется именно этими двумя сторонами:
    мутация «сдвинуть предел на сутки» краснит один из них.
    """
    moment = datetime.now(UTC)
    _, token = await make_user(session, role="moderator")

    boundary = await _boundary(session, moment)
    response = await _http_metrics(
        session,
        token,
        period_from=boundary + timedelta(days=1),
        period_to=moment,
    )
    assert response.status_code == 200, response.text


async def test_period_starting_one_hour_before_boundary_is_rejected(session: AsyncSession) -> None:
    """AC-1, вторая сторона границы: `now − 6 мес − 1 час` → `422`.

    **Зачем вторая сторона.** Замок «внутри границы → `200`» ловит только
    мутацию «предел стал СТРОЖЕ» (сдвиг вперёд). Сдвиг назад («предел стал
    мягче на сутки») оставлял бы его зелёным: окно `boundary + 1 сут` всё ещё
    внутри. Отступ в час, а не в сутки, — потому что сдвиг на сутки вниз
    обязан краснеть, а граница берётся по часам сервера в момент запроса:
    час перекрывает разницу между `moment` теста и `now` роутера с запасом.
    """
    moment = datetime.now(UTC)
    _, token = await make_user(session, role="moderator")

    boundary = await _boundary(session, moment)
    response = await _http_metrics(
        session,
        token,
        period_from=boundary - timedelta(hours=1),
        period_to=moment,
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"code": "period_too_old"}
