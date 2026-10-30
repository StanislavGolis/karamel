"""Настройка версии текста правил (стори 11.4, Task 3.2; AC-3, NFR36).

Краснеет при снятии `field_validator` на `moderation_rules_version`: без него
`MODERATION_RULES_VERSION=v1` доходил бы до `users.consent_version` и записывал
в юридически значимое поле строку, которую нельзя сопоставить с редакцией
текста.

§18.2: проверяется наблюдаемый результат — исключение на старте, а не факт
вызова регулярки.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_version_format_rejects_non_semver() -> None:
    """AC-3: `v1` и `1.0-beta` роняют старт.

    Краснеет, если валидатор снят: значение ушло бы в `VARCHAR(16)` как есть,
    и модератор увидел бы «версию правил», которой не существует.
    """
    for bad in ("v1", "1.0-beta", "1,0", "", "latest"):
        with pytest.raises(ValidationError):
            Settings(moderation_rules_version=bad)


def test_version_format_accepts_semver() -> None:
    """AC-3: `1.0`, `1.2`, `10.11` принимаются без изменений.

    Краснеет, если валидатор отвергает легальные значения: бамп версии
    правил тогда невозможен без правки кода.
    """
    for good in ("1.0", "1.2", "10.11", "2.0"):
        assert Settings(moderation_rules_version=good).moderation_rules_version == good


def test_version_strips_surrounding_whitespace() -> None:
    """AC-3: `.env` с `MODERATION_RULES_VERSION=" 1.0 "` работает.

    Пробелы вокруг значения — обычная опечатка в `.env`; без `strip()`
    старт падал бы на значении, которое визуально верное.
    """
    assert Settings(moderation_rules_version=" 1.0 ").moderation_rules_version == "1.0"


def test_version_longer_than_column_is_rejected() -> None:
    """AC-1, AC-3: значение длиннее 16 символов роняет старт.

    Краснеет, если верхняя граница снята: `VARCHAR(16)` отвергла бы значение
    уже на `INSERT`, то есть `500` в момент принятия согласия — худший момент
    для ошибки конфигурации.
    """
    with pytest.raises(ValidationError):
        Settings(moderation_rules_version="1234567890.123456")


def test_default_version_is_valid_format() -> None:
    """AC-3: дефолт класса — валидная версия формата `X.Y`.

    **Со значением из окружения НЕ сравнивается** (CR 11.4, P7). Прежняя
    версия требовала `Settings(_env_file=None) == get_settings()`: это
    запрещало штатный бамп версии через `.env`, ради которого настройка и
    введена (AC-3, §18.6 — без пересборки образа). На стенде, где
    `MODERATION_RULES_VERSION=1.1`, тест был бы красным при верном коде.

    Совпадение с текстом правил проверяет `test_rules_version_alignment.py` —
    он читает TS-константу. Здесь — только формат дефолта: краснеет, если
    литерал в `config.py` станет `"1.0-beta"` или `"latest"`.

    `validate_default=True` (CR 11.4, P3) делает эту проверку возможной:
    без него Pydantic v2 дефолт не валидирует вовсе.
    """
    default = Settings(_env_file=None).moderation_rules_version

    assert default == "1.0" or (
        default.count(".") == 1 and all(part.isdigit() for part in default.split("."))
    ), f"дефолт MODERATION_RULES_VERSION не в формате X.Y: {default!r}"
    assert len(default) <= 16
