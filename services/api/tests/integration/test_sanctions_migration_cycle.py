"""Миграция `0025` на живой БД (стори 8.1; AC-09, AC-10).

Файл СИНХРОННЫЙ: `alembic/env.py` в async-режиме вызывает `asyncio.run`, а
вложенный `asyncio.run` внутри работающего event-loop падает. Приём тот же,
что в `test_reports_migration_cycle.py`.

**Каждая вставка — в СВОЕЙ транзакции.** `IntegrityError`/`RuntimeError`
абортит транзакцию, и любой `DELETE` в её `finally` упал бы с «current
transaction is aborted».

**Схема возвращается на `head` после КАЖДОГО теста.** Тесты намеренно
снимают `0025` (guard отката). Падение в середине оставило бы БД на `0024`,
и следующий тест сессии упал бы на `TRUNCATE sanctions` с «relation does not
exist» — один дефект теста превратился бы в каскад ложных падений.

Покрытие: `S01` (цепочка и head), `S02` (guard непустой `sanctions`),
`S03` (отсутствие остатков после отката), `S04` (повторный upgrade).
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.conftest import HEAD_REVISION

pytestmark = [
    pytest.mark.integration,
    # Стори 12.9 (AC-4, W-11.4-d): DDL на общей схеме — несовместим с xdist.
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


API_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_head() -> Any:
    """Возвращает схему на `head` после каждого теста модуля.

    Тесты намеренно снимают `0025` (guard отката). Падение в середине
    оставило бы БД на `0024`, и следующий тест сессии упал бы на
    `TRUNCATE sanctions` с «relation does not exist» — один дефект теста
    превратился бы в каскад ложных падений.
    """
    yield
    command.upgrade(_config(), "head")


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


def _down_revision_of(revision_id: str) -> str | None:
    script = ScriptDirectory.from_config(_config())
    revision = script.get_revision(revision_id)
    return revision.down_revision  # type: ignore[return-value]


def _current() -> str:
    async def _go() -> str:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                return str(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _regclass(name: str) -> str | None:
    async def _go() -> str | None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT to_regclass(:n)::text"), {"n": f"public.{name}"}
                )
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _attendance_nullable() -> str | None:
    async def _go() -> str | None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'reports' AND column_name = 'attendance_id'"
                    )
                )
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _seed_sanction() -> dict[str, Any]:
    """Жалоба + санкция по ней. Возвращает id ВСЕЙ цепочки.

    Нужны пользователи, карточка и пара `attendance` — `reports` требует
    валидной цели по `target_scope`.

    Возвращаются все id, а не только `sanctions.id`: `reports`, `attendance`,
    `event_cards`, `places` и `users` держат guard'ы и FK следующих ревизий.
    Остаток любой из них роняет ЧУЖИЕ тесты — `0023` на непустом `reports`,
    `places` на `RESTRICT` из `fk_event_cards_place_id_places`.
    """

    async def _go() -> dict[str, Any]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:

                async def _user() -> int:
                    phone = f"+7904{uuid_lib.uuid4().int % 10**7:07d}"
                    row = await connection.execute(
                        text(
                            "INSERT INTO users (uuid, provider, provider_user_id, phone, role, "
                            "status, age_confirmed, consent_given_at, city_slug) "
                            "VALUES (gen_random_uuid(), 'phone', :p, :p, 'user', 'active', "
                            "true, now(), 'tomsk') RETURNING id"
                        ),
                        {"p": phone},
                    )
                    return int(row.scalar_one())

                author = await _user()
                participant = await _user()
                reporter = await _user()

                place = await connection.execute(
                    text(
                        "INSERT INTO places (name, category, address_text, city_slug, "
                        "is_active) VALUES (:n, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
                    ),
                    {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
                )
                place_id = int(place.scalar_one())

                now = datetime.now(UTC)
                card = await connection.execute(
                    text(
                        "INSERT INTO event_cards (author_id, place_id, city_slug, title, "
                        "description, category, starts_at, capacity, decision_window_minutes, "
                        "expires_at, status, filled_count, verification_status, published_at,"
                        "kind, activity) "
                        "VALUES (:a, :p, 'tomsk', 'Встреча', NULL, 'cafe', :s, 4, 2880, :e, "
                        "'published', 0, 'approved', :pub, 'company', 'other') RETURNING id"
                    ),
                    {
                        "a": author,
                        "p": place_id,
                        "s": now + timedelta(days=3),
                        "e": now + timedelta(days=1),
                        "pub": now,
                    },
                )
                card_id = int(card.scalar_one())

                att = await connection.execute(
                    text(
                        "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
                        "participant_confirmed, status, confirmed_at) "
                        "VALUES (:c, :p, false, false, 'no_show', now()) RETURNING id"
                    ),
                    {"c": card_id, "p": participant},
                )
                attendance_id = int(att.scalar_one())

                report = await connection.execute(
                    text(
                        "INSERT INTO reports (reporter_id, attendance_id, event_card_id, "
                        "target_type, status) "
                        "VALUES (:r, :a, :c, 'no_show', 'open') RETURNING id"
                    ),
                    {"r": reporter, "a": attendance_id, "c": card_id},
                )
                report_id = int(report.scalar_one())

                sanction = await connection.execute(
                    text(
                        "INSERT INTO sanctions (user_id, report_id, type, reason) "
                        "VALUES (:u, :r, 'warning', 'причина') RETURNING id"
                    ),
                    {"u": participant, "r": report_id},
                )
                return {
                    "sanction": int(sanction.scalar_one()),
                    "report": report_id,
                    "attendance": attendance_id,
                    "card": card_id,
                    "place": place_id,
                    "users": [author, participant, reporter],
                }
        finally:
            await engine.dispose()

    return asyncio.run(_go())


# --- S01: цепочка -------------------------------------------------------------


def test_down_revision_chain() -> None:
    """AC-12: `0025` продолжает `0024`, а `0024` — `0023`."""
    assert _down_revision_of("0025") == "0024"
    assert _down_revision_of("0024") == "0023"
    # Стори 9.1: вершина — `0030` (уведомления и обращения). Проверяется «БД на вершине», а не номер
    # миграции 8.1: цепочка `0025 -> 0024 -> 0023` выше от вершины не зависит.
    assert _current() == HEAD_REVISION


# --- S02: guard непустой `sanctions` -----------------------------------------


def test_downgrade_blocked_by_sanction_rows() -> None:
    """AC-10: непустая `sanctions` останавливает откат.

    Санкции — это данные. `DROP TABLE` уничтожил бы их молча, поэтому
    guard обязан упасть с внятным сообщением, а не выполнить откат.
    """
    seeded = _seed_sanction()

    # `finally`: строки переживают падение ассерта и уронили бы чужие откаты
    # своими guard'ами (`0025` на `sanctions`, `0023` на `reports`).
    try:
        with pytest.raises(RuntimeError) as exc:
            # Явная цель, а не `-1`: с 9.1 head = `0030`, и `-1` снял бы ЕГО.
            # Тест проверяет откат ИМЕННО 0025 (sanctions).
            command.downgrade(_config(), "0024")

        assert "sanctions" in str(exc.value)
        # Схема не тронута: guard падает ДО `DROP TABLE`. Alembic откатывает
        # ВСЮ транзакцию, поэтому вершина возвращается — остаётся `0027`
        # (8.3), а не `0025`. Это и есть доказательство атомарности: откат
        # ВСЕХ ревизий 0025..0029 отменён целиком, а не частично (0030 не затронут).
        assert _current() == HEAD_REVISION
        assert _regclass("sanctions") == "sanctions"
    finally:
        _cleanup_sanctions(seeded)


def _column_exists(table: str, column: str) -> bool:
    """CR 10.1, P-11: есть ли колонка в схеме (после отката 0032)."""

    async def _go() -> bool:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            " WHERE table_name = :t AND column_name = :c"
                        ),
                        {"t": table, "c": column},
                    )
                ).first()
                return row is not None
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _cleanup_sanctions(seeded: dict[str, Any]) -> None:
    """Удаляет санкцию и ВСЮ её цепочку в СВОЕЙ транзакции.

    Порядок обратен вставке: `sanctions` → `reports` → `attendance` →
    `event_cards` → `places` → `users`. Иначе `fk_event_cards_place_id_places`
    (`RESTRICT`) не даст удалить место, а остаток `reports` включит guard
    `0023` в следующем тесте.
    """

    async def _go() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM sanctions WHERE id = :i"),
                    {"i": seeded["sanction"]},
                )
                await connection.execute(
                    text("DELETE FROM reports WHERE id = :i"), {"i": seeded["report"]}
                )
                await connection.execute(
                    text("DELETE FROM attendance WHERE id = :i"),
                    {"i": seeded["attendance"]},
                )
                await connection.execute(
                    text("DELETE FROM event_cards WHERE id = :i"), {"i": seeded["card"]}
                )
                await connection.execute(
                    text("DELETE FROM places WHERE id = :i"), {"i": seeded["place"]}
                )
                await connection.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"),
                    {"ids": seeded["users"]},
                )
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _count_sanctions() -> int:
    """Число строк в `sanctions` в СВОЕЙ транзакции (для предусловий)."""

    async def _go() -> int:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text("SELECT count(*) FROM sanctions"))
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(_go())


# --- S03, S04: откат без остатков и повторный upgrade ------------------------


def test_downgrade_leaves_no_leftovers_then_upgrade() -> None:
    """AC-10: после отката нет ни одного объекта 8.1; повторный upgrade — на месте."""
    # Предусловие: guard 0025 не должен сработать на чужих остатках. Без этого
    # тест падал бы по причине соседнего теста, а не по своему предмету.
    assert _count_sanctions() == 0

    # Явная цель, а не `-1`: с 9.1 head = `0030`, и `-1` снял бы ЕГО.
    # Тест проверяет откат ИМЕННО 0025 (sanctions).
    command.downgrade(_config(), "0024")

    assert _current() == "0024"
    # CR 10.1, P-11, AC-03: `downgrade 0032` вернул колонки лайков. Стори 5.3
    # требовала этого ассерта явно; до правки тест проходил через откат 0032,
    # но не проверял, что тот действительно что-то восстановил.
    assert _column_exists("users", "likes_used_today")
    assert _column_exists("users", "likes_counted_on")
    assert _regclass("sanctions") is None
    assert _regclass("uq_attendance_id_card_id") is None
    assert _regclass("uq_reports_chat_id_reporter_id") is None
    # `attendance_id` возвращается в `NOT NULL`: иначе `message`-жалобы
    # остались бы легальными в схеме без `target_scope`.
    assert _attendance_nullable() == "NO"

    command.upgrade(_config(), "head")

    # `head` после 9.1 — `0030` (уведомления и обращения). Сравнение с вершиной,
    # а не с номером 8.1: тест проверяет, что `upgrade` вернул ВСЁ снятое,
    # включая `0026`…`0029`.
    assert _current() == HEAD_REVISION
    assert _regclass("sanctions") == "sanctions"
    assert _regclass("uq_attendance_id_card_id") is not None
    assert _attendance_nullable() == "YES"
