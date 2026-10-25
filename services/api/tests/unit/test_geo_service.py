"""Определение города по координатам (Task 7.3–7.4; AC-06, FR12, FR59).

§18.2: проверяется решение «внутри пилота / снаружи» на реальных координатах,
а не сравнение константы с собой. Граница включена в обе стороны — иначе
пользователь на краю прямоугольника получал бы `outside_pilot_city` при
фактически верной геолокации.

§D-9: координаты не логируются. Тест 7.4 ловит регрессию, в которой
в геосервис добавляют «полезный» лог с `lat`/`lng` при отладке.
"""

from __future__ import annotations

import logging

import pytest

from app.services.geo_service import locate_city

# Прямоугольник Томска из конфига по умолчанию (Task 1.2).
BBOX = (56.30, 56.60, 84.80, 85.20)
PILOT_CITY = "Томск"


def test_point_inside_bbox_returns_pilot_city() -> None:
    """AC-06: центр Томска внутри прямоугольника → город пилота.

    Координаты реальные (56.49, 84.97), а не середина константы: тест
    поймает и ошибку порядка осей, и слишком узкий прямоугольник.
    """
    assert locate_city(lat=56.49, lng=84.97, bbox=BBOX, pilot_city=PILOT_CITY) == "Томск"


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        (56.30, 84.80),  # левый нижний угол
        (56.60, 85.20),  # правый верхний угол
        (56.30, 85.20),  # левый верхний
        (56.60, 84.80),  # правый нижний
    ],
)
def test_boundary_points_are_inside(lat: float, lng: float) -> None:
    """AC-06: граница пилота включена в обе стороны.

    Житель на краю прямоугольника — всё ещё житель пилота; исключение
    границы давало бы «город не определился» на фактически верной
    геолокации, и пользователь винил бы приложение.
    """
    assert locate_city(lat=lat, lng=lng, bbox=BBOX, pilot_city=PILOT_CITY) == "Томск"


@pytest.mark.parametrize(
    ("lat", "lng", "where"),
    [
        (55.75, 37.61, "Москва"),
        (59.93, 30.36, "Санкт-Петербург"),
        (56.61, 84.97, "севернее Томска"),
        (56.29, 84.97, "южнее Томска"),
        (56.49, 84.79, "западнее Томска"),
        (56.49, 85.21, "восточнее Томска"),
    ],
)
def test_point_outside_bbox_returns_none(lat: float, lng: float, where: str) -> None:
    """AC-06: вне прямоугольника — `None` (роутер отдаст `outside_pilot_city`).

    Проверяются точки с каждой стороны, а не только Москва: дефект
    «перепутаны lat/lng местами» даёт город для далёкой точки и не даёт
    для соседней, и тест только на Москве его пропустил бы.
    """
    assert locate_city(lat=lat, lng=lng, bbox=BBOX, pilot_city=PILOT_CITY) is None


# CR 2.3, патч 8: значения, которые не должны появиться ни в одном логе.
_FORBIDDEN = ("56.49", "84.97", "55.75", "37.61")


def test_coordinates_are_not_logged(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-06, D-9, FR59: координаты не попадают в лог.

    `lat`/`lng` — ПД (FR59 прямо запрещает хранение). Лог контейнера читается
    поддержкой, попадает в агрегаторы и хранится дольше БД, поэтому даже
    «отладочная» строка с координатами — утечка.

    CR 2.3, патч 8: проверяются ТРИ канала, а не только `getMessage()`.
      * `record.getMessage()` — обычный `logging` без аргументов;
      * `record.__dict__` — `logging.info("locate", extra={"lat": ...})`:
        в `getMessage()` такого текста нет, значение лежит в атрибутах записи;
      * `capsys` — structlog пишет JSON в stdout мимо `logging`, поэтому
        `structlog.get_logger("geo").info("locate", lat=...)` в геосервисе
        раньше оставлял тест зелёным (проверено экспериментом).
    """
    caplog.set_level(logging.DEBUG)

    locate_city(lat=56.49, lng=84.97, bbox=BBOX, pilot_city=PILOT_CITY)
    locate_city(lat=55.75, lng=37.61, bbox=BBOX, pilot_city=PILOT_CITY)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    attributes = "\n".join(
        f"{key}={value}" for record in caplog.records for key, value in record.__dict__.items()
    )
    console = capsys.readouterr()
    everything = "\n".join((messages, attributes, console.out, console.err))

    for value in _FORBIDDEN:
        assert value not in everything, f"координата {value} попала в лог"


def test_structlog_leak_is_visible_to_the_guard(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """CR 2.3, патч 8: доказательство, что проверка выше НЕ слепа к structlog.

    Без этой проверки «тишина в логе» могла означать не отсутствие утечки,
    а слепоту теста: `caplog.records` не содержит событий structlog, потому
    что тот рендерит строку сам (в тестах — в stdout).

    Здесь координата пишется ровно так, как её написал бы неосторожный
    разработчик в геосервисе. Тест падает, если ни один из каналов, которые
    читает `test_coordinates_are_not_logged`, координату не видит: значит
    защита от утечки в тесте отсутствует, и зелёный прогон ничего не значит.
    """
    import structlog

    caplog.set_level(logging.DEBUG)
    structlog.get_logger("geo").info("locate", lat=56.49, lng=84.97)

    console = capsys.readouterr()
    seen = "\n".join((caplog.text, console.out, console.err))

    assert "56.49" in seen, "каналы теста не видят structlog — проверка слепа"
