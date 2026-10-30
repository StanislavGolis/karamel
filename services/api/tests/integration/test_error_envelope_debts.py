"""QD A1–A6: конверт ошибок §4 на всех путях (долги CR 3.2).

Каждый тест краснеет от отката своего патча (§18.1). Сетевых вызовов нет:
`ASGITransport` поднимает приложение в процессе, база не нужна — все шесть
дефектов лежат в HTTP-слое (`main.py`, middleware), а не в SQL.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.main import PLACES_PATH_PREFIX, app


@pytest.fixture
async def client() -> Any:
    """Клиент без подмены сессии: проверяются ответы ДО обращения к БД.

    `raise_app_exceptions=False` обязателен для A1: без него `ASGITransport`
    пробрасывает исключение в тест, и обработчик `Exception` не участвует —
    тест проверял бы сам httpx, а не приложение.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


# --- A1: необработанное исключение ------------------------------------------------


async def test_unhandled_exception_returns_code_envelope_with_no_store(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QD A1: необработанный сбой — `500 {"code": "internal_error"}` + `no-store`.

    §18.1: КРАСНЕЕТ от отката обработчика — Starlette отвечал
    `500 text/plain` без `{"code": ...}` и без заголовка (именно это наблюдалось
    в CR 3.2 на `PATCH` с `null`). §18.2: проверяется тело и заголовок, а не
    факт «обработчик зарегистрирован».
    """

    @app.get("/__qd_a1_probe")
    async def _boom() -> None:
        raise RuntimeError("внутренний секрет: pg://user:pass@host/db")

    try:
        response = await client.get("/__qd_a1_probe")
    finally:
        app.router.routes = [
            route for route in app.router.routes if getattr(route, "path", "") != "/__qd_a1_probe"
        ]

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"code": "internal_error"}
    assert response.headers["cache-control"] == "no-store"
    # Секрет из текста исключения наружу не уходит.
    assert "pg://user:pass" not in response.text
    assert "RuntimeError" not in response.text


# --- A5: 405 --------------------------------------------------------------------


async def test_method_not_allowed_has_domain_code(client: AsyncClient) -> None:
    """QD A5: `405` отдаёт `method_not_allowed`, а не заглушку `error`.

    §18.1: краснеет от отката строки в `_STATUS_CODES` — ответ становится
    `{"code": "error"}`, неотличимым для клиента от прочих сбоев.

    Стори 3.3: пример `405` — `PUT /api/v1/places`. Раньше здесь стоял `GET`
    («выдача ещё не создана», D-4 стори 3.2), но 3.3 его реализовала.
    `PUT` на коллекцию не объявлен ни в одной стори: правка идёт `PATCH` по
    `uuid` (D-7 стори 3.2), и полная замена справочника запрещена.
    """
    response = await client.put(f"{PLACES_PATH_PREFIX}", json={})

    assert response.status_code == 405
    assert response.json() == {"code": "method_not_allowed"}
    assert response.headers["cache-control"] == "no-store"


# --- A6: trailing slash ----------------------------------------------------------


async def test_redirect_response_has_no_store(client: AsyncClient) -> None:
    """QD A6: `307` от `redirect_slashes` тоже несёт `no-store`.

    §18.1: КРАСНЕЕТ от отката `NoStoreMiddleware` — у редиректа заголовка нет,
    и промежуточный кэш мог бы отдать адрес защищённого ресурса другому
    пользователю (§4: заголовок на КАЖДОМ ответе).
    """
    response = await client.patch(
        f"{PLACES_PATH_PREFIX}/00000000-0000-0000-0000-000000000000/",
        json={"name": "x"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"].endswith(
        f"{PLACES_PATH_PREFIX}/00000000-0000-0000-0000-000000000000"
    )


async def test_health_keeps_no_store_without_overwriting(
    client: AsyncClient,
) -> None:
    """QD A6: middleware не ломает успешные ответы и не дублирует заголовок."""
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    values = response.headers.get_list("cache-control")
    assert values == ["no-store"], f"ожидался один заголовок, получено: {values}"


# --- A3/A4: доменные коды валидации ----------------------------------------------


def _request(path: str) -> Request:
    """Минимальный `Request` для прямого вызова обработчика.

    Обработчик валидации смотрит только на путь, поэтому полноценный ASGI-вызов
    с поднятием зависимостей не нужен: тест проверяет решение обработчика,
    а не маршрутизацию (она покрыта интеграционными тестами).
    """
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


async def test_category_branch_only_for_places_path() -> None:
    """QD A3: `category_invalid` срабатывает ТОЛЬКО на путях справочника.

    §18.1: КРАСНЕЕТ от отката условия `startswith(PLACES_PATH_PREFIX)` — тогда
    ошибка категории на `/api/v1/profiles` получила бы код и перечень категорий
    МЕСТА (чужой домен), и клиент анкеты показал бы админу справочника
    выпадашку «кафе/кино/каток».

    §18.2: обработчик вызывается ПО-НАСТОЯЩЕМУ с реальным
    `RequestValidationError`, и проверяется ТЕЛО ответа на обоих путях.
    Проверка «функция существует» ничего не доказывала бы.
    """
    from fastapi.exceptions import RequestValidationError

    from app.main import validation_error_handler

    def _category_error() -> RequestValidationError:
        return RequestValidationError(
            [
                {
                    "type": "enum",
                    "loc": ("body", "category"),
                    "msg": "Input should be a valid enum member",
                    "input": "nonsense",
                }
            ]
        )

    places_response = await validation_error_handler(
        _request(f"{PLACES_PATH_PREFIX}"), _category_error()
    )
    foreign_response = await validation_error_handler(
        _request("/api/v1/profiles/me"), _category_error()
    )

    assert places_response.status_code == 422
    places_body = json.loads(places_response.body)
    assert places_body["code"] == "category_invalid"
    assert places_body["details"]["allowed"], "перечень категорий обязателен (AC-02)"

    # Тот же ввод на чужом пути — НЕ код места.
    foreign_body = json.loads(foreign_response.body)
    assert foreign_body["code"] != "category_invalid", (
        f"код чужого домена на пути анкеты: {foreign_body}"
    )


async def test_patch_with_city_slug_gets_immutable_code() -> None:
    """QD A4 (R-05): попытка переноса между сообществами — доменный код.

    §18.1: КРАСНЕЕТ от отката ветки `extra_forbidden` — ответ становится общим
    `invalid_request`, и «перенос запрещён» неотличим от сломанной формы.

    §18.2: проверяется РЕАЛЬНЫЙ ответ обработчика на настоящем
    `RequestValidationError` того типа, который даёт Pydantic при
    `extra="forbid"` (`extra_forbidden`, `loc` содержит имя поля). Проверка
    через HTTP-клиент без cookie доказывала бы только работу авторизации.
    """
    from fastapi.exceptions import RequestValidationError

    from app.main import validation_error_handler

    exc = RequestValidationError(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", "city_slug"),
                "msg": "Extra inputs are not permitted",
                "input": "kemerovo",
            }
        ]
    )

    response = await validation_error_handler(
        _request(f"{PLACES_PATH_PREFIX}/00000000-0000-0000-0000-000000000000"), exc
    )

    assert response.status_code == 422
    assert json.loads(response.body) == {"code": "place_city_immutable"}


async def test_extra_forbidden_other_field_stays_generic() -> None:
    """QD A4, §18.2: чужое лишнее поле не получает код переноса сообщества.

    Краснеет, если ветку сделать по одному `extra_forbidden` без проверки
    имени поля: `uuid`/`id`/`created_at` в теле `PATCH` тоже запрещены (AC-06),
    но это не «перенос между сообществами», и админ получил бы неверную
    подсказку.
    """
    from fastapi.exceptions import RequestValidationError

    from app.main import validation_error_handler

    exc = RequestValidationError(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", "uuid"),
                "msg": "Extra inputs are not permitted",
                "input": "00000000-0000-0000-0000-000000000000",
            }
        ]
    )

    response = await validation_error_handler(
        _request(f"{PLACES_PATH_PREFIX}/00000000-0000-0000-0000-000000000000"), exc
    )

    body = json.loads(response.body)
    assert body["code"] != "place_city_immutable", f"неверный код: {body}"


# --- Стори 12.3, AC-2/AC-4: код ошибки по дате рождения -----------------------


def _validation_exc(field: str, value: object, *, type_: str, msg: str) -> Any:
    """`RequestValidationError` глазами Pydantic: `loc` включает источник."""
    from fastapi.exceptions import RequestValidationError

    return RequestValidationError(
        [{"type": type_, "loc": ("body", field), "msg": msg, "input": value}]
    )


async def test_lat_error_gets_invalid_coordinates() -> None:
    """AC-2 (Task 3.4, M10): ошибка по `lat` — доменный код координат.

    Краснеет, если заменить `COORDINATE_FIELDS` на `frozenset()`: клиент
    получил бы `invalid_request` и не смог отличить «сломанную форму» от
    «геолокация вернула мусор» — а второе лечится повтором определения города.
    """
    from app.main import validation_error_handler

    exc = _validation_exc("lat", 91.0, type_="less_than_equal", msg="Input should be <= 90")
    response = await validation_error_handler(_request("/api/v1/places"), exc)

    assert response.status_code == 422
    assert json.loads(response.body) == {"code": "invalid_coordinates"}


async def test_birth_date_error_gets_invalid_request() -> None:
    """AC-4 (Task 3.4, M9): ошибка по `birth_date` — НЕЙТРАЛЬНЫЙ код.

    Краснеет, если в `main.py` подставить `SENSITIVE_FIELDS` вместо
    `COORDINATE_FIELDS`: `birth_date` входит в реестр ПД, и клиент показал бы
    «повторите определение города» на ошибке в дате рождения.
    """
    from app.main import validation_error_handler

    exc = _validation_exc(
        "birth_date", "not-a-date", type_="date_from_datetime_parsing", msg="Invalid date"
    )
    response = await validation_error_handler(_request("/api/v1/profiles"), exc)

    assert response.status_code == 422
    assert json.loads(response.body) == {"code": "invalid_request"}


async def test_birth_date_error_does_not_echo_value() -> None:
    """AC-4 (Task 3.5, M14): отправленное значение НЕ возвращается клиенту.

    Краснеет, если добавить `details: {'input': value}` в ответ: значение
    пользователя возвращалось бы в теле ошибки, то есть ПД попадали бы в
    ответ, который кэшируется браузером и пишется в сетевые логи.
    """
    from app.main import validation_error_handler

    secret_value = "1990-13-45"
    exc = _validation_exc(
        "birth_date", secret_value, type_="date_from_datetime_parsing", msg="Invalid date"
    )
    response = await validation_error_handler(_request("/api/v1/profiles"), exc)

    assert secret_value not in response.body.decode()


async def test_birth_date_error_is_not_logged() -> None:
    """AC-4 (Task 3.6, M15): обработчик не пишет ни значения, ни ключа в лог.

    Краснеет, если добавить `logger.warning('validation_error',
    errors=exc.errors())`: `exc.errors()` содержит `input` с самим значением,
    и дата рождения попала бы в журнал — прямое нарушение NFR66.

    **Почему `structlog.testing.capture_logs`, а не `caplog` (находка DS).**
    Первая версия этого теста брала stdlib `caplog` и ПРОПУСТИЛА мутацию:
    `structlog` с `make_filtering_bound_logger` пишет через свой рендерер в
    stdout, а не через `logging`-хендлеры, поэтому `caplog` не видит ни одной
    записи — тест зеленел при любом логе. Ровно урок 11.10: тест, который не
    исполняет проверяемый путь, проверяет текст, а не поведение.
    `capture_logs` подменяет цепочку процессоров structlog и перехватывает
    вызов независимо от рендерера.
    """
    from structlog.testing import capture_logs

    from app.main import validation_error_handler

    secret_value = "1990-13-45"
    exc = _validation_exc(
        "birth_date", secret_value, type_="date_from_datetime_parsing", msg="Invalid date"
    )
    with capture_logs() as captured:
        await validation_error_handler(_request("/api/v1/profiles"), exc)

    rendered = repr(captured)
    assert secret_value not in rendered, "значение даты утекло в лог (NFR66)"
    assert "birth_date" not in rendered, "ключ birth_date утёк в лог (NFR66)"
