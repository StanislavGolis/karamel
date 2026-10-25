"""Unit-тесты нормализации номера (Task 9.1; AC-05).

Функция чистая: без БД и Redis. Единая точка нормализации вызывается и в
`/request`, и в `/verify` — иначе вход и повторная проверка разошлись бы,
и код, выданный на `8…`, не нашёлся бы по `+7…`.
"""

import pytest

from app.services.phone import InvalidPhoneError, normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "+79001234567",
        "89001234567",
        "79001234567",
        "+7 900 123-45-67",
        "8 (900) 123 45 67",
        "+7(900)123.45.67",
        "  8-900-123-45-67  ",
    ],
)
def test_normalize_accepts_any_form(raw: str) -> None:
    """AC-05: пробелы, дефисы, скобки и точки не мешают; результат всегда один."""
    assert normalize_phone(raw) == "+79001234567"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "+7900123456",  # 9 цифр
        "+790012345678",  # 12 цифр
        "+19001234567",  # чужой код страны
        "+380900123456",  # Украина
        "+7abc9001234567",
        "девятьсот",
        "+7 900 123 45 6a",
        "+7",
    ],
)
def test_normalize_rejects_invalid(raw: str) -> None:
    """AC-05: только российские номера из 11 цифр с кодом 7.

    Пилот в Томске, провайдер российский: чужой код страны ушёл бы в SMS.ru
    как международное сообщение и не дошёл бы до пользователя.
    """
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


def test_normalize_is_idempotent() -> None:
    """Task 2.3: повторная нормализация не меняет значение."""
    once = normalize_phone("89001234567")

    assert normalize_phone(once) == once
