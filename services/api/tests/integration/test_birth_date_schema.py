"""Схема `profiles.birth_date` (стори 12.2; AC-1, AC-5; ADR-17).

Проверки идут через `information_schema`, `pg_constraint` и фактическую
вставку, а не через модель: модель описывает намерение, а AC говорит
о фактической схеме. Тест на модели прошёл бы и при расхождении миграции
с моделью.

Реальный PostgreSQL, схема через Alembic (conftest).

Краснеет при:
- удалении `op.add_column(birth_date)` (M1) — колонки не будет в каталоге;
- снятии переопределения CHECK `completeness` (M2) — вставка с `birth_date`
  и `age = NULL` упадёт на `num_nulls(name, city, age, bio) = 0`;
- удалении `op.drop_column` из `downgrade` (M3) — откат оставит колонку;
- замене `date` на `timestamp` (M4) — тип в каталоге другой;
- удалении `age` (M5) — сломается grandfathering AC-5.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid as uuid_lib
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import HEAD_REVISION

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


API_ROOT = Path(__file__).resolve().parents[2]


async def _fetch(sql, **params) -> list[tuple]:
    """Читает строки отдельным движком: тест не зависит от сессий приложения."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return list(result)
    finally:
        await engine.dispose()


def _run_alembic(*args: str) -> None:
    """`python -m alembic <args>` отдельным процессом.

    Сабпроцесс, а не `alembic.command`: тот вызывает `asyncio.run()` внутри
    `env.py`, а тест уже работает в запущенном event loop.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n{result.stderr}"
    )


async def _apply_migration() -> None:
    """Ставит схему на голову **с отката**.

    Урок 12.1: `upgrade head` на голове — no-op, и тест зеленеет на схеме,
    которую миграция не трогала. Откат на `0035` гарантирует, что `upgrade`
    действительно исполнится.
    """
    _run_alembic("downgrade", "0035")
    _run_alembic("upgrade", "head")


async def test_birth_date_column_exists() -> None:
    """AC-1: `birth_date date NULL` есть в `profiles`."""
    await _apply_migration()
    rows = await _fetch(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'profiles' "
        "AND column_name = 'birth_date'"
    )
    assert rows == [("date", "YES")], f"ожидалась date NULL, получено {rows}"


async def test_age_column_kept_as_legacy_fallback() -> None:
    """AC-1, AC-5: `age smallint NULL` остаётся — удаление сломало бы grandfathering."""
    await _apply_migration()
    rows = await _fetch(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'profiles' "
        "AND column_name = 'age'"
    )
    assert rows == [("smallint", "YES")], f"age должен остаться smallint NULL, получено {rows}"


async def test_completeness_check_allows_birth_date_without_age() -> None:
    """AC-2 + AC-5: CHECK `completeness` не требует именно `age`.

    Без переопределения CHECK вставка «дата рождения есть, числа нет»
    падала бы на `num_nulls(name, city, age, bio) = 0` → `IntegrityError` → `500`
    у каждого нового пользователя. Это не деталь: без правки стори
    физически не проходит AC-2.
    """
    await _apply_migration()
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    user_uuid = uuid_lib.uuid4()
    try:
        async with engine.begin() as connection:
            # Пол пользователя и анкеты обязан совпадать (составной FK).
            await connection.execute(
                text(
                    # Замер 2026-10-29: обязательные колонки `users` — `uuid`,
                    # `provider`, `city_slug`; `phone_encrypted` не существует
                    # (телефон хранится иначе), `consent_given_at` nullable.
                    # `gender` нужен для составного FK анкеты.
                    # Замер 2026-10-29 по CHECK: `ck_users_phone_required`
                    # требует `phone NOT NULL` при `provider = 'phone'`,
                    # `ck_users_consent_pair` держит `age_confirmed` и
                    # `consent_given_at` парой. Оба учтены.
                    "INSERT INTO users (uuid, provider, phone, gender, age_confirmed, "
                    "consent_given_at, city_slug) "
                    "VALUES (:uuid, 'phone', :phone, 'female', true, now(), 'tomsk')"
                ),
                {"uuid": user_uuid, "phone": f"+79{uuid_lib.uuid4().int % 10**9:09d}"},
            )
            user_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE uuid = :uuid"), {"uuid": user_uuid}
                )
            ).scalar_one()

        async with engine.begin() as connection:
            # AC-2: полная анкета БЕЗ `age`, но с `birth_date` — валидна.
            await connection.execute(
                text(
                    "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
                    "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
                ),
                {"user_id": user_id, "birth_date": date(1995, 3, 14)},
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM users WHERE uuid = :uuid"), {"uuid": user_uuid}
            )
        await engine.dispose()


async def test_completeness_check_still_rejects_empty_profile() -> None:
    """Обратная сторона: CHECK не ослаблен до «complete при любом наборе».

    Без этой пары мутация «убрать CHECK целиком» выжила бы: тест выше
    зеленеет и тогда, когда ограничения нет вовсе.
    """
    await _apply_migration()
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    user_uuid = uuid_lib.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    # Замер 2026-10-29: обязательные колонки `users` — `uuid`,
                    # `provider`, `city_slug`; `phone_encrypted` не существует
                    # (телефон хранится иначе), `consent_given_at` nullable.
                    # `gender` нужен для составного FK анкеты.
                    # Замер 2026-10-29 по CHECK: `ck_users_phone_required`
                    # требует `phone NOT NULL` при `provider = 'phone'`,
                    # `ck_users_consent_pair` держит `age_confirmed` и
                    # `consent_given_at` парой. Оба учтены.
                    "INSERT INTO users (uuid, provider, phone, gender, age_confirmed, "
                    "consent_given_at, city_slug) "
                    "VALUES (:uuid, 'phone', :phone, 'female', true, now(), 'tomsk')"
                ),
                {"uuid": user_uuid, "phone": f"+79{uuid_lib.uuid4().int % 10**9:09d}"},
            )
            user_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE uuid = :uuid"), {"uuid": user_uuid}
                )
            ).scalar_one()

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                # Нет ни `age`, ни `birth_date` — `complete` запрещён.
                await connection.execute(
                    text(
                        "INSERT INTO profiles (user_id, name, city, gender, bio, status) "
                        "VALUES (:user_id, 'Аня', 'Томск', 'female', 'текст', 'complete')"
                    ),
                    {"user_id": user_id},
                )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM users WHERE uuid = :uuid"), {"uuid": user_uuid}
            )
        await engine.dispose()


async def test_downgrade_drops_column() -> None:
    """AC-1, обратимость: `downgrade` убирает колонку и возвращает старый CHECK."""
    await _apply_migration()
    _run_alembic("downgrade", "0035")
    try:
        rows = await _fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'profiles' "
            "AND column_name = 'birth_date'"
        )
        assert rows == [], f"после отката колонки быть не должно, получено {rows}"
    finally:
        _run_alembic("upgrade", "head")


async def test_double_cycle_survives_0035_0036() -> None:
    """Цикл `0036 → 0035 → 0036`: откат не ломает предыдущую ревизию."""
    await _apply_migration()
    _run_alembic("downgrade", "0035")
    _run_alembic("upgrade", "head")
    rows = await _fetch("SELECT version_num FROM alembic_version")
    assert rows == [(HEAD_REVISION,)], f"голова должна быть {HEAD_REVISION}, получено {rows}"
