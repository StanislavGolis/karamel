"""Безопасность отката `0036` на РЕАЛЬНЫХ данных (CR 12.2, находка H-1).

**Что нашёл CR.** Замер на живой БД:

    rows_before=1
    ALTER TABLE ... ADD CONSTRAINT ck_profiles_completeness
      CHECK (status <> 'complete' OR num_nulls(name, city, age, bio) = 0)
    ERROR: check constraint "ck_profiles_completeness" is violated by some row

`downgrade()` возвращает старый CHECK, который требует **именно `age`**.
Любая анкета, заполненная после 12.2 (`birth_date` есть, `age = NULL`),
делает откат невыполнимым: `alembic downgrade` падает, схема остаётся
на `0036`, и вернуться на предыдущую версию продукта нельзя.

Это класс «миграция обратима по схеме, но не по данным»: `upgrade`
проходит всегда, `downgrade` — только на пустой базе.

**Решение.** `downgrade()` **вычисляет** `age` из `birth_date` ДО возврата
старого CHECK. Это законно именно в эту сторону: из даты число лет
восстанавливается однозначно (на дату отката), обратное — нет (ADR-17).
Строки с `birth_date` получают `age`, `birth_date` затем удаляется.

Краснеет при:
- удалении `UPDATE ... SET age = ...` из `downgrade` (M16) — откат падает;
- вычислении возраста неверной формулой (M17) — тест сверяет число;
- возврате старого CHECK раньше заполнения `age` (M18) — падение сохраняется.
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
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n{result.stderr}"
    )


def _birth_date_for_age(years: int) -> date:
    """Дата рождения, дающая ровно `years` полных лет на сегодня."""
    today = date.today()
    return date(today.year - years, today.month, today.day)


@pytest.fixture
async def _engine_with_profile():
    """Анкета «нового типа»: `birth_date` есть, `age` — `NULL`.

    Именно такое состояние создаёт 12.2 у каждого нового пользователя.
    Без него тест отката проходил бы на пустой базе и не доказывал ничего.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    user_uuid = uuid_lib.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
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
        await connection.execute(
            text(
                "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
                "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
            ),
            {"user_id": user_id, "birth_date": _birth_date_for_age(30)},
        )
    try:
        yield user_uuid
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM users WHERE uuid = :uuid"), {"uuid": user_uuid}
            )
        await engine.dispose()


async def _age_of(user_uuid) -> int | None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = await connection.execute(
                text(
                    "SELECT p.age FROM profiles p JOIN users u ON u.id = p.user_id "
                    "WHERE u.uuid = :uuid"
                ),
                {"uuid": user_uuid},
            )
            return row.scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_succeeds_on_new_style_profiles(_engine_with_profile) -> None:
    """H-1: откат `0036` проходит при наличии анкет с `birth_date` и `age = NULL`.

    До правки: `ERROR: check constraint ... is violated by some row`.
    Продукт нельзя откатить — это блокер выпуска, а не косметика.
    """
    try:
        _run_alembic("downgrade", "0035")
    finally:
        _run_alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_downgrade_fills_age_from_birth_date(_engine_with_profile) -> None:
    """H-1: перед возвратом старого CHECK `age` заполняется вычисленным.

    Сверяется ЧИСЛО, а не факт `NOT NULL`: иначе мутация «записать константу 18»
    выжила бы (18 — тоже не NULL).
    """
    _run_alembic("downgrade", "0035")
    try:
        age = await _age_of(_engine_with_profile)
    finally:
        _run_alembic("upgrade", "head")
    assert age == 30, f"ожидалось 30 (дата рождения 30 лет назад), получено {age}"
