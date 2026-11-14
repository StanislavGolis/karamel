"""Валидаторы настроек адресного геокодера (стори 13.1; CR C3, W-13.1-j).

**Почему этот файл появился только после CR.** Оба валидатора роняют СТАРТ
приложения на неверном `.env`, но до CR их проверял только код: мутация
`v.encode("ascii")` → `pass` (C3) выжила, потому что ни один тест не подавал
кириллицу в `NOMINATIM_USER_AGENT`.

**Почему это важнее, чем кажется.** `User-Agent` — HTTP-заголовок, и httpx
кодирует его ASCII. Кириллица в настройке означает `UnicodeEncodeError` на
присвоении значения, то есть `500` на КАЖДОМ поиске адреса. Проверка стоит
на старте именно для того, чтобы такая правка не дошла до первого живого
запроса: падение при запуске видно сразу, падение на поиске — нет.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    """`Settings` без `.env`: значения берутся только из аргументов."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_user_agent_with_cyrillic_fails_on_start() -> None:
    """C3 CR 2026-10-31: кириллица в UA роняет старт, а не первый запрос.

    Краснеет, если убрать `v.encode("ascii")` из валидатора: значение
    сохранилось бы, и КАЖДЫЙ поиск адреса отвечал бы `500`
    (`UnicodeEncodeError` на конструировании заголовка).
    """
    with pytest.raises(ValidationError) as error:
        _settings(nominatim_user_agent="karamel/0.1 (+https://крмл.рф)")

    assert "NOMINATIM_USER_AGENT" in str(error.value) or "ASCII" in str(error.value)


def test_user_agent_ascii_is_accepted() -> None:
    """C3: пара к отказу — punycode-домен (ASCII) проходит.

    Без этой пары тест выше зеленел бы и при валидаторе, отвергающем ЛЮБОЕ
    значение, то есть проверял бы «UA запрещён», а не «не-ASCII запрещён».
    """
    assert _settings(nominatim_user_agent="karamel/0.1 (+https://xn--j1accp.xn--p1ai)").nominatim_user_agent


def test_empty_user_agent_fails_on_start() -> None:
    """AC-3: пустой UA роняет старт — OSM блокирует запросы без контакта."""
    with pytest.raises(ValidationError):
        _settings(nominatim_user_agent="")


def test_viewbox_for_unknown_city_fails_on_start() -> None:
    """AC-10 (O-5): рамка для города вне `city_slugs` роняет старт.

    Краснеет, если убрать проверку `unknown`: опечатка в `GEO_CITY_VIEWBOX`
    (`tomsk1`) молчала бы — рамка лежит, город не существует, поиск по нему
    невозможен, и понять это по ответу API нельзя.
    """
    with pytest.raises(ValidationError):
        _settings(
            city_slugs="tomsk",
            geo_city_viewbox={"tomsk1": "84.80,56.60,85.20,56.30"},
        )


def test_viewbox_malformed_numbers_fail_on_start() -> None:
    """AC-10: нечисловая рамка роняет старт (перевёрнутый прямоугольник = пустая выдача)."""
    with pytest.raises(ValidationError):
        _settings(city_slugs="tomsk", geo_city_viewbox={"tomsk": "a,b,c,d"})


def test_city_without_viewbox_is_allowed() -> None:
    """`W-13.1-f`: город БЕЗ рамки — законная конфигурация, а не ошибка старта.

    DS-находка на baseline: первая редакция падала здесь, и приложение не
    поднималось на верном `.env` пилота (`CITY_SLUGS=tomsk,kemerovo`, рамка
    одна). Рамка — уточнение поиска; без неё геокодер ищет по стране.
    """
    settings = _settings(
        city_slugs="tomsk,kemerovo",
        geo_city_viewbox={"tomsk": "84.80,56.60,85.20,56.30"},
    )

    assert settings.city_slugs == ("tomsk", "kemerovo")
    assert settings.geo_city_viewbox == {"tomsk": "84.80,56.60,85.20,56.30"}


def test_viewbox_with_three_numbers_fails_on_start() -> None:
    """M25 CR r2: рамка из ТРЁХ чисел роняет старт.

    Краснеет при `len(parts) != 4` → `< 2`: три числа распакуются в
    `(lng, lat_max, lng_max)` и упадут на распаковке в четыре переменные
    только в момент запроса, то есть `500` на первом поиске вместо отказа
    на старте. `viewbox` Nominatim требует ровно четыре.
    """
    # Проверяется ТЕКСТ, а не только факт отказа. Замер: три числа падают и
    # БЕЗ проверки длины — распаковка `lng_min, lat_max, lng_max, lat_min`
    # даёт `ValueError: not enough values to unpack`, и pydantic ловит его как
    # ошибку валидации. То есть `len(parts) != 4` (мутация M25) эквивалентна
    # по ФАКТУ отказа, и различие только в сообщении. Без этой проверки
    # оператор увидел бы «not enough values to unpack» вместо адреса правки.
    with pytest.raises(ValidationError) as error:
        _settings(city_slugs="tomsk", geo_city_viewbox={"tomsk": "84.80,56.60,85.20"})

    assert "ожидаются четыре числа" in str(error.value), (
        "сообщение обязано называть формат: иначе правка .env превращается в разбор "
        "трассировки распаковки"
    )


def test_viewbox_with_five_numbers_fails_on_start() -> None:
    """M25: пара к трём — ПЯТЬ чисел тоже отказ (не «лишние игнорируем»).

    Молчаливое отбрасывание лишнего дало бы непредсказуемую рамку: какой
    из пяти чисел лишний, знает только автор `.env`.
    """
    with pytest.raises(ValidationError) as error:
        _settings(
            city_slugs="tomsk",
            geo_city_viewbox={"tomsk": "84.80,56.60,85.20,56.30,99.0"},
        )

    assert "ожидаются четыре числа" in str(error.value)


def test_viewbox_with_reversed_latitudes_fails_on_start() -> None:
    """M26 CR r2: широты в обратном порядке роняют старт.

    Краснеет при отсутствии `lat_min < lat_max`: перевёрнутый прямоугольник
    Nominatim не отвергает — он молча возвращает пустой список, и
    пользователь решает, что мест в городе нет. Рамка `56.30..56.60` верна,
    `56.60..56.30` — нет.
    """
    with pytest.raises(ValidationError):
        _settings(city_slugs="tomsk", geo_city_viewbox={"tomsk": "84.80,56.30,85.20,56.60"})


def test_viewbox_with_correct_latitudes_is_accepted() -> None:
    """M26: пара к отказу — прямой порядок широт проходит.

    Без пары тест выше зеленел бы при валидаторе, отвергающем ЛЮБУЮ рамку,
    то есть проверял бы «рамка запрещена», а не «порядок обязателен».
    """
    settings = _settings(
        city_slugs="tomsk",
        geo_city_viewbox={"tomsk": "84.80,56.60,85.20,56.30"},
    )

    assert settings.geo_city_viewbox == {"tomsk": "84.80,56.60,85.20,56.30"}
