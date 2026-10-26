"""Цикл миграции `0030` на живой БД (стори 9.1; AC-04, D-9.1-2).

**Почему файл СИНХРОННЫЙ.** `alembic/env.py` в async-режиме вызывает
`asyncio.run`, а вложенный `asyncio.run` внутри работающего event-loop падает.
Все обращения к БД идут короткими соединениями в `asyncio.run`, миграции —
через `alembic.command`. Схема возвращается на `head` в `finally` КАЖДОГО
теста: иначе один упавший тест оставил бы БД на `0029`, и следующий тест
сессии упал бы на `TRUNCATE support_tickets` с «relation does not exist» —
то есть один дефект теста превратился бы в каскад ложных падений.

**Что именно доказывает AC-04 «без остатков».** После `downgrade -1` в БД не
должно остаться: таблицы `support_tickets`, её индексов, её ограничений;
у `notifications` — колонки `uuid`, `UNIQUE uuid`, перечня типов и индекса
`user_read`. При этом САМА `notifications` и её строки обязаны остаться
(D-9.1-2): её дроп — предмет отката `0021` с guard'ом на непустую таблицу.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]

_INSERT_USER = (
    "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
    "community_slug) "
    "VALUES ('phone', :p, :p, 'user', 'female', 'active', 'tomsk') RETURNING id"
)

_INSERT_NOTIFICATION = (
    "INSERT INTO notifications (user_id, type, payload) "
    "VALUES (:u, 'attendance.request', CAST('{}' AS jsonb)) RETURNING id"
)


@pytest.fixture(autouse=True)
def _restore_head() -> Iterator[None]:
    """Возвращает схему на `head` после КАЖДОГО теста модуля (в т.ч. упавшего)."""
    yield
    command.upgrade(_config(), "head")


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


async def _scalar(statement: str, params: dict[str, Any] | None = None) -> Any:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement), params or {})).scalar()
    finally:
        await engine.dispose()


async def _execute(statement: str, params: dict[str, Any] | None = None) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), params or {})
    finally:
        await engine.dispose()


def _regclass(name: str) -> str | None:
    return asyncio.run(_scalar(f"SELECT to_regclass('public.{name}')::text"))


def _count_users() -> int:
    """Пользователь в СВОЕЙ транзакции: адресат уведомления (FK на `users`)."""

    async def run() -> int:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(_INSERT_USER),
                    {"p": f"+7912{uuid_lib.uuid4().int % 10**7:07d}"},
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# AC-04: цикл upgrade → downgrade → upgrade
# ---------------------------------------------------------------------------


def test_0033_is_the_single_head() -> None:
    """Ровно один head — `0033` (стори 10.2, индекс удаления). Развилка head'ов сделала бы
    `upgrade head` недетерминированным, а `down_revision` из эпика (`0013`) — неверным (D-9.1-5)."""
    heads = tuple(ScriptDirectory.from_config(_config()).get_heads())
    # Стори 10.2: вершина стала `0033` (частичный индекс `users.deletion_requested_at`).
    assert heads == ("0033",)


def test_downgrade_leaves_no_residue_of_support_tickets() -> None:
    """AC-04: после отката от `support_tickets` не остаётся ничего.

    M11/M12: если `downgrade` не дропает таблицу, роняет первая проверка;
    если не дропает индекс — вторая (индексы уходят вместе с таблицей, но
    проверяется это ОТДЕЛЬНО: `DROP TABLE` без `CASCADE` при живом индексе
    упал бы, и тест поймал бы миграцию, которая «дропает» таблицу, оставляя
    индексы висеть).
    """
    command.downgrade(_config(), "0029")

    assert _regclass("support_tickets") is None
    assert _regclass("ix_support_tickets_queue") is None
    assert _regclass("ix_support_tickets_user_created") is None
    assert _regclass("uq_support_tickets_uuid") is None

    constraints = asyncio.run(
        _scalar("SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck_support_tickets_%'")
    )
    assert constraints == 0, "остались CHECK-ограничения удалённой таблицы"


def test_downgrade_does_not_drop_notifications() -> None:
    """D-9.1-2: откат `0030` НЕ трогает таблицу `notifications`.

    M10: `DROP TABLE notifications` в `downgrade` роняет этот тест. Это не
    стилистика: дроп обошёл бы guard `0021` (непустая таблица останавливает
    откат) и молча уничтожил адресные запросы явки, которые 7.5 использует
    как источник истории.

    Строка кладётся ДО отката: пустая таблица пережила бы дроп «незаметно»,
    и тест на пустой таблице ничего не доказывал бы.
    """
    user_id = _count_users()
    asyncio.run(_execute(_INSERT_NOTIFICATION, {"u": user_id}))

    command.downgrade(_config(), "0029")

    assert _regclass("notifications") == "notifications", (
        "downgrade 0030 удалил notifications — обход guard 0021"
    )
    rows = asyncio.run(_scalar("SELECT count(*) FROM notifications"))
    assert rows >= 1, "строки notifications уничтожены откатом"

    asyncio.run(_execute("DELETE FROM notifications WHERE user_id = :u", {"u": user_id}))
    asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": user_id}))


def test_downgrade_removes_notifications_extensions() -> None:
    """AC-04: надстройки `notifications` (D-9.1-8, D-9.1-3, AC-03) сняты.

    Проверяются ВСЕ три по отдельности: колонка, ограничение, индекс. Общая
    проверка «чего-то не осталось» не сказала бы, какая часть отката забыта.
    """
    command.downgrade(_config(), "0029")

    column = asyncio.run(
        _scalar(
            "SELECT count(*) FROM information_schema.columns "
            " WHERE table_name = 'notifications' AND column_name = 'uuid'"
        )
    )
    assert column == 0, "колонка uuid не снята откатом"

    type_check = asyncio.run(
        _scalar("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_notifications_type'")
    )
    assert type_check == 0, "перечень типов не снят откатом"

    index = _regclass("ix_notifications_user_read")
    assert index is None, "индекс непрочитанных не снят откатом"

    # Форма типа — наследие 0021 и обязана пережить откат 0030.
    format_check = asyncio.run(
        _scalar(
            "SELECT count(*) FROM pg_constraint  WHERE conname = 'ck_notifications_type_format'"
        )
    )
    assert format_check == 1, "откат снял ограничение 0021 — он не его субъект"


def test_full_cycle_restores_all_objects() -> None:
    """AC-04: `upgrade → downgrade → upgrade` возвращает ВСЁ.

    Откат, который «снимает лишнее» или «не восстанавливает снятое», проходит
    одностороннюю проверку и падает здесь. Это единственный тест, который
    доказывает, что `upgrade` идемпотентен после отката.
    """
    command.downgrade(_config(), "0029")
    command.upgrade(_config(), "head")

    assert _regclass("support_tickets") == "support_tickets"
    assert _regclass("ix_support_tickets_queue") is not None
    assert _regclass("ix_support_tickets_user_created") is not None
    assert _regclass("ix_notifications_user_read") is not None

    checks = asyncio.run(
        _scalar(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            " ('ck_support_tickets_status', 'ck_support_tickets_answer_pair', "
            "  'ck_support_tickets_answer_order', 'ck_support_tickets_body_not_blank', "
            "  'ck_support_tickets_id_positive', 'ck_notifications_type')"
        )
    )
    assert checks == 6

    # `uuid` вернулся и снова заполняется дефолтом (D-9.1-8).
    user_id = _count_users()
    asyncio.run(_execute(_INSERT_NOTIFICATION, {"u": user_id}))
    generated = asyncio.run(
        _scalar("SELECT uuid FROM notifications WHERE user_id = :u", {"u": user_id})
    )
    assert generated is not None
    asyncio.run(_execute("DELETE FROM notifications WHERE user_id = :u", {"u": user_id}))
    asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": user_id}))
