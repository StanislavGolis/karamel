"""Enum'ы карточки события (Task 7.1; AC-01, AC-03, AC-05).

§18.1: каждый тест краснеет от конкретной правки. Здесь проверяется не
«значение прочиталось», а состав наборов: на конкретные значения опираются
частичные индексы (`WHERE status = 'published'`, `WHERE verification_status
= 'pending'`). Переименование значения без правки индекса даёт индекс,
который никогда не совпадает, и воркер молча не находит просроченные окна
(NFR20) — дефект без единой ошибки в логах.
"""

from __future__ import annotations

from app.models.enums import CardVerificationStatus, EventCardStatus
from app.models.event_card import CARD_REQUEST_MODE_CAPACITY


def test_event_card_status_values_are_exact() -> None:
    """AC-01 (FR73, FR74, FR100): ровно шесть состояний, в порядке архитектуры.

    Краснеет, если из набора убрать `closed` (FR74 «закрыть набор вручную»)
    или `cancelled` (FR100 «отменить»): переход стал бы невозможен, а CHECK
    в БД отверг бы легальный статус `500`-й ошибкой.

    Порядок сверяется кортежем, а не множеством: он задаёт порядок в схеме
    OpenAPI и в выпадающем списке админки, и его случайная перестановка
    меняет контракт, не меняя поведения.
    """
    assert tuple(EventCardStatus) == (
        EventCardStatus.DRAFT,
        EventCardStatus.PUBLISHED,
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
        EventCardStatus.FINISHED,
        EventCardStatus.CANCELLED,
    )


def test_card_verification_status_values_are_exact() -> None:
    """AC-01 (FR101, FR113): ровно три исхода проверки контента.

    Краснеет, если добавить четвёртое значение (например `skipped`):
    частичный индекс `ix_event_cards_pending_verification` опирается на
    `pending`, и карточка с новым статусом выпала бы из очереди модератора
    молча.
    """
    assert tuple(CardVerificationStatus) == (
        CardVerificationStatus.PENDING,
        CardVerificationStatus.APPROVED,
        CardVerificationStatus.REJECTED,
    )


def test_request_mode_capacity_is_one() -> None:
    """AC-03 (FR70): режим заявок — ровно вместимость 1.

    Краснеет, если константу поменять на 2: `is_request_mode` стал бы `True`
    у карточек открытого набора, и 4.2 потребовал бы окно решения у формы,
    где FR70 его не запрашивает.
    """
    assert CARD_REQUEST_MODE_CAPACITY == 1
