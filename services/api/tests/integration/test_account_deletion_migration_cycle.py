"""Цикл миграции `0033` на живой БД (стори 10.2; P-3 из CR 10.2, P-13 из DS).

**Зачем файл.** До CR 10.2 цикл `upgrade → downgrade → upgrade` для `0033`
проверялся ТОЛЬКО руками через `pg_indexes` и в тестах закреплён не был.
У `0029`–`0031` такие цикл-тесты есть, `0033` была единственной без них —
то есть `downgrade`, забывший `DROP INDEX`, прошёл бы приёмку.

**Почему файл СИНХРОННЫЙ.** `alembic/env.py` в async-режиме вызывает
`asyncio.run`, а вложенный `asyncio.run` внутри работающего event-loop падает.
Все обращения к БД идут короткими соединениями в `asyncio.run`, миграции —
через `alembic.command`. Схема возвращается на `head` в `finally` КАЖДОГО
теста: иначе упавший тест оставил бы БД на `0032`, и следующий тест сессии
упал бы на `TRUNCATE users` с «relation does not exist».

**Что именно доказывается (AC-02, D-10.2-2).**
- индекс существует после `upgrade` и он ЧАСТИЧНЫЙ (иначе воркер теряет
  селективность, а `alembic check` предложит пересоздать индекс);
- после `downgrade -1` индекса нет НИ В КАКОМ виде — повторный `upgrade`
  падал бы на «relation already exists», если бы `downgrade` его не снял;
- после повторного `upgrade` индекс снова на месте.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
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

#: Имя индекса — полное, в naming convention индексы не участвуют (прецедент 0009).
_INDEX = "idx_users_deletion_requested_at"


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


@pytest.fixture(autouse=True)
def _restore_head() -> Iterator[None]:
    """Возвращает схему на `head` после КАЖДОГО теста модуля (в т.ч. упавшего)."""
    yield
    command.upgrade(_config(), "head")


def _indexdef(name: str) -> str | None:
    """`pg_indexes.indexdef` — единственный способ увидеть ЧАСТИЧНОСТЬ индекса.

    `to_regclass` отвечает «есть/нет», но не отличает полный индекс от
    частичного. Мутация «убрать `postgresql_where`» прошла бы на `to_regclass`.
    """

    async def _go() -> str | None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": name}
                )
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _revision() -> Any:
    async def _go() -> Any:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                return result.scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# S01–S02: голова и цепочка
# ---------------------------------------------------------------------------


def test_0033_is_single_head_and_its_down_revision_is_0032() -> None:
    """S01, S02: `0033` — единственная голова, её родитель — `0032`.

    Развилка голов сделала бы `upgrade head` недетерминированным; неверный
    `down_revision` откатил бы не ту ревизию, и цикл ниже проверял бы
    чужую миграцию.
    """
    script = ScriptDirectory.from_config(_config())
    assert tuple(script.get_heads()) == (HEAD_REVISION,)
    revision = script.get_revision("0033")
    assert revision.down_revision == "0032"


# ---------------------------------------------------------------------------
# S03: индекс после наката
# ---------------------------------------------------------------------------


def test_index_exists_and_is_partial() -> None:
    """S03, AC-02: индекс на месте и ЧАСТИЧНЫЙ по `deletion_requested_at`.

    Частичность — не украшение: `NULL` — состояние почти всех строк, и
    полный индекс был бы вдвое дороже при той же селективности. Проверяется
    текстом `indexdef`, потому что `to_regclass` частичность не видит.
    """
    assert _revision() == HEAD_REVISION
    definition = _indexdef(_INDEX)
    assert definition is not None, "индекс не создан миграцией 0033"
    assert "deletion_requested_at" in definition
    assert "WHERE" in definition.upper()
    assert "IS NOT NULL" in definition.upper()


# ---------------------------------------------------------------------------
# S04–S05: цикл upgrade → downgrade → upgrade
# ---------------------------------------------------------------------------


def test_downgrade_removes_index_and_upgrade_restores_it() -> None:
    """S04, S05 (P-3): откат снимает индекс, повторный накат создаёт заново.

    Проверяется ЦИКЛ, а не один откат: остаток виден только на повторном
    `upgrade`. Мутация «`downgrade` без `drop_index`» делает второй накат
    падающим на «relation already exists» — именно этого не хватало до
    CR 10.2, когда цикл проверялся руками.
    """
    command.downgrade(_config(), "0032")
    assert _revision() == "0032"
    # Главное: индекса нет. Иначе повторный `CREATE` упал бы.
    assert _indexdef(_INDEX) is None

    command.upgrade(_config(), "head")

    assert _revision() == HEAD_REVISION
    restored = _indexdef(_INDEX)
    assert restored is not None
    assert "WHERE" in restored.upper()


def test_concurrently_is_used_in_upgrade() -> None:
    """P-11 (CR 10.2): индекс строится `CONCURRENTLY`, а не под `SHARE`-блокировкой.

    Проверяется ТЕКСТ миграции, а не поведение: `CONCURRENTLY` внутри
    транзакции ревизии невозможен, поэтому одного факта «индекс создан» мало
    — он создаётся и без `CONCURRENTLY`. Красным станет, если кто-то вернёт
    `op.create_index`: на живом стенде это `SHARE`-блокировка на `users`
    на всё время построения, то есть остановка регистрации.
    """
    source = (API_ROOT / "alembic" / "versions").glob("0033_*.py")
    files = list(source)
    assert len(files) == 1, f"ожидался один файл 0033_*, найдено {len(files)}"
    body = files[0].read_text(encoding="utf-8")
    # Комментарии тоже содержат слово — проверяется код, а не докстринг.
    code_lines = [
        line
        for line in body.splitlines()
        if not line.lstrip().startswith("#") and not line.lstrip().startswith('"""')
    ]
    assert "CONCURRENTLY" in "\n".join(code_lines)
    assert "autocommit_block" in "\n".join(code_lines)
    # `op.create_index` берёт `SHARE` и несовместим с `CONCURRENTLY`.
    assert "op.create_index(" not in "\n".join(code_lines)
