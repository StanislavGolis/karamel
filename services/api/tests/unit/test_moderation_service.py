"""Сервис модерации карточек (стори 4.3, Task 7; AC-01, AC-03…AC-06).

§18.2: проверяется наблюдаемый результат (поля карточки, коды ошибок),
а не факт вызова. Репозиторий и сессия подменяются — тест идёт без БД.

§18.1: в каждом docstring указано, какая правка сделает тест красным.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import CardVerificationStatus, EventCardStatus
from app.models.event_card import EventCard
from app.services.moderation_service import (
    CARD_ALREADY_PUBLISHED_CODE,
    CARD_ALREADY_VERIFIED_CODE,
    MODERATION_CARD_NOT_FOUND_CODE,
    REJECTION_REASON_REQUIRED_CODE,
    REJECTION_REASON_TOO_LONG_CODE,
    CardAlreadyPublishedError,
    CardAlreadyVerifiedError,
    ModerationCardNotFoundError,
    RejectionReasonRequiredError,
    RejectionReasonTooLongError,
    approve_card,
    reject_card,
)

CARD_UUID = uuid_lib.UUID("55555555-5555-4555-8555-555555555555")
MODERATOR_ID = 77
NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


class _Settings:
    """Минимум настроек: срез сообщества и стоп-слова (CR 4.3, патч 1)."""

    default_city_slug = "tomsk"
    card_content_stop_words = ("казино", "займ")


class _FakeSession:
    """Сессия-заглушка: `commit` считает вызовы, БД не трогает."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _FakeRepository:
    """Подмена `EventCardRepository`: отдаёт заданную карточку."""

    def __init__(self, card: EventCard | None) -> None:
        self._card = card

    async def get_for_moderation(
        self, card_uuid: uuid_lib.UUID, *, city_slug: str
    ) -> EventCard | None:
        # CR 4.3, патч 2: срез сообщества обязателен в сигнатуре.
        # Фейк отдаёт карточку независимо от слага — срез проверяется
        # интеграционным тестом на реальной БД.
        return self._card


def _card(
    *,
    status: str = EventCardStatus.DRAFT,
    verification_status: str = CardVerificationStatus.PENDING,
    rejection_reason: str | None = None,
) -> EventCard:
    """Карточка в памяти: только поля, которые читает сервис модерации."""
    return EventCard(
        author_id=1,
        city_slug="tomsk",
        title="Кино в пятницу",
        description=None,
        category="cinema",
        starts_at=NOW + timedelta(days=3),
        capacity=2,
        decision_window_minutes=2880,
        expires_at=NOW + timedelta(days=1),
        status=status,
        verification_status=verification_status,
        filled_count=0,
        rejection_reason=rejection_reason,
        uuid=CARD_UUID,
    )


# --- approve: AC-04 ----------------------------------------------------------


async def test_approve_pending_sets_moderator_and_time() -> None:
    """AC-04: `approve` пишет `verified_by` модератора и `verified_at`.

    Краснеет, если `verified_by` брать из тела или не заполнять `verified_at`:
    решение стало бы анонимным, а история проверки — недостоверной.
    """
    card = _card()
    session = _FakeSession()

    result = await approve_card(
        session,  # type: ignore[arg-type]
        card_uuid=CARD_UUID,
        moderator_id=MODERATOR_ID,
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card),  # type: ignore[arg-type]
    )

    assert result.verification_status == CardVerificationStatus.APPROVED
    assert result.verified_by == MODERATOR_ID
    assert result.verified_at == NOW
    assert session.commits == 1


async def test_approve_clears_rejection_reason() -> None:
    """AC-04: одобрение отклонённой карточки стирает причину.

    Краснеет, если не сбрасывать `rejection_reason`: CHECK
    `ck_event_cards_rejection_reason` держит причину только у `rejected`,
    и запись упала бы `IntegrityError` → `500`.
    """
    card = _card(verification_status=CardVerificationStatus.REJECTED, rejection_reason="Стоп-слово")

    result = await approve_card(
        _FakeSession(),  # type: ignore[arg-type]
        card_uuid=CARD_UUID,
        moderator_id=MODERATOR_ID,
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card),  # type: ignore[arg-type]
    )

    assert result.rejection_reason is None


async def test_approve_already_approved_conflicts() -> None:
    """AC-04 (OQ-3): повторное одобрение — `409 CARD_ALREADY_VERIFIED`.

    Краснеет, если вернуть `200`: `verified_at`/`verified_by` стали бы
    неотличимы от «кто проверил первым».
    """
    card = _card(verification_status=CardVerificationStatus.APPROVED)

    with pytest.raises(CardAlreadyVerifiedError) as error:
        await approve_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_ALREADY_VERIFIED_CODE


async def test_approve_published_card_conflicts() -> None:
    """AC-04 (OQ-5): решение по опубликованной карточке — `409`.

    Краснеет, если убрать проверку статуса: модерация получила бы второй,
    несогласованный путь скрытия контента (снятие с ленты — жалобы 6.x).
    """
    card = _card(status=EventCardStatus.PUBLISHED)

    with pytest.raises(CardAlreadyPublishedError) as error:
        await approve_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_ALREADY_PUBLISHED_CODE


async def test_approve_missing_card_is_not_found() -> None:
    """AC-04: нет карточки — `404 card_not_found`.

    Краснеет, если вернуть `None` или `403`: `403` раскрыл бы существование.
    """
    with pytest.raises(ModerationCardNotFoundError) as error:
        await approve_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(None),  # type: ignore[arg-type]
        )

    assert error.value.code == MODERATION_CARD_NOT_FOUND_CODE


async def test_approve_published_checked_before_verified() -> None:
    """AC-04: порядок проверок — «опубликована» раньше «уже одобрена».

    Краснеет, если поменять порядок ветвей: опубликованная И одобренная
    карточка отдала бы `CARD_ALREADY_VERIFIED` вместо `CARD_ALREADY_PUBLISHED`,
    и автор решения получил бы неверную причину отказа.
    """
    card = _card(
        status=EventCardStatus.PUBLISHED, verification_status=CardVerificationStatus.APPROVED
    )

    with pytest.raises(CardAlreadyPublishedError):
        await approve_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card),  # type: ignore[arg-type]
        )


# --- reject: AC-05 -----------------------------------------------------------


async def test_reject_stores_reason_and_moderator() -> None:
    """AC-05 (FR113): причина и модератор записываются.

    Краснеет, если не сохранять причину: автор не увидит, что исправлять.
    """
    card = _card()
    session = _FakeSession()

    result = await reject_card(
        session,  # type: ignore[arg-type]
        card_uuid=CARD_UUID,
        moderator_id=MODERATOR_ID,
        settings=_Settings(),  # type: ignore[arg-type]
        reason="Стоп-слово в заголовке",
        now=NOW,
        repository=_FakeRepository(card),  # type: ignore[arg-type]
    )

    assert result.verification_status == CardVerificationStatus.REJECTED
    assert result.rejection_reason == "Стоп-слово в заголовке"
    assert result.verified_by == MODERATOR_ID
    assert session.commits == 1


@pytest.mark.parametrize("reason", ["", "   ", "\u00a0\u2009"])
async def test_reject_empty_reason_required(reason: str) -> None:
    """AC-05 (FR113): пустая причина — `422 rejection_reason_required`.

    Краснеет, если убрать проверку в сервисе: админский скрипт идёт мимо
    Pydantic, а CHECK в БД дал бы `IntegrityError` → `500`.
    """
    with pytest.raises(RejectionReasonRequiredError) as error:
        await reject_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            reason=reason,
            now=NOW,
            repository=_FakeRepository(_card()),  # type: ignore[arg-type]
        )

    assert error.value.code == REJECTION_REASON_REQUIRED_CODE


async def test_reject_twice_replaces_reason() -> None:
    """AC-05 (OQ-4): повторное отклонение — `200` с ЗАМЕНОЙ причины.

    Краснеет, если добавить `409` на `rejected`: модератору пришлось бы
    уходить в SQL, чтобы уточнить формулировку.
    """
    card = _card(
        verification_status=CardVerificationStatus.REJECTED, rejection_reason="Старая причина"
    )

    result = await reject_card(
        _FakeSession(),  # type: ignore[arg-type]
        card_uuid=CARD_UUID,
        moderator_id=MODERATOR_ID,
        settings=_Settings(),  # type: ignore[arg-type]
        reason="Новая причина",
        now=NOW,
        repository=_FakeRepository(card),  # type: ignore[arg-type]
    )

    assert result.rejection_reason == "Новая причина"
    assert result.verification_status == CardVerificationStatus.REJECTED


async def test_reject_published_card_conflicts() -> None:
    """AC-05 (OQ-5): отклонение опубликованной — `409`.

    Краснеет, если убрать проверку статуса: опубликованная карточка получила бы
    `rejected` и выпала бы из ленты без процедуры жалоб.
    """
    card = _card(status=EventCardStatus.PUBLISHED)

    with pytest.raises(CardAlreadyPublishedError):
        await reject_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            reason="Причина",
            now=NOW,
            repository=_FakeRepository(card),  # type: ignore[arg-type]
        )


async def test_reject_missing_card_is_not_found() -> None:
    """AC-05: нет карточки — `404 card_not_found`."""
    with pytest.raises(ModerationCardNotFoundError):
        await reject_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            reason="Причина",
            now=NOW,
            repository=_FakeRepository(None),  # type: ignore[arg-type]
        )


async def test_reject_too_long_reason_rejected() -> None:
    """AC-05 (CR 4.3, патч 4): причина длиннее 500 символов — `422`, не `500`.

    Краснеет, если проверять длину только в схеме: админский скрипт идёт мимо
    Pydantic, и 100 КБ текста легли бы в `rejection_reason` (колонка `Text`).
    """
    with pytest.raises(RejectionReasonTooLongError) as error:
        await reject_card(
            _FakeSession(),  # type: ignore[arg-type]
            card_uuid=CARD_UUID,
            moderator_id=MODERATOR_ID,
            settings=_Settings(),  # type: ignore[arg-type]
            reason="я" * 501,
            now=NOW,
            repository=_FakeRepository(_card()),  # type: ignore[arg-type]
        )

    assert error.value.code == REJECTION_REASON_TOO_LONG_CODE
