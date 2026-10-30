"""Валидаторы фиксированного SMS-кода в конфигурации (2026-10-25).

Зачем этот файл существует. Фиксированный код в боевом окружении — это вход
в любой аккаунт с этим номером без доступа к SMS: второй фактор перестаёт быть
фактором. Цена ошибки несопоставима с ценой проверки, поэтому значение
проверяется на старте процесса, а не «аккуратностью при деплое».

Что здесь доказывается:

* код неверной длины и не-ASCII цифры роняют старт (иначе он не совпадёт
  ни с одним введённым, и причину искали бы в Redis и хеше);
* код при боевом провайдере роняет старт (боевой клиент его не читает, но
  `.env` создаёт ложное впечатление включённой заглушки);
* список номеров без кода роняет старт (почти наверняка забыта строка выше).

`_env_file=None` обязателен: без него `Settings` прочитает `.env` разработчика,
где заглушка включена, и тесты на отказ стали бы зелёными по чужой причине.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

#: Секреты, без которых `_validate_secrets` роняет `Settings` в любом окружении.
REQUIRED_ENV = {
    "JWT_SECRET": "unit_test_jwt_secret_at_least_32_chars",
    "SMS_CODE_SECRET": "unit_test_sms_secret_at_least_32_chars",
    "CHAT_MESSAGE_KEY": "unit_test_chat_message_key_32_chars_x",
}

#: Имена настроек заглушки: чистятся перед каждым сбором, чтобы значения из
#: процесса-родителя не влияли на ожидаемый отказ.
_STUB_VARS = (
    "SMS_STUB_FIXED_CODE",
    "SMS_STUB_FIXED_PHONES",
    "SMS_PROVIDER",
    "ENV",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Собирает настройки из явного окружения, игнорируя локальный `.env`."""
    for name in _STUB_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_stub_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустые значения по умолчанию — обычная генерация, заглушка не включена.

    Краснеет, если задать ненулевой дефолт: тогда фиксированный код работал бы
    у всех, кто не заполнил `.env`, включая тестовые стенды.
    """
    built = _settings(monkeypatch, SMS_PROVIDER="stub")

    assert built.sms_stub_fixed_code == ""
    assert built.sms_stub_fixed_phones == ""


def test_valid_stub_config_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Корректная комбинация проходит: заглушка + dev + 6 цифр + номер."""
    built = _settings(
        monkeypatch,
        ENV="development",
        SMS_PROVIDER="stub",
        SMS_STUB_FIXED_CODE="111111",
        SMS_STUB_FIXED_PHONES="+79234490800",
    )

    assert built.sms_stub_fixed_code == "111111"
    assert built.sms_stub_fixed_phones == "+79234490800"


def test_fixed_code_with_combat_provider_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SMS_PROVIDER=smsru` + фиксированный код — падение старта, не предупреждение.

    Боевой клиент значение не читает: без этой проверки `.env` с обеими
    строками выглядел бы как включённая заглушка, и владелец искал бы причину
    «код не подходит» в генераторе.
    """
    with pytest.raises(ValidationError, match="SMS_STUB_FIXED_CODE"):
        _settings(
            monkeypatch,
            ENV="development",
            SMS_PROVIDER="smsru",
            SMS_STUB_FIXED_CODE="111111",
        )


@pytest.mark.parametrize(
    "value",
    [
        "12345",  # короче sms_code_length
        "1234567",  # длиннее sms_code_length
        "abcdef",  # не цифры
        "12 456",  # пробел внутри
        "１２３４５６",  # полноширинные юникодные цифры: isdigit() истинно, код не совпадёт
    ],
)
def test_fixed_code_wrong_format_fails(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Код не из ровно `sms_code_length` ASCII-цифр роняет старт.

    Краснеет, если проверять только `isdigit()` и длину: полноширинная запись
    проходит обе проверки, но сравнивается с ASCII-вводом пользователя и не
    совпадает никогда — дефект проявился бы как «код не подходит» без причины.
    """
    with pytest.raises(ValidationError, match="ASCII-цифр"):
        _settings(
            monkeypatch,
            ENV="development",
            SMS_PROVIDER="stub",
            SMS_STUB_FIXED_CODE=value,
        )


def test_phones_without_code_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Список номеров без кода — падение старта.

    Такая комбинация ничего не включает и почти всегда означает забытую строку
    `SMS_STUB_FIXED_CODE=` выше: молчаливое игнорирование скрыло бы опечатку
    ровно в момент, когда разработчик ждёт работающую заглушку.
    """
    with pytest.raises(ValidationError, match="SMS_STUB_FIXED_PHONES"):
        _settings(
            monkeypatch,
            ENV="development",
            SMS_PROVIDER="stub",
            SMS_STUB_FIXED_PHONES="+79234490800",
        )


def test_whitespace_only_code_equals_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Код из пробелов считается пустым, и список без него — падение.

    `SMS_STUB_FIXED_CODE=   ` в `.env` глазами человека выглядит как «пусто», а
    для сравнения строк — как значение. Нормализация `strip()` делает поведение
    одинаковым, и ошибка указывает на реальную причину.
    """
    with pytest.raises(ValidationError, match="SMS_STUB_FIXED_PHONES"):
        _settings(
            monkeypatch,
            ENV="development",
            SMS_PROVIDER="stub",
            SMS_STUB_FIXED_CODE="   ",
            SMS_STUB_FIXED_PHONES="+79234490800",
        )
