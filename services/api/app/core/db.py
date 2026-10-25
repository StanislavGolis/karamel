"""Асинхронный движок SQLAlchemy 2.0 (asyncpg).

Движок создаётся лениво при первом обращении, а не на импорте модуля:
иначе любая смена конфигурации (тесты, повторный запуск) требует перезапуска процесса,
а пул соединений остаётся привязанным к адресу, прочитанному один раз.

Сессии и модели подключаются в стори 1.2 (AR7, AR39).
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Ленивая инициализация движка."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх ленивого движка."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """Task 6.4: зависимость FastAPI — одна сессия на запрос.

    Коммит делает роут, а не эта функция: иначе исключение после `yield`
    не даёт откатить частично записанные данные, и «пользователь без токена»
    остаётся в БД.
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    """Закрывает пул. Вызывается из lifespan при остановке приложения."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
