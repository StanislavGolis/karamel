"""Адресный геокодер через прокси Nominatim (стори 13.1, Task 1).

AC-2, AC-3, AC-5, AC-10, AC-11. Прокси, а не клиентский вызов: ключ
атрибуции запроса к OSM — наш `User-Agent` с контактом, и запрос из браузера
ушёл бы с чужим UA, за который отвечает владелец браузера. Плюс лимит
1 req/s имеет смысл только на стороне, которая видит ВСЕ запросы.

**Границы доверия (NFR34, AC-5).** `lat`/`lng` живут только в ответе API и
в состоянии компонента: не в `places`, не в Redis, не в structlog.
`SENSITIVE_FIELDS` в `core/logging.py` уже вырезает их из логов — расширять
не нужно, но и обходить фильтр (логировать координату внутри строки)
нельзя.

**Кэша НЕТ** (решение владельца 1, 2026-10-31). Ответ Nominatim содержит
`display_name` и координаты — то есть ПД, — и хранение их в Redis сделало
бы кэш вторым хранилищем персональных данных без срока и без основания.
Цена решения: недоступность провайдера = честный `503`, а текст запроса
остаётся у пользователя в поле ввода (`ux-design-v2:913`).
"""

from __future__ import annotations

import math
from typing import Any, Final

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.core.redis import get_redis
from app.models.enums import PlaceCategory
from app.services.profile_normalize import normalize_text

logger = structlog.get_logger(__name__)

# AC-2, Task 1.1: минимум два символа ПОСЛЕ нормализации. Один символ даёт
# десятки совпадений на всю страну, и прокси превратился бы в способ дёшево
# нагрузить OSM через нас.
MIN_QUERY_LENGTH: Final = 2

# AC-10: `limit=5` — пять вариантов закрывают выбор в форме и не заставляют
# пользователя листать; большее число увеличивает ответ и трафик к OSM при
# том же результате.
NOMINATIM_LIMIT: Final = 5

# AC-11: ширина колонки `places.address_text` (`VARCHAR(255)`). Усечение здесь,
# а не в БД: `CHECK`-нарушение дало бы `500` на ответе провайдера.
ADDRESS_MAX_LENGTH: Final = 255

# AC-3: ключ глобального лимита. Константа, а не `user_id`: правило OSM
# ограничивает НАШ IP, а не аккаунт, поэтому окно общее на процесс
# (`uvicorn` без `--workers`, замер 12 — значит и глобальное).
_RATE_KEY: Final = "address:nominatim_rate"

# AC-9: сопоставление OSM `class/type` с нашим справочником. Приблизительное
# по определению: справочники разные, и точного соответствия не существует.
# `dict`, а не цепочка `if`: два источника правды разошлись бы при первой
# правке, и подсказка стала бы зависеть от порядка проверок.
_CATEGORY_BY_OSM: Final[dict[tuple[str, str], PlaceCategory]] = {
    ("amenity", "cinema"): PlaceCategory.CINEMA,
    ("amenity", "cafe"): PlaceCategory.CAFE,
    ("amenity", "restaurant"): PlaceCategory.CAFE,
    ("amenity", "bar"): PlaceCategory.OTHER,
    ("amenity", "pub"): PlaceCategory.OTHER,
    ("leisure", "park"): PlaceCategory.PARK,
    ("leisure", "ice_rink"): PlaceCategory.SKATING,
    ("leisure", "sports_centre"): PlaceCategory.SPORT,
    ("leisure", "fitness_centre"): PlaceCategory.SPORT,
    ("tourism", "attraction"): PlaceCategory.WALK,
}

#: Транспорт как модульная переменная — единственная точка подмены в тестах.
#: `None` создаётся лениво: импорт модуля не должен открывать сокеты, иначе
#: любой тест без сети падал бы на сборке импортов (тот же приём, что в
#: `services/oauth`).
_client: httpx.AsyncClient | None = None


class AddressQueryTooShortError(Exception):
    """AC-2: запрос короче двух символов после нормализации."""

    code = "QUERY_TOO_SHORT"


class GeoUnavailableError(Exception):
    """AC-4: провайдер недоступен (таймаут, `5xx`, `429`, нечитаемое тело).

    Кэша нет, вернуть нечего — поэтому `503`, а не пустой список: «ничего не
    найдено» и «поиск недоступен» требуют разных действий от пользователя.
    """

    code = "GEO_UNAVAILABLE"


class GeoRateLimitedError(Exception):
    """AC-3: наш собственный глобальный лимит 1 req/s исчерпан.

    `retry_after` — остаток окна в секундах: клиент повторит запрос позже,
    а не немедленно (`Retry-After: 0` заставил бы крутиться в цикле).
    """

    code = "GEO_UNAVAILABLE"

    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class AddressSuggestion(BaseModel):
    """AC-5: одна подсказка адреса. Координаты — ТОЛЬКО в ответе этого маршрута.

    `lng`, а не `lon`: Nominatim отдаёт `lon`, но весь остальной проект
    (и `SENSITIVE_FIELDS`, и клиент) говорит `lng`. Проброс `lon` как есть
    оставил бы координату за пределами известного имени поля — клиент
    прочитал бы `undefined` молча.

    `display_name` отдаётся отдельно от `address_text`: Nominatim кладёт туда
    всю иерархию (объект, улица, дом, город, страна), и обрезать его до
    `address_text` значит терять то, что пользователю нужно для выбора между
    двумя похожими вариантами.
    """

    # AC-5: лишние поля запрещены — ответ этого маршрута единственный, где
    # координаты легальны, и появление здесь `id` или `place_id` расширило бы
    # контракт без решения.
    model_config = ConfigDict(extra="forbid")

    name: str
    address_text: str
    display_name: str
    category_hint: PlaceCategory
    lat: float
    lng: float


def get_http_client() -> httpx.AsyncClient:
    """AC-3: один клиент на процесс, таймаут — из настроек.

    Клиент на каждый запрос открывал бы новое соединение (TLS-рукопожатие
    к чужому хосту — самая дорогая часть) и не переиспользовал пул.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.nominatim_timeout_seconds)
    return _client


async def dispose_http_client() -> None:
    """Закрывает клиент на остановке приложения (рядом с `dispose_redis`)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _acquire_global_slot() -> None:
    """AC-3: `SET NX PX 1000` — ровно один запрос к Nominatim в секунду.

    Без `asyncio.Lock` (замер 12: `uvicorn` без `--workers` = один процесс,
    значит `SET NX` сам по себе глобален). С `Lock` было бы два механизма на
    одно свойство, и они разошлись бы при переходе на несколько воркеров
    (тогда `Lock` перестал бы работать молча, а `SET NX` — нет).

    Сбой Redis — `503`: в отличие от лимита подач (NFR17, анти-абьюз), здесь
    недоступность хранилища означает, что мы НЕ ЗНАЕМ, не нарушаем ли
    правило OSM, а нарушение стоит блокировки IP для всего сервиса.
    """
    client = get_redis()
    try:
        # M-1 CR 2026-10-31: `1` — ЛИТЕРАЛ, а не настройка. Правило OSM —
        # один запрос в секунду на IP; это константа протокола, а не
        # параметр развёртывания. Настройка `nominatim_rate_limit` удалена:
        # она не читалась и создавала ложное впечатление управления.
        # Окно (`nominatim_rate_window_ms`) остаётся: его меняют при
        # отладке на песочнице OSM.
        allowed = await client.set(_RATE_KEY, 1, nx=True, px=settings.nominatim_rate_window_ms)
        if allowed:
            return
        ttl_ms = await client.pttl(_RATE_KEY)
    except Exception as error:  # noqa: BLE001 — RedisError и его подклассы
        # Redis недоступен: пропускать запрос нельзя (правило OSM соблюдается
        # только нами), поэтому отказ, а не «на всякий случай разрешим».
        logger.warning("address.geocoder.rate.failed", error=str(error))
        raise GeoUnavailableError from error
    retry_after = math.ceil(ttl_ms / 1000) if ttl_ms > 0 else 1
    raise GeoRateLimitedError(retry_after=max(1, retry_after))


def _truncate_on_word_boundary(text: str, limit: int) -> str:
    """AC-11: усечение до `limit` по границе слова.

    Обрыв посреди слова делает адрес нечитаемым, а обрыв по индексу без
    поиска пробела — именно это и делает `[:limit]`. Хвостовой пробел
    снимается: `CHECK` в БД требует `btrim`, и значение с пробелом упало бы
    `500` при создании места.

    Одно слово длиннее лимита обрезается по индексу: альтернатива — пустая
    строка, то есть потеря адреса целиком.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip()


def _category_hint_of(item: dict[str, Any]) -> PlaceCategory:
    """AC-9: подсказка категории из OSM `class/type`; вне справочника → `other`.

    Подсказка, а не решение: автор видит её в форме и может исправить.
    `other` вместо `None` — потому что `PlaceCreate.category` типизирован
    `PlaceCategory` и `None` дал бы `422` на легальном выборе.
    """
    key = (str(item.get("class", "")), str(item.get("type", "")))
    return _CATEGORY_BY_OSM.get(key, PlaceCategory.OTHER)


def _coordinates_of(item: dict[str, Any]) -> tuple[float, float] | None:
    """AC-5: `lat`/`lon` строкой от Nominatim → числа; мусор отбрасывается.

    Nominatim отдаёт координаты СТРОКАМИ (`"56.4977"`). Приведение здесь,
    а не в схеме Pydantic: строка с мусором (`"abc"`) дала бы `ValidationError`
    на чужом ответе, то есть `500`.
    """
    try:
        lat = float(item["lat"])
        lng = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    # M-2 CR 2026-10-31: `isfinite`, а не `isnan`. `float('inf')` НЕ NaN,
    # поэтому проходил дальше, а `model_dump(mode="json")` отдаёт `Infinity` —
    # это не валидный JSON (RFC 8259): браузерный `response.json()` падает,
    # и поиск «ломается молча» без единой ошибки в логах.
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    return lat, lng


def _to_suggestion(item: dict[str, Any]) -> AddressSuggestion | None:
    """AC-11: запись Nominatim → подсказка; `None` = запись отбрасывается.

    Три фолбэка, каждый с парой тестов:

    1. `name` ← `name` или первый компонент `display_name` до запятой. Нет ни
       того, ни другого → запись отбрасывается: в `places` попало бы пустое
       имя, а `ck_places_name` вернул бы `500` на пользовательском вводе.
    2. `address_text` ← `display_name`, усечённый до 255 по границе слова.
    3. Нет координат → запись отбрасывается: выбрать место на карте без точки
       нельзя, а контракт требует `lat`/`lng` непустыми.
    """
    display_name = normalize_text(str(item.get("display_name") or ""))
    name = normalize_text(str(item.get("name") or ""))
    if not name:
        name = display_name.split(",")[0].strip()
    if not name or not display_name:
        return None
    coordinates = _coordinates_of(item)
    if coordinates is None:
        return None
    lat, lng = coordinates
    return AddressSuggestion(
        name=_truncate_on_word_boundary(name, 120),
        address_text=_truncate_on_word_boundary(display_name, ADDRESS_MAX_LENGTH),
        display_name=display_name,
        category_hint=_category_hint_of(item),
        lat=lat,
        lng=lng,
    )


def _viewbox_for(city_slug: str) -> str | None:
    """AC-10 (O-5): рамка города, если она настроена.

    **`None`, а не `503`.** Первая редакция падала здесь, и это ломало город
    без рамки: в `CITY_SLUGS` пилота два города, рамка была одна, и живой
    поиск во втором возвращал отказ при исправном Nominatim. Рамка —
    *уточнение* поиска, а не его условие: страну ограничивает
    `countrycodes=ru`, поэтому поиск без рамки находит места в России, а не
    «весь мир» (проверено поведением Nominatim: `bounded` без `viewbox`
    провайдер игнорирует).

    Отсутствие рамки остаётся видимым: `WARNING` в логе, чтобы город без
    рамки не превращался в молчаливую тишину при разборе жалоб.
    """
    viewbox = settings.geo_city_viewbox.get(city_slug)
    if viewbox is None:
        logger.warning("address.geocoder.viewbox.missing", city_slug=city_slug)
    return viewbox


def _parse_payload(payload: object) -> list[dict[str, Any]]:
    """AC-4: тело ответа — список словарей; иначе недоступность.

    Nominatim при ошибке отвечает `200` с объектом `{"error": ...}` (не
    списком). Без этой проверки итерация по словарю дала бы ключи строками
    и упала бы на `.get` — `500` на чужом ответе.
    """
    if not isinstance(payload, list):
        logger.warning("address.geocoder.unexpected_payload", kind=type(payload).__name__)
        raise GeoUnavailableError
    return [item for item in payload if isinstance(item, dict)]


async def search_addresses(query: str, *, city_slug: str) -> list[AddressSuggestion]:
    """AC-2, AC-3, AC-10, AC-11: поиск адреса через прокси Nominatim.

    Порядок шагов намеренный: короткий запрос отбивается ДО лимита (иначе
    опечатка расходовала бы глобальное окно), лимит — до сети.

    Идентификатора у подсказки нет намеренно: `place_id` Nominatim — чужой
    ключ, и адресовать по нему место нельзя.
    """
    normalized_query = normalize_text(query)
    if len(normalized_query) < MIN_QUERY_LENGTH:
        raise AddressQueryTooShortError

    await _acquire_global_slot()

    params = {
        "q": normalized_query,
        # AC-10 (O-5): только Россия — иначе первым результатом на «Прага»
        # пришёл бы кинотеатр в Чехии.
        "countrycodes": "ru",
        "format": "jsonv2",
        "limit": str(NOMINATIM_LIMIT),
        "accept-language": "ru",
        "addressdetails": "1",
    }

    # AC-10 (O-5): рамка города + `bounded=1` — жёсткая отсечка, а не
    # «предпочтение»: без `bounded` провайдер вернул бы совпадения по всей
    # стране. Пара добавляется ТОЛЬКО вместе: `bounded` без `viewbox`
    # провайдер игнорирует, а `viewbox` без `bounded` — лишь подсказка ранга,
    # и место из другого города всё равно попало бы в ответ.
    viewbox = _viewbox_for(city_slug)
    if viewbox is not None:
        params["viewbox"] = viewbox
        params["bounded"] = "1"

    try:
        response = await get_http_client().get(
            f"{settings.nominatim_base_url.rstrip('/')}/search",
            params=params,
            headers={"User-Agent": settings.nominatim_user_agent},
            timeout=settings.nominatim_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        # AC-4: таймаут, `4xx`, `5xx`, `429` — всё одна ветка. Причина в логе
        # (без координат и без текста запроса: запрос содержит адрес, а адрес —
        # персональные данные, `NFR34`), пользователю — `503`.
        logger.warning(
            "address.geocoder.unavailable",
            error=type(error).__name__,
            status=getattr(getattr(error, "response", None), "status_code", None),
        )
        raise GeoUnavailableError from error

    try:
        payload = response.json()
    except ValueError as error:
        # AC-4: `JSONDecodeError` не является `httpx.HTTPError` и вышел бы
        # наружу как `500` на ответе провайдера — то есть вина пользователя
        # за чужой сбой.
        logger.warning("address.geocoder.broken_json")
        raise GeoUnavailableError from error

    suggestions: list[AddressSuggestion] = []
    for item in _parse_payload(payload):
        suggestion = _to_suggestion(item)
        if suggestion is not None:
            suggestions.append(suggestion)
    return suggestions
