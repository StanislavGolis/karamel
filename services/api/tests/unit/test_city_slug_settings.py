"""`Settings`: `city_slugs`, `default_city_slug`, `city_timezone` (стори 12.1, AC-6).

AC-6 требует три свойства и **ноль** свойств с `community`. Проверяется через
`hasattr`, а не грепом по исходнику: гард на текст зеленел бы при любом
упоминании слова в комментарии, а `P-25` требует проверять эффект — реальное
отсутствие атрибута у объекта настроек.

`city_timezone` — новое поле, не переименование: до этой стори зоны города в
коде не было вовсе (`grep 'ZoneInfo|Asia/' app/` → 0 вхождений). Без проверки
неизвестная зона всплывала бы `ZoneInfoNotFoundError` посреди сборки ленты,
то есть `500` на валидном запросе.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    """Настройки с подменёнными полями города (остальное — дефолты).

    `_env_file=None` — иначе значения `.env` разработчика перебивали бы
    переданные аргументы, и тест проверял бы чужой конфиг.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_city_fields_exist() -> None:
    """AC-6: три свойства на месте и имеют предсказуемые дефолты."""
    settings = _settings()

    assert settings.city_slugs == ("tomsk",)
    assert settings.default_city_slug == "tomsk"
    assert settings.city_timezone == "Asia/Tomsk"


def test_no_community_attributes() -> None:
    """AC-6: свойств с `community` в имени у `Settings` нет.

    Проверяется объект, а не текст файла: `P-25` — гард обязан проверять
    эффект. Атрибут, оставленный ради совместимости, прошёл бы греп-тест,
    если бы слово встречалось только в комментарии.
    """
    settings = _settings()

    leftovers = [name for name in dir(settings) if "community" in name.lower()]
    assert leftovers == [], f"в Settings остались свойства с `community`: {leftovers}"


def test_city_slugs_parsed_from_comma_string() -> None:
    """AC-6: `tomsk,kemerovo` разбирается в кортеж (владелец правит `.env`)."""
    settings = _settings(city_slugs="tomsk,kemerovo")

    assert settings.city_slugs == ("tomsk", "kemerovo")


def test_city_slugs_rejects_uppercase() -> None:
    """AC-6: `Tomsk` роняет старт, а не превращается тихо в `tomsk`.

    Приведение регистра сделало бы значение в `.env` не тем, что реально
    используется (`D-5`), и расхождение было бы невидимым.
    """
    with pytest.raises(ValidationError) as exc:
        _settings(city_slugs="Tomsk")

    # CR 12.1 (P-29): проверяется ИМЕННО регекс-гард. Без сверки текста тест
    # зеленел и при ослабленном `_CITY_SLUG_RE = re.compile(r".+")`: `Tomsk`
    # отвергал соседний `_validate_city` («дефолт не входит в список»), и
    # утверждение «регистр не приводится» оставалось недоказанным —
    # мутация C7 выжила до этой правки.
    message = str(exc.value)
    assert "каждый слаг должен соответствовать" in message, (
        f"`Tomsk` отвергнут не регекс-гардом: {message}"
    )


def test_city_slugs_rejects_empty() -> None:
    """AC-6: пустой список — падение на старте, а не «город без имени».

    Сообщение проверяется по существу: пустой список ловит и соседний
    `_validate_city` (дефолт не входит в пустой кортеж), поэтому без сверки
    текста тест оставался зелёным при отключённом разборе — проверено
    мутацией M8 (`if not normalized:` → `if False:`), которая выжила.
    """
    with pytest.raises(ValidationError) as exc:
        _settings(city_slugs=",")

    assert "не может быть пустым" in str(exc.value), (
        f"пустой список отвергнут не своим гардом: {exc.value}"
    )


def test_default_city_slug_must_be_in_list() -> None:
    """AC-6: дефолт вне списка — падение на старте.

    Иначе `POST /places` без явного слага отказывал бы всегда, а причина
    выяснялась бы на первом живом сохранении.
    """
    with pytest.raises(ValidationError) as exc:
        _settings(city_slugs="tomsk", default_city_slug="kemerovo")

    assert "default_city_slug" in str(exc.value).lower()


def test_city_timezone_accepts_iana_zone() -> None:
    """AC-6: валидная зона IANA принимается и сохраняется как есть."""
    settings = _settings(city_timezone="Europe/Moscow")

    assert settings.city_timezone == "Europe/Moscow"


def test_city_timezone_rejects_unknown_zone() -> None:
    """AC-6: неизвестная зона роняет старт, а не первый запрос.

    `ZoneInfoNotFoundError` в середине сборки ленты — это `500` на валидной
    форме и трейсбек вместо имени настройки. Ошибка обязана быть видна при
    запуске.
    """
    with pytest.raises(ValidationError) as exc:
        _settings(city_timezone="Mars/Olympus")

    assert "city_timezone" in str(exc.value).lower()


def test_city_timezone_rejects_empty() -> None:
    """AC-6: пустая зона — падение (пустая строка не зона IANA)."""
    with pytest.raises(ValidationError):
        _settings(city_timezone="")
