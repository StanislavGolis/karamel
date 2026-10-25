"""Коды ошибок сервиса заявок (стори 5а.1, Task 4.3; AC-06).

Краснеет, если код переименовать в стиль, отличный от остальных
(`STARTS_AT_IN_PAST` vs `card_not_found` в проекте соседствуют, но
пустая строка или `camelCase` сломали бы клиентскую обработку §7.2).
"""

from __future__ import annotations

import re

from app.services import join_request_service as service

CODE_NAMES = (
    "JOIN_REQUEST_OWN_CARD_CODE",
    "JOIN_REQUEST_LIMIT_REACHED_CODE",
    "ALREADY_REQUESTED_CODE",
    "CAPACITY_ONE_USES_REQUESTS_CODE",
    "CARD_NOT_ACCEPTING_CODE",
)


def test_codes_are_upper_snake_non_empty() -> None:
    """AC-06: каждый код — непустой `UPPER_SNAKE`.

    Краснеет, если код оставить пустой строкой или записать в `camelCase`:
    §7.2 требует машиночитаемый код, и пустой код превратил бы ошибку
    в `500` без диагностики.
    """
    for name in CODE_NAMES:
        value = getattr(service, name)

        assert value, f"{name} пустой"
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", value), f"{name}={value!r}"


def test_own_card_code_matches_story_contract() -> None:
    """AC-06: код «своя карточка» — ровно тот, что ждёт 5а.2.

    Краснеет, если код переименовать при реализации 5а.2: контракт ошибок
    зафиксирован в 5а.1, и смена кода — это смена публичного API.
    """
    assert service.JOIN_REQUEST_OWN_CARD_CODE == "JOIN_REQUEST_OWN_CARD"


def test_exceptions_expose_their_codes() -> None:
    """AC-06: исключения отдают объявленный код через `code`.

    Краснеет, если у класса `code` останется от родителя или станет
    `None`: роутер 5а.2 маппит исключение в ответ по `code`, и пустое
    значение дало бы ответ без машиночитаемой причины.
    """
    pairs = (
        (service.JoinRequestOwnCardError, service.JOIN_REQUEST_OWN_CARD_CODE),
        (service.AlreadyRequestedError, service.ALREADY_REQUESTED_CODE),
        (
            service.CapacityOneUsesRequestsError,
            service.CAPACITY_ONE_USES_REQUESTS_CODE,
        ),
        (service.CardNotAcceptingError, service.CARD_NOT_ACCEPTING_CODE),
    )

    for exception_type, expected in pairs:
        assert exception_type.code == expected


def test_limit_error_carries_numbers_only() -> None:
    """AC-04: `details` лимита — только числа, без `uuid`.

    Краснеет, если в `details` добавить список карточек: он раскрыл бы
    состав заявок пользователя и позволил бы перебор (тот же выбор,
    что у `CardLimitReachedError` стори 4.4).
    """
    error = service.JoinRequestLimitReachedError(limit=2, active_count=2)

    assert error.code == service.JOIN_REQUEST_LIMIT_REACHED_CODE
    assert error.details == {"limit": 2, "active_count": 2}
    assert all(isinstance(value, int) for value in error.details.values())
