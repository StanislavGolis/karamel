"""Гард выхода из режима заявок (стори 6.1; AC-08, OQ-5b.3-3 вариант «б»).

Правило: `PATCH`, повышающий вместимость `1 → 2..6`, запрещён и при живой
`pending`-заявке, а не только при `filled_count > 0`. Иначе заявитель,
ждавший решения, остался бы с заявкой на карточке, которая уже не собирает
заявки (`pending` перестал бы быть осмысленным состоянием, FR76/FR77).

§18.11: оба значения признака проверяются — «есть `pending`» (отказ) И
«нет `pending`» (успех). Тест только на отказ прошёл бы и при гарде,
запрещающем любую правку.

Подмена `JoinRequestRepository` — в модуле сервиса: сервис создаёт его сам
(`JoinRequestRepository(session)`), и без подмены тест требовал бы БД.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.enums import CardVerificationStatus, EventCardStatus
from app.models.event_card import EventCard
from app.schemas.event_card import EventCardUpdate
from app.services.event_card_service import CardAlreadyJoinedError, update_card

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
STARTS_AT = NOW + timedelta(days=3)


class _Settings:
    """Минимум настроек — те же поля, что у `CardSettings`."""

    default_city_slug = "tomsk"
    city_slugs = ("tomsk",)
    card_default_decision_window_minutes = 2880
    card_content_stop_words: tuple[str, ...] = ()
    card_active_limit = 2
    card_reminder_threshold_minutes = 15


class _Session:
    """Сессия-заглушка: `commit` считает вызовы, БД не трогает."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _Repo:
    """Подмена `EventCardRepository`: отдаёт заданную карточку."""

    def __init__(self, card: EventCard) -> None:
        self.card = card
        # X-6.1-1 (chore P-7): последовательность блокировок и значения
        # `for_update`. Проверяется отдельным тестом порядка, а не здесь.
        self.locks: list[str] = []
        self.for_update_requests: list[bool] = []

    async def lock_author(self, author_id: int) -> None:
        """ADR-09: автор блокируется ПЕРВЫМ (X-6.1-1, chore P-7)."""
        self.locks.append("users")

    async def get_owned(
        self, author_id: int, card_uuid: uuid_lib.UUID, *, for_update: bool = False
    ) -> EventCard:
        """Сигнатура повторяет репозиторий: `PATCH` читает карточку под блокировкой."""
        self.for_update_requests.append(for_update)
        if for_update:
            self.locks.append("event_cards")
        return self.card


class _JoinRequests:
    """Подмена `JoinRequestRepository`: отвечает заранее заданным `has_pending`."""

    def __init__(self, _session: Any, *, has_pending: bool) -> None:
        self._has_pending = has_pending

    async def has_pending_for_card(self, *, card_id: int) -> bool:
        return self._has_pending


def _card(**overrides: Any) -> EventCard:
    """Опубликованная карточка режима заявок (`capacity=1`)."""
    values: dict[str, Any] = {
        "author_id": 7,
        "place_id": 42,
        "city_slug": "tomsk",
        # Стори 13.2 (AC-7): title — NOT NULL в БД, в фабрике модели задаётся явно.
        "title": "Настольные игры",
        "category": "cafe",
        "starts_at": STARTS_AT,
        "capacity": 1,
        "decision_window_minutes": 2880,
        "expires_at": STARTS_AT - timedelta(hours=24),
        "status": EventCardStatus.PUBLISHED,
        "verification_status": CardVerificationStatus.APPROVED,
        "filled_count": 0,
    }
    values.update(overrides)
    return EventCard(**values)


def _run_update(
    monkeypatch: pytest.MonkeyPatch,
    *,
    card: EventCard,
    payload: EventCardUpdate,
    has_pending: bool,
) -> EventCard:
    """Прогоняет `update_card` с подменёнными репозиториями."""

    def _factory(session: Any) -> _JoinRequests:
        return _JoinRequests(session, has_pending=has_pending)

    monkeypatch.setattr("app.services.event_card_service.JoinRequestRepository", _factory)
    return asyncio.run(
        update_card(
            _Session(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            payload=payload,
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_Repo(card),  # type: ignore[arg-type]
        )
    )


def test_capacity_one_to_two_blocked_by_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-08: `1 → 2` при живой `pending` → `CARD_ALREADY_JOINED`.

    Краснеет, если гард смотрит только на `filled_count` (OQ-5b.3-3):
    заявитель остался бы с заявкой на карточке открытого набора.
    """
    card = _card()

    with pytest.raises(CardAlreadyJoinedError):
        _run_update(
            monkeypatch,
            card=card,
            payload=EventCardUpdate(capacity=2, decision_window_minutes=None),
            has_pending=True,
        )

    # §18.2: отказ не меняет строку — «проверка до присвоения».
    assert card.capacity == 1


def test_capacity_one_to_two_allowed_without_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-08, §18.11: `1 → 2` без `pending` → успех.

    Краснеет, если гард запрещает выход из режима заявок всегда: карточка
    без заявителей — законный случай смены режима (FR70).
    """
    card = _card()

    updated = _run_update(
        monkeypatch,
        card=card,
        payload=EventCardUpdate(capacity=2),
        has_pending=False,
    )

    assert updated.capacity == 2


def test_patch_title_keeps_working_with_live_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-08: правка БЕЗ смены режима не блокируется живой заявкой.

    Краснеет, если гард расширить до «есть `pending` → любой `PATCH` нельзя»:
    автор не смог бы поправить опечатку в заголовке, пока заявитель ждёт
    решения (регресс CR 4.2, патч 5).
    """
    card = _card()

    updated = _run_update(
        monkeypatch,
        card=card,
        payload=EventCardUpdate(description="Новое название"),
        has_pending=True,
    )

    assert updated.description == "Новое название"
    assert updated.capacity == 1


def test_capacity_two_to_one_still_blocked_by_filled_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-08, регресс-гард: `filled_count > 0` блокирует смену режима как раньше.

    Краснеет, если новый гард заменить (а не дополнить) старым условием:
    карточка с участником может уехать в режим одного места.
    """
    card = _card(capacity=3, filled_count=1)

    with pytest.raises(CardAlreadyJoinedError):
        _run_update(
            monkeypatch,
            card=card,
            payload=EventCardUpdate(capacity=1, decision_window_minutes=60),
            has_pending=False,
        )
