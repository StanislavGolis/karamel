"""Конфигурация города пилота (Task 1.4; AC-04, AC-06).

§18.1: каждый тест падает без валидатора. Проверяется не «значение прочиталось»,
а «кривое значение роняет старт»: перевёрнутый bbox и пустой эталон не должны
доживать до первого запроса, иначе `locate-city` тихо отвечает
`outside_pilot_city`, а канонизация города молча перестаёт работать.

`_env_file=None` обязателен: без него `Settings` читает `.env` из корня
репозитория, и тест на пустой `PILOT_CITY` зависел бы от локального файла
разработчика.
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
    """Собирает настройки из явного окружения, игнорируя локальный `.env`.

    Переменные, которых нет в `env`, снимаются с процесса: иначе значение
    из оболочки разработчика (например, стендовый `PILOT_CITY`) подменило бы
    проверяемое поведение.
    """
    for name in ("PILOT_CITY", "PILOT_CITY_BBOX", *env):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


# --- AC-04: эталон нормализации -------------------------------------------------


def test_pilot_city_default_is_tomsk(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04: без переменной окружения эталон берётся из дефолта.

    Дефолт — не украшение: он единственный источник написания города, пока
    владелец не задал `PILOT_CITY` в `.env` стенда.
    """
    assert _settings(monkeypatch).pilot_city == "Томск"


def test_pilot_city_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04, §18.2: `PILOT_CITY=" Томск "` приводится к эталону без краёв.

    Без трима эталон не совпал бы ни с одним нормализованным городом
    (`casefold` сравнивает уже обрезанные строки), и канонизация перестала бы
    работать молча — дефект виден только на живом сохранении анкеты.
    """
    assert _settings(monkeypatch, PILOT_CITY="  Томск  ").pilot_city == "Томск"


@pytest.mark.parametrize(
    "raw",
    ["Томск\tцентр", "Томск  центр", "Томск\nцентр", "Том\u200bск"],
)
def test_pilot_city_is_canonicalized_like_saved_city(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """CR 2.6, патч 1 (AC-03): эталон нормализуется так же, как ввод анкеты.

    Дефект: `strip()` + замена NBSP не убирали внутренний таб, двойной пробел
    и ZWSP. `normalize_city` записывает в БД `normalize_text(ввод)`, поэтому
    `PILOT_CITY="Томск\tцентр"` давал эталон `'Томск\tцентр'`, тогда как
    в `profiles.city` лежало `'Томск центр'`. Точное `=` в предикате видимости
    возвращало 0 строк — молча, без ошибки и без лога.

    Тест краснеет, если вернуть `strip()` вместо `normalize_text`.
    """
    from app.services.profile_normalize import normalize_text

    assert _settings(monkeypatch, PILOT_CITY=raw).pilot_city == normalize_text(raw)


def test_pilot_city_replaces_nbsp(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04: NBSP в `.env` не переживает чтение.

    NBSP не режется `str.strip()` и не режется `btrim` в БД. Эталон
    `'Томск\u00a0'` не совпал бы с нормализованным `'Томск'`, и канонизация
    по эталону не срабатывала бы никогда.
    """
    assert _settings(monkeypatch, PILOT_CITY="Томск\u00a0").pilot_city == "Томск"


@pytest.mark.parametrize("value", ["", "   ", "\u00a0"])
def test_empty_pilot_city_fails_fast(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-04: пустой эталон роняет старт, а не «просто не канонизирует».

    Пустой `PILOT_CITY` означает, что нормализация города работает без
    эталона: `'томск'` и `'Томск'` лягут в БД разными строками, и выборка
    по городу (2.6) потеряет половину анкет.
    """
    with pytest.raises(ValidationError, match="PILOT_CITY"):
        _settings(monkeypatch, PILOT_CITY=value)


def test_too_long_pilot_city_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04: эталон длиннее `varchar(100)` не сохранится в `profiles.city`.

    Строка длиннее предела колонки дала бы `DBAPIError` на первом сохранении
    анкеты, то есть `500` на пользовательском вводе (запрет §«Как проверять»).
    """
    with pytest.raises(ValidationError, match="PILOT_CITY"):
        _settings(monkeypatch, PILOT_CITY="Т" * 101)


# --- AC-06: прямоугольник пилота ------------------------------------------------


def test_bbox_default_covers_tomsk_and_excludes_moscow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-06: дефолтный прямоугольник содержит Томск и не содержит Москву.

    Проверка не тавтологична (§18.2): значение сравнивается с реальными
    координатами двух городов, а не с самим собой. Дефолт, накрывающий
    Москву, отдавал бы `{"city": "Томск"}` пользователю из Москвы.
    """
    lat_min, lat_max, lng_min, lng_max = _settings(monkeypatch).pilot_city_bbox
    tomsk_lat, tomsk_lng = 56.49, 84.97
    moscow_lat, moscow_lng = 55.75, 37.61

    assert lat_min <= tomsk_lat <= lat_max and lng_min <= tomsk_lng <= lng_max
    assert not (lat_min <= moscow_lat <= lat_max and lng_min <= moscow_lng <= lng_max)


def test_bbox_is_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-06: строка `lat_min,lat_max,lng_min,lng_max` из `.env` разбирается.

    pydantic-settings по умолчанию ждёт JSON для кортежа; без `NoDecode`
    `PILOT_CITY_BBOX=56.30,56.60,84.80,85.20` роняло старт с `SettingsError`.
    Порядок элементов проверяется поимённо: перестановка осей даёт
    прямоугольник в Атлантике и ответ «вне города пилота» жителю Томска.
    """
    bbox = _settings(monkeypatch, PILOT_CITY_BBOX="56.1,56.9,84.5,85.5").pilot_city_bbox

    assert bbox == (56.1, 56.9, 84.5, 85.5)


@pytest.mark.parametrize(
    "value",
    [
        "56.60,56.30,84.80,85.20",  # перевёрнутые широты
        "56.30,56.60,85.20,84.80",  # перевёрнутые долготы
        "56.30,56.60,84.80",  # три числа
        "56.30,56.60,84.80,85.20,1",  # пять чисел
        "-91,56.60,84.80,85.20",  # широта вне -90..90
        "56.30,56.60,84.80,181",  # долгота вне -180..180
        "56.30,abc,84.80,85.20",  # не число
    ],
)
def test_bad_bbox_fails_fast(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-06, §18.1: любой кривой прямоугольник роняет старт.

    Перевёрнутый bbox — не «пустой», а невалидный: точка не попадёт внутрь
    никогда, и `locate-city` будет всегда отвечать `outside_pilot_city`.
    Такое значение должно падать на старте, а не на живом пользователе.
    """
    with pytest.raises(ValidationError, match="PILOT_CITY_BBOX"):
        _settings(monkeypatch, PILOT_CITY_BBOX=value)
