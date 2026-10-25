"""Цикл миграции `0031` на живой БД (стори 9.3; Task 1.4, D-9.3-4).

**Почему файл СИНХРОННЫЙ.** `alembic/env.py` в async-режиме вызывает
`asyncio.run`, а вложенный `asyncio.run` внутри работающего event-loop падает
(тот же приём, что в `test_support_tickets_migration_cycle.py`).

**Что доказывает «без остатков».** После `downgrade` до `0030` в БД не должно
остаться ни таблицы `notification_mutes`, ни её ограничений. При этом
`notifications` и `support_tickets` ОБЯЗАНЫ пережить откат: они принадлежат
`0021`/`0030`, и их дроп обошёл бы guard `0021` («непустая таблица
останавливает откат»), молча уничтожив адресные запросы явки.

**Почему `upgrade → downgrade → upgrade`, а не только `downgrade`.**
Односторонняя проверка проходит и у миграции, которая не восстанавливается
после отката: `downgrade` снял таблицу, `upgrade` её не создал — и никто не
заметил бы до деплоя.
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


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


@pytest.fixture(autouse=True)
def _restore_head() -> Iterator[None]:
    """Возвращает схему на `head` после КАЖДОГО теста (в т.ч. упавшего).

    Без этого один упавший тест оставил бы БД на `0030`, и следующий тест
    сессии упал бы на `TRUNCATE notification_mutes` с «relation does not exist» —
    один дефект теста превратился бы в каскад ложных падений.
    """
    yield
    command.upgrade(_config(), "head")


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


def _make_user() -> int:
    """Пользователь в СВОЕЙ транзакции: адресат настройки (FK на `users`)."""

    async def run() -> int:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        "INSERT INTO users (provider, provider_user_id, phone, "
                        "role, gender, status) "
                        "VALUES ('phone', :p, :p, 'user', 'female', 'active') RETURNING id"
                    ),
                    {"p": f"+7914{uuid_lib.uuid4().int % 10**7:07d}"},
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_0031_is_the_single_head() -> None:
    """Ровно один head — `0031`. Развилка head'ов сделала бы `upgrade head`
    недетерминированным, а `down_revision` перепутанным (D-9.3-4)."""
    heads = tuple(ScriptDirectory.from_config(_config()).get_heads())
    assert heads == ("0031",)


def test_0031_continues_0030() -> None:
    """Цепочка `0031 -> 0030`: миграция не ответвляет историю.

    Отдельно от проверки head: при развилке (`0031` и `0030` — оба head)
    `get_heads()` вернул бы кортеж из двух значений и покраснел бы выше,
    но причина была бы не видна.
    """
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision("0031").down_revision == "0030"


def test_downgrade_removes_table_and_constraints() -> None:
    """Task 1.4: после отката от `notification_mutes` не остаётся ничего.

    Проверяются таблица и ОБА ограничения по отдельности: общая проверка
    «чего-то не осталось» не сказала бы, какая часть отката забыта.
    Индексы уходят вместе с таблицей — проверяется `to_regclass` самой таблицы,
    а `DROP TABLE` без `CASCADE` при живом индексе упал бы сам.
    """
    command.downgrade(_config(), "0030")

    assert _regclass("notification_mutes") is None

    constraints = asyncio.run(
        _scalar(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            " ('ck_notification_mutes_type', 'ck_notification_mutes_required', "
            "  'pk_notification_mutes', 'fk_notification_mutes_user_id_users')"
        )
    )
    assert constraints == 0, "остались ограничения удалённой таблицы"


def test_downgrade_does_not_touch_notifications_or_support_tickets() -> None:
    """D-9.3-4: откат `0031` НЕ трогает чужие таблицы.

    Строка в `notifications` кладётся ДО отката: пустая таблица пережила бы
    дроп «незаметно», и тест на пустой таблице ничего не доказывал бы.
    """
    user_id = _make_user()
    asyncio.run(
        _execute(
            "INSERT INTO notifications (user_id, type, payload) "
            "VALUES (:u, 'report.created', CAST('{}' AS jsonb))",
            {"u": user_id},
        )
    )

    command.downgrade(_config(), "0030")

    assert _regclass("notifications") == "notifications", (
        "downgrade 0031 удалил notifications — обход guard 0021"
    )
    assert _regclass("support_tickets") == "support_tickets", (
        "downgrade 0031 удалил support_tickets — он не его субъект"
    )
    rows = asyncio.run(_scalar("SELECT count(*) FROM notifications"))
    assert rows >= 1, "строки notifications уничтожены откатом"

    asyncio.run(_execute("DELETE FROM notifications WHERE user_id = :u", {"u": user_id}))
    asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": user_id}))


def test_full_cycle_restores_table_and_checks() -> None:
    """Task 1.4: `upgrade → downgrade → upgrade` возвращает ВСЁ.

    Откат, который «снимает лишнее» или «не восстанавливает снятое», проходит
    одностороннюю проверку и падает здесь.
    """
    command.downgrade(_config(), "0030")
    command.upgrade(_config(), "head")

    assert _regclass("notification_mutes") == "notification_mutes"

    checks = asyncio.run(
        _scalar(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            " ('ck_notification_mutes_type', 'ck_notification_mutes_required')"
        )
    )
    assert checks == 2

    # Вставка проходит и после цикла: `created_at` заполняется дефолтом.
    user_id = _make_user()
    asyncio.run(
        _execute(
            "INSERT INTO notification_mutes (user_id, type) VALUES (:u, 'report.created')",
            {"u": user_id},
        )
    )
    assert (
        asyncio.run(
            _scalar("SELECT count(*) FROM notification_mutes WHERE user_id = :u", {"u": user_id})
        )
        == 1
    )
    asyncio.run(_execute("DELETE FROM notification_mutes WHERE user_id = :u", {"u": user_id}))
    asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": user_id}))


def test_migration_creates_required_check() -> None:
    """D-9.3-5, M12: `ck_notification_mutes_required` создаётся МИГРАЦИЕЙ.

    **Почему тест пересобирает схему, а не читает каталог.** Проверка
    `pg_constraint` на уже поднятой БД доказывает лишь то, что ограничение
    там ЕСТЬ; она не заметит, если `upgrade()` перестанет его создавать —
    на живой базе оно останется с прошлого прогона, а на новой не появится.

    Откат до `0030` и повторный `upgrade` прогоняют `upgrade()` заново.
    Мутация M12 (убрать `CheckConstraint(required)` из миграции) роняет
    `IntegrityError`-ожидание: вставка `report.resolved` пройдёт.
    """
    from sqlalchemy.exc import IntegrityError

    command.downgrade(_config(), "0030")
    command.upgrade(_config(), "head")

    user_id = _make_user()

    async def _try_insert() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
                    {"u": user_id, "t": "report.resolved"},
                )
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError) as exc:
        asyncio.run(_try_insert())
    assert "ck_notification_mutes_required" in str(exc.value)

    command.downgrade(_config(), "0030")
    asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": user_id}))
