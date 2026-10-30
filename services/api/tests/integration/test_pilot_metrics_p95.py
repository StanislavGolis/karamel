"""p95 метрик пилота и план запроса (стори 10.5, Task 8; AC-06).

Два разных требования, два разных теста:

- **p95 ≤ 500 мс** (AC-06.2) — время СЕРВИСА, не HTTP: HTTP добавляет
  сериализацию, сеть и `no-store`, то есть шум в десятки мс. Мерится
  `build_metrics`, 50 прогонов, `statistics.quantiles(times, n=20)[18]`.
- **План запроса** (AC-06, D-TD-2) — `EXPLAIN (ANALYZE, BUFFERS)` на **10 000**
  карточек, а не на 2 000: прецедент `test_moderation_indexes.py:24-27`
  прямо говорит, что при меньшем объёме планировщик предпочитает `Seq Scan`
  независимо от индекса, и тест проверял бы не индекс, а удачу с оценкой
  стоимости.

**Сид объёма AC-06.1 — ОДНИМ запросом `generate_series`.** 1000 участников,
2000 карточек, 5000 заявок, 5000 строк `attendance`: 13 000 отдельных
`INSERT` заняли бы больше времени, чем сам замер, и p95 мерил бы сеть
тестового драйвера, а не запрос.

**`assert len(times) == 50` обязателен (мутация M10).** Без него «50
прогонов → 1» прошло бы незамеченным: `quantiles` по одному элементу даёт
список, и `[18]` упал бы `IndexError` — то есть красный по причине, не
связанной с бюджетом (P-21). Явная проверка длины делает мутацию честной.
"""

from __future__ import annotations

import os
import statistics
import time
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.pilot_metrics_service import build_metrics

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-06.2: бюджет p95 и число прогонов.
P95_BUDGET_MS = 500.0
RUNS = 50

#: AC-06.1: объём сида — точно, числа из AC.
PARTICIPANTS = 1000
CARDS = 2000
REQUESTS = 5000
ATTENDANCES = 5000

#: D-TD-2: объём для `EXPLAIN` — 10 000, как в прецеденте 4.3.
EXPLAIN_ROWS = 10_000

FROM = datetime(2026, 7, 1, tzinfo=UTC)
TO = datetime(2026, 7, 15, tzinfo=UTC)


async def _seed_users(session: AsyncSession, count: int, *, offset: int = 0) -> None:
    """Пачка пользователей одним запросом."""
    await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, role, status, gender, "
            "age_confirmed, consent_given_at, city_slug) "
            "SELECT 'phone', '+7908' || lpad((g + :off)::text, 7, '0'), "
            "       '+7908' || lpad((g + :off)::text, 7, '0'), "
            "       'user', 'active', 'female', true, now(), :slug "
            "  FROM generate_series(1, :count) AS g"
        ),
        {"off": offset, "count": count, "slug": settings.default_city_slug},
    )


async def _seed_cards(
    session: AsyncSession,
    count: int,
    *,
    author_id: int,
    place_id: int,
    filled: int = 1,
    spread_hours: int = 0,
) -> list[int]:
    """Карточки одним запросом; статус согласован с занятостью (CHECK `full_capacity`).

    `CAST(:created AS timestamptz)` обязателен: без приведения PostgreSQL выводит тип
    параметра как `unknown` и в `unknown + interval` выбирает `interval +
    interval`, после чего падает `column starts_at is of type timestamp with
    time zone but expression is of type interval` — красный тест не по делу (P-21).
    Форма `CAST(...)`, а не `::timestamptz`: SQLAlchemy разбирает
    `:param::type` как ДВА параметра и получает `syntax error at or
    near ":"` — проверено исполнением.

    `spread_hours` разносит `created_at` по времени. Для замера p95 разброс не
    нужен (мерится объём), а для `EXPLAIN` он ОБЯЗАТЕЛЕН: если все 10 000
    карточек лежат ВНУТРИ окна, планировщик честно выбирает `Seq Scan` —
    прочитать всю таблицу дешевле, чем ходить по индексу за каждой строкой.
    Тест на «индекс не используется» в такой постановке требовал бы
    невозможного и был бы отключён (дефект D-TD-2 в другой формулировке).
    """
    created = FROM + timedelta(days=2)
    status = "full" if filled >= 2 else "published"
    rows = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, created_at, "
            "kind, activity) "
            "SELECT :a, :p, :slug, 'Встреча ' || g, NULL, 'cafe',"
            "       CAST(:created AS timestamptz) + (g * :step) * interval '1 hour' + interval '2 "
            " hours',"
            "       2, 60,"
            "       CAST(:created AS timestamptz) + (g * :step) * interval '1 hour' + interval '1 "
            " hour',"
            "       :status, :filled, 'approved',"
            "       CAST(:created AS timestamptz) + (g * :step) * interval '1 hour' - interval '1 "
            " day',"
            "       CAST(:created AS timestamptz) + (g * :step) * interval '1 hour', "
            "       'company', 'coffee' "
            "  FROM generate_series(1, :count) AS g "
            "RETURNING id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "slug": settings.default_city_slug,
            "status": status,
            "filled": filled,
            "created": created,
            "count": count,
            "step": spread_hours,
        },
    )
    return [int(row[0]) for row in rows.all()]


async def _seed_volume(session: AsyncSession) -> None:
    """Сид объёма AC-06.1 через `generate_series`.

    Порядок обязателен: `places` → `users` → карточки → заявки → `attendance`.
    Каждая таблица ссылается на предыдущую через FK, и обратный порядок дал бы
    `ForeignKeyViolation` в фикстуре (P-21: красный не по тому `assert`).
    """
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_id = int(place.scalar_one())

    await _seed_users(session, PARTICIPANTS)
    participants = (
        (
            await session.execute(
                text("SELECT id FROM users ORDER BY id LIMIT :n"), {"n": PARTICIPANTS}
            )
        )
        .scalars()
        .all()
    )

    cards = await _seed_cards(
        session, CARDS, author_id=int(participants[0]), place_id=place_id, filled=1
    )

    # 5000 заявок РАЗНЫМИ заявителями: частичный `UNIQUE (card_id, applicant_id)
    # WHERE status IN ('pending','selected')` отверг бы 5000 заявок от одного
    # человека на 2000 карточек (IntegrityError в фикстуре — P-21).
    # `::bigint[]` — обязательное приведение: asyncpg не выводит тип массива
    # из параметра и отвечает `cannot subscript type unknown`.
    await session.execute(
        text(
            "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
            "submitted_at, expires_at) "
            "SELECT gen_random_uuid(), c, p, 'Хочу присоединиться', 'pending', "
            "       now(), now() + interval '2 days' "
            "  FROM generate_series(1, :count) AS g "
            "  JOIN LATERAL (SELECT"
            "    (CAST(:cards AS bigint[]))[1 + (g % array_length(CAST(:cards AS bigint[]), 1))] "
            " AS c,"
            "    (CAST(:people AS bigint[]))[1 + (g % array_length(CAST(:people AS bigint[]), "
            " 1))] AS p) x"
            "    ON true"
        ),
        {
            "count": REQUESTS,
            "cards": cards,
            "people": [int(p) for p in participants[1:500]],
        },
    )

    # 5000 строк attendance: статус `confirmed` требует ОБА флага true.
    await session.execute(
        text(
            "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
            "participant_confirmed, status, confirmed_at) "
            "SELECT c, p, true, true, 'confirmed', now() "
            "  FROM generate_series(1, :count) AS g "
            "  JOIN LATERAL (SELECT"
            "    (CAST(:cards AS bigint[]))[1 + (g % array_length(CAST(:cards AS bigint[]), 1))] "
            " AS c,"
            "    (CAST(:people AS bigint[]))[1 + (g % array_length(CAST(:people AS bigint[]), "
            " 1))] AS p) x"
            "    ON true"
            " ON CONFLICT (card_id, participant_id) DO NOTHING"
        ),
        {
            "count": ATTENDANCES,
            "cards": cards,
            "people": [int(p) for p in participants[1:200]],
        },
    )


async def test_p95_service_time_within_budget(session: AsyncSession) -> None:
    """AC-06.2: p95 времени `build_metrics` ≤ 500 мс на объёме AC-06.1.

    Мерится СЕРВИС, а не HTTP (D-10.5-7): сеть и сериализация добавили бы
    десятки мс шума, и «500 мс» перестало бы относиться к запросам.

    Первый прогон отбрасывается как прогрев: он включает открытие соединения
    и разбор планов, которых в рабочем цикле нет.
    """
    await _seed_volume(session)

    # `ANALYZE` ОБЯЗАТЕЛЕН после массовой вставки: 13 000 строк, вставленных
    # одним `generate_series`, не попадают в статистику планировщика, и он
    # оценивает таблицу по прежним данным. Замер без `ANALYZE` мерил бы не
    # запрос, а ошибку оценки: воспроизведено — p95 = 1594.7 мс в общем
    # прогоне против 32.9 мс изолированно, при неизменном коде.
    await session.execute(text("ANALYZE event_cards"))
    await session.execute(text("ANALYZE attendance"))

    # Прогрев: без него первый прогон мерил бы установку соединения.
    await build_metrics(session, period_from=FROM, period_to=TO)

    times: list[float] = []
    for _ in range(RUNS):
        started = time.perf_counter()
        await build_metrics(session, period_from=FROM, period_to=TO)
        times.append((time.perf_counter() - started) * 1000)

    # M10: «50 прогонов → 1» обязано покраснеть здесь, а не `IndexError` ниже.
    assert len(times) == RUNS, f"прогонов {len(times)}, ожидалось {RUNS}"

    p95 = statistics.quantiles(times, n=20)[18]
    assert p95 <= P95_BUDGET_MS, (
        f"p95 = {p95:.1f} мс > {P95_BUDGET_MS} мс (прогонов {len(times)}); "
        f"нужен индекс из миграции 0034 (Task 8.3)"
    )
    print(f"p95 = {p95:.1f} мс, медиана = {statistics.median(times):.1f} мс, прогонов {len(times)}")


async def test_explain_is_consistent_with_p95_budget(session: AsyncSession) -> None:
    """AC-06.3/4, D-TD-2: план и бюджет p95 СОГЛАСОВАНЫ на 10 000 карточках.

    **Почему не «нет `Seq Scan` во что бы то ни стало».** Требование AC-06
    условно: индекс добавляется, ЕСЛИ p95 > 500 мс (AC-06.3), и НЕ добавляется,
    если p95 укладывается в бюджет (AC-06.4). При окне в 14 дней из 10 000
    карточек планировщик отбирает ~1% таблицы, и `Seq Scan` объективно дешевле
    индекса — требовать `Index Scan` здесь значило бы требовать от планировщика
    работать против собственной оценки. Такой тест красный навсегда и будет
    отключён, то есть перестанет проверять что-либо (дефект D-TD-2 в
    изначальной формулировке Task 8.5).

    Проверяется ИНВАРИАНТ: медленный план допустим только при соблюдённом
    бюджете. Если p95 выйдет за 500 мс и плана с индексом нет — тест
    покраснеет, и это будет настоящий сигнал «нужна миграция 0034» (Task 8.3).

    `EXPLAIN (ANALYZE, BUFFERS)` на 10 000 — прецедент
    `test_moderation_indexes.py:24-27`: при меньшем объёме планировщик
    предпочитает `Seq Scan` независимо от индекса, и тест проверял бы не план,
    а удачу с оценкой стоимости.
    """
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
    )
    place_id = int(place.scalar_one())
    await _seed_users(session, 10)
    author_id = int(
        (await session.execute(text("SELECT id FROM users ORDER BY id LIMIT 1"))).scalar_one()
    )
    # Разброс 2 часа × 10 000 карточек ≈ 2.3 года: окно метрик (14 дней)
    # содержит ~1% таблицы. Без разброса все строки лежали бы в окне, и
    # `Seq Scan` был бы объективно оптимален — тест требовал бы невозможного.
    await _seed_cards(
        session, EXPLAIN_ROWS, author_id=author_id, place_id=place_id, filled=1, spread_hours=2
    )
    await session.execute(text("ANALYZE event_cards"))

    plan = "\n".join(
        str(row[0])
        for row in (
            await session.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS) "
                    "SELECT count(*) FILTER (WHERE filled_count >= 1), NULLIF(count(*), 0) "
                    "  FROM event_cards "
                    " WHERE city_slug = :slug "
                    "   AND created_at >= :f AND created_at < :t "
                    "   AND status IN ('published','full','closed','finished')"
                ),
                {"slug": settings.default_city_slug, "f": FROM, "t": TO},
            )
        ).all()
    )

    # Бюджет мерится на ТОМ ЖЕ объёме, что и план: иначе решение «Seq Scan
    # допустим» опиралось бы на замер при другом количестве строк.
    times: list[float] = []
    for _ in range(10):
        started = time.perf_counter()
        await build_metrics(session, period_from=FROM, period_to=TO)
        times.append((time.perf_counter() - started) * 1000)
    p95 = statistics.quantiles(times, n=20)[18]

    if "Seq Scan on event_cards" in plan:
        assert p95 <= P95_BUDGET_MS, (
            f"Seq Scan при p95 = {p95:.1f} мс > {P95_BUDGET_MS} мс: нужен индекс "
            f"`ix_event_cards_metrics (city_slug, created_at, status)` "
            f"миграцией 0034 (AC-06.3, Task 8.3). План:\n{plan}"
        )
    else:
        assert "Index Scan" in plan or "Bitmap Index Scan" in plan, (
            f"план без индекса, но и не Seq Scan — разбор требует внимания:\n{plan}"
        )
        assert p95 <= P95_BUDGET_MS, f"p95 = {p95:.1f} мс > {P95_BUDGET_MS} мс:\n{plan}"
