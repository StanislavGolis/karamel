"""Точка входа FastAPI «Карамели».

Стори 1.1: каркас приложения и health-эндпоинт.
Health не обращается к БД и Redis — иначе старт зависит от их готовности
(проверка зависимостей появится в стори 8.6, NFR37).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.api.v1.health import router as health_router
from app.core.body_limit import MULTIPART_OVERHEAD, BodySizeLimitMiddleware
from app.core.config import settings
from app.core.db import dispose_engine
from app.core.logging import configure_logging
from app.core.no_store import NoStoreMiddleware
from app.core.redis import dispose_redis
from app.models.enums import PlaceCategory
from app.services.oauth import dispose_http_client
from app.services.place_service import (
    PLACE_COMMUNITY_IMMUTABLE_CODE,
    PLACE_UNKNOWN_COMMUNITY_CODE,
)
from app.services.ws_broker import ws_shutdown, ws_startup

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Закрывает пулы при остановке — иначе воркеры висят до таймаута.

    HTTP-клиент OAuth закрывается здесь же: пул соединений к провайдеру,
    переживший приложение, держит сокеты до сборки мусора.
    """
    configure_logging()
    # Стори 6.5 (Task 5.1): подписка на `ws:chat:*` ОДИН раз на процесс.
    # Не бросает: без Redis чат деградирует до локальной доставки (Task 5.2).
    await ws_startup()
    yield
    await ws_shutdown()
    await dispose_http_client()
    await dispose_redis()
    await dispose_engine()


# В production документация закрыта целиком: и /docs, и /redoc, и /openapi.json.
# Иначе схема остаётся публичной и раскрывает все эндпоинты, что обесценивает скрытие /docs.
_docs_enabled = not settings.is_production

app = FastAPI(
    title="Карамель API",
    version="0.1.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# Фикс OOM (2026-10-22): гард размера тела.
# `add_middleware` делает `user_middleware.insert(0, ...)`, а `build_middleware_stack`
# собирает стек через `reversed` — поэтому ПЕРВЫЙ добавленный оказывается
# ВНУТРИ последнего (проверено на starlette 1.9: `CORS > BodySizeLimit >
# ExceptionMiddleware`). Для preflight это и нужно: `OPTIONS` без тела
# обрабатывает CORS, не доходя до гарда. Если гард понадобится вынести наружу
# CORS, добавлять его надо ПОСЛЕДНИМ, а не первым.
#
# Стори 2.2, Task 8.3 (AC-08): карта `overrides` даёт загрузке фото собственный
# лимит. `MULTIPART_OVERHEAD` обязателен: тело multipart больше самого файла
# на границы частей и заголовки, и лимит, равный `MAX_PHOTO_BYTES`, отбивал бы
# `413` легальный файл ровно в лимит. Точный размер файла проверяет
# `photo_validation` (Task 4.2), а не гард. Порядок относительно CORS не меняется.
# CR 2.2, патч 7: путь вынесен в константу и связан с реальными маршрутами
# тестом `test_photos_override_matches_real_route`. Литерал внутри
# `add_middleware` ничем не был связан с роутером: смена префикса или пути
# оставляла тесты гарда зелёными (они подают путь сами), а прод получал `413`
# на легальном фото — ровно тот дефект, который закрывает AC-08.
PHOTOS_UPLOAD_PATH = "/api/v1/profiles/me/photos"

# Стори 2.5, Task 2.3 (D-5): замена фото адресуется `PUT /me/photos/{uuid}`,
# а карта `overrides` сравнивается с путём ТОЧНО — маршрут с параметром
# получал бы общий лимит 1 МиБ и отбивал бы легальное фото `413`. Ключ `/*`
# разворачивается в префиксное правило ПОСЛЕ точного совпадения
# (`BodySizeLimitMiddleware._limit_for`), поэтому `POST /me/photos` сохраняет
# свой лимит, а все прочие маршруты — общий: защита от OOM не ослабевает.
PHOTOS_ITEM_PATH_PREFIX = f"{PHOTOS_UPLOAD_PATH}/*"

# Стори 3.2, AC-07: префикс путей справочника мест. Ветка «доменные коды полей
# из Pydantic» в `validation_error_handler` ограничена этим префиксом: у анкеты
# (`ProfileCreate`) поле `name` тоже есть, но её коды — предмет стори 2.3,
# и общий маппинг изменил бы её ответы.
PLACES_PATH_PREFIX = "/api/v1/places"

# Стори 4.5, AC-05: префикс ленты карточек. `category` — общий query-параметр
# ленты и справочника мест (`PlaceCategory` — единственный белый список,
# ADR-12), и клиент обязан получить ОДИН код ошибки на один и тот же мусор.
# Без этого `/cards?category=movies` отдавал бы `invalid_request` без перечня
# допустимых значений, и выпадашка фильтра в ленте не смогла бы его показать.
#
# Ветка ограничена префиксом и здесь: `category` живёт только в query ленты
# (у карточки в теле — `EventCardCreate.category`, но там `extra="forbid"`
# и `422` приходит из схемы, а не из этого маппинга). Префикс `/api/v1/cards`
# не покрывает `/api/v1/moderation/cards` (другой корень) — очередь модерации
# не начнёт отдавать перечень категорий на свои ошибки.
CARDS_FEED_PATH_PREFIX = "/api/v1/cards"

# AC-07: пределы полей места те же, что в схемах и колонках (`schemas/place.py`).
# Импортировать их сюда нельзя без цикла (`main` → `schemas` → `models`), поэтому
# значения повторены как КЛЮЧИ маппинга: расхождение поймано тестом
# `test_place_field_limits_match_schemas`. Значения пределов берутся из
# `pydantic`-ошибки, а не из этой карты.
_PLACE_FIELD_LIMITS: dict[str, int] = {"name": 120, "address_text": 255}

# QD A6 (CR 3.2): `no-store` на КАЖДОМ ответе, включая `307` от
# `redirect_slashes`. Редирект генерирует роутер Starlette ДО обработчиков
# исключений, поэтому ни `_error()` в роутерах, ни `http_error_handler` на него
# не влияют. `add_middleware` вставляет в НАЧАЛО списка, а сборка идёт через
# `reversed` — значит этот middleware окажется ГЛУБЖЕ CORS и гарда тела, то
# есть увидит ответы всех маршрутов, не мешая preflight (комментарий выше).
app.add_middleware(NoStoreMiddleware)

app.add_middleware(
    BodySizeLimitMiddleware,
    max_body_bytes=settings.max_request_body_bytes,
    overrides={
        PHOTOS_UPLOAD_PATH: settings.max_photo_bytes + MULTIPART_OVERHEAD,
        PHOTOS_ITEM_PATH_PREFIX: settings.max_photo_bytes + MULTIPART_OVERHEAD,
    },
)

# BFF-прокси Next обращается к API с той же машины; origin ограничен WEB_ORIGIN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    # CR 2.5, патч P-7: `PUT` добавлен вместе с маршрутом замены фото
    # (`PUT /me/photos/{uuid}`). Через BFF preflight не возникает (вызов
    # серверный, same-origin), поэтому дефект латентный, но любой прямой
    # кросс-origin вызов нового метода получал бы отказ preflight при
    # рабочем маршруте.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["content-type", "authorization", "x-request-id"],
)


# Task 6.5: единственное место, где добавляется версия API.
app.include_router(api_router, prefix="/api/v1")


# CR 2.3, патч 13: код ошибки выводится из статуса, а не из текста `detail`.
# Третий конверт `{"code": "error", "message": ...}` был бессмысленным для
# клиента (он разбирает только `code`) и выносил наружу внутренний текст
# исключения. Здесь закрытый набор: незнакомый статус остаётся `error`,
# но и он не раскрывает `str(detail)`.
_STATUS_CODES: dict[int, str] = {
    status.HTTP_401_UNAUTHORIZED: "unauthenticated",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    # QD A5 (CR 3.2): `GET /api/v1/places` (выдача — в 3.3) отдавал `{"code": "error"}` —
    # тот же код, что у любого незнакомого статуса, то есть «метод не поддержан»
    # неотличим для клиента от прочих сбоев. Один статус — один код (как у 413).
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    # Фикс OOM (2026-10-22): без этой строки `HTTPException(413)` из домена
    # отдавал бы `{"code": "error"}`, хотя гард тела отвечает
    # `{"code": "request_too_large"}` на тот же статус. Один статус — один код.
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "request_too_large",
    status.HTTP_429_TOO_MANY_REQUESTS: "too_many_requests",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}

# CR 2.3, патч 14: `no-store` ставится и на ошибках. `profiles.py` объявляет
# инвариант «заголовок на каждом ответе», но 401/403/422 рождаются здесь —
# промежуточный кэш иначе мог бы отдать 401 другому пользователю.
_NO_STORE = {"cache-control": "no-store"}


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Task 1 (AC-05): единый контракт ошибки `{"code": ...}` для зависимостей.

    Зависимость авторизации не может вернуть `JSONResponse` — она только
    бросает исключение. Без этого обработчика `/auth/me` отдавал бы
    `{"detail": {"code": "unauthenticated"}}`, а роуты рядом — `{"code": ...}`:
    клиент разбирал бы две разные формы одной и той же ошибки.
    """
    headers = dict(_NO_STORE)
    if exc.headers:
        headers.update(exc.headers)
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(detail, status_code=exc.status_code, headers=headers)
    code = _STATUS_CODES.get(exc.status_code, "error")
    return JSONResponse({"code": code}, status_code=exc.status_code, headers=headers)


# 404/405 поднимает `starlette.exceptions.HTTPException` (в отличие от
# fastapi-класса), и раньше они уходили в дефолтный обработчик с формой
# `{"detail": "Not Found"}` — без `code` и без `no-store`. Клиент разбирает
# только `code` и на опечатке в пути получал неразобранное тело.
@app.exception_handler(StarletteHTTPException)
async def starlette_http_error_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Тот же конверт `{code}` для ошибок роутера и методов (404/405)."""
    code = _STATUS_CODES.get(exc.status_code, "error")
    headers = dict(_NO_STORE)
    if exc.headers:
        headers.update(exc.headers)
    return JSONResponse({"code": code}, status_code=exc.status_code, headers=headers)


def _is_category_scope_error(request: Request, exc: RequestValidationError) -> bool:
    """CR 4.5 (AC-05): где именно `category_invalid` — доменный код.

    Два разных источника одного имени поля:

    - **`/places`** — `PlaceCreate`/`PlaceUpdate`: `category` в ТЕЛЕ, и перечень
      допустимых значений нужен админке (CR 3.1, патч 9). Поведение 3.2
      сохраняется без изменений — там проверяется и тело, и query;
    - **`/cards`** — `category` только в QUERY ленты. В теле `POST /cards`
      поле тоже есть (`EventCardCreate`), но его ошибка обязана остаться
      `invalid_request`: это форма создания, а не фильтр, и `details.allowed`
      с перечнем категорий не помогает исправить тело.

    Без этого сужения расширение 4.5 меняло ответ 4.2: найденное код-ревью
    исполнением — `POST /cards` с `category: 'movies'` в теле отдавал
    `category_invalid` вместо `invalid_request`, то есть стори 4.5 меняла
    контракт уже одобренной стори.

    Проверка идёт по `loc[0]`: Pydantic кладёт источник первым элементом
    (`("query", "category")` или `("body", "category")`). Если хотя бы одна
    ошибка категории пришла из тела — ветка доменного кода НЕ применяется.
    """
    if not request.url.path.startswith((PLACES_PATH_PREFIX, CARDS_FEED_PATH_PREFIX)):
        return False
    # `/places` — как было в 3.2, без сужения по источнику.
    if request.url.path.startswith(PLACES_PATH_PREFIX):
        return True
    # `/cards` — только ошибка query-параметра ленты.
    for error in exc.errors():
        loc = error.get("loc") or ()
        if loc and loc[-1] == "category" and loc[0] == "query":
            return True
    return False


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """D-9: 422 от Pydantic приводится к закрытому контракту `{"code": ...}`.

    Без этого обработчика клиент, разбирающий только `code`, падал бы на
    необработанной форме `{"detail": [...]}`: два разных формата ошибки
    для одного и того же пользовательского ввода.
    """
    # Поле `phone` в теле — самый частый случай: отдаём доменный код.
    fields = {err.get("loc", [])[-1] for err in exc.errors() if err.get("loc")}
    if fields == {"phone"}:
        code = "invalid_phone"
    # Стори 2.3, Task 6.6 (AC-06): координаты вне глобуса — доменный код.
    # Без него `lat=91` давал бы общий `invalid_request`, и клиент не мог
    # отличить «сломанную форму» от «геолокация вернула мусор»: первый случай
    # требует исправить ввод, второй — повторить определение города.
    elif fields and fields <= {"lat", "lng", "accuracy"}:
        code = "invalid_coordinates"
    # CR 3.1, патч 9 (AC-02): категория места вне белого списка — доменный код
    # И перечень допустимых значений. AC-02 требует «`422` с перечнем
    # допустимых категорий», а обработчик до этого отдавал только
    # `invalid_request` без списка: клиент 3.2 не мог показать админу
    # выпадашку. Перечень берётся из `PlaceCategory` — единственного источника
    # (D-2), а не переписывается строкой здесь.
    # QD A3 (CR 3.2): ветка ограничена путём места. До правки `category_invalid`
    # срабатывал на ЛЮБОМ маршруте с полем `category`: как только категория
    # появится у карточки события (эпик 4), чужие эндпоинты начали бы отдавать
    # код и перечень категорий ЧУЖОГО домена. Сейчас безопасно только потому,
    # что `category` есть лишь в схемах места (проверено grep по `app/schemas/`).
    elif "category" in fields and _is_category_scope_error(_request, exc):
        return JSONResponse(
            {
                "code": "category_invalid",
                "details": {"allowed": [str(item) for item in PlaceCategory]},
            },
            status_code=422,
            headers=_NO_STORE,
        )
    else:
        # Стори 3.2, AC-07: «форма ответа одинакова от Pydantic и от сервиса».
        # Проверки длины в схемах места ловят часть ввода РАНЬШЕ сервиса, и без
        # этой ветки админ получал бы `invalid_request` на пустое имя там, где
        # на другом пути (воркер, импорт) сервис отвечает `name_required`.
        # Ветка ограничена путями места: у анкеты (`ProfileCreate`) `name` —
        # необязательное к правке поле со своими кодами, и общий маппинг
        # изменил бы её ответы (FR9, стори 2.3).
        place_error = _place_field_error(_request, exc)
        if place_error is not None:
            body, status_code = place_error
            return JSONResponse(body, status_code=status_code, headers=_NO_STORE)
        code = "invalid_request"
    return JSONResponse({"code": code}, status_code=422, headers=_NO_STORE)


def _place_field_error(
    request: Request, exc: RequestValidationError
) -> tuple[dict[str, object], int] | None:
    """Стори 3.2, AC-07: доменные коды полей места из ошибок Pydantic.

    Границы и длины объявлены в схемах (`min_length`/`max_length`), поэтому
    часть невалидного ввода отбивает Pydantic — ДО сервиса. AC-07 требует
    одинаковой формы ответа на обоих путях, иначе клиент ветвился бы по двум
    разным кодам одного и того же нарушения.

    Область строго `/api/v1/places`: у `ProfileCreate` поле `name` тоже есть,
    но его коды (`name_required` для анкеты) — предмет стори 2.3, и менять их
    здесь нельзя.
    """
    if not request.url.path.startswith(PLACES_PATH_PREFIX):
        return None

    # Первая ошибка решает: остальные поля формы админ исправит за тем же
    # проходом, а склейка двух кодов в один ответ сломала бы контракт `{code}`.
    for error in exc.errors():
        loc = error.get("loc") or ()
        field = loc[-1] if loc else None
        error_type = str(error.get("type", ""))

        # QD A4 (R-05): перенос места между сообществами запрещён. Поле убрано из
        # `PlaceUpdate`, но старый клиент или редизайн формы может его прислать;
        # без этой ветки ответ был общим `invalid_request`, и «перенос запрещён»
        # выглядел как сломанная форма.
        if field == "community_slug" and error_type == "extra_forbidden":
            return {"code": PLACE_COMMUNITY_IMMUTABLE_CODE}, 422

        # CR 3.2, патч 4 (AC-07): слаг, не проходящий `COMMUNITY_SLUG_PATTERN`,
        # отбивается Pydantic, а сервис на том же классе нарушения отвечает
        # `PLACE_UNKNOWN_COMMUNITY`. Без этой ветки `"Tomsk"` давал
        # `invalid_request`, а `"zzzz"` — `PLACE_UNKNOWN_COMMUNITY`, то есть два
        # кода на одно действие админа («сообщество не то»). Форма ответа
        # обязана совпадать на обоих путях.
        if field == "community_slug" and error_type == "string_pattern_mismatch":
            return {"code": PLACE_UNKNOWN_COMMUNITY_CODE}, 422

        if field not in _PLACE_FIELD_LIMITS:
            continue
        limit = _PLACE_FIELD_LIMITS[field]
        if error_type == "string_too_short":
            # Пустое после нормализации (`''`, ZWSP): сервис отвечает так же.
            return {"code": f"{field}_required"}, 422
        if error_type == "string_type":
            # CR 3.2, патч 4 (AC-07): явный `null` или не-строка. Сервис на
            # `None`/числе даёт `*_required` (стереть поле нельзя), значит и
            # путь Pydantic обязан отдавать тот же код, а не `invalid_request`.
            return {"code": f"{field}_required"}, 422
        if error_type == "string_too_long":
            # `actual_length` считается по нормализованному значению: схема
            # нормализует в `mode="before"`, поэтому `input` — уже результат
            # `normalize_text`, и NFC-удлинение здесь учтено (CR 3.1, патч 4).
            normalized = error.get("input")
            actual = len(normalized) if isinstance(normalized, str) else None
            details: dict[str, object] = {"max_length": limit}
            if actual is not None:
                details["actual_length"] = actual
            return {"code": f"{field}_too_long", "details": details}, 422
    return None


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """QD A1 (CR 3.2): любой необработанный сбой — конверт §4, а не `text/plain`.

    До правки необработанное исключение уходило в дефолтный обработчик Starlette
    и отдавало `500 Internal Server Error` с `content-type: text/plain`, без
    `{"code": ...}` и без `no-store` (обнаружено при CR 3.2 на `PATCH` с `null`).
    project-context §4 требует единственный конверт для ВСЕХ ошибок, а клиент
    ветвится по `code` — на `text/plain` он падал бы в неразобранное состояние.

    Регистрация не перехватывает то, что уже обработано: Starlette выбирает
    обработчик по типу исключения, и для `HTTPException` /
    `RequestValidationError` есть свои, более точные. Здесь оказываются только
    действительно необработанные сбои.

    `str(exc)` в тело НЕ попадает: текст исключения раскрывает пути, SQL и
    внутренние имена. Диагностика уходит в лог через `structlog`; секреты и
    тело запроса не логируются — только метод, путь и тип исключения.
    """
    logger.exception(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    code = _STATUS_CODES.get(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error")
    return JSONResponse({"code": code}, status_code=500, headers=_NO_STORE)


@app.get("/health")
async def health() -> dict[str, str]:
    """AC-01: GET /health → 200 {"status": "ok"} без обращения к внешним сервисам."""
    return {"status": "ok"}


# Стори 10.7, Task 2.2 (AC-01, D-10.7-1): `/health/deep` — процесс + БД + Redis.
# Подключается ЗДЕСЬ, а не через `api_router`: `include_router(api_router,
# prefix="/api/v1")` дал бы `/api/v1/health/deep`, тогда как probe (Task 5.1)
# и compose (Task 7.1) ждут `/health/deep` рядом с `/health`. Один маршрут —
# один путь; второй адрес-алиас создал бы два контракта на один вопрос.
# `healthcheck:` compose по-прежнему смотрит на `/health` и НЕ меняется:
# при лежачей БД api обязан оставаться `healthy`, иначе перезапуск api сделает
# недоступным и web (`docker-compose.yml:141-143`).
app.include_router(health_router)
