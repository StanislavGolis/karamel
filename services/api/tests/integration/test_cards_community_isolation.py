"""Изоляция сообществ при чтении карточки (стори 10.1; AC-05, ADR-13).

**Что доказывается.** Срез сообщества при чтении берётся из
`users.community_slug` зрителя, а НЕ из `settings.default_community_slug`.
До 10.1 источником был конфиг, и участник «другого сообщества» физически
не мог получить `404` — оба читали один и тот же слаг.

**Почему сервисный уровень, а не API.** Роутер передаёт `user.community_slug`
одной строкой; если проверять только через API, тест останется зелёным при
любой реализации внутри `get_card`. Здесь параметр задаётся явно — тест
краснеет ровно тогда, когда сервис снова начнёт читать конфиг.

**P-19: базовая линия по обе стороны границы.** Свой слаг → карточка
найдена; чужой → `CardNotFoundError`. Без первой половины тест был бы
зелёным и при полностью сломанном чтении.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.event_card_service import CardNotFoundError, get_card, list_cards

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


class _CardSettings:
    """Минимум настроек `CardSettings` без чтения `.env` (как в 4.1).

    Поля обязаны повторять протокол целиком: урезанная заглушка падала бы
    `AttributeError` на живом коде — тест проверял бы заглушку.
    """

    default_community_slug = settings.default_community_slug
    community_slugs = (settings.default_community_slug,)
    card_default_decision_window_minutes = 60
    card_content_stop_words: tuple[str, ...] = ()
    card_active_limit = 2


async def _user(session: AsyncSession, slug: str) -> int:
    """Пользователь с ЯВНЫМ сообществом: слага нет в дефолте модели."""
    phone = f"+7913{uuid_lib.uuid4().int % 10**7:07d}"
    row = await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, community_slug) "
            "VALUES ('phone', :p, :p, :slug) RETURNING id"
        ),
        {"p": phone, "slug": slug},
    )
    return int(row.scalar_one())


async def _card(session: AsyncSession, author_id: int, slug: str) -> uuid_lib.UUID:
    """Опубликованная карточка: `published` — единственный статус ленты."""
    starts_at = datetime.now(UTC) + timedelta(days=1)
    place_id = (
        await session.execute(
            text(
                "INSERT INTO places (name, category, address_text, community_slug) "
                "VALUES (:n, 'cafe', 'пр. Ленина, 1', :slug) RETURNING id"
            ),
            {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": slug},
        )
    ).scalar_one()
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, "
            "description, category, starts_at, capacity, decision_window_minutes, "
            "expires_at, status, filled_count, verification_status, published_at) "
            "VALUES (:a, :p, :slug, 'Встреча', NULL, 'cafe', :starts, 3, 60, "
            ":expires, 'published', 0, 'approved', now()) RETURNING uuid"
        ),
        {
            "a": author_id,
            "p": place_id,
            "slug": slug,
            "starts": starts_at,
            "expires": starts_at - timedelta(hours=1),
        },
    )
    return uuid_lib.UUID(str(row.scalar_one()))


async def test_own_community_reads_card(session: AsyncSession) -> None:
    """AC-05, базовая линия (P-19): свой слаг — карточка ДОСТУПНА.

    Без этой половины следующий тест был бы зелёным при реализации,
    возвращающей `404` на любую карточку.
    """
    slug = settings.default_community_slug
    author = await _user(session, slug)
    card_uuid = await _card(session, author, slug)

    item = await get_card(
        session,
        card_uuid=card_uuid,
        viewer_id=author,
        viewer_community_slug=slug,
        settings=_CardSettings(),
    )
    assert item.card.uuid == card_uuid


async def test_other_community_gets_404(session: AsyncSession) -> None:
    """AC-05, ADR-13: участник другого сообщества получает `404`, не `403`.

    Краснеет ровно тогда, когда `get_card` снова начнёт брать слаг из
    `settings.default_community_slug`: конфиг один, и карточка нашлась бы
    независимо от сообщества зрителя.
    """
    author = await _user(session, settings.default_community_slug)
    card_uuid = await _card(session, author, settings.default_community_slug)
    stranger = await _user(session, "other-city")

    with pytest.raises(CardNotFoundError):
        await get_card(
            session,
            card_uuid=card_uuid,
            viewer_id=stranger,
            viewer_community_slug="other-city",
            settings=_CardSettings(),
        )


async def test_other_community_feed_is_empty(session: AsyncSession) -> None:
    """AC-05, задача 5.5 (CR 10.1, P-04): лента чужого сообщества пуста.

    Вторая из двух точек чтения (`event_card_service.py:941`). До этого теста
    `list_cards` не покрывался ничем: мутация «вернуть `_resolve_community_slug`
    в `:941`» выживала, потому что все пользователи в тестах получали слаг
    из конфига, и обе реализации давали одинаковый результат.
    """
    slug = settings.default_community_slug
    author = await _user(session, slug)
    await _card(session, author, slug)
    stranger = await _user(session, "other-city")

    page = await list_cards(
        session,
        viewer_id=stranger,
        viewer_community_slug="other-city",
        settings=_CardSettings(),
    )

    assert page.items == []
    assert page.total == 0


async def test_own_community_feed_is_not_empty(session: AsyncSession) -> None:
    """AC-05, P-19: базовая линия — своя лента НЕ пуста.

    Без этой половины предыдущий тест был бы зелёным и при полностью
    сломанном фильтре, возвращающем пустоту всегда.
    """
    slug = settings.default_community_slug
    author = await _user(session, slug)
    card_uuid = await _card(session, author, slug)

    page = await list_cards(
        session,
        viewer_id=author,
        viewer_community_slug=slug,
        settings=_CardSettings(),
    )

    assert [item.card.uuid for item in page.items] == [card_uuid]
    assert page.total == 1
