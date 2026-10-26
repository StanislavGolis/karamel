"""Контроль зависших окон решения (стори 10.7, Task 6.3/6.4; AC-03, NFR64).

**Что проверяется.** Предикат ADR-14 дословно: `status='published' AND
expires_at < now() - interval '5 minutes'`. Три утверждения:

1. окно, зависшее на 10 минут, считается (`1`);
2. окно, истёкшее минуту назад, НЕ считается (`0`) — это разница между
   «контроль» и «обычная работа воркера», и без второй проверки первая была
   бы тавтологией «считаем всё, что истекло»;
3. карточка в статусе `closed` с истёкшим окном не считается — статус
   входит в предикат, иначе контроль срабатывал бы на здоровой истории.

**Почему integration, а не unit.** Предикат — SQL с интервалом времени.
Проверка «интервал 5 минут» на фейковой сессии проверяла бы фейк, а не
PostgreSQL: граница `now() - interval '5 minutes'` считается базой.

**Дедуп (Task 6.4).** Второе утверждение — два прогона контроля при том же
зависшем окне дают ДВА лога `worker.window.overdue`, но ОДИН POST: лог
нужен каждый прогон (иначе не видно, что инцидент продолжается), сообщение
— раз в час (иначе бот замьючен, D-10.7-6).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.alert_service as alert_module
import app.workers.card_windows as worker_module
from app.core.config import settings
from app.models.enums import EventCardStatus
from app.repositories.event_cards import EventCardRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика на собственном движке: коммиты настоящие, соединения разные."""
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Убирает созданные строки: глобальные `count(*)` в других тестах."""
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    async with factory() as session:
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM event_cards WHERE author_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
        if _CREATED_PLACES:
            await session.execute(
                text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
            )
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
            )
        await session.commit()


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, expires_minutes: int, status: str
) -> int:
    """Создаёт пользователя, место и карточку. Возвращает `id` карточки.

    Прямой SQL, а не сервис: проверяется SQL-предикат контроля, и путь через
    сервис добавил бы к нему правила публикации, которых тест не касается.
    """
    async with factory() as session:
        phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
        user_row = await session.execute(
            text(
                "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
                "community_slug) VALUES ('phone', :pid, :phone, 'user', 'female', 'active', :slug) "
                "RETURNING id"
            ),
            {"pid": phone, "phone": phone, "slug": settings.default_community_slug},
        )
        author_id = int(user_row.scalar_one())
        _CREATED_USERS.append(author_id)

        place_row = await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug, is_active) "
                "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_community_slug},
        )
        place_id = int(place_row.scalar_one())
        _CREATED_PLACES.append(place_id)

        now = datetime.now(UTC)
        card_row = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
                "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
                "filled_count, verification_status, published_at) "
                "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
                ":expires, :status, 0, 'approved', :published_at) RETURNING id"
            ),
            {
                "author": author_id,
                "place": place_id,
                "slug": settings.default_community_slug,
                "starts": now + timedelta(days=3),
                "expires": now + timedelta(minutes=expires_minutes),
                "status": status,
                "published_at": now + timedelta(minutes=expires_minutes - 60),
            },
        )
        card_id = int(card_row.scalar_one())
        await session.commit()
        return card_id


async def test_window_overdue_by_ten_minutes_is_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03: `expires_at = now()-10 мин` при `published` → `1`.

    Запас 5 минут рассчитан на цикл воркера 60 с: 10 минут — гарантированно
    инцидент, а не дрожание расписания.
    """
    await _seed(factory, expires_minutes=-10, status=EventCardStatus.PUBLISHED.value)

    async with factory() as session:
        count = await EventCardRepository(session).count_overdue_windows(now=datetime.now(UTC))

    assert count == 1


async def test_window_expired_one_minute_ago_is_not_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03: `expires_at = now()-1 мин` → `0`: это работа воркера, не инцидент.

    Без этой границы контроль поднимал бы алерт на каждом прогоне: окно
    истекает за секунды до того, как воркер его закроет (NFR20).
    """
    await _seed(factory, expires_minutes=-1, status=EventCardStatus.PUBLISHED.value)

    async with factory() as session:
        count = await EventCardRepository(session).count_overdue_windows(now=datetime.now(UTC))

    assert count == 0


async def test_closed_card_with_expired_window_is_not_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03: статус входит в предикат — история не считается зависшей.

    Без `status='published'` контроль срабатывал бы на НОРМАЛЬНОЙ истории:
    каждая закрытая карточка имеет `expires_at` в прошлом.
    """
    await _seed(factory, expires_minutes=-10, status=EventCardStatus.CLOSED.value)

    async with factory() as session:
        count = await EventCardRepository(session).count_overdue_windows(now=datetime.now(UTC))

    assert count == 0


class _FakeRedis:
    """Минимум Redis: `SET NX EX` (дедуп)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


async def test_two_runs_log_twice_but_post_alert_once(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6.4 (D-10.7-6): два прогона → два лога, один POST.

    Лог каждый прогон: по нему видно, что инцидент ПРОДОЛЖАЕТСЯ. Сообщение
    раз в час: без дедупа зависшее окно слало бы 1440 сообщений в сутки, и
    бот был бы замьючен — канал исчез бы ровно тогда, когда нужен.
    """
    await _seed(factory, expires_minutes=-10, status=EventCardStatus.PUBLISHED.value)

    redis = _FakeRedis()
    posts: list[str] = []
    logs: list[str] = []

    monkeypatch.setattr(worker_module, "get_redis", lambda: redis)
    monkeypatch.setattr(alert_module.settings, "alert_telegram_bot_token", "123:TESTTOKEN")
    monkeypatch.setattr(alert_module.settings, "alert_telegram_chat_id", "-1001234567890")

    async def _counting_alert(text_value: str, *, key: str, redis: Any, client: Any = None) -> bool:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: posts.append(text_value) or httpx.Response(200, json={"ok": True})
            )
        ) as alert_client:
            return await alert_module.send_alert(
                text_value, key=key, redis=redis, client=alert_client
            )

    monkeypatch.setattr(worker_module, "send_alert", _counting_alert)

    # Контроль вызывается НАПРЯМУЮ, а не через `run_once`. Это не обход
    # проверки, а её условие: `run_once` штатно закрывает просроченное окно
    # первым шагом (`run_decisions`), и зависших окон после него НЕ ОСТАЁТСЯ —
    # именно так и должен работать здоровый воркер. Зависшее окно означает
    # «воркер не отработал», и воспроизводится оно вызовом контроля без
    # переходов.
    now = datetime.now(UTC)
    with structlog.testing.capture_logs() as captured:
        first = await worker_module.check_overdue_windows(factory, now=now, duration_ms=1)
        second = await worker_module.check_overdue_windows(factory, now=now, duration_ms=2)

    logs = [entry["event"] for entry in captured]

    assert first == 1
    assert second == 1
    assert logs.count("worker.window.overdue") == 2
    assert len(posts) == 1
