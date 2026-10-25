"""Конфиг порога напоминания (стори 5б.4, Task 5.2; AC-02, FR103).

§18.1: каждый тест краснеет от конкретной правки. Порог вне `1..1440`
прошёл бы Pydantic и дал бы либо напоминание сразу после публикации (`0`),
либо «за сутки до закрытия» (сутки и больше), то есть FR103 формально
выполнялся бы, а пользы не приносил. Ошибка конфигурации обязана валить
старт, а не выглядеть «напоминания не работают».

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
    for name in ("CARD_REMINDER_THRESHOLD_MINUTES", *env):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_default_threshold_is_fifteen_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-02, D-5b.4-1: дефолт 15 минут — нижняя граница окна решения.

    Краснеет, если вернуть другое число: 15 — минимальное `decision_window`
    (`CARD_MIN_DECISION_WINDOW_MINUTES`), и порог больше него на коротком окне
    не сработал бы никогда (окно закрывается раньше, чем наступает «за N минут»).
    """
    assert _settings(monkeypatch).card_reminder_threshold_minutes == 15


def test_threshold_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-02, §18.6: порог меняется из `.env` без пересборки образа.

    Краснеет, если поле объявлено константой модуля, а не настройкой.
    """
    settings = _settings(monkeypatch, CARD_REMINDER_THRESHOLD_MINUTES="45")
    assert settings.card_reminder_threshold_minutes == 45


@pytest.mark.parametrize("value", ["0", "-1", "1441", "100000"])
def test_threshold_out_of_range_fails_fast(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-02: порог вне `1..1440` роняет старт, а не работает «как-то».

    Краснеет, если валидатор снять: `0` слал бы напоминание каждому
    опубликованному окну, `1441` — за сутки до закрытия, когда автор ещё
    ничего не решил.
    """
    with pytest.raises(ValidationError, match="CARD_REMINDER_THRESHOLD_MINUTES"):
        _settings(monkeypatch, CARD_REMINDER_THRESHOLD_MINUTES=value)


@pytest.mark.parametrize("value", ["1", "15", "1440"])
def test_threshold_boundaries_accepted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-02: границы диапазона включительны.

    Краснеет, если валидатор написать строгим (`1 < v < 1440`): значение
    по умолчанию 15 и нижняя граница 1 стали бы недостижимы, и дефолт
    отвергался бы собственным валидатором.
    """
    settings = _settings(monkeypatch, CARD_REMINDER_THRESHOLD_MINUTES=value)
    assert settings.card_reminder_threshold_minutes == int(value)
