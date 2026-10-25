"""Окружение Alembic для «Карамели».

Правила (стори 1.2, Task 1.3–1.4):
- URL БД берётся из `settings.database_url` (env `DATABASE_URL`), а не из `alembic.ini`:
  статический URL в ini = пароль в git.
- `target_metadata` — `Base.metadata` из `app.models`; autogenerate видит только те модели,
  что реэкспортированы в `app/models/__init__.py`.
- online-режим асинхронный (asyncpg): движок создаётся `async_engine_from_config`,
  миграции выполняются через `connection.run_sync`.
- `compare_type` и `compare_server_default` включены: без них расхождения типов
  и серверных дефолтов autogenerate не замечает.
- offline-режим (`alembic upgrade head --sql`) сохранён: он не требует драйвера БД.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# AC-01/AC-04: схема описывается моделями, а не руками в env.py.
target_metadata = Base.metadata

# URL НЕ кладётся в ConfigParser: знак `%` в пароле ломает подстановку,
# а одиночное экранирование превращает `pa%ss` в невалидный URL при разборе.
# Движок создаётся напрямую из settings — конфиг остаётся свободным от секретов.


def run_migrations_offline() -> None:
    """Offline-режим: генерирует SQL без подключения к БД (`--sql`)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online-режим: async-движок, синхронный callback через run_sync."""
    connectable = create_async_engine(settings.database_url, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
