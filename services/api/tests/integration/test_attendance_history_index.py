"""Индекс истории явок: цикл миграции `0024` и фактический план (стори 7.5; AC-07, AC-12).

**Тесты СИНХРОННЫЕ намеренно** (образец — `test_attendance_migration_cycle.py`):
`alembic/env.py` в async-режиме вызывает `asyncio.run`, а вложенный
`asyncio.run` внутри работающего event-loop падает. Проверки каталога идут
через `asyncio.run` + `asyncpg`.

**Почему план проверяется на НАПОЛНЕННОЙ таблице.** На пустой таблице
планировщик даёт `Seq Scan` независимо от индексов, и проверка
`'Sort' not in plan` была бы ложно-зелёной: узла `Sort` там нет потому, что
сортировать нечего. Поэтому тест вставляет данные, делает `ANALYZE` и только
потом читает план (Task 5.6).

**Почему `enable_seqscan=off`.** Без него планировщик вправе предпочесть
`Seq Scan` на маленькой таблице — это КОРРЕКТНОЕ решение, но тест проверяет
пригодность индекса, а не жадность планировщика. Отключение seqscan
изолирует проверяемое свойство: при наличии подходящего индекса план обязан
быть индексным и БЕЗ `Sort`.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command

pytestmark = [
    pytest.mark.integration,
    # Стори 12.9 (AC-4, W-11.4-d): DDL на общей схеме — несовместим с xdist.
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]

INDEX_NAME = "ix_attendance_participant_created"
KEPT_INDEX_NAME = "ix_attendance_participant_status"


def _alembic_config() -> AlembicConfig:
    """Конфиг как в `conftest.migrated_database`: URL берёт `env.py` из настроек."""
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


async def _run(statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    """Выполняет оператор на коротком соединении и возвращает строки."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params or {})
            return [tuple(row) for row in result.all()]
    finally:
        await engine.dispose()


def _indexdef(name: str) -> str | None:
    """`indexdef` из `pg_indexes` или `None`, если индекса нет."""
    rows = asyncio.run(
        _run(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'attendance' AND indexname = :n",
            {"n": name},
        )
    )
    return rows[0][0] if rows else None


def test_migration_cycle_creates_and_drops_index() -> None:
    """AC-07, AC-12: `upgrade → downgrade -1 → upgrade`, индекс появляется и исчезает.

    `ix_attendance_participant_status` проверяется на ВСЕХ трёх шагах
    (регресс-защита): AC-07 прямо запрещает его удалять, и молчаливое удаление
    уронило бы фильтр по статусу в Seq Scan.
    """
    on_head = _indexdef(INDEX_NAME)
    assert on_head is not None, "индекс 0024 должен существовать на head"
    assert "created_at DESC" in on_head
    assert "card_id DESC" in on_head
    assert _indexdef(KEPT_INDEX_NAME) is not None, "ix_attendance_participant_status удалён!"

    config = _alembic_config()
    # Явная цель `0023`, а не `-1`: с 8.1 head = `0025`, и `-1` снял бы
    # только `0025`. Тест проверяет откат ИМЕННО `0024` — того, что создал
    # индекс, поэтому целимся на ревизию ПЕРЕД ним. Online-режим не
    # принимает диапазон (`0025:0024`) — только целевую ревизию.
    command.downgrade(config, "0023")
    try:
        assert _indexdef(INDEX_NAME) is None, "downgrade 0024 не удалил индекс"
        # AC-07: старый индекс пережил откат — регресс-защита.
        assert _indexdef(KEPT_INDEX_NAME) is not None
    finally:
        command.upgrade(config, "head")

    restored = _indexdef(INDEX_NAME)
    assert restored is not None, "повторный upgrade не вернул индекс"
    assert "created_at DESC" in restored
    assert _indexdef(KEPT_INDEX_NAME) is not None


async def test_history_query_uses_index_without_sort(session: AsyncSession) -> None:
    """AC-07: `EXPLAIN` запроса истории — `Index Scan` без узла `Sort`.

    Проверяется ФАКТИЧЕСКИЙ план (Task 5.6), а не форма индекса: X-7.1-4 был
    гипотезой именно про план, и тест «в индексе правильные колонки» её не
    закрывает.

    **Почему в `session`, а не на коротком соединении.** `SET enable_seqscan`
    живёт в СЕССИИ: на коротком соединении настройка теряется вместе с ним, и
    план читался бы на дефолтном `on` (проверено — `Seq Scan` в плане).

    **Почему 5 000 строк.** Меньше — планировщик выбирает `Seq Scan`
    независимо от индекса (образец `test_moderation_indexes.py`, 10 000 строк).
    Данные пишутся в SAVEPOINT-транзакцию фикстуры и откатываются: база
    остаётся чистой, а guard отката миграции не срабатывает на чужих строках.

    `ANALYZE` обязателен: на пустой статистике план даёт `Seq Scan` даже при
    `enable_seqscan=off` в части случаев — тест был бы ложно-зелёным.
    """
    author_phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
    author_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
                    "age_confirmed, consent_given_at, city_slug) "
                    "VALUES ('phone', :pu, :pu, 'user', 'female', 'active', true, "
                    "now(), 'tomsk') "
                    "RETURNING id"
                ),
                {"pu": author_phone},
            )
        ).scalar_one()
    )
    place_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO places (name, category, address_text, city_slug, is_active) "
                    "VALUES (:n, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
                ),
                {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
            )
        ).scalar_one()
    )
    # 100 карточек и 100 участников → 5 000 строк `attendance`.
    await session.execute(
        text(
            """
            INSERT INTO users (provider, provider_user_id, phone, role, gender, status,
                               age_confirmed, consent_given_at, city_slug)
            SELECT 'phone', '+7904' || lpad(g::text, 7, '0'),
                   '+7904' || lpad(g::text, 7, '0'), 'user',
                   'female', 'active', true, now(), 'tomsk'
              FROM generate_series(1, 100) AS g
            """
        )
    )
    await session.execute(
        text(
            """
            INSERT INTO event_cards (author_id, place_id, city_slug, title, description,
                                     category, starts_at, capacity, decision_window_minutes,
                                     expires_at, status, filled_count, verification_status,
                                     published_at, kind, activity)
            SELECT :a, :p, 'tomsk', 'Массовая встреча ' || g::text, NULL, 'cafe',
                   now() - interval '1 hour', 6, 60, now() - interval '2 hours',
                   'full', 6, 'approved', now() - interval '1 day',
                   'company', 'other'
              FROM generate_series(1, 100) AS g
            """
        ),
        {"a": author_id, "p": place_id},
    )
    await session.execute(
        text(
            """
            INSERT INTO attendance (card_id, participant_id, created_at)
            SELECT c.id, u.id, now() - (random() * interval '30 days')
              FROM event_cards AS c
              JOIN users AS u ON u.provider_user_id LIKE '+7904%'
             WHERE c.title LIKE 'Массовая встреча %'
            """
        )
    )
    await session.execute(text("ANALYZE attendance"))
    await session.execute(text("ANALYZE event_cards"))

    participant_id = int(
        (
            await session.execute(
                text(
                    "SELECT participant_id FROM attendance GROUP BY participant_id "
                    "ORDER BY count(*) DESC LIMIT 1"
                )
            )
        ).scalar_one()
    )

    # `SET LOCAL` — настройка действует до конца транзакции и НЕ течёт в
    # пул после откатов SAVEPOINT-фикстуры (обычный `SET` пережил бы тест).
    await session.execute(text("SET LOCAL enable_seqscan = off"))

    async def _plan() -> str:
        result = await session.execute(
            text(
                """
                EXPLAIN
                SELECT a.created_at, a.card_id, c.uuid, c.title, c.starts_at, a.status
                  FROM attendance AS a
                  JOIN event_cards AS c ON c.id = a.card_id
                 WHERE a.participant_id = :p
                 ORDER BY a.created_at DESC, a.card_id DESC
                 LIMIT 21
                """
            ),
            {"p": participant_id},
        )
        return "\n".join(str(row[0]) for row in result.all())

    with_index = await _plan()
    assert "Sort" not in with_index, with_index
    assert INDEX_NAME in with_index, with_index

    # **Само-доказательство (P-13), а не «зелёное на всякий случай».** Откат
    # миграции здесь недостижим: `conftest` возвращает схему на `head` перед
    # каждым тестом, поэтому `alembic downgrade -1` в отдельном соединении
    # НЕ краснит этот тест (проверено — план оставался индексным). DDL в
    # PostgreSQL транзакционен, поэтому индекс удаляется ВНУТРИ транзакции
    # теста: план обязан стать сортирующим, а после пересоздания — снова
    # индексным. Откат фикстуры возвращает схему, ручных следов не остаётся.
    await session.execute(text(f"DROP INDEX {INDEX_NAME}"))
    without_index = await _plan()
    assert "Sort" in without_index, without_index

    await session.execute(
        text(
            f"CREATE INDEX {INDEX_NAME} ON attendance "
            "(participant_id, created_at DESC, card_id DESC)"
        )
    )
    restored = await _plan()
    assert "Sort" not in restored, restored
    assert INDEX_NAME in restored, restored


def test_downgrade_refuses_offline_mode() -> None:
    """CR 7.5 M-3: откат `0024` в offline-режиме (`--sql`) запрещён.

    В offline нет соединения: проверить размер `attendance` невозможно, а
    `DROP INDEX` на большой таблице блокирует запись. Сгенерировать скрипт без
    guard значило бы отдать оператору защиту ровно там, где её не видно
    (урок CR 7.3).

    Диапазон задан явно (`0024:0023`): в offline-режиме alembic не выводит его
    из вершины, и без диапазона ошибка была бы «Range revision not allowed»,
    то есть тест проверял бы alembic, а не наш guard.

    Мутация, которая краснит: убрать проверку `context.as_sql` — исключения не будет.
    """
    with pytest.raises(RuntimeError, match="offline"):
        command.downgrade(_alembic_config(), "0024:0023", sql=True)


def test_downgrade_guard_stops_above_threshold() -> None:
    """CR 7.5 M-3: guard по размеру таблицы останавливает откат и сохраняет индекс.

    Порог НЕ подменяется: alembic перезагружает модуль ревизии, и правка
    константы в уже импортированном объекте не действует (проверено —
    `DID NOT RAISE`). Поэтому строк наливается на одну больше настоящего
    `DOWNGRADE_GUARD_ROWS` — `generate_series`, один оператор, ~0.3 с.

    Мутация, которая краснит: убрать `raise` (откат пройдёт и индекс исчезнет).
    """
    from alembic.script import ScriptDirectory

    config = _alembic_config()
    threshold = ScriptDirectory.from_config(config).get_revision("0024").module.DOWNGRADE_GUARD_ROWS

    async def _seed() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                author_id = int(
                    (
                        await connection.execute(
                            text(
                                "INSERT INTO users (provider, provider_user_id, phone, role, "
                                "gender, status, age_confirmed, consent_given_at, "
                                "city_slug) "
                                "VALUES ('phone', :p, :p, 'user', 'female', 'active', "
                                "true, now(), 'tomsk') RETURNING id"
                            ),
                            {"p": f"+7998{uuid_lib.uuid4().int % 10**7:07d}"},
                        )
                    ).scalar_one()
                )
                place_id = int(
                    (
                        await connection.execute(
                            text(
                                "INSERT INTO places (name, category, address_text, "
                                "city_slug, is_active) "
                                "VALUES (:n, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
                            ),
                            {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
                        )
                    ).scalar_one()
                )
                card_id = int(
                    (
                        await connection.execute(
                            text(
                                "INSERT INTO event_cards (author_id, place_id, city_slug, "
                                "title, description, category, starts_at, capacity, "
                                "decision_window_minutes, expires_at, status, filled_count, "
                                "verification_status, published_at, kind, activity) "
                                "VALUES (:a, :p, 'tomsk', 'Guard 0024', NULL, 'cafe', "
                                "now() - interval '1 hour', 2, 60, now() - interval '2 hours', "
                                "'full', 2, 'approved', now() - interval '1 day',"
                                "'company', 'other') RETURNING id"
                            ),
                            {"a": author_id, "p": place_id},
                        )
                    ).scalar_one()
                )
                await connection.execute(
                    text(
                        "INSERT INTO users (provider, provider_user_id, phone, role, gender, "
                        "status, age_confirmed, consent_given_at, city_slug) "
                        "SELECT 'phone', '+7996' || lpad(g::text, 7, '0'), "
                        "'+7996' || lpad(g::text, 7, '0'), 'user', 'female', 'active', "
                        "true, now(), 'tomsk' "
                        "FROM generate_series(1, :n) AS g"
                    ),
                    {"n": threshold + 1},
                )
                await connection.execute(
                    text(
                        "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
                        "participant_confirmed, status, confirmed_at) "
                        "SELECT :c, u.id, false, false, 'no_show', now() FROM users AS u "
                        "WHERE u.provider_user_id LIKE '+7996%'"
                    ),
                    {"c": card_id},
                )
        finally:
            await engine.dispose()

    async def _cleanup() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM attendance WHERE participant_id IN "
                        "(SELECT id FROM users WHERE provider_user_id LIKE '+7996%')"
                    )
                )
                await connection.execute(
                    text(
                        "DELETE FROM attendance WHERE card_id IN "
                        "(SELECT id FROM event_cards WHERE title = 'Guard 0024')"
                    )
                )
                await connection.execute(text("DELETE FROM event_cards WHERE title = 'Guard 0024'"))
                await connection.execute(text("DELETE FROM places WHERE name LIKE 'Кафе %'"))
                await connection.execute(
                    text("DELETE FROM users WHERE provider_user_id LIKE '+7996%'")
                )
                await connection.execute(text("DELETE FROM users WHERE phone LIKE '+7998%'"))
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    try:
        with pytest.raises(RuntimeError, match="Откат 0024 остановлен"):
            command.downgrade(config, "0023")
        # Индекс на месте: guard не «откатил наполовину».
        assert _indexdef(INDEX_NAME) is not None, "guard бросил исключение, но индекс удалён"
    finally:
        command.upgrade(config, "head")
        asyncio.run(_cleanup())
