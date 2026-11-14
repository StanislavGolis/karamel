"""Прокси адресного поиска и автосоздание места (стори 13.1, Task 3).

AC-2, AC-4, AC-5, AC-6, AC-9. Роут не содержит бизнес-логики: параметры
запроса к Nominatim, лимиты и фолбэки живут в `address_geocoder`, дедуп —
в `place_service`. Здесь только HTTP-форма: статусы, конверт ошибки,
`cache-control` и единица работы (`commit`).

**Почему это прокси, а не клиентский вызов OSM.** Ключ атрибуции запроса —
`User-Agent` с контактом; из браузера он ушёл бы с UA браузера, и за
нарушение правила OSM отвечал бы наш IP. Плюс лимит 1 req/s осмыслен только
там, где видно ВСЕ запросы.

**Авторизация обязательна** (AC-2). Анонимный прокси превратил бы сервис
в бесплатный релей к OSM с нашего адреса: любой человек без учётки мог бы
нагрузить чужой сервис через нас.

**Координаты не кэшируются** (`NFR34`). `cache-control: no-store` стоит на
КАЖДОМ ответе, включая успешный: тело `search` содержит `lat`/`lng`, и кэш
прокси сохранил бы их на диск — то есть координата оказалась бы ХРАНИМА.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.db import get_session
from app.repositories.places import PlaceRepository
from app.schemas.address import AddressPlaceCreate, AddressPlaceRead
from app.services.address_geocoder import (
    AddressQueryTooShortError,
    AddressSuggestion,
    GeoRateLimitedError,
    GeoUnavailableError,
    search_addresses,
)
from app.services.join_request_rate import JoinRequestRateLimiter, RateLimiter
from app.services.place_service import (
    PlaceArchivedError,
    PlaceDuplicateError,
    PlaceFieldError,
    PlaceHiddenError,
    PlaceUnknownCommunityError,
    create_or_get_place,
)

router = APIRouter(prefix="/address", tags=["address"])

# AC-5, project-context §4: конверт ошибки `{"code": ...}`.
_NO_STORE = {"cache-control": "no-store"}

# AC-6: ключ лимита аккаунта. Отдельный префикс от подач заявок — иначе
# `INCR` поиска расходовал бы суточный лимит подач (Task 2.3).
_ADDRESS_SEARCH_KEY_PREFIX = "address:search:"

# M-3 CR 2026-10-31: отдельный ключ на СОЗДАНИЕ места. Общий счётчик с поиском
# означал бы, что автор, набравший адрес (10 запросов/мин), не может сохранить
# найденное место — лимит исчерпан действием на другом смысле.
_ADDRESS_PLACE_KEY_PREFIX = "address:place:"


def _error(code: str, status_code: int, details: dict[str, Any] | None = None) -> JSONResponse:
    """Конверт `{"code": ...}` с необязательным `details` (приём `places.py`)."""
    body: dict[str, Any] = {"code": code}
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


def _rate_limited(retry_after: int) -> JSONResponse:
    """AC-6: ТОТ ЖЕ конверт, что `join_requests._rate_limited`.

    Код В НИЖНЕМ регистре (`rate_limited`) — как у `join_requests.py:108`
    и `phone_auth`: клиент ветвится по строке, и второй регистр завёл бы
    вторую ветку обработки на одном смысле.

    `retry_after <= 0` означает «окно неизвестно»: `Retry-After: 0` заставил бы
    клиента повторить немедленно (урок CR 1.4, патч 16).
    """
    if retry_after <= 0:
        retry_after = settings.address_search_rate_window_seconds
    return JSONResponse(
        {"code": "rate_limited", "retry_after": retry_after},
        status_code=429,
        headers={**_NO_STORE, "Retry-After": str(retry_after)},
    )


def get_address_rate_limiter() -> RateLimiter:
    """AC-6: лимитер как зависимость — тесты подставляют фейк без Redis.

    Фабрика, а не модульная константа: значения берутся из настроек на момент
    запроса, что позволяет менять `.env` без пересборки (FR61).
    """
    return JoinRequestRateLimiter(
        limit=settings.address_search_rate_limit,
        window_seconds=settings.address_search_rate_window_seconds,
        key_prefix=_ADDRESS_SEARCH_KEY_PREFIX,
    )


def get_address_place_rate_limiter() -> RateLimiter:
    """M-3 CR 2026-10-31: лимит на СОЗДАНИЕ места, отдельный от поиска.

    **Зачем.** `POST /address/place` пишет в справочник города
    (`is_active=true`, O-1) без модерации (`W-13.1-a`). Без лимита один
    аккаунт создаёт неограниченное число мест: справочник, из которого
    выбирают ВСЕ авторы города, засоряется за минуту.

    **Почему отдельный префикс.** Общий счётчик с поиском означал бы, что
    автор, набравший адрес (10 запросов/мин), не может сохранить найденное
    место — лимит уже исчерпан чужим действием на том же смысле. `INCR`
    одного ключа на два разных действия — класс `D-10.4-1`.

    Порог СВОЙ (`address_place_rate_limit`), а не поисковый: квота поиска
    защищает трафик, квота создания — содержимое справочника. Одно число на
    два разных риска означало бы, что правка частоты поиска молча меняет
    скорость наполнения справочника.
    """
    return JoinRequestRateLimiter(
        limit=settings.address_place_rate_limit,
        window_seconds=settings.address_place_rate_window_seconds,
        key_prefix=_ADDRESS_PLACE_KEY_PREFIX,
    )


@router.get("/city")
async def city_viewbox_endpoint(user: CurrentUser) -> JSONResponse:
    """H-1 CR 2026-10-31: рамка города автора — центр карты ДО выбора места.

    **Зачем отдельный маршрут.** AC-1 требует карту на шаге 2 сразу: с
    тайлами OSM и атрибуцией. Чтобы показать город, клиенту нужен его центр,
    а в браузере его взять неоткуда: `SessionUser` слага не содержит, а
    `places` координат не хранит (`NFR34`). Зашивать координаты Томска в
    бандл нельзя — при втором городе карта показывала бы чужой город.

    **Почему `viewbox` строкой, а не `lat`/`lng`.** Гард
    `test_no_coordinates_fields_in_openapi_responses` запрещает поля
    `lat`/`lng` во всех ответах, кроме `/address/search`. Рамка города — не
    координата объекта: это прямоугольник поиска, и он уходит клиенту в том
    же виде, в каком лежит в настройках и в параметре `viewbox` Nominatim.
    Клиент считает центр сам. Обходить гард исключением ради удобства
    значило бы открыть вторую дверь к тем же данным.

    Города без рамки → `viewbox: null`: карта покажет мир, поиск работает
    по стране (`W-13.1-f`). Врать центром Томска нельзя.
    """
    return JSONResponse(
        {"viewbox": settings.geo_city_viewbox.get(user.city_slug)},
        status_code=200,
        headers=_NO_STORE,
    )


@router.get("/search", response_model=list[AddressSuggestion])
async def search_addresses_endpoint(
    user: CurrentUser,
    limiter: Annotated[RateLimiter, Depends(get_address_rate_limiter)],
    # M-4 CR 2026-10-31: верхняя граница обязательна. Без неё 10-КБ `q`
    # нормализуется и уходит в Nominatim: наш лимит 1 req/s сожжён одним
    # запросом, egress потрачен, а провайдер ответит `414` → `503` для
    # пользователя. 200 — с запасом: самый длинный адрес в OSM короче.
    #
    # `min_length` здесь НЕ ставится намеренно: нижнюю границу проверяет
    # сервис и отдаёт СВОЙ код `QUERY_TOO_SHORT` (AC-2). Валидатор FastAPI
    # ответил бы `invalid_request`, и клиент потерял бы ветку «запрос
    # слишком короткий» — проверено тестом `test_short_query_gets_422`
    # (покраснел ровно так, когда `min_length=2` стоял здесь).
    q: Annotated[str, Query(max_length=200, description="Название или адрес места")],
) -> list[AddressSuggestion] | JSONResponse:
    """AC-2, AC-4, AC-5, AC-6, AC-10: поиск адреса через прокси Nominatim.

    Порядок: лимит аккаунта → сервис (там — короткий запрос и глобальный
    лимит). Лимит аккаунта ПЕРВЫМ намеренно: он дешевле (один `INCR`) и
    защищает от потока, который всё равно не прошёл бы глобальное окно.

    `city_slug` берётся из профиля автора (`user.city_slug`), не из query:
    параметр в запросе позволил бы искать по чужому городу и получать
    координаты мест, которых в своей ленте не увидишь (AC-9, Task 3.4).
    """
    retry_after = await limiter.hit(user.id)
    if retry_after is not None:
        return _rate_limited(retry_after)

    try:
        suggestions = await search_addresses(q, city_slug=user.city_slug)
    except AddressQueryTooShortError:
        # AC-2: `422`, а не `400`: запрос синтаксически валиден, но
        # семантически недостаточен (project-context §4).
        return _error("QUERY_TOO_SHORT", 422)
    except GeoRateLimitedError as error:
        # AC-4: наш собственный 1 req/s — это НЕДОСТУПНОСТЬ, а не «вы слишком
        # часто»: повторять сейчас бесполезно, и клиент должен перейти на
        # ручной ввод. `Retry-After` отдаётся, но статус `503`, не `429`.
        response = _error("GEO_UNAVAILABLE", 503)
        response.headers["Retry-After"] = str(error.retry_after)
        return response
    except GeoUnavailableError:
        # AC-4: кэша нет (решение 1) — вернуть нечего. Текст запроса остаётся
        # у клиента в поле ввода (`ux-design-v2:913`).
        return _error("GEO_UNAVAILABLE", 503)

    return JSONResponse(
        [suggestion.model_dump(mode="json") for suggestion in suggestions],
        status_code=200,
        headers=_NO_STORE,
    )


@router.post("/place", response_model=AddressPlaceRead)
async def create_place_from_address_endpoint(
    payload: AddressPlaceCreate,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[RateLimiter, Depends(get_address_place_rate_limiter)],
) -> AddressPlaceRead | JSONResponse:
    """AC-9: автосоздание места по результату поиска — тонкая обёртка.

    Ноль нового SQL: дедуп, нормализация и вставка живут в `place_service`
    (`find_by_city_and_name:260`, `create_place`, `is_duplicate_name_error:415`).

    `city_slug` — из профиля автора, не из тела (Task 3.4): иначе клиент
    создал бы место в чужом городе, и оно было бы невидимо в ленте, но заняло
    бы имя в `uq_places_city_name`.

    Скрытое место → `422 PLACE_HIDDEN` БЕЗ `existing_uuid`: администраторский
    `POST /places` отдаёт `409 PLACE_ARCHIVED` с `existing_uuid` (он
    восстанавливает место), а автору чужое скрытое место не касается — UUID
    отдавать нельзя, иначе перебором имён можно узнать, какие скрытые места
    существуют (D-6).
    """
    # M-3 CR 2026-10-31: лимит ПЕРВЫМ — до обращения к БД. Иначе поток
    # запросов жёг бы соединения и транзакции, не создавая мест.
    retry_after = await limiter.hit(user.id)
    if retry_after is not None:
        return _rate_limited(retry_after)

    try:
        place, created = await create_or_get_place(
            session,
            name=payload.name,
            category=payload.category_hint,
            address_text=payload.address_text,
            city_slug=user.city_slug,
            settings=settings,
        )
    except PlaceHiddenError:
        # Решение 3 (2026-10-31): реактивации нет — `uq_places_city_name`
        # не включает `is_active`, поэтому имя занято и создать место нельзя.
        return _error("PLACE_HIDDEN", 422)
    except PlaceArchivedError:
        # H-2 CR 2026-10-31: ОКНО ГОНКИ между `find_by_city_and_name` и
        # `create_place`. Обёртка проверила имя и не нашла строки, но внутри
        # `create_place` проверка повторяется, и второй автор успевает создать
        # скрытое место. `PlaceArchivedError` несёт `existing_uuid` — для
        # администратора это полезно (он восстанавливает место), для автора
        # карточки это перебор имён (D-6, решение 3): `422` без UUID,
        # тот же ответ, что и при обычном попадании в скрытое.
        return _error("PLACE_HIDDEN", 422)
    except PlaceDuplicateError:
        # H-2 CR 2026-10-31: второй исход гонки — активное место успел создать
        # ДРУГОЙ автор. Для этого автора это УСПЕХ, а не конфликт: он выбрал
        # это место, и оно теперь существует. `409 PLACE_DUPLICATE` — код
        # администратора («придумайте другое имя»); автору он не годится.
        # Перечитываем и отдаём `200 {created: false}` — тот же ответ, что при
        # повторном выборе существующего места (AC-9).
        existing = await PlaceRepository(session).find_by_city_and_name(
            user.city_slug, payload.name
        )
        if existing is None:
            # H-2 CR 2026-10-31: строки нет — гонка была не с активным местом
            # (например, скрытое отфильтровано позже). Честный отказ вместо
            # выдуманного UUID: клиент сохранил бы карточку со ссылкой на
            # место, которого нет, и получил бы `500` на создании карточки.
            # `409 PLACE_CONFLICT` — «состояние изменилось, повторите»: это
            # не ошибка ввода (`422`), а конфликт с параллельным действием.
            return _error("PLACE_CONFLICT", 409)
        return AddressPlaceRead(place_uuid=existing.uuid, created=False)
    except (PlaceFieldError, PlaceUnknownCommunityError):
        # `place_invalid` — код соседнего маршрута `POST /places` для того же
        # класса ошибок: клиент не должен учить второй код на один смысл.
        return _error("place_invalid", 422)

    await session.commit()
    return AddressPlaceRead(place_uuid=place.uuid, created=created)
