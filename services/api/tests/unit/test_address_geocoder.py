"""Адресный геокодер Nominatim (стори 13.1, Task 1; AC-2, AC-3, AC-10, AC-11).

**Сеть не используется ни в одном тесте.** Транспорт подменяется
`httpx.MockTransport` на всех ветвях — успех, `503`, таймаут, `429`.
Причина не стилистическая: зелёный локально / красный в CI стоил проекту
трёх дефектов (12.6, 12.7, 12.9), и класс дефекта один — тест читает
окружение, которого в CI нет. Здесь окружением был бы чужой HTTP-сервис.

§18.2: обработчик `MockTransport` собирает ЗАПРОСЫ, а тесты проверяют
значения параметров, а не факт вызова. Мутация `limit=5 → 10` обязана
красить AC-10, а не проходить незамеченной.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import app.services.address_geocoder as geocoder_module
from app.core.config import settings
from app.models.enums import PlaceCategory
from app.services.address_geocoder import (
    AddressQueryTooShortError,
    GeoRateLimitedError,
    GeoUnavailableError,
    search_addresses,
)

CITY_SLUG = "tomsk"
QUERY = "Прага"


def _nominatim_item(**overrides: Any) -> dict[str, Any]:
    """Запись в форме Nominatim `jsonv2`: `lat`/`lon` строками, не числами."""
    base: dict[str, Any] = {
        "place_id": 1,
        "display_name": "Кинотеатр «Прага», улица Ленина, 12, Томск, Россия",
        "name": "Кинотеатр «Прага»",
        "lat": "56.4977",
        "lon": "84.9481",
        "class": "amenity",
        "type": "cinema",
    }
    base.update(overrides)
    return base


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    """Ставит `MockTransport` в модуль геокодера и возвращает список запросов."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_record), timeout=3.0)
    monkeypatch.setattr(geocoder_module, "_client", client)
    return seen


@pytest.fixture(autouse=True)
async def _dispose() -> Any:
    """Клиент закрывается после теста: открытый `AsyncClient` шумит предупреждениями."""
    yield
    client = geocoder_module._client
    if client is not None:
        await client.aclose()
        geocoder_module._client = None


class _FakeRedis:
    """Минимум Redis: `SET NX PX`, `TTL` в миллисекундах."""

    def __init__(self) -> None:
        self.data: dict[str, int] = {}
        self.ttl_ms = 1000

    async def set(self, key: str, value: int, *, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = int(value)
        if px is not None:
            self.ttl_ms = px
        return True

    async def pttl(self, key: str) -> int:
        return self.ttl_ms


@pytest.fixture
async def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(geocoder_module, "get_redis", lambda: fake)
    return fake


async def test_success_returns_suggestion_with_lng(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-5: Nominatim отдаёт `lon`, контракт — `lng`.

    Краснеет, если пробросить `lon` как есть: клиент читал бы `undefined`
    и карта не получила бы координату вовсе — молча, без ошибки.
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert len(result) == 1
    assert result[0].name == "Кинотеатр «Прага»"
    assert result[0].lat == pytest.approx(56.4977)
    assert result[0].lng == pytest.approx(84.9481)
    assert "lon" not in result[0].model_dump()


async def test_request_parameters_match_contract(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-10: значения каждого параметра, а не факт запроса.

    Краснеет от подмены любого значения: `countrycodes` (поиск за границей),
    `viewbox`/`bounded` (чужой город), `limit` (нагрузка и мусор в выдаче),
    `accept-language` (нерусские подписи), `addressdetails` (нет `address`
    в фолбэке AC-11), `format` (форма ответа без `display_name`).
    """
    seen = _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()])
    )

    await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert len(seen) == 1
    params = dict(seen[0].url.params)
    assert params["q"] == QUERY
    assert params["countrycodes"] == "ru"
    assert params["bounded"] == "1"
    assert params["format"] == "jsonv2"
    assert params["limit"] == "5"
    assert params["accept-language"] == "ru"
    assert params["addressdetails"] == "1"
    assert params["viewbox"] == settings.geo_city_viewbox[CITY_SLUG]
    assert str(seen[0].url).startswith(settings.nominatim_base_url)


async def test_user_agent_header_equals_setting(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-3: UA с контактом — правило OSM, без него провайдер блокирует.

    Пара к следующему тесту: здесь заголовок ЕСТЬ и равен настройке.
    """
    seen = _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()])
    )

    await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert seen[0].headers["user-agent"] == settings.nominatim_user_agent


async def test_missing_user_agent_is_detectable(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-3: если заголовок убрать из запроса — его не будет в `seen`.

    Пара к предыдущему: гард, который зеленеет и при отсутствии заголовка,
    защищает только текст. Здесь проверяется, что проверка заголовка
    РАЗЛИЧАЕТ два состояния (мутация «убрать `headers=`» краснит предыдущий).
    """
    seen = _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()])
    )

    await search_addresses(QUERY, city_slug=CITY_SLUG)

    # httpx подставляет свой `python-httpx/x.y` по умолчанию — значит проверка
    # выше ловит именно НАШ заголовок, а не любой непустой.
    assert seen[0].headers["user-agent"] != "python-httpx"
    assert "karamel" in seen[0].headers["user-agent"]


async def test_timeout_setting_is_applied(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-3: таймаут 3 с доходит до транспорта.

    Краснеет от мутации `3.0 → 30.0`: цифра в настройке — единственный
    источник, и тест читает её из запроса, а не из константы в тесте.
    """
    seen = _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()])
    )

    await search_addresses(QUERY, city_slug=CITY_SLUG)

    timeout = seen[0].extensions.get("timeout")
    assert timeout is not None, "таймаут не дошёл до запроса"
    assert any(
        value == pytest.approx(settings.nominatim_timeout_seconds) for value in timeout.values()
    )
    assert settings.nominatim_timeout_seconds == pytest.approx(3.0)


async def test_timeout_raises_geo_unavailable(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-4: таймаут → `503 GEO_UNAVAILABLE`, не пустой список.

    Краснеет, если ветку обработки заменить на `-> []`: клиент показал бы
    «ничего не найдено» вместо «поиск недоступен, введите вручную».
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("таймаут", request=request)

    _install_transport(monkeypatch, handler)

    with pytest.raises(GeoUnavailableError) as exc:
        await search_addresses(QUERY, city_slug=CITY_SLUG)
    assert exc.value.code == "GEO_UNAVAILABLE"


async def test_server_error_raises_geo_unavailable(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-4: `5xx` провайдера → `503`, кэша нет — вернуть нечего."""
    _install_transport(monkeypatch, lambda request: httpx.Response(503, json={"error": "down"}))

    with pytest.raises(GeoUnavailableError):
        await search_addresses(QUERY, city_slug=CITY_SLUG)


async def test_provider_rate_limit_raises_geo_unavailable(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-4: `429` от Nominatim — тоже недоступность, а не «пусто».

    Свой лимит (AC-6) отдаёт `429 rate_limited` клиенту; `429` ПРОВАЙДЕРА
    означает, что наш IP упёрся в правило OSM, и повторить сейчас нельзя.
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(429))

    with pytest.raises(GeoUnavailableError):
        await search_addresses(QUERY, city_slug=CITY_SLUG)


async def test_empty_result_is_empty_list(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-4: пустой ответ провайдера — `200 []`, это НЕ деградация.

    Пара к `503`: «ничего не найдено» и «поиск недоступен» требуют разных
    действий от пользователя, и сливать их в один ответ значит соврать.
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result == []


@pytest.mark.parametrize("query", ["", " ", "а", "  а  ", "\u00a0б\u00a0"])
async def test_query_too_short_raises(
    query: str, monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-2, Task 1.1: меньше двух символов после нормализации → `422 QUERY_TOO_SHORT`.

    Пробелы и NBSP не считаются символами: `'  а  '` — один символ, и запрос
    ушёл бы в Nominatim как валидный (`class P-27`: «форма прошла проверку,
    а провайдер получил мусор»).
    """
    seen = _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()])
    )

    with pytest.raises(AddressQueryTooShortError) as exc:
        await search_addresses(query, city_slug=CITY_SLUG)
    assert exc.value.code == "QUERY_TOO_SHORT"
    assert seen == [], "запрос с коротким q не должен уходить в сеть"


async def test_global_rate_limit_blocks_second_call(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-3: глобальный лимит 1 req/s — второй вызов за окно отклоняется.

    Краснеет, если убрать `SET NX PX`: провайдер получал бы столько запросов,
    сколько пользователей одновременно печатают адрес, и блокировка по
    правилу OSM (бан IP) выглядела бы как «геокодер недоступен».

    Проверяется ключ и `PX`: миллисекунды, а не секунды — `px=1000` в тесте
    отличается от `ex=1` на два порядка.
    """
    seen = _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()])
    )

    await search_addresses(QUERY, city_slug=CITY_SLUG)
    with pytest.raises(GeoRateLimitedError) as exc:
        await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert len(seen) == 1, "второй запрос не должен уходить в сеть"
    assert exc.value.retry_after >= 1
    assert "address:nominatim_rate" in fake_redis.data
    assert fake_redis.ttl_ms == settings.nominatim_rate_window_ms


async def test_name_falls_back_to_first_display_name_component(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-11: нет `name` — берётся первый компонент `display_name` до запятой.

    Краснеет, если убрать фолбэк: в `places` попало бы пустое имя, а `CHECK`
    `ck_places_name` вернул бы `500` на пользовательском вводе.
    """
    item = _nominatim_item()
    item.pop("name")
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result[0].name == "Кинотеатр «Прага»"


async def test_name_field_wins_over_display_name(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-11: пара к фолбэку — при наличии `name` берётся ИМЕННО он.

    Краснеет, если фолбэк заменить на безусловный разбор `display_name`:
    норма ухудшилась бы (имя объекта точнее первого компонента адреса).
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result[0].name == "Кинотеатр «Прага»"
    assert result[0].address_text.startswith("Кинотеатр «Прага»")


async def test_address_text_truncated_to_column_width_on_word_boundary(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-11: `address_text` усечён до 255 по ГРАНИЦЕ СЛОВА.

    Краснеет, если резать по индексу: `address_text` упирался бы в
    `ck_places_address_text`/ширину колонки и давал `500`, либо обрывался
    посреди слова, и адрес становился нечитаемым.
    """
    long_tail = " ".join(["улица"] * 60)  # заметно длиннее 255 символов
    item = _nominatim_item(display_name=long_tail)
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    text = result[0].address_text
    assert len(text) <= 255
    assert text.endswith("улица"), "обрыв посреди слова: последний токен цел"
    assert not text.endswith(" "), "хвостовой пробел прошёл бы normalize_text только позже"


async def test_short_address_text_is_not_altered(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-11: пара к усечению — короткий `display_name` не меняется.

    Краснеет, если усечение сделать безусловным (`[:255]` без проверки длины
    и без границы слова): короткий адрес получил бы хвост обрезанного слова.
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result[0].address_text == "Кинотеатр «Прага», улица Ленина, 12, Томск, Россия"


async def test_entry_without_name_and_display_name_is_dropped(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-11: оба поля пусты → запись ОТБРАСЫВАЕТСЯ, а не «пустое имя».

    Краснеет, если оставить запись: пользователь увидел бы вариант без
    названия, выбрал бы его, и место не создалось бы (или создалось
    безымянным). «Лучше меньше, но валидное».
    """
    item = _nominatim_item(name="", display_name="")
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result == []


async def test_category_hint_outside_enum_becomes_other(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-9: `category_hint` вне `PlaceCategory` → `other`, не `None`.

    Краснеет, если пробросить как есть: `PlaceCreate.category` типизирован
    enum, и `POST /address/place` падал бы `422` на легальном выборе
    (OSM `class/type` не совпадает с нашим справочником один к одному).
    """
    item = _nominatim_item(**{"class": "highway", "type": "bus_stop"})
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result[0].category_hint == PlaceCategory.OTHER


async def test_category_hint_inside_enum_is_preserved(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-9: пара к `other` — известный тип сохраняется.

    Краснеет, если всегда возвращать `other`: подсказка категории перестала
    бы помогать, и автор вводил бы категорию вручную каждый раз.
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result[0].category_hint == PlaceCategory.CINEMA


async def test_broken_json_raises_geo_unavailable(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-4: нечитаемое тело — недоступность, а не `500`.

    Краснеет, если `response.json()` звать без обработки: `JSONDecodeError`
    не является `httpx.HTTPError` и вышел бы наружу как `500` на ответ
    провайдера — то есть вина пользователя за чужой сбой.
    """
    _install_transport(
        monkeypatch, lambda request: httpx.Response(200, content=json.dumps({}).encode())
    )

    with pytest.raises(GeoUnavailableError):
        await search_addresses(QUERY, city_slug=CITY_SLUG)


async def test_entry_without_display_name_is_dropped(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """AC-11: `name` есть, а `display_name` пуст → запись ОТБРАСЫВАЕТСЯ.

    **Пара к `test_entry_without_name_and_display_name_is_dropped`.** Тот тест
    гасит ОБА поля, поэтому мутация `not name or not display_name` →
    `not name` (M22) выжила: при пустом `name` обе редакции возвращают `None`.
    Здесь `name` заполнен — и разница видна: без проверки `display_name`
    запись прошла бы с пустым `address_text`, а `ck_places_address_text`
    вернул бы `500` при сохранении места.

    Пустой адрес бесполезен и на карте: пользователь не отличит два
    одноимённых заведения без адреса.
    """
    item = _nominatim_item(name="Кинотеатр «Прага»", display_name="")
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result == []
    assert all(suggestion.address_text for suggestion in result)


async def test_infinite_coordinates_are_dropped(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """M-2 CR 2026-10-31: `inf` координата — запись ОТБРАСЫВАЕТСЯ.

    Краснеет при `math.isnan` вместо `math.isfinite`: `float('inf')` не NaN,
    проходит дальше, и `model_dump(mode="json")` отдаёт `Infinity` — невалидный
    JSON (RFC 8259). Браузерный `response.json()` на таком теле падает, то есть
    поиск «ломается молча» без единой ошибки в логах.
    """
    item = _nominatim_item(lat="inf", lon="84.9481")
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result == []


async def test_nan_coordinates_are_dropped(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """M-2: пара к `inf` — `nan` отбрасывается так же (регрессия `isnan`)."""
    item = _nominatim_item(lat="nan", lon="84.9481")
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[item]))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert result == []



# --- CR r2 2026-11-01: закрытие выживших мутаций --------------------------------


async def test_redis_failure_raises_geo_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M06 CR r2: сбой Redis в глобальном лимите → `503`, а НЕ «пропустить».

    Краснеет, если в `except` стоит `return` вместо `raise GeoUnavailableError`:
    при упавшем Redis сервис молча обходил бы лимит OSM и долбил Nominatim
    без ограничения. Правило OSM соблюдается ТОЛЬКО нами, и его нарушение
    стоит блокировки IP для всего продукта.
    """
    class _BrokenRedis:
        async def set(self, *args: object, **kwargs: object) -> bool:
            raise ConnectionError("redis недоступен")

    monkeypatch.setattr(geocoder_module, "get_redis", lambda: _BrokenRedis())
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()]))

    with pytest.raises(GeoUnavailableError):
        await search_addresses(QUERY, city_slug=CITY_SLUG)


async def test_short_query_does_not_touch_global_limit(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """M08 CR r2: короткий запрос отбивается ДО глобального лимита.

    Краснеет при перестановке `_acquire_global_slot()` перед проверкой длины:
    каждый опечатанный символ съедал бы окно 1 req/s, и честный пользователь
    ждал бы секунды из-за чужой опечатки. Проверяется СОСТОЯНИЕ Redis, а не
    факт исключения: `test_query_too_short_raises` смотрел только `seen == []`
    и такую перестановку пропускал.
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=[_nominatim_item()]))

    with pytest.raises(AddressQueryTooShortError):
        await search_addresses("a", city_slug=CITY_SLUG)

    # Ключ лимита НЕ создан: до сети и до Redis дело не дошло.
    assert fake_redis.data == {}


async def test_query_is_normalized_before_sending(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """M09 CR r2: `q` нормализуется ПЕРЕД отправкой в Nominatim.

    Краснеет, если отправить `query` вместо `normalized_query`: `'  Прага  '`
    ушёл бы с пробелами, и провайдер искал бы « Прага » — лишний расход
    лимита и менее точная выдача. Существующий тест сравнивал с `QUERY`,
    который уже нормализован, поэтому подмена была неотличима.
    """
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[_nominatim_item()])

    _install_transport(monkeypatch, handler)

    await search_addresses("  Прага  ", city_slug=CITY_SLUG)

    assert seen[0]["q"] == "Прага", "запрос ушёл с пробелами — нормализация не применена"


async def test_non_dict_items_are_filtered_not_crash(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """M10 CR r2: не-dict элемент в ответе провайдера ОТБРАСЫВАЕТСЯ, не `500`.

    Краснеет, если убрать `isinstance(item, dict)` из `_parse_payload`: строка
    или `null` в списке дали бы `AttributeError` на `.get` — то есть `500` на
    чужом ответе, вина пользователя за сбой провайдера.

    Пара к `test_broken_json_raises_geo_unavailable`: тот проверяет НЕ-список
    целиком, этот — мусор ВНУТРИ списка (разные ветви `_parse_payload`).
    """
    payload = [_nominatim_item(), "мусор", None, 42]
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))

    result = await search_addresses(QUERY, city_slug=CITY_SLUG)

    assert len(result) == 1, "годная запись потеряна или мусор прошёл"
    assert result[0].name == "Кинотеатр «Прага»"
