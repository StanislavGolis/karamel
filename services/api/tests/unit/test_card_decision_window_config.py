"""Конфиг окна решения карточки (Task 7.5; AC-03, OQ-1).

§18.1: каждый тест краснеет от конкретной правки. Проверяется не «значение
прочиталось», а «опечатка роняет старт»: `CARD_DEFAULT_DECISION_WINDOW_MINUTES
= 5` прошёл бы Pydantic, но `ck_event_cards_decision_window (15..10080)`
отверг бы вставку — автор получил бы `500` на валидной форме, и причина
лежала бы в `.env`, а не в коде.

`_env_file=None` обязателен: без него `Settings` читает `.env` из корня
репозитория, и тест зависел бы от локального файла разработчика.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

REQUIRED_ENV = {
    "JWT_SECRET": "unit_test_jwt_secret_at_least_32_chars",
    "SMS_CODE_SECRET": "unit_test_sms_secret_at_least_32_chars",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Собирает настройки из явного окружения, игнорируя локальный `.env`."""
    for name in ("CARD_DEFAULT_DECISION_WINDOW_MINUTES", *env):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_default_decision_window_is_48_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03, OQ-1: дефолт 2880 минут = 48 часов.

    Краснеет, если вернуть 1440: OQ-1 подтверждал 48 ч осознанно — сутки
    заставляют автора сидеть у экрана, трое суток выкидывают карточку из
    ленты на три дня. Значение остаётся конфигурируемым внутри 15..10080.
    """
    settings = _settings(monkeypatch)

    assert settings.card_default_decision_window_minutes == 2880


def test_decision_window_from_env_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03 (FR70): значение приходит из конфигурации без правки кода.

    Краснеет, если заменить поле на константу в коде 4.2: смена продуктового
    дефолта требовала бы правки кода и релиза вместо правки `.env`.
    """
    settings = _settings(monkeypatch, CARD_DEFAULT_DECISION_WINDOW_MINUTES="60")

    assert settings.card_default_decision_window_minutes == 60


@pytest.mark.parametrize("value", ["14", "10081"])
def test_decision_window_outside_range_fails_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """AC-03: значение вне 15..10080 роняет старт, а не создание карточки.

    Краснеет, если снять `field_validator`: значение дошло бы до CHECK
    `ck_event_cards_decision_window` и стало бы `500` при создании первой
    карточки — при том что ошибка конфигурационная и видна на старте.

    Обе границы в одной параметризации: нижняя и верхняя проверяются
    разными половинами условия, и падение одной не видно по другой.
    """
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CARD_DEFAULT_DECISION_WINDOW_MINUTES=value)
