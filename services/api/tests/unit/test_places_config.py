"""Конфиг сообщества пилота (Task 2.4; AC-03).

§18.1: каждый тест краснеет от конкретной правки. Проверяется не «значение
прочиталось», а «опечатка роняет старт»: слаг неизвестного сообщества даёт
место, которого не видно ни в одной ленте (`community_slug` фильтрует всё),
и такой дефект находят только по пустому кабинету администратора.

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
    for name in ("COMMUNITY_SLUGS", "DEFAULT_COMMUNITY_SLUG", *env):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_default_community_slug_is_tomsk(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03: дефолт задан и входит в список сообществ.

    Краснеет, если `default_community_slug` поменять на слаг, которого нет
    в `community_slugs`: тогда сервис 3.1 отказывает на собственном дефолте,
    и `POST /places` без явного слага перестаёт работать.
    """
    settings = _settings(monkeypatch)

    assert settings.default_community_slug == "tomsk"
    assert "tomsk" in settings.community_slugs


def test_community_slugs_parsed_from_comma_separated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03: `COMMUNITY_SLUGS=tomsk,kemerovo` разбирается в кортеж.

    Без `NoDecode` pydantic-settings ждёт JSON для кортежа и роняет старт
    `SettingsError` — ровно тот дефект, что был у `PILOT_CITY_BBOX`.
    Краснеет при удалении `NoDecode`.
    """
    settings = _settings(monkeypatch, COMMUNITY_SLUGS="tomsk,kemerovo")

    assert settings.community_slugs == ("tomsk", "kemerovo")


def test_community_slugs_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03, CR 2.6: слаги канонизируются тем же `normalize_text`, что и ввод.

    Краснеет, если вернуть `strip()`: `COMMUNITY_SLUGS="tomsk,\u00a0kemerovo "`
    дал бы слаг с NBSP, который не совпадёт ни с одним сохранённым значением,
    и место легло бы в «невидимое сообщество» молча.
    """
    from app.services.profile_normalize import normalize_text

    settings = _settings(monkeypatch, COMMUNITY_SLUGS=" tomsk ,\u00a0kemerovo ")

    assert settings.community_slugs == ("tomsk", normalize_text("kemerovo"))


def test_default_community_slug_must_be_in_slugs(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03: `DEFAULT_COMMUNITY_SLUG` вне списка роняет старт.

    Краснеет, если убрать `model_validator`: сервис отказывал бы на каждом
    создании места без явного слага, то есть админка 3.2 не работала бы
    вовсе — и это выяснилось бы на первом живом нажатии «Сохранить».
    """
    with pytest.raises(ValidationError, match="DEFAULT_COMMUNITY_SLUG"):
        _settings(
            monkeypatch,
            COMMUNITY_SLUGS="tomsk,kemerovo",
            DEFAULT_COMMUNITY_SLUG="novosibirsk",
        )


def test_community_slug_regex_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03: слаг обязан соответствовать `^[a-z0-9_-]{3,64}$`.

    Тот же шаблон стоит в `ck_places_community_slug`. Расхождение дало бы
    `500` на сохранении: Pydantic пропустил, БД отвергла. Краснеет при
    удалении проверки шаблона в конфиге.
    """
    with pytest.raises(ValidationError, match="COMMUNITY_SLUGS"):
        _settings(monkeypatch, COMMUNITY_SLUGS="Tomsk")


@pytest.mark.parametrize("value", ["", "   ", "\u00a0"])
def test_empty_community_slugs_fails_fast(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-03: пустой список сообществ роняет старт.

    Пустой список означает, что ни одно сообщество не разрешено: создание
    места отказывает всегда, а админка 3.2 показывает пустую форму без
    объяснения. Ошибка обязана быть на старте.
    """
    with pytest.raises(ValidationError, match="COMMUNITY_SLUGS"):
        _settings(monkeypatch, COMMUNITY_SLUGS=value)
