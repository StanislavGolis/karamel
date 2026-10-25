"""Фикстуры integration-тестов (Task 6.6).

D-8: схема поднимается **Alembic**, а не `create_all`. `create_all` строит таблицы
из моделей и не проверяет миграцию — баг в миграции прошёл бы мимо тестов.

Без `DATABASE_URL` тесты не «тихо зелёные»: модули помечены skip с причиной.
Изоляция — внешняя транзакция и её откат: каждый тест видит чистую базу,
при этом DDL-миграции выполняются один раз за сессию.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command

API_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("DATABASE_URL")

SKIP_REASON = (
    "integration-тесты требуют реальный PostgreSQL: задайте DATABASE_URL="
    "postgresql+asyncpg://karamel:karamel_pass@127.0.0.1:5432/karamel_db"
)


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """D-8: поднимает схему миграциями Alembic до `head`."""
    if not DATABASE_URL:
        return
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    # URL здесь НЕ задаётся: env.py берёт его из settings.database_url.
    # set_main_option создавал иллюзию настройки — env.py перезаписывал значение.
    command.upgrade(config, "head")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Сессия внутри транзакции, которая откатывается после теста.

    CR 1.5, патч 3: `join_transaction_mode="create_savepoint"`. Раньше коммит
    подменялся на `flush`, и тест не мог отличить «погашено и зафиксировано»
    от «погашено и откатится». Теперь `commit()` действительно фиксирует работу,
    но освобождает SAVEPOINT внутри внешней транзакции — её откат в конце
    по-прежнему изолирует тесты, а утечки строк невозможны.
    """
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as db_session:
            yield db_session
        await transaction.rollback()
    await engine.dispose()


# X-5a.6-3: доменные таблицы, которые чистит сессионная фикстура.
# `alembic_version` не входит: миграции не должны выполняться заново из-за очистки.
_DOMAIN_TABLES = (
    # Стори 6.1: сообщения и чаты. Порядок в `TRUNCATE ... CASCADE` не важен
    # (CASCADE снимает зависимость), но список обязан включать обе таблицы:
    # иначе строки переживают прогон и ломают проверки `count(*)`/состава.
    # Стори 7.3 (D-TD-3): `notifications` и `attendance` перечислены ЯВНО.
    # До этого они чистились только транзитивно, через `TRUNCATE users CASCADE`:
    # изоляция держалась на `ON DELETE CASCADE` у FK и молча ломалась бы при
    # смене на `SET NULL` (разумный вариант для аудита). Тогда второй прогон
    # подряд дал бы другой результат — нарушение P-12 и X-6.7-2.
    "notifications",
    # Стори 7.4 (урок D-TD-3): `reports` перечислен ЯВНО по той же причине, что
    # `notifications`/`attendance` в 7.3. Жалоба ссылается на `attendance` и
    # `users` через `ON DELETE CASCADE`, но изоляция не должна держаться на
    # каскаде: при смене правила ретенции (разумный вариант — `SET NULL` для
    # аудита) строки пережили бы прогон, и второй прогон подряд дал бы другой
    # результат — нарушение P-12 и X-6.7-2.
    # Стори 8.1 (тот же урок D-TD-3): `sanctions` перечислен ЯВНО.
    # Санкция ссылается на `reports`/`users` каскадом, но изоляция не должна
    # держаться на каскаде: при смене правила (`SET NULL` для аудита) строки
    # пережили бы прогон, и второй прогон подряд дал бы другой результат.
    "sanctions",
    "reports",
    "attendance",
    "messages",
    "chats",
    "join_requests",
    "event_cards",
    "profile_photos",
    "profiles",
    "refresh_tokens",
    "places",
    "users",
)


async def _truncate_domain_tables() -> None:
    """Удаляет все строки доменных таблиц настоящим `TRUNCATE ... CASCADE`.

    Гоночные тесты коммитят по-настоящему (§18.3) и убирают за собой
    фикстурами `_cleanup_seed`, но ручной прогон сервиса на живой БД или
    упавший до уборки тест оставляют строки. Глобальные `SELECT count(*)`
    и лента видят их и падают в посторонних тестах — 45 ложных падений (CR 5а.6).
    """
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    try:
        async with engine.begin() as connection:
            # Стори 7.3 (D-TD-3): список фильтруется по фактическому наличию
            # таблиц. Тесты миграций (7.1, 7.3) намеренно снимают схему до
            # предыдущей ревизии; жёсткий `TRUNCATE` по несуществующей таблице
            # падал бы каскадом ложных ошибок по всей сессии.
            present = [
                name
                for name in _DOMAIN_TABLES
                if (
                    await connection.execute(
                        text("SELECT to_regclass(:n)::text"), {"n": f"public.{name}"}
                    )
                ).scalar_one()
                is not None
            ]
            if present:
                await connection.execute(
                    text(f"TRUNCATE {', '.join(present)} RESTART IDENTITY CASCADE")
                )
    finally:
        await engine.dispose()


# ——— Chore P-6: обвязка реального времени (TD 6.5, O2/O5) —————————————
#
# `second_engine`/`second_session` существуют потому, что основная фикстура
# `session` живёт в SAVEPOINT (`join_transaction_mode="create_savepoint"`).
# Проверено экспериментально: `commit()` внутри savepoint НЕ виден из
# другого соединения вообще (0 строк), а реальный `commit()` в отдельной
# сессии — виден (1 строка). Поэтому доказательство «публикация видит
# закоммиченные данные» (TD 6.5, W4) на фикстуре `session` было бы
# тавтологией: она видит незакоммиченное по построению (§18.8).


@pytest.fixture
async def second_engine() -> AsyncIterator[AsyncEngine]:
    """Движок для НЕЗАВИСИМОГО соединения к той же БД.

    **Почему function-scoped, а не session.** pytest-asyncio создаёт новый
    event loop на каждый тест, а `AsyncEngine` запоминает loop, в котором
    открыл первое соединение. Session-scoped движок во втором тесте давал
    `asyncpg.InterfaceError: cannot perform operation: another operation is
    in progress` (проверено: 2 из 3 O2-тестов падали именно так). Тот же
    класс ошибки проект уже проходил на клиенте Redis в 1.4 — там лечится
    сбросом кэша клиента, здесь — областью жизни движка. Цена решения нулевая:
    `poolclass=None`, соединение всё равно открывается на тест.
    """
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def second_session(second_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Реальная сессия с настоящими коммитами (TD 6.5, O2).

    Используется для проверки ВИДИМОСТИ: тест пишет через неё, а код читает
    через своё соединение — или наоборот. Строки, созданные здесь, убирает
    сессионная TRUNCATE-фикстура `clean_database`.
    """
    factory = async_sessionmaker(bind=second_engine, expire_on_commit=False)
    async with factory() as db_session:
        try:
            yield db_session
        finally:
            await db_session.rollback()


@pytest.fixture
async def fresh_redis() -> AsyncIterator[None]:
    """Сбрасывает ленивый клиент Redis до и после теста.

    `get_redis()` кэширует клиент первого event loop; pytest-asyncio создаёт
    новый loop на каждый тест, и второе обращение падает с
    `RuntimeError: Event loop is closed`. Образец — `test_sms_store_redis.py`.
    """
    from app.core import redis as redis_core

    previous = redis_core._client
    redis_core._client = None
    try:
        yield
    finally:
        client = redis_core._client
        redis_core._client = None
        if client is not None and client is not previous:
            await client.aclose()


@pytest.fixture
async def dead_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Подменяет клиент Redis на бросающий `ConnectionError` (TD 6.5, O5).

    Нужен для инварианта «Redis недоступен — чат живёт»: `publish` обязан
    залогировать `ws.publish.failed` и НЕ бросить, а HTTP-ответ остаться
    успешным (ADR-11, R8). Подменяется именно клиент, а не `redis_url`:
    пересоздание клиента в середине теста не гарантирует, что код возьмёт
    новый объект.
    """
    from app.core import redis as redis_core

    class _DeadRedis:
        """Минимальная поверхность клиента, которой пользуется публикация."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def publish(self, channel: str, message: str) -> None:
            self.calls.append(channel)
            raise ConnectionError(f"Redis недоступен (тест): {channel}")

        async def psubscribe(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("Redis недоступен (тест): psubscribe")

        async def aclose(self) -> None:
            return None

    dead = _DeadRedis()
    previous = redis_core._client
    previous_loop = redis_core._client_loop
    monkeypatch.setattr(redis_core, "_client", dead, raising=False)
    monkeypatch.setattr(redis_core, "get_redis", lambda: dead, raising=False)
    # CR 7.3 LOW: привязка к ТЕКУЩЕМУ loop обязательна. `get_redis` пересоздаёт
    # клиент при смене loop (см. его докстринг), и в общем прогоне файла второй
    # тест получает новый loop — подменённый `_client` затирался настоящим
    # клиентом, поэтому `dead.calls` оставался пустым и проверка «попытка
    # публикации была» падала. В одиночном прогоне дефект невидим.
    monkeypatch.setattr(redis_core, "_client_loop", asyncio.get_running_loop(), raising=False)
    try:
        # CR 7.3 LOW: фикстура отдаёт сам объект, а не None. Иначе тест не
        # может отличить «publish упал» от «publish не вызывался вовсе»:
        # оба дают одинаковые счётчики и одинаковые строки в БД.
        yield dead  # type: ignore[misc]
    finally:
        redis_core._client = previous
        redis_core._client_loop = previous_loop


@pytest.fixture
def domain_cleanup() -> Generator[None]:
    """Чистит доменные таблицы ПОСЛЕ теста (CR 6.5, класс X-5a.6-3).

    Нужна тестам, которые пишут РЕАЛЬНЫМИ коммитами в собственном движке
    (WS-тесты 6.5, воркер-тесты): сессионный `clean_database` срабатывает
    только на старте и в конце прогона, поэтому строки переживают тест и
    ломают чужие проверки глобальных `count(*)` в том же файле/прогоне
    (доказано: `assert 14 == 1` в `test_close_due_cards.py`).

    Скоп `function`, а не `session`: цена — один `TRUNCATE` на тест
    (миллисекунды), а изоляция полная.
    """
    yield
    if DATABASE_URL:
        asyncio.run(_truncate_domain_tables())


@pytest.fixture(scope="session", autouse=True)
def clean_database(migrated_database: None) -> Generator[None]:
    """X-5a.6-3: прогон начинается и заканчивается на пустых доменных таблицах.

    Синхронная фикстура намеренно: сессионная async-фикстура привязала бы
    собственный event loop к движку и конфликтовала с `asyncio_mode = "auto"`.
    Цена — один `TRUNCATE` на прогон (миллисекунды), набор не замедляется.
    """
    if DATABASE_URL:
        asyncio.run(_truncate_domain_tables())
    yield
    if DATABASE_URL:
        asyncio.run(_truncate_domain_tables())
