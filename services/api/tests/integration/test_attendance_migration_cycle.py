"""Цикл миграции `0020` на живой БД (стори 7.1; AC-07; CR 7.1 C2 и patch).

Откат нельзя проверять внутри сессионной фикстуры `session`: она держит одну
транзакцию, а `alembic downgrade` выполняет DDL на своём соединении и увидел бы
чужой незакоммиченный слой. Поэтому файл работает напрямую через
`alembic.command` и сам возвращает схему на `head` в `finally`.

Тесты СИНХРОННЫЕ намеренно: `alembic/env.py` в async-режиме вызывает
`asyncio.run`, а вложенный `asyncio.run` внутри работающего event-loop падает.
Проверки каталога выполняются через `asyncio.run` + `asyncpg` (единственный
установленный драйвер; `psycopg` в окружении отсутствует).

§12.3: откат обязан возвращать ровно предыдущее состояние и не оставлять
остатков. §18.11 / P-11: «guard срабатывает» и «пустая таблица откатывается» —
два значения одного признака, проверяются оба.
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
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> AlembicConfig:
    """Конфиг как в `conftest.migrated_database`: URL берёт `env.py` из настроек."""
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


async def _scalar(statement: str, params: dict[str, Any] | None = None) -> Any:
    """Один скаляр из живой БД (короткое соединение)."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), params or {})
            return result.scalar_one()
    finally:
        await engine.dispose()


def _attendance_regclass() -> str | None:
    return asyncio.run(_scalar("SELECT to_regclass('public.attendance')::text"))


def _count(statement: str, params: dict[str, Any] | None = None) -> int:
    return int(asyncio.run(_scalar(statement, params)))


def _execute(statement: str) -> None:
    """Выполняет оператор, не возвращающий строк (DELETE без RETURNING)."""

    async def _run() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _constraint_names() -> set[str]:
    async def _fetch() -> set[str]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'attendance'::regclass AND contype IN ('p','u','c','f')"
                    )
                )
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(_fetch())


async def _seed_attendance_row() -> str:
    """Вставляет один факт явки и КОММИТИТ — иначе downgrade его не увидит.

    Возвращает метку `suffix`, по которой потом чистятся посеянные строки.
    """
    suffix = uuid_lib.uuid4().hex[:8]
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            place_id = (
                await connection.execute(
                    text(
                        "INSERT INTO places (name, category, address_text, "
                        "community_slug, is_active) "
                        "VALUES (:name, 'cafe', 'addr', 'tomsk', true) RETURNING id"
                    ),
                    {"name": f"CR-cycle-{suffix}"},
                )
            ).scalar_one()
            author_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (uuid, provider, provider_user_id, phone, role, status, "
                        "age_confirmed, consent_given_at) "
                        "VALUES (gen_random_uuid(), 'phone', :pid, :phone, 'user', 'active', "
                        "true, now()) RETURNING id"
                    ),
                    {
                        "pid": f"author-{suffix}",
                        "phone": f"+7911{uuid_lib.uuid4().int % 10**7:07d}",
                    },
                )
            ).scalar_one()
            participant_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (uuid, provider, provider_user_id, phone, role, status, "
                        "age_confirmed, consent_given_at) "
                        "VALUES (gen_random_uuid(), 'phone', :pid, :phone, 'user', 'active', "
                        "true, now()) RETURNING id"
                    ),
                    {
                        "pid": f"part-{suffix}",
                        "phone": f"+7912{uuid_lib.uuid4().int % 10**7:07d}",
                    },
                )
            ).scalar_one()
            card_id = (
                await connection.execute(
                    text(
                        "INSERT INTO event_cards (author_id, place_id, community_slug, title, "
                        "category, starts_at, capacity, decision_window_minutes, expires_at, "
                        "status, filled_count, verification_status, published_at) "
                        "VALUES (:author, :place, 'tomsk', :title, 'cafe', "
                        "now() + interval '3 days', 4, 2880, "
                        "now() + interval '1 day', 'published', 1, 'approved', now()) "
                        "RETURNING id"
                    ),
                    {"author": author_id, "place": place_id, "title": f"CR-cycle-{suffix}"},
                )
            ).scalar_one()
            await connection.execute(
                text("INSERT INTO attendance (card_id, participant_id) VALUES (:card, :p)"),
                {"card": card_id, "p": participant_id},
            )
    finally:
        await engine.dispose()
    return suffix


def _seed() -> str:
    return asyncio.run(_seed_attendance_row())


async def _delete_seeded(suffix: str) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM event_cards WHERE title = :title"),
                {"title": f"CR-cycle-{suffix}"},
            )
            await connection.execute(
                text("DELETE FROM users WHERE provider_user_id = :a OR provider_user_id = :p"),
                {"a": f"author-{suffix}", "p": f"part-{suffix}"},
            )
            await connection.execute(
                text("DELETE FROM places WHERE name = :name"),
                {"name": f"CR-cycle-{suffix}"},
            )
    finally:
        await engine.dispose()


def test_downgrade_refuses_when_attendance_has_facts() -> None:
    """AC-07, CR 7.1 (C2): непустая `attendance` останавливает откат.

    Ранее `downgrade` молча удалял таблицу с фактами: `DROP TABLE` не смотрит
    на FK, направленные НАРУЖУ, поэтому docstring «упадёт на FK/данных» был
    ложным. Проверено в CR: 1 факт → `alembic downgrade -1` прошёл без ошибки,
    данные уничтожены. Здесь guard обязан выбросить `RuntimeError`, а схема —
    остаться на месте.
    """
    config = _alembic_config()
    suffix = ""
    try:
        command.upgrade(config, "head")
        suffix = _seed()

        with pytest.raises(Exception) as excinfo:
            command.downgrade(config, "0019")

        assert "фактов явки" in str(excinfo.value)
        # P-11: вторая половина — схема НЕ пострадала.
        assert _attendance_regclass() == "attendance"
        assert _count("SELECT count(*) FROM attendance") >= 1
    finally:
        if suffix:
            asyncio.run(_delete_seeded(suffix))
        command.upgrade(config, "head")


def test_downgrade_and_upgrade_leave_no_residue() -> None:
    """AC-07: на пустой таблице откат чист, повторный накат возвращает всё.

    Проверяются оба направления (§12.3): после `downgrade` таблицы и её
    объектов нет (`to_regclass` = NULL, пустые `pg_constraint`/`pg_indexes`),
    после `upgrade` — все объекты на месте, включая `ck_attendance_id_positive`
    (CR 7.1, D2) и `ck_users_id_positive`.
    """
    config = _alembic_config()
    expected = {
        "pk_attendance",
        "uq_attendance_card_id_participant_id",
        "ck_attendance_status",
        "ck_attendance_status_consistency",
        "ck_attendance_confirmed_at_consistency",
        "ck_attendance_id_positive",
        "fk_attendance_card_id_event_cards",
        "fk_attendance_participant_id_users",
        # Стори 8.1 (AC-04, D-14): цель составного FK из `reports`.
        "uq_attendance_id_card_id",
    }
    try:
        command.upgrade(config, "head")
        _execute("DELETE FROM attendance")

        command.downgrade(config, "0019")

        assert _attendance_regclass() is None
        assert (
            _count(
                "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck_attendance_%' "
                "OR conname LIKE 'fk_attendance_%' OR conname = 'pk_attendance' "
                "OR conname = 'uq_attendance_card_id_participant_id'"
            )
            == 0
        )
        assert (
            _count(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname = 'ix_attendance_participant_status'"
            )
            == 0
        )
        assert (
            _count(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'users'::regclass AND conname = 'ck_users_id_positive'"
            )
            == 0
        )

        command.upgrade(config, "head")

        assert _attendance_regclass() == "attendance"
        assert _constraint_names() == expected
        assert (
            _count(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'users'::regclass AND conname = 'ck_users_id_positive'"
            )
            == 1
        )
    finally:
        command.upgrade(config, "head")


def test_offline_sql_generation_does_not_crash() -> None:
    """AC-08, CR 7.1 (patch): `alembic upgrade --sql` строит скрипт.

    До правки предусловие `op.get_bind().scalar_one()` в offline-режиме падало
    `AttributeError: 'NoneType' object has no attribute 'scalar_one'`: у
    offline-контекста нет соединения. Это ломало генерацию статического SQL
    для ВСЕЙ цепочки (offline-режим включён в `alembic/env.py`), то есть путь
    DBA-ревью и деплоя без прямого доступа к базе.

    Теперь предусловие ветвится по `context.as_sql` и в offline-режиме
    печатает предупреждение. Краснеет при возврате к безусловному
    `op.get_bind()`.
    """
    import io
    from contextlib import redirect_stdout

    from alembic import command as alembic_command

    config = _alembic_config()
    buffer = io.StringIO()

    # `--sql` работает без соединения: `env.py` уходит в run_migrations_offline.
    with redirect_stdout(buffer):
        alembic_command.upgrade(config, "0019:0020", sql=True)

    generated = buffer.getvalue()
    assert "CREATE TABLE attendance" in generated
    assert "ck_attendance_id_positive" in generated
    assert "ck_users_id_positive" in generated
    assert "WARNING: offline-режим" in generated
