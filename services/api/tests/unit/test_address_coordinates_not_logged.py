"""Координаты адресного поиска не попадают в логи (стори 13.1, Task 5.2; AC-5).

`NFR34`/FR59 запрещают ХРАНИТЬ координаты, а лог контейнера — это хранение:
его читает поддержка, он уезжает в агрегаторы и живёт дольше БД. Поэтому
«отладочная» строка с `lat`/`lng` — такая же утечка, как колонка в таблице.

**Имя намеренно отличается** от `test_coordinates_are_not_logged`
(`tests/unit/test_geo_service.py:78`): тот гард про `locate_city` (город по
координатам устройства, `D-9`), этот — про адресный геокодер. Слить их
значило бы получить тест, который не падает при поломке ни одного из двух,
потому что проверяет объединение каналов.

**Гард на эффект (`P-25`), три канала.** structlog пишет JSON в stdout мимо
`logging`, поэтому проверка только `caplog.getMessage()` зеленела бы при
живой утечке (найдено CR 2.3, патч 8).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

import app.services.address_geocoder as geocoder_module
from app.services.address_geocoder import GeoUnavailableError, search_addresses

#: Значения, которых не должно быть ни в одном канале. Взяты те же, что
#: отдаёт провайдер в тестовой записи: сравнение идёт по ПОДСТРОКЕ, поэтому
#: `56.4977` ловится и в `56.4977`, и в `"lat":"56.4977"`.
_FORBIDDEN = ("56.4977", "84.9481")


class _FakeRedis:
    """Минимум Redis: `SET NX PX` и `PTTL`. Ключ живёт окно, потом снова можно."""

    def __init__(self) -> None:
        self.data: dict[str, int] = {}

    async def set(self, key: str, value: int, *, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = int(value)
        return True

    async def pttl(self, key: str) -> int:
        return 1000


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geocoder_module, "get_redis", lambda: _FakeRedis())


@pytest.fixture(autouse=True)
async def _dispose(monkeypatch: pytest.MonkeyPatch) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "display_name": "Кинотеатр «Прага», улица Ленина, 12, Томск",
                    "name": "Кинотеатр «Прага»",
                    "lat": "56.4977",
                    "lon": "84.9481",
                    "class": "amenity",
                    "type": "cinema",
                }
            ],
        )

    from httpx import MockTransport

    client = httpx.AsyncClient(transport=MockTransport(handler), timeout=3.0)
    monkeypatch.setattr(geocoder_module, "_client", client)
    yield
    await client.aclose()
    geocoder_module._client = None


def _everything(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> str:
    """Склеивает ТРИ канала: сообщение, атрибуты записи, stdout/stderr."""
    messages = "\n".join(record.getMessage() for record in caplog.records)
    attributes = "\n".join(
        f"{key}={value}" for record in caplog.records for key, value in record.__dict__.items()
    )
    console = capsys.readouterr()
    return "\n".join((messages, attributes, console.out, console.err))


async def test_address_success_does_not_log_coordinates(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-5: успешный поиск не пишет координаты ни в один канал.

    Краснеет, если добавить `logger.info("found", lat=suggestion.lat)`: это
    выглядит безобидной диагностикой, а на деле публикует точку встречи
    в лог, который живёт дольше БД.
    """
    caplog.set_level(logging.DEBUG)

    await search_addresses("Прага", city_slug="tomsk")

    everything = _everything(caplog, capsys)
    for value in _FORBIDDEN:
        assert value not in everything, f"координата {value} попала в лог"


async def test_failure_does_not_log_coordinates(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-4, AC-5: отказ провайдера тоже не логирует координаты.

    Пара к успеху: ветка `except` — самое вероятное место, где «для отладки»
    положено `str(error)`. `httpx.HTTPStatusError.__str__` содержит URL
    запроса, а URL содержит `q=<адрес>` — то есть персональные данные
    в тексте лога. Поэтому логируется `type(error).__name__` и статус.
    """
    caplog.set_level(logging.DEBUG)

    import app.services.address_geocoder as module

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout for {request.url} 56.4977,84.9481", request=request)

    from httpx import MockTransport

    await module._client.aclose() if module._client is not None else None
    module._client = httpx.AsyncClient(transport=MockTransport(handler), timeout=3.0)
    try:
        # Конкретный класс, а не `Exception` (B017): широкий `pytest.raises`
        # зеленел бы на ЛЮБОМ сбое — включая `UnboundLocalError` и опечатку
        # в тесте, то есть перестал бы проверять ветку деградации.
        with pytest.raises(GeoUnavailableError):
            await search_addresses("Прага", city_slug="tomsk")
    finally:
        await module._client.aclose()

    everything = _everything(caplog, capsys)
    for value in _FORBIDDEN:
        assert value not in everything, f"координата {value} попала в лог"
