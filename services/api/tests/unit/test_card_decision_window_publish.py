"""Окно решения при публикации (стори 5б.1; AC-01…AC-04).

§18.2: часть утверждений проверяется ТЕКСТОМ запроса без БД — снятие
предиката окна в ленте не покраснело бы на данных, если все карточки в
фикстуре имеют открытое окно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.repositories.event_cards import EventCardRepository
from app.services.event_card_service import _compute_expires_at

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def test_expires_at_is_lesser_of_window_and_starts_at() -> None:
    """AC-01, AC-03 (ADR-02): `LEAST(starts_at, now + окно)` — включительно.

    Краснеет от снятия `min`: окно закрывалось бы ПОСЛЕ начала встречи, и
    CHECK `ck_event_cards_expires_at` отверг бы строку (`500` на публикации).
    """
    starts_at = _NOW + timedelta(hours=10)

    # Окно меньше времени до встречи — побеждает окно.
    assert _compute_expires_at(
        starts_at=starts_at, window_minutes=60, now=_NOW
    ) == _NOW + timedelta(hours=1)

    # Окно больше времени до встречи — побеждает `starts_at`.
    assert _compute_expires_at(
        starts_at=_NOW + timedelta(minutes=30), window_minutes=60, now=_NOW
    ) == _NOW + timedelta(minutes=30)

    # Граница включительно: ровно равные значения — `expires_at == starts_at`.
    assert _compute_expires_at(
        starts_at=_NOW + timedelta(hours=1), window_minutes=60, now=_NOW
    ) == _NOW + timedelta(hours=1)


def test_feed_statement_excludes_expired_published_window() -> None:
    """AC-02 (NFR20): `published` с истёкшим окном не показывается открытым.

    Краснеет от снятия `or_(status != 'published', expires_at > now)`:
    карточка висела бы в ленте до прогона воркера (до 60 с), и участник
    видел бы набор, где решение уже не принимается.
    """
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=False, after=None, limit=21
    )
    sql = str(statement)

    assert "event_cards.expires_at > " in sql, sql
    # `full` НЕ фильтруется по окну: места кончились сами (ADR-10), и участие
    # возможно после отмены чужого участия — предикат привязан к `published`.
    assert "event_cards.status != " in sql, sql


def test_feed_count_statement_uses_same_window_predicate() -> None:
    """AC-02: `total` считается по ТОМУ ЖЕ предикату, что `items`.

    Краснеет, если добавить окно только в `build_feed_statement`: клиент
    получил бы «5 всего, 0 на странице» либо курсор в пустоту.
    """
    statement = EventCardRepository.build_feed_count_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=False
    )
    sql = str(statement)

    assert "event_cards.expires_at > " in sql, sql
    assert "event_cards.status != " in sql, sql
