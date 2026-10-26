"""Гонка двойного `POST /me/delete` (§18.3; P-1 из CR 10.2).

**Дефект, который доказывается.** `request_deletion` читал
`user.deletion_requested_at`, проверял `is not None` и только потом писал
`now`. Между проверкой и записью окно открыто: два параллельных запроса
оба видели `NULL`, оба ставили метку, второй ПЕРЕЗАПИСЫВАЛ дату. Следствия:
`409` не отдавался ни одному клиенту, а окно продлевалось — то есть AC-01
(«окно не продлевается») держалось только при последовательных запросах.

**Почему одна сессия не годится (§18.3).** `FOR UPDATE` в единственном
соединении ведёт себя как без него: блокировку никто не держит. Поэтому
фикстура `session` здесь НЕ используется — данные готовятся собственным
движком и коммитятся по-настоящему, а действия идут в РАЗНЫХ сессиях
на разных соединениях.

**Порядок задаётся жёстко, а не планировщиком.** Параллельный `gather`
дал бы исход, зависящий от того, кто успел первым: при «B первым» дефектный
код тоже отдал бы `409`, и тест был бы зелёным на сломанном коде. Поэтому:
1. сессия A захватывает строку `FOR UPDATE` и держит блокировку;
2. сессия B вызывает `request_deletion` и ВСТАЁТ на этой блокировке —
   факт ожидания проверяется через `pg_locks`, а не `sleep`;
3. A записывает метку и коммитит — блокировка снимается;
4. B разблокируется. Без `refresh` в его объекте остался бы снимок `NULL`
   (`expire_on_commit=False`), и он бы метку перезаписал.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.repositories.users import UserRepository
from app.services.account_deletion_service import (
    DeletionAlreadyRequestedError,
    request_deletion,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Коммиты здесь НАСТОЯЩИЕ (§18.3): строки обязаны исчезнуть до старта других тестов.
_CREATED_USERS: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика на собственном движке: коммиты настоящие, соединения разные."""
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Убирает созданные тестом строки: уборка в отдельной сессии."""
    _CREATED_USERS.clear()
    yield
    if not _CREATED_USERS:
        return
    async with factory() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
        )
        await session.commit()


async def _new_user(factory: async_sessionmaker[AsyncSession]) -> int:
    """Учётка без пометки удаления, с настоящим коммитом."""
    async with factory() as session:
        phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(session).create(
            provider="phone",
            provider_user_id=phone,
            phone=phone,
            role="user",
            gender="female",
            status="active",
        )
        await session.commit()
        _CREATED_USERS.append(int(user.id))
        return int(user.id)


async def _wait_for_ungranted_lock(factory: async_sessionmaker[AsyncSession]) -> None:
    """Ждёт, пока появится НЕвыданная блокировка — доказательство ожидания B.

    `sleep` дал бы «скорее всего успел»; `pg_locks` даёт факт: сессия B
    действительно стоит на `FOR UPDATE`, значит порядок шагов доказан,
    а не предположен. Таймаут 5 с — диагностика вместо вечного зависания.
    """
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            waiting = (
                await session.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
            ).scalar_one()
        if int(waiting) > 0:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("сессия B не встала на блокировке строки users")


async def _markup(factory: async_sessionmaker[AsyncSession], user_id: int) -> datetime | None:
    """Пометка из БД — источник правды, не поле объекта."""
    async with factory() as session:
        row = await session.execute(
            text("SELECT deletion_requested_at FROM users WHERE id = :uid"), {"uid": user_id}
        )
        return row.scalar_one()


async def test_double_post_while_first_holds_lock_gives_409(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01, P-1: второй `POST` в окне даёт `409` и НЕ продлевает окно.

    Краснеет на дефектном коде дважды:
    1. без блокировки B не ждёт A и не получает `409` (обе попытки «успешны»);
    2. без `refresh` после блокировки B держит снимок `NULL` и перезаписывает
       дату — итоговая метка отличается от записанной A.

    Проверяется и исключение, и ФАКТ неизменности даты: `409` без второй
    проверки прошёл бы, если бы код отдавал `409`, но всё равно писал метку.
    """
    user_id = await _new_user(factory)
    winner_moment = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    async with factory() as session_a:
        # Шаг 1: A захватывает строку — имитация «первый POST уже внутри».
        await UserRepository(session_a).lock_for_update(user_id)

        async def _second_post() -> Any:
            """Шаг 2: B читает снимок и встаёт на блокировке внутри сервиса."""
            async with factory() as session_b:
                # Чтение ДО разблокировки: в объекте остаётся `deletion_requested_at = NULL`.
                user = await session_b.get(User, user_id)
                assert user is not None
                try:
                    await request_deletion(session_b, user, now=winner_moment)
                except DeletionAlreadyRequestedError as error:
                    return error
                await session_b.commit()
                return None

        second = asyncio.create_task(_second_post())
        try:
            # Шаг 2а: доказательство, что B действительно ждёт на блокировке.
            await _wait_for_ungranted_lock(factory)

            # Шаг 3: A записывает метку и снимает блокировку коммитом.
            await session_a.execute(
                text("UPDATE users SET deletion_requested_at = :ts WHERE id = :uid"),
                {"ts": winner_moment, "uid": user_id},
            )
            await session_a.commit()
        finally:
            outcome = await second

    # Шаг 4: B разблокировался и обязан увидеть ЧУЖУЮ метку.
    assert isinstance(outcome, DeletionAlreadyRequestedError)
    assert outcome.purge_after.replace(microsecond=0) == winner_moment.replace(
        microsecond=0
    ) + timedelta(days=7)
    # Окно не продлено: дата та же, что записала A.
    assert await _markup(factory, user_id) == winner_moment


async def test_sequential_second_post_also_gives_409(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01: последовательный повторный `POST` — тоже `409` (регресс-щит).

    Дополняет гоночный тест: после правки блокировка не должна сломать
    обычный путь «два запроса подряд». Без этого теста ошибка в
    `lock_for_update` (например, блокировка не той строки) прошла бы незамеченной.
    """
    user_id = await _new_user(factory)
    moment = datetime(2026, 2, 1, 9, 30, tzinfo=UTC)

    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        await request_deletion(session, user, now=moment)
        await session.commit()

    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        with pytest.raises(DeletionAlreadyRequestedError):
            await request_deletion(session, user, now=moment)

    assert await _markup(factory, user_id) == moment
