"""Диагностика миграций (стори 6.1; AC-07, долг X-5a.4-2).

`alembic heads` падал `NameError: Can't invoke function 'f'` из-за `op.f(...)`
на уровне модуля `0003_consent_pair_check.py`: прокси `Operations` ещё не
установлен, и импорт цепочки ревизий ломался. `alembic check` при этом был
зелёным — поэтому дефект жил незамеченным и не ловился ни одним тестом.

Тест запускает НАСТОЯЩИЙ `alembic` сабпроцессом: воспроизводится ровно тот
путь, которым пользуется человек и CI, а не повторная сборка окружения
в процессе теста.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL: без него нет окружения миграций"
)
def test_alembic_heads_succeeds() -> None:
    """AC-07: `alembic heads` выходит с кодом 0 и печатает `(head)`.

    Краснеет на module-level `op.f`: `NameError` в stderr и код 1.
    Без этого теста регресс X-5a.4-2 вернулся бы незамеченным — `alembic check`
    остаётся зелёным при сломанном `heads`, и CI этого не видит.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=API_ROOT,
        capture_output=True,
        check=False,
        env={**os.environ},
    )

    assert result.returncode == 0, result.stderr.decode()
    assert b"NameError" not in result.stderr
    assert b"(head)" in result.stdout


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL: `check` ходит в БД"
)
def test_alembic_check_reports_no_pending_operations() -> None:
    """AC-07, AC-09: `alembic check` не видит расхождения моделей и миграций.

    Краснеет, если модель `chats`/`messages` или индексы долгов объявлены
    в модели, но не в миграции (или наоборот) — то есть новая схема
    существует только в коде.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=API_ROOT,
        capture_output=True,
        check=False,
        env={**os.environ},
    )

    assert result.returncode == 0, result.stderr.decode()
    assert b"No new upgrade operations detected" in result.stdout
