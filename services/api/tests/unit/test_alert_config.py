"""Валидаторы настроек алертинга (стори 10.7, Task 1.1; AC-01; P-5 из CR).

**Почему этот файл exists.** Task 1.1 объявлял два валидатора защитой:
«значение 1 отменяет защиту от флейка — валидатор такое значение роняет на
старте». Ревьюер проверил: `grep tests/` по `ALERT_PROBE_INTERVAL_*` и
`ALERT_CONSECUTIVE_FAILURES` дал **0** в тестах. Мутация «удалить оба
валидатора» прошла бы зелёной — то есть заявление было без доказательства
(тот же класс, что P-16 в 10.2, где DS сам раскрыл тавтологию).

**Что проверяется.** Не только «плохое значение падает», но и ГРАНИЦЫ:
значение на границе диапазона обязано ПРОХОДИТЬ, а на единицу за границей —
падать. Без нижней границы проверки валидатор можно заменить на
`if v < 0` и тесты останутся зелёными.

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

# Переменные, которые тест обязан контролировать: иначе значение из окружения
# CI переопределит переданное и тест проверял бы окружение, а не валидатор.
_CONTROLLED = (
    "ALERT_PROBE_INTERVAL_SECONDS",
    "ALERT_CONSECUTIVE_FAILURES",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Собирает настройки из явного окружения, игнорируя локальный `.env`."""
    for name in (*_CONTROLLED, *env):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


# --- ALERT_PROBE_INTERVAL_SECONDS: диапазон 30..3600 ---------------------------


def test_probe_interval_default_is_one_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01: дефолт 60 с — такт probe, названный в D-10.7-5.

    Краснеет, если дефолт поменять: 60 с — компромисс между «заметить отказ
    быстро» и «не долбить `/health/deep` каждую секунду».
    """
    config = _settings(monkeypatch)

    assert config.alert_probe_interval_seconds == 60


@pytest.mark.parametrize("value", ["29", "0", "-1", "3601", "100000"])
def test_probe_interval_out_of_range_fails_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """AC-01: значение вне `30..3600` валит СТАРТ, а не «алерты не приходят».

    `29` и меньше: две неудачи за минуту, и один сетевой флейк превращается
    в алерт — бот замьючен в первый день, и настоящий инцидент пропущен.
    `3601` и больше: падение обнаруживается позже, чем пользователь его
    заметит, то есть контроль не нужен.
    """
    with pytest.raises(ValidationError, match="ALERT_PROBE_INTERVAL_SECONDS"):
        _settings(monkeypatch, ALERT_PROBE_INTERVAL_SECONDS=value)


@pytest.mark.parametrize("value", ["30", "3600"])
def test_probe_interval_boundaries_are_accepted(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Границы `30` и `3600` ПРОХОДЯТ: иначе валидатор был бы строже контракта.

    Без этой проверки валидатор можно заменить на `if not 31 <= v <= 3599` и
    тесты на отказ останутся зелёными — то есть граница проверялась бы в одну
    сторону.
    """
    config = _settings(monkeypatch, ALERT_PROBE_INTERVAL_SECONDS=value)

    assert config.alert_probe_interval_seconds == int(value)


# --- ALERT_CONSECUTIVE_FAILURES: порог >= 2 ------------------------------------


def test_consecutive_failures_default_is_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01: дефолт 2 — «две подряд», как требует AC-01 дословно."""
    config = _settings(monkeypatch)

    assert config.alert_consecutive_failures == 2


@pytest.mark.parametrize("value", ["1", "0", "-1", "-100"])
def test_consecutive_failures_below_two_fails_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """AC-01, D-10.7-2: порог меньше двух отменяет защиту от флейка.

    `1`: единственный таймаут `GET /health/deep` (перезапуск api, сборка
    мусора в БД) поднял бы бота по тревоге, и алерт перестал бы что-либо
    значить. `0` и отрицательное: алерт не ушёл бы никогда, а настройка
    выглядела бы рабочей.
    """
    with pytest.raises(ValidationError, match="ALERT_CONSECUTIVE_FAILURES"):
        _settings(monkeypatch, ALERT_CONSECUTIVE_FAILURES=value)


@pytest.mark.parametrize("value", ["2", "3", "100"])
def test_consecutive_failures_from_two_is_accepted(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Порог `2` и выше проходит: верхней границы нет намеренно.

    Больший порог — осознанный выбор оператора (меньше ложных тревог ценой
    задержки обнаружения), и запрещать его значило бы спорить с эксплуатацией.
    """
    config = _settings(monkeypatch, ALERT_CONSECUTIVE_FAILURES=value)

    assert config.alert_consecutive_failures == int(value)
