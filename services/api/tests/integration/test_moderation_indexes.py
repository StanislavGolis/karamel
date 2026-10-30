"""Индексы очередей модерации и их план запроса (стори 4.3, Task 10; AC-09).

Проверяется НАБЛЮДАЕМОЕ: план запроса очереди на реальном объёме. Без индекса
`(created_at, uuid) WHERE ...` PostgreSQL делает `Seq Scan` + `Sort`, и keyset-
пагинация теряет строки между страницами (D-4.3-8).

§18.1: тест краснеет, если откатить `0009` (`alembic downgrade -1`): появляются
`Seq Scan` и `Sort`. Проверено исполнением — снимки плана в Dev Agent Record стори.

10 000 строк на таблицу — компромисс: при меньшем объёме планировщик
предпочитает `Seq Scan` независимо от индекса, и тест проверял бы не индекс,
а удачу с оценкой стоимости.
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

ROWS = 10_000


async def _plan(session: AsyncSession, statement: str) -> str:
    """`EXPLAIN (ANALYZE, BUFFERS)` одним текстом — разбирается подстрокой."""
    result = await session.execute(text(statement))
    return "\n".join(str(row[0]) for row in result.all())


async def test_card_queue_uses_index_without_sort(session: AsyncSession) -> None:
    """AC-09, D-4.3-8: очередь карточек — `Index Scan` без `Sort`.

    Краснеет, если откатить `0009` (индекс без `uuid`): при равных `created_at`
    PostgreSQL сортирует результат, и keyset-пагинация теряет строки.
    """
    # Готовим автора и место: FK обязательны.
    author_id = (
        await session.execute(
            text(
                "INSERT INTO users (provider, provider_user_id, phone, role, status, gender, "
                "age_confirmed, consent_given_at, city_slug) "
                "VALUES ('phone', :pid, :pid, 'user', 'active', 'male', true, "
                "now(), 'tomsk') RETURNING id"
            ),
            # CHECK `ck_users_phone_required`: `phone` обязателен для провайдера
            # `phone` — без него вставка падает `IntegrityError`.
            {"pid": f"+7907{uuid_lib.uuid4().int % 10**7:07d}"},
        )
    ).scalar_one()
    place_id = (
        await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, city_slug, is_active) "
                "VALUES (:name, 'cafe', 'пр. Ленина, 1', 'tomsk', true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
        )
    ).scalar_one()

    # 10 000 `pending` карточек одной вставкой: `generate_series` быстрее цикла.
    await session.execute(
        text(
            "INSERT INTO event_cards (uuid, author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, "
            "verification_status, filled_count, created_at, kind, activity) "
            "SELECT gen_random_uuid(), :author, :place, 'tomsk', 'Карточка ' || g, 'cafe', "
            "now() + interval '3 days', 2, 2880, now() + interval '1 day', 'draft', "
            "'pending', 0, now() - (g || ' seconds')::interval, "
            "'company', 'other' "
            "FROM generate_series(1, :rows) AS g"
        ),
        {"author": author_id, "place": place_id, "rows": ROWS},
    )
    await session.execute(text("ANALYZE event_cards"))

    plan = await _plan(
        session,
        "EXPLAIN (ANALYZE, BUFFERS) "
        "SELECT uuid FROM event_cards "
        "WHERE verification_status = 'pending' "
        "ORDER BY created_at, uuid LIMIT 21",
    )

    assert "ix_event_cards_pending_verification" in plan, plan
    assert "Sort" not in plan, plan


async def test_photo_queue_uses_index_without_sort(session: AsyncSession) -> None:
    """AC-09, Х-01: очередь фото — `Index Scan` без отдельного `Sort`.

    Краснеет, если откатить `0009`: `idx_profile_photos_status_pending`
    покрывает только фильтр, и порядок `created_at, id` требует сортировки.

    **Почему 10 000 ПРОФИЛЕЙ:** `uq_profile_photos_profile_id_position` разрешает
    одно фото на позицию. Чтобы получить 10 000 строк очереди, нужно 10 000
    разных анкет — иначе вставка падает `UniqueViolationError`. Три запроса
    через `generate_series` дешевле 10 000 циклов.
    """
    # 1. Владельцы. `phone` обязателен (CHECK `ck_users_phone_required`),
    #    маркер `+7990` отделяет эти строки от других тестов внутри транзакции.
    await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, role, status, gender, "
            "age_confirmed, consent_given_at, city_slug) "
            "SELECT 'phone', '+7990' || lpad(g::text, 7, '0'), '+7990' || lpad(g::text, 7, '0'), "
            "'user', 'active', 'male', true, now(), 'tomsk' "
            "FROM generate_series(1, :rows) AS g"
        ),
        {"rows": ROWS},
    )

    # 2. Анкеты: `complete` требует все четыре поля (CHECK `completeness`).
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, bio, gender, status) "
            "SELECT u.id, 'Анна', 'Томск', 29, 'Люблю кино', 'male', 'complete' "
            "FROM users u WHERE u.phone LIKE '+7990%'"
        )
    )

    # 3. Фото: позиция 1 у каждого профиля — уникальность соблюдена.
    await session.execute(
        text(
            "INSERT INTO profile_photos (uuid, profile_id, storage_key, content_type, "
            "size_bytes, consent_given_at, status, position, created_at) "
            "SELECT gen_random_uuid(), p.id, 'obj/o-' || p.id, 'image/jpeg', 1024, now(), "
            "'pending', 1, now() - (p.id % :rows || ' seconds')::interval "
            "FROM profiles p JOIN users u ON u.id = p.user_id WHERE u.phone LIKE '+7990%'"
        ),
        {"rows": ROWS},
    )
    await session.execute(text("ANALYZE profile_photos"))

    plan = await _plan(
        session,
        "EXPLAIN (ANALYZE, BUFFERS) "
        "SELECT id FROM profile_photos "
        "WHERE status = 'pending' "
        "ORDER BY created_at, id LIMIT 21",
    )

    assert "idx_profile_photos_created_at_id" in plan, plan
    assert "Sort" not in plan, plan


async def test_keyset_pagination_covers_all_rows_with_equal_timestamps(
    session: AsyncSession,
) -> None:
    """AC-03, AC-08, D-4.3-8: keyset по `(created_at, uuid)` не теряет строки.

    Равные `created_at` — ровно тот случай, ради которого в индексе и в
    сортировке появился `uuid`: при `ORDER BY created_at` порядок среди
    равных не определён, и `WHERE (created_at, uuid) >` пропускал или
    дублировал строки между страницами.

    Краснеет, если убрать `EventCard.uuid` из `order_by`: тест собирает ВСЕ
    страницы по курсору и сравнивает с полным набором.
    """
    from app.core.config import settings as global_settings
    from app.services.moderation_service import list_moderation_queue

    author_id = (
        await session.execute(
            text(
                "INSERT INTO users (provider, provider_user_id, phone, role, status, gender, "
                "age_confirmed, consent_given_at, city_slug) "
                "VALUES ('phone', :pid, :pid, 'user', 'active', 'male', true, "
                "now(), 'tomsk') RETURNING id"
            ),
            {"pid": f"+7991{uuid_lib.uuid4().int % 10**7:07d}"},
        )
    ).scalar_one()
    place_id = (
        await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, city_slug, is_active) "
                "VALUES (:name, 'cafe', 'пр. Ленина, 1', 'tomsk', true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
        )
    ).scalar_one()

    # ОДИН И ТОТ ЖЕ `created_at` у всех строк: ключ `uuid` обязателен.
    # Именно `datetime`, а не строка: asyncpg для `timestamptz` требует объект
    # и отвергает строку (`DBAPIError`), даже если PostgreSQL её разобрал бы.
    from datetime import UTC, datetime

    fixed_created = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    await session.execute(
        text(
            "INSERT INTO event_cards (uuid, author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, "
            "verification_status, filled_count, created_at, kind, activity) "
            "SELECT gen_random_uuid(), :author, :place, 'tomsk', 'Карточка ' || g, 'cafe', "
            "now() + interval '3 days', 2, 2880, now() + interval '1 day', 'draft', "
            "'pending', 0, :created, "
            "'company', 'other' "
            "FROM generate_series(1, 7) AS g"
        ),
        {"author": author_id, "place": place_id, "created": fixed_created},
    )

    # Сравнение в `str`: из БД приходит `UUID`, а курсор-путь отдаёт `str(uuid)`.
    # Приведение на границе теста, а не в сервисе: контракт API — строка UUID.
    expected = {
        str(row[0])
        for row in (
            await session.execute(
                text(
                    "SELECT uuid FROM event_cards WHERE verification_status = 'pending' "
                    "AND created_at = :created"
                ),
                {"created": fixed_created},
            )
        ).all()
    }
    assert len(expected) == 7

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # заведомо больше числа страниц
        page = await list_moderation_queue(
            session,
            status="all",
            cursor=cursor,
            limit=2,
            settings=global_settings,
        )
        seen.extend(str(entry.card.uuid) for entry in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    # Ни одна строка не потеряна и не повторена.
    assert len(seen) == len(set(seen))
    assert set(seen) == expected
