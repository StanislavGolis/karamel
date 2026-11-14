"""Миграция `0039`: маркер `audience_skipped_at` (CR 12.6 H-1/H-3).

Проверяется ФАКТИЧЕСКАЯ схема через `information_schema` и настоящий откат,
а не модель: модель описывает намерение (урок 12.2 — тест на модели зеленел
бы при расхождении с миграцией).

**Двойной цикл обязателен** (урок 12.1): `upgrade head` на голове — no-op,
и тест зеленел бы на схеме, которую миграция не трогала. Поэтому сначала
`downgrade 0038`, затем `upgrade head`, затем снова `downgrade`/`upgrade`:
второй цикл ловит необратимость `downgrade`.

Краснеет при:
- удалении `op.add_column` — колонки не будет в каталоге;
- удалении `op.drop_column` из `downgrade` — откат оставит колонку;
- появлении `server_default` — `NULL` перестал бы быть значением по умолчанию,
  то есть старые карточки получили бы «уже отсеяны» без всякого воркера.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [
    pytest.mark.integration,
    # Стори 13.2 (P-32): файл исполняет `alembic upgrade/downgrade`
    # сабпроцессом и откатывает ОБЩУЮ схему. Маркер обязателен: под xdist
    # хук `tests/integration/conftest.py` превращает его в `skip`, иначе
    # чужой воркер прочитает таблицу, которую мы откатили.
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]


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
        env={**os.environ},
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n{result.stderr}"
    )


async def _column_info() -> list[tuple]:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'event_cards' AND column_name = 'audience_skipped_at'"
                )
            )
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


async def test_migration_0039_adds_and_removes_marker() -> None:
    """`0039` добавляет NULL-able колонку без дефолта; откат её убирает."""
    # Цикл 1: откат на 0038 гарантирует, что `upgrade` реально исполнится.
    _run_alembic("downgrade", "0038")
    assert await _column_info() == [], (
        "предусловие: на 0038 колонки `audience_skipped_at` быть не должно"
    )

    _run_alembic("upgrade", "head")
    columns = await _column_info()
    assert len(columns) == 1, f"0039 обязана добавить ровно одну колонку, получено {columns}"

    name, data_type, is_nullable, default = columns[0]
    assert name == "audience_skipped_at"
    assert data_type == "timestamp with time zone", (
        f"тип обязан быть `timestamptz` (момент метки), получено {data_type}"
    )
    assert is_nullable == "YES", (
        "NULL = «рамки никого не отсекали» — законное значение для всех карточек до 0039"
    )
    assert default is None, (
        "`server_default` означал бы «все старые карточки уже отсеяны» — ложь "
        f"в поле, влияющем на работу воркера, получено {default}"
    )

    # Цикл 2: обратимость. Без него `downgrade`, удаляющий колонку, не проверен.
    _run_alembic("downgrade", "0038")
    assert await _column_info() == [], "`downgrade` обязан удалить колонку"

    _run_alembic("upgrade", "head")
    assert len(await _column_info()) == 1, "повторный `upgrade` обязан вернуть колонку"
