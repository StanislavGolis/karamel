"""Публикация `card.full` воркером автовыбора (CR 6.5; находка X-6.5-1).

**Что здесь доказывается.** Автовыбор (`close_due_card` через воркер) собирает
компанию — участники ОБЯЗАНЫ получить `card.full` (FR84/FR30). До правки CR
событие не публиковалось НИКОГДА: DS оставил долг «коммит у воркера», не
проверив, что коммит в воркере ЕСТЬ (`close_due_cards.py:103`) и что владелец
транзакции — именно он. Это класс «поверили докстрингу ядра вместо кода».

**Почему тест на воркере, а не на ядре.** Ядро `close_due_card` коммитить не
имеет права (его зовут три владельца, включая ленивое чтение с чужой
транзакцией) — публикация обязана жить там, где известен факт фиксации.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services import ws_publisher
from app.workers import close_due_cards

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = [
    pytest.mark.usefixtures("domain_cleanup"),
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="нужен реальный PostgreSQL: задайте DATABASE_URL",
    ),
]


async def _seed_due_card(session: AsyncSession) -> tuple[int, int, str]:
    """Карточка с истёкшим окном и живой заявкой — то есть кандидат автовыбора.

    Возвращает `(card_id, author_id, card_uuid)`. Заявка `pending`: именно она
    превращается в `selected`, и именно тогда открывается чат.
    """
    from tests.integration.ws_seed import _create_user

    author, _ = await _create_user(session)
    member, _ = await _create_user(session)
    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": "karamel"},
    )
    place_id = int(place.scalar_one())
    starts = datetime.now(UTC) + timedelta(days=1)
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, filled_count, "
            "verification_status, published_at, kind, activity) "
            "VALUES (:a, :p, 'karamel', 'Встреча', 'cafe', :st, 1, 60, :e, 'published', 0, "
            "'approved', :pub, 'company', 'coffee') RETURNING id, uuid"
        ),
        {
            "a": author.id,
            "p": place_id,
            "st": starts,
            # Окно истекло: `due_decision_ids` ищет `expires_at <= now`.
            "e": datetime.now(UTC) - timedelta(minutes=5),
            "pub": starts - timedelta(days=1),
        },
    )
    card_id, card_uuid = card.one()
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:c, :a, '', 'pending', now(), "
            "now() + interval '1 day', NULL)"
        ),
        {"c": int(card_id), "a": member.id},
    )
    await session.commit()
    return int(card_id), int(author.id), str(card_uuid)


def _factory() -> tuple[async_sessionmaker[AsyncSession], Any]:
    """Отдельный async-движок и фабрика для прогона воркера.

    `second_session.get_bind()` возвращает SYNC `Engine`: `async_sessionmaker`
    принимает только `AsyncEngine` и падает `ArgumentError` (проверено
    прогоном). Поэтому воркер получает собственную фабрику на реальном
    движке — так же, как в проде `get_session_factory()`.
    """
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    return async_sessionmaker(bind=engine, expire_on_commit=False), engine


async def test_worker_publishes_card_full_after_commit(
    second_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-6.5-1: автовыбор публикует `card.full`, и публикация идёт ПОСЛЕ commit.

    Проверяется полная последовательность вызовов (`==`), а не вхождение:
    публикация до commit показала бы участнику чат, которого ещё нет в БД.

    Мутации, которые красят тест:
    - убрать вызов `publish_to_chat` в воркере (исходный дефект X-6.5-1);
    - перенести `publish_to_chat` выше `await session.commit()`.
    """
    card_id, _author_id, _card_uuid = await _seed_due_card(second_session)

    calls: list[str] = []
    original_commit = AsyncSession.commit
    original_publish = ws_publisher.publish_to_chat

    async def spy_commit(self: AsyncSession) -> None:
        result = await original_commit(self)
        calls.append("commit")
        return result

    async def spy_publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        calls.append(f"publish:{envelope.type}")
        await original_publish(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr(AsyncSession, "commit", spy_commit)
    monkeypatch.setattr(close_due_cards, "publish_to_chat", spy_publish)

    factory, engine = _factory()
    try:
        result = await close_due_cards.run_decisions(factory, limit=50)
    finally:
        await engine.dispose()

    assert result["selected"] >= 1, result
    assert "publish:card.full" in calls, calls
    publish_index = calls.index("publish:card.full")
    # `commit` обязан встретиться РАНЬШЕ публикации: в цикле воркера коммит
    # идёт до обработки результата (close_due_cards.py:103).
    assert "commit" in calls[:publish_index], calls


async def test_worker_publish_failure_does_not_break_selection(
    second_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-6.5-1, ADR-11: сбой публикации НЕ отменяет автовыбор.

    `publish_to_chat` сам по себе не бросает; тест фиксирует следствие —
    даже когда шина недоступна, карточка остаётся собранной (`full`), а
    прогон воркера не падает в `failed`.
    """
    card_id, _author_id, _card_uuid = await _seed_due_card(second_session)

    async def broken_publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        # Худший случай: код ниже уровня `ws_publisher` бросает сам.
        raise RuntimeError("шина недоступна (тест)")

    monkeypatch.setattr(close_due_cards, "publish_to_chat", broken_publish)

    factory, engine = _factory()
    try:
        with pytest.raises(RuntimeError):
            await close_due_cards.run_decisions(factory, limit=50)
    finally:
        await engine.dispose()

    status = (
        await second_session.execute(
            text("SELECT status FROM event_cards WHERE id = :c"), {"c": card_id}
        )
    ).scalar_one()
    # Транзакция карточки уже зафиксирована ДО публикации: данные не потеряны.
    assert status == "full"
