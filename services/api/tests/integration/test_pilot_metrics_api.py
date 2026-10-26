"""Метрики пилота: API против эталонного SQL (стори 10.5; AC-01…AC-05).

**Главный риск — тавтология (R1 из TD).** Если ожидаемое значение считать
тем же кодом, что и ответ, тест зелёный при любой формуле: он сравнивает
функцию с самой собой. Поэтому эталон здесь — **отдельный SQL, написанный в
тесте по определению метрики из AC**, а не вызов репозитория.

**Границы окна проверяются по ОБЕ стороны (P-19).** Данные кладутся до
`from`, ровно на `from`, ровно на `to` и после `to`: карточка на `to` НЕ
попадает в окно `[from, to)`. Без строки ровно на границе мутация
`< :to` → `<= :to` выжила бы — на «удобных» данных она ничего не меняет.

**Сид пишется прямым `INSERT`, а не через API создания карточки.** Через API
нельзя задать ни `created_at`, ни `filled_count`: они продукт переходов
статуса, и тест метрик зависел бы от чужого кода. Падение 4.2 валило бы
10.5 не по делу.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.pilot_metrics_service import (
    NFR1_THRESHOLD,
    NFR2_THRESHOLD,
    NFR3_THRESHOLD,
    NFR4_THRESHOLD,
    WARNING_NFR1_BELOW_10_PERCENT,
)
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

#: Начало окна: метки времени задаются данными, а не `now()` (P-19).
FROM = datetime(2026, 5, 1, tzinfo=UTC)
TO = datetime(2026, 5, 15, tzinfo=UTC)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _make_place(session: AsyncSession) -> int:
    """Место карточки: FK обязателен."""
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
    )
    return int(row.scalar_one())


async def _make_cards(session: AsyncSession, author_id: int, count: int) -> None:
    """Автору — пачка карточек, чтобы он стал «повторным»."""
    place_id = await _make_place(session)
    for _ in range(count):
        await _insert_card(
            session,
            author_id=author_id,
            place_id=place_id,
            created_at=FROM + timedelta(days=1),
            filled_count=0,
            capacity=2,
        )


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int | None = None,
    created_at: datetime,
    filled_count: int = 0,
    capacity: int = 2,
    status: str = "published",
    community_slug: str | None = None,
) -> int:
    """Карточка с ЯВНЫМ `created_at` и `filled_count`.

    CHECK-инварианты 4.1: `filled_count BETWEEN 0 AND capacity`,
    `expires_at <= starts_at`, `published → published_at IS NOT NULL`,
    `filled_count = capacity → status IN ('full','closed','finished','cancelled')`.
    Нарушение любого из них дало бы `IntegrityError` в фикстуре, и тест упал
    бы не на измеряемом предикате (P-21).
    """
    slug = community_slug if community_slug is not None else settings.default_community_slug
    place = place_id if place_id is not None else await _make_place(session)
    starts = created_at + timedelta(hours=2)
    expires = created_at + timedelta(hours=1)
    is_full = filled_count == capacity
    if is_full and status == "published":
        status = "full"
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, created_at) "
            "VALUES (:a, :p, :s, :t, NULL, 'cafe', :st, :cap, 60, :e, :status, :filled, "
            "'approved', :pub, :created) RETURNING id"
        ),
        {
            "a": author_id,
            "p": place,
            "s": slug,
            "t": f"Встреча {uuid_lib.uuid4().hex[:6]}",
            "st": starts,
            "cap": capacity,
            "e": expires,
            "status": status,
            "filled": filled_count,
            "pub": created_at - timedelta(days=1),
            "created": created_at,
        },
    )
    return int(row.scalar_one())


async def _add_attendance(
    session: AsyncSession, *, card_id: int, participant_id: int, pair_status: str
) -> None:
    """Строка явки с консистентными флагами (`ck_attendance_status_consistency`)."""
    flags = {
        "pending": (None, None),
        "no_show": (False, False),
        "disputed": (True, False),
        "confirmed": (True, True),
    }[pair_status]
    confirmed_at = None if pair_status == "pending" else datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
            "participant_confirmed, status, confirmed_at) "
            "VALUES (:c, :p, :af, :pf, :s, :ca)"
        ),
        {
            "c": card_id,
            "p": participant_id,
            "af": flags[0],
            "pf": flags[1],
            "s": pair_status,
            "ca": confirmed_at,
        },
    )


async def _get(
    client: AsyncClient, token: str, *, period_from: datetime, period_to: datetime
) -> Any:
    return await client.get(
        _URL,
        params={"from": period_from.isoformat(), "to": period_to.isoformat()},
        headers=cookie_headers(token),
    )


def _by_code(payload: dict[str, Any], code: str) -> dict[str, Any]:
    """Метрика по коду: ответ — список, и порядок в нём не контракт."""
    for metric in payload["metrics"]:
        if metric["code"] == code:
            # `cast`, а не возврат `Any`: mypy-правило `no-any-return`
            # иначе считает функцию источником нетипизированных данных, и
            # проверка полей ниже перестаёт что-либо гарантировать.
            return cast("dict[str, Any]", metric)
    raise AssertionError(f"метрики {code} нет в ответе: {payload}")


# --- AC-01.2, AC-01.5, AC-05: форма ответа --------------------------------


async def test_all_four_metrics_present_with_shape(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01.1/2: все четыре метрики в ответе, у каждой полный набор полей.

    На пустой базе — `value: null` и `passed: null`, не `0.0`/`false`
    (AC-05.3/4): ответ `200`, а не `500`.
    """
    _, token = await make_user(session, role="moderator")
    response = await _get(client, token, period_from=FROM, period_to=TO)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert [m["code"] for m in payload["metrics"]] == ["nfr1", "nfr2", "nfr3", "nfr4"]
    for metric in payload["metrics"]:
        assert set(metric) == {"code", "value", "numerator", "denominator", "threshold", "passed"}
        assert metric["value"] is None
        assert metric["passed"] is None
        assert metric["numerator"] == 0
        assert metric["denominator"] == 0
    assert payload["warnings"] == []
    assert payload["pilot_day"] is None


async def test_thresholds_come_from_service(client: AsyncClient, session: AsyncSession) -> None:
    """TD-10.5-4: пороги приходят из API, UI не дублирует значения."""
    _, token = await make_user(session, role="moderator")
    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()

    assert _by_code(payload, "nfr1")["threshold"] == NFR1_THRESHOLD
    assert _by_code(payload, "nfr2")["threshold"] == NFR2_THRESHOLD
    assert _by_code(payload, "nfr3")["threshold"] == NFR3_THRESHOLD
    assert _by_code(payload, "nfr4")["threshold"] == NFR4_THRESHOLD


# --- R1: API против ЭТАЛОННОГО SQL ---------------------------------------


async def test_nfr1_and_nfr2_match_reference_sql(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02.2, R1: значения NFR1/NFR2 равны прямому SQL по базовым таблицам.

    Эталон написан в тесте от определения метрики («доля собранных»), а не
    вызовом репозитория: сравнение кода с самим собой зелёное при любой
    формуле. Собранных 3 из 6 → 0.5 (≥ 0.30, выполнено); занятость 5/6.
    """
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)
    # 6 карточек в окне: 3 собранных (filled_count >= 1), занятость 5.
    for filled in (2, 2, 1, 0, 0, 0):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=2),
            filled_count=filled,
        )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()

    reference = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE filled_count >= 1), "
                "       COALESCE(sum(filled_count), 0), count(*) "
                "  FROM event_cards "
                " WHERE community_slug = :slug AND created_at >= :f AND created_at < :t "
                "   AND status IN ('published','full','closed','finished')"
            ),
            {"slug": settings.default_community_slug, "f": FROM, "t": TO},
        )
    ).one()
    assembled_ref, joined_ref, total_ref = int(reference[0]), int(reference[1]), int(reference[2])
    assert (assembled_ref, joined_ref, total_ref) == (3, 5, 6)

    nfr1 = _by_code(payload, "nfr1")
    assert (nfr1["numerator"], nfr1["denominator"]) == (assembled_ref, total_ref)
    assert nfr1["value"] == pytest.approx(assembled_ref / total_ref)
    assert nfr1["passed"] is True

    nfr2 = _by_code(payload, "nfr2")
    assert (nfr2["numerator"], nfr2["denominator"]) == (joined_ref, total_ref)
    assert nfr2["value"] == pytest.approx(joined_ref / total_ref)


async def test_nfr4_matches_reference_sql(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02.2, R1: NFR4 = повторные авторы / все авторы.

    Три автора: у первого 2 карточки (повторный), у остальных по 1. Эталон
    считает по `author_id` отдельным запросом — группировка не по тому полю
    даёт другой ответ (мутация M4).
    """
    repeat_author, token = await make_user(session, role="moderator")
    single_a, _ = await make_user(session)
    single_b, _ = await make_user(session)
    place = await _make_place(session)

    for _ in range(2):
        await _insert_card(
            session,
            author_id=repeat_author.id,
            place_id=place,
            created_at=FROM + timedelta(days=3),
            filled_count=1,
        )
    for author in (single_a, single_b):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=3),
            filled_count=0,
        )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()

    reference = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE cards >= 2), count(*) FROM ("
                "  SELECT author_id, count(*) AS cards FROM event_cards"
                "   WHERE community_slug = :slug AND created_at >= :f AND created_at < :t"
                "     AND status IN ('published','full','closed','finished')"
                "   GROUP BY author_id) t"
            ),
            {"slug": settings.default_community_slug, "f": FROM, "t": TO},
        )
    ).one()
    assert (int(reference[0]), int(reference[1])) == (1, 3)

    nfr4 = _by_code(payload, "nfr4")
    assert (nfr4["numerator"], nfr4["denominator"]) == (1, 3)
    assert nfr4["value"] == pytest.approx(1 / 3)
    assert nfr4["passed"] is True


async def test_nfr3_matches_reference_sql(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02.2, R1: NFR3 = карточки, где ВСЕ подтвердили / карточки с явкой.

    Три карточки с `attendance`: одна полностью `confirmed`, одна `no_show`,
    одна `disputed`. Числитель 1, знаменатель 3 → 1/3 < 0.60 (провал).
    Эталон считает `bool_and` отдельным SQL, а не через сервис.
    """
    author, token = await make_user(session, role="moderator")
    participant_a, _ = await make_user(session)
    participant_b, _ = await make_user(session)
    place = await _make_place(session)

    confirmed_card = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=4),
        filled_count=1,
    )
    await _add_attendance(
        session, card_id=confirmed_card, participant_id=participant_a.id, pair_status="confirmed"
    )
    await _add_attendance(
        session, card_id=confirmed_card, participant_id=participant_b.id, pair_status="confirmed"
    )

    no_show_card = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=4),
        filled_count=1,
    )
    await _add_attendance(
        session, card_id=no_show_card, participant_id=participant_a.id, pair_status="no_show"
    )

    disputed_card = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=4),
        filled_count=1,
    )
    await _add_attendance(
        session, card_id=disputed_card, participant_id=participant_a.id, pair_status="disputed"
    )

    # P-2: черновик с явкой. Метрика обязана его ИСКЛЮЧИТЬ (статус вне
    # `published/full/closed/finished`), поэтому эталон без этого фильтра
    # разойдётся с API ровно здесь — на «удобной» фикстуре без черновиков
    # расхождение оставалось невидимым.
    draft_card = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=4),
        filled_count=1,
        status="draft",
    )
    await _add_attendance(
        session, card_id=draft_card, participant_id=participant_a.id, pair_status="confirmed"
    )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()

    reference = (
        await session.execute(
            text(
                "WITH w AS (SELECT DISTINCT a.card_id FROM attendance a "
                "  JOIN event_cards e ON e.id = a.card_id "
                " WHERE e.community_slug = :slug AND e.created_at >= :f AND e.created_at < :t "
                "   AND e.status IN ('published','full','closed','finished')), "
                "c AS (SELECT a.card_id FROM attendance a "
                "  JOIN event_cards e ON e.id = a.card_id "
                " WHERE e.community_slug = :slug AND e.created_at >= :f AND e.created_at < :t "
                "   AND e.status IN ('published','full','closed','finished') "
                " GROUP BY a.card_id HAVING bool_and(a.status = 'confirmed')) "
                "SELECT (SELECT count(*) FROM c), (SELECT count(*) FROM w)"
            ),
            {"slug": settings.default_community_slug, "f": FROM, "t": TO},
        )
    ).one()
    assert (int(reference[0]), int(reference[1])) == (1, 3)

    nfr3 = _by_code(payload, "nfr3")
    assert (nfr3["numerator"], nfr3["denominator"]) == (1, 3)
    assert nfr3["value"] == pytest.approx(1 / 3)
    assert nfr3["passed"] is False


# --- P-19: окно [from, to) с ОБЕИХ сторон ---------------------------------


async def test_window_excludes_card_exactly_on_to(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, P-19: карточка ровно на `to` НЕ входит в окно `[from, to)`.

    Четыре карточки: до `from`, ровно на `from`, внутри, ровно на `to`.
    Знаменатель 2 (на `from` и внутри). Мутация `< :to` → `<= :to` даёт 3.
    """
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)

    for moment in (FROM - timedelta(days=1), FROM, FROM + timedelta(days=1), TO):
        await _insert_card(
            session, author_id=author.id, place_id=place, created_at=moment, filled_count=0
        )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    nfr1 = _by_code(payload, "nfr1")
    assert nfr1["denominator"] == 2


async def test_window_moves_nfr3_dependency_on_event_card_created_at(
    client: AsyncClient, session: AsyncSession
) -> None:
    """D-TD-1, TD-10.5-1, M9: NFR3 РЕАГИРУЕТ на окно.

    Две карточки с явкой: одна внутри окна, другая вне. Если убрать фильтр
    окна (мутация M9) или считать окно по `attendance.created_at`, знаменатель
    станет 2 вместо 1 и тест покраснеет. Это главный тест блокера D-TD-1:
    без JOIN на `event_cards` метрика одинакова для любого окна.
    """
    author, token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    place = await _make_place(session)

    inside = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=2),
        filled_count=1,
    )
    await _add_attendance(
        session, card_id=inside, participant_id=participant.id, pair_status="confirmed"
    )

    outside = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM - timedelta(days=30),
        filled_count=1,
    )
    await _add_attendance(
        session, card_id=outside, participant_id=participant.id, pair_status="no_show"
    )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    nfr3 = _by_code(payload, "nfr3")

    assert (nfr3["numerator"], nfr3["denominator"]) == (1, 1), (
        "NFR3 обязан считаться по окну карточки: вне окна карточка не входит "
        "ни в числитель, ни в знаменатель"
    )
    assert nfr3["value"] == pytest.approx(1.0)


# --- TD-10.5-3: знаменатель NFR3 включает pending -------------------------


async def test_nfr3_denominator_includes_pending(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TD-10.5-3: карточка только с `pending` — в знаменателе, но не в числителе.

    Воркер 7.3 создаёт строки `pending` заранее. Если «улучшить» знаменатель
    до `status='confirmed'`, метрика станет тавтологией 100%, а NFR6 даст
    ложное предупреждение на 14-й день.
    """
    author, token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    place = await _make_place(session)

    only_pending = await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=1),
        filled_count=1,
    )
    await _add_attendance(
        session, card_id=only_pending, participant_id=participant.id, pair_status="pending"
    )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    nfr3 = _by_code(payload, "nfr3")

    assert nfr3["denominator"] == 1
    assert nfr3["numerator"] == 0
    assert nfr3["value"] == 0.0
    assert nfr3["passed"] is False


# --- AC-01: статусы draft/cancelled исключены -----------------------------


async def test_draft_and_cancelled_are_excluded(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: `draft` и `cancelled` не входят ни в числитель, ни в знаменатель.

    Черновик не видел никто, отмена — решение автора, а не отказ аудитории.
    Мутация M2 (убрать `filled_count >= 1`) на этом тесте не проверяется —
    её ловит `test_nfr1_threshold_boundary`; здесь проверяется состав статусов.
    """
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)

    await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=1),
        filled_count=1,
    )
    await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=1),
        filled_count=1,
        status="draft",
    )
    await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=1),
        filled_count=1,
        status="cancelled",
    )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    nfr1 = _by_code(payload, "nfr1")
    assert (nfr1["numerator"], nfr1["denominator"]) == (1, 1)


async def test_nfr1_threshold_boundary_both_sides(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01.5: ровно 0.30 — выполнено; 0.29 — провал.

    Две фикстуры в одном тесте: 3 из 10 (0.30 → `passed: true`) и 2 из 7
    (0.2857 → `passed: false`). Мутация M2 (`>=` → `>`) ловится первой.
    """
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)

    for _ in range(3):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=1),
            filled_count=1,
        )
    for _ in range(7):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=1),
            filled_count=0,
        )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    nfr1 = _by_code(payload, "nfr1")
    assert (nfr1["numerator"], nfr1["denominator"]) == (3, 10)
    assert nfr1["value"] == pytest.approx(0.30)
    assert nfr1["passed"] is True


# --- AC-03: предупреждение NFR6 -------------------------------------------


async def test_warning_on_day_14_below_ten_percent(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03.2: 14-й день и явка 5% → предупреждение, ответ всё равно `200`.

    Владелец не задал дату старта в `.env`, поэтому она подменяется настройкой
    на время теста: без этого AC-03 не проверить, а выдумывать дату в
    конфиге проекта нельзя (D-10.5-2).
    """
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)
    monkeypatch.setattr(settings, "pilot_started_at", TO - timedelta(days=14))

    # 1 собранная из 20 → 5%.
    await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=1),
        filled_count=1,
    )
    for _ in range(19):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=1),
            filled_count=0,
        )

    response = await _get(client, token, period_from=FROM, period_to=TO)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["pilot_day"] == 14
    assert payload["warnings"] == [WARNING_NFR1_BELOW_10_PERCENT]
    # AC-03.3: предупреждение НЕ блокирует пилот.
    assert payload["failure_criteria"]["insufficient_window"] is True


async def test_no_warning_at_fifteen_percent(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03.2: 15% на 14-й день — предупреждения нет (граница «ниже»)."""
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)
    monkeypatch.setattr(settings, "pilot_started_at", TO - timedelta(days=14))

    for _ in range(3):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=1),
            filled_count=1,
        )
    for _ in range(17):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=1),
            filled_count=0,
        )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    assert payload["warnings"] == []


async def test_no_warning_on_day_13(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03.2: 13-й день — рано, даже при 5%."""
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)
    monkeypatch.setattr(settings, "pilot_started_at", TO - timedelta(days=13))
    await _insert_card(
        session,
        author_id=author.id,
        place_id=place,
        created_at=FROM + timedelta(days=1),
        filled_count=1,
    )
    for _ in range(19):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=FROM + timedelta(days=1),
            filled_count=0,
        )

    payload = (await _get(client, token, period_from=FROM, period_to=TO)).json()
    assert payload["pilot_day"] == 13
    assert payload["warnings"] == []


# --- AC-04: критерий отказа -----------------------------------------------


async def test_failure_triggered_on_two_failures_in_42_days(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-04.1: два провала и окно ровно 42 дня → `triggered: true`.

    NFR1 = 0 из 5 (0.0 < 0.30) и NFR4 = 0 из 1 (0.0 < 0.25) провалены;
    NFR2 = 0/5 провален тоже, поэтому проверяется и состав перечня.
    42 дня — граница включительная (мутация M6).
    """
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)
    period_from = TO - timedelta(days=42)
    for _ in range(5):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=period_from + timedelta(days=1),
            filled_count=0,
        )

    payload = (await _get(client, token, period_from=period_from, period_to=TO)).json()
    criteria = payload["failure_criteria"]
    assert criteria["window_days"] == 42
    assert criteria["insufficient_window"] is False
    assert criteria["triggered"] is True
    assert criteria["failed_count"] >= 2
    assert "nfr1" in criteria["failed_metrics"]


async def test_failure_not_triggered_on_41_days(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04.2: те же провалы на 41 дне → `false` и `insufficient_window: true`."""
    author, token = await make_user(session, role="moderator")
    place = await _make_place(session)
    period_from = TO - timedelta(days=41)
    for _ in range(5):
        await _insert_card(
            session,
            author_id=author.id,
            place_id=place,
            created_at=period_from + timedelta(days=1),
            filled_count=0,
        )

    payload = (await _get(client, token, period_from=period_from, period_to=TO)).json()
    criteria = payload["failure_criteria"]
    assert criteria["window_days"] == 41
    assert criteria["insufficient_window"] is True
    assert criteria["triggered"] is False


async def test_failure_not_triggered_when_nulls(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04.3: `passed: null` — не провал; на пустой базе критерий не срабатывает."""
    _, token = await make_user(session, role="moderator")
    period_from = TO - timedelta(days=60)
    payload = (await _get(client, token, period_from=period_from, period_to=TO)).json()
    criteria = payload["failure_criteria"]
    assert criteria["triggered"] is False
    assert criteria["failed_count"] == 0
    assert criteria["failed_metrics"] == []


# --- AC-01.3/4: валидация окна --------------------------------------------


async def test_invalid_period_when_from_not_before_to(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01.3: `from == to` → `422 invalid_period` (окно нулевой длины)."""
    _, token = await make_user(session, role="moderator")
    response = await _get(client, token, period_from=FROM, period_to=FROM)
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_period"}


async def test_invalid_period_when_from_after_to(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01.3, вторая сторона: перевёрнутое окно — та же ошибка."""
    _, token = await make_user(session, role="moderator")
    response = await _get(client, token, period_from=TO, period_to=FROM)
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_period"}


async def test_period_too_long_over_365_days(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01.4: окно 366 дней → `422 period_too_long`."""
    _, token = await make_user(session, role="moderator")
    response = await _get(client, token, period_from=TO - timedelta(days=366), period_to=TO)
    assert response.status_code == 422
    assert response.json() == {"code": "period_too_long"}


async def test_period_exactly_365_days_is_allowed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01.4, граница: ровно 365 дней — легально (`>` , не `>=`)."""
    _, token = await make_user(session, role="moderator")
    response = await _get(client, token, period_from=TO - timedelta(days=365), period_to=TO)
    assert response.status_code == 200, response.text


# --- гарды ---------------------------------------------------------------


async def test_regular_user_forbidden(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, D-10.5-1: обычный `user` → `403 {"code": "forbidden"}`.

    Тот же конверт, что у `/moderation/cards`: две формы одной ошибки дали бы
    клиенту два разбора.
    """
    _, token = await make_user(session, role="user")
    response = await _get(client, token, period_from=FROM, period_to=TO)
    assert response.status_code == 403
    assert response.json() == {"code": "forbidden"}


async def test_unauthenticated_without_cookie(client: AsyncClient) -> None:
    """AC-01: без сессии — `401` и конверт `unauthenticated`, не `500`."""
    response = await client.get(_URL, params={"from": FROM.isoformat(), "to": TO.isoformat()})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


async def test_openapi_period_params_have_date_time_format() -> None:
    """AC-01.3: `from`/`to` в OpenAPI — `format: date-time` (Task 4.7).

    Без `format` клиент сгенерировал бы `String` и мог передать `2026-05-01`
    (дата без времени), которую сервер отверг бы только в рантайме.
    """
    from app.main import app

    params = app.openapi()["paths"][_URL]["get"]["parameters"]
    by_name = {param["name"]: param for param in params}
    for name in ("from", "to"):
        assert by_name[name]["schema"]["format"] == "date-time", name


# --- P-1: пояс `from`/`to` (AC-01.3, AC-03.3) ------------------------------


async def _raw_get(client: AsyncClient, token: str, *, raw_from: str, raw_to: str) -> Any:
    """Запрос БЕЗ `isoformat()`: пояс задаётся строкой, а не `datetime`.

    `_get` всегда форматирует `datetime`, а naive-значение получается только
    строкой без смещения — иначе тест не может отправить то, что отправит
    внешний клиент по контракту `format: date-time` (P-1).
    """
    return await client.get(
        _URL,
        params={"from": raw_from, "to": raw_to},
        headers=cookie_headers(token),
    )


async def test_naive_from_with_aware_to_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """P-1 (а): naive `from` + aware `to` → `422 invalid_period`, НЕ `500`.

    До фикса падало `TypeError: can't compare offset-naive and offset-aware
    datetimes` на `metrics.py:78`. AC-03.3 запрещает `403`/`409` на этом
    роуте, а `500` — тем более: это не «нет данных», а сбой сервера.
    """
    _, token = await make_user(session, role="moderator")
    response = await _raw_get(
        client,
        token,
        raw_from="2026-05-01T00:00:00",
        raw_to="2026-05-15T00:00:00+00:00",
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"code": "invalid_period"}


async def test_aware_from_with_naive_to_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """P-1 (а, зеркало): aware `from` + naive `to` → та же ошибка.

    Вторая сторона проверяется отдельно: сравнение `period_from >= period_to`
    при naive `to` падает так же, но правка одной стороны оставила бы дыру.
    """
    _, token = await make_user(session, role="moderator")
    response = await _raw_get(
        client,
        token,
        raw_from="2026-05-01T00:00:00+00:00",
        raw_to="2026-05-15T00:00:00",
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"code": "invalid_period"}


async def test_both_naive_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """P-1: оба naive → `422`, а не тихий `200`.

    Тихий `200` здесь опаснее падения: пояс не указан, сервер угадал за клиента,
    и окно `[from, to)` посчиталось в неявном поясе процесса (AC-01.3 — UTC).
    """
    _, token = await make_user(session, role="moderator")
    response = await _raw_get(
        client,
        token,
        raw_from="2026-05-01T00:00:00",
        raw_to="2026-05-15T00:00:00",
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"code": "invalid_period"}


async def test_both_aware_is_200(client: AsyncClient, session: AsyncSession) -> None:
    """P-1, контроль: оба с поясом → `200`. Иначе фикс отбивал бы валидный вход."""
    _, token = await make_user(session, role="moderator")
    response = await _raw_get(
        client,
        token,
        raw_from="2026-05-01T00:00:00+00:00",
        raw_to="2026-05-15T00:00:00+00:00",
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["metrics"]) == 4


async def test_naive_period_with_pilot_started_at_is_422(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-1 (б): naive-запрос при заданном `PILOT_STARTED_AT` → `422`, не `500`.

    Второй воспроизведённый ревьюером путь: `_pilot_day` вычитал aware
    `started_at` из naive `period_to` (`pilot_metrics_service.py:154`).
    Пока `PILOT_STARTED_AT` не задан, эта ветка не исполняется — отсюда
    важность явного случая: после включения настройки в проде любой
    naive-клиент получал бы `500`.
    """
    _, token = await make_user(session, role="moderator")
    monkeypatch.setattr(settings, "pilot_started_at", TO - timedelta(days=14))

    response = await _raw_get(
        client,
        token,
        raw_from="2026-05-01T00:00:00",
        raw_to="2026-05-15T00:00:00",
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"code": "invalid_period"}


async def test_period_with_non_utc_offset_is_converted_to_utc(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01.3: пояс `+07:00` принимается и приводится к UTC.

    Валидатор не должен отбивать валидный `date-time` со смещением: Томск
    живёт в `+07:00`, и клиент оттуда пришлёт именно такое значение.
    Смещение сохраняется в контракте, но окно считается в UTC.
    """
    _, token = await make_user(session, role="moderator")
    response = await _raw_get(
        client,
        token,
        raw_from="2026-05-01T07:00:00+07:00",
        raw_to="2026-05-15T07:00:00+07:00",
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["metrics"]) == 4
