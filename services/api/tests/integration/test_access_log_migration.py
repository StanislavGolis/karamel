"""Цикл миграции `0029` на живой БД (стори 8.6; AC-06, AC-07, AC-10).

Откат нельзя проверять внутри сессионной фикстуры `session`: `alembic
downgrade` выполняет DDL на своём соединении и увидел бы чужой
незакоммиченный слой. Файл работает через `alembic.command` и сам возвращает
схему на `head` в `finally`.

**Почему проверяется ОТСУТСТВИЕ FK.** D-92: с `ON DELETE SET NULL` удаление
чата обнулило бы `chat_uuid`, и запись перестала бы отвечать «к какому
диалогу». Тест смотрит в `pg_constraint` — это единственное место, где
отсутствие FK видно как факт, а не как обещание.
"""

from __future__ import annotations

import asyncio
import os
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


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


async def _scalar(statement: str, params: dict[str, Any] | None = None) -> Any:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), params or {})
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


def _table_exists() -> bool:
    return asyncio.run(_scalar("SELECT to_regclass('public.access_logs')::text")) == "access_logs"


def _index_valid(name: str) -> bool:
    return (
        asyncio.run(
            _scalar(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)",
                {"n": name},
            )
        )
        is True
    )


def _foreign_keys() -> list[str]:
    return (
        asyncio.run(
            _scalar(
                "SELECT coalesce(string_agg(conname, ','), '') FROM pg_constraint "
                "WHERE conrelid = to_regclass('access_logs') AND contype = 'f'"
            )
        ).split(",")
        if asyncio.run(
            _scalar(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = to_regclass('access_logs') AND contype = 'f'"
            )
        )
        else []
    )


def test_upgrade_creates_table_and_indexes() -> None:
    """AC-06: `0029` создаёт таблицу, оба индекса валидны, FK НЕТ (D-92)."""
    config = _config()
    try:
        # Откат → upgrade: иначе на применённой ревизии upgrade = no-op.
        command.downgrade(config, "0028")
        command.upgrade(config, "0029")

        assert _table_exists()
        assert _index_valid("ix_access_logs_created_at")
        assert _index_valid("ix_access_logs_moderator_uuid_created_at")
        # D-92: ни одного FK — иначе ссылки обнулялись бы вместе с объектами.
        assert _foreign_keys() == [], _foreign_keys()
    finally:
        command.upgrade(config, "head")


def test_downgrade_removes_table_and_indexes() -> None:
    """AC-06: откат `0029 → 0028` снимает таблицу и оба индекса атомарно."""
    config = _config()
    try:
        command.upgrade(config, "0029")
        command.downgrade(config, "0028")

        assert not _table_exists()
        assert _index_valid("ix_access_logs_created_at") is False
        assert _index_valid("ix_access_logs_moderator_uuid_created_at") is False
    finally:
        command.upgrade(config, "head")
