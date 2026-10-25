"""Карта `overrides` в гарде размера тела (Task 9.3; AC-08).

AC-08 закрывает долг из `deferred-work.md`: `MAX_REQUEST_BODY_BYTES = 1 МиБ`
не имел исключений по путям, и легальное фото до 5 МиБ отбивалось `413`.

§18.1: тесты падают без реализации. Если убрать карту `overrides`
(`_limit_for` всегда возвращает `max_body_bytes`),
`test_photos_route_allows_large_body` краснеет — фото отбивается общим
лимитом. Если поднять общий лимит «для простоты» — краснеет
`test_profile_route_keeps_global_limit`.

§18.2: ассерты на наблюдаемое поведение (код ответа, вызвано ли приложение,
сколько байт прочитано), а не на константы внутри теста.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.types import Message, Receive, Scope, Send

from app.core.body_limit import BODY_TOO_LARGE_CODE, MULTIPART_OVERHEAD, BodySizeLimitMiddleware

GLOBAL_LIMIT = 1024
PHOTO_LIMIT = GLOBAL_LIMIT * 4  # «фото больше анкеты» — суть AC-08
PHOTOS_PATH = "/api/v1/profiles/me/photos"
PROFILES_PATH = "/api/v1/profiles"


def _scope(path: str, *, content_length: int | None = None) -> Scope:
    """HTTP-scope с телом и опциональным `Content-Length`."""
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/octet-stream")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


def _collector() -> tuple[list[Message], Send]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return sent, send


def _status_of(sent: list[Message]) -> int:
    start = next(m for m in sent if m["type"] == "http.response.start")
    status: int = start["status"]
    return status


def _body_of(sent: list[Message]) -> dict[str, Any]:
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


class _Recorder:
    """Приложение-заглушка: считает вызовы и прочитанные байты, отвечает `200`."""

    def __init__(self) -> None:
        self.called = False
        self.received = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.request":
                self.received += len(message.get("body", b""))
                if not message.get("more_body"):
                    break
            elif message["type"] == "http.disconnect":
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


# Стори 2.5, Task 2.4 (D-5): путь замены фото с параметром `{uuid}`.
PHOTO_ITEM_PATH = PHOTOS_PATH + "/7f3c1a9e-0000-4000-8000-000000000001"


def _middleware(app: Any) -> BodySizeLimitMiddleware:
    """Гард с обеими формами ключа: точной и префиксной (как в `main.py`).

    Стори 2.5: карта содержит и `POST`-путь, и `/*`-префикс. Тесты ниже
    проверяют, что префикс не отменяет точное совпадение и не расширяет
    общий лимит.
    """
    return BodySizeLimitMiddleware(
        app,
        max_body_bytes=GLOBAL_LIMIT,
        overrides={PHOTOS_PATH: PHOTO_LIMIT, PHOTOS_PATH + "/*": PHOTO_LIMIT},
    )


# CR 2.5, патч P-3: точному и префиксному ключам даны РАЗНЫЕ лимиты.
# В `_middleware` они равны, поэтому порядок разрешения конфликта там
# недоказуем: перестановка циклов в `_limit_for` оставляла тест зелёным.
EXACT_LIMIT = PHOTO_LIMIT * 2


def _middleware_distinct(app: Any) -> BodySizeLimitMiddleware:
    """Гард, где точный ключ ВЛОЖЕН в префикс, а лимиты разные.

    Важно: `PHOTOS_PATH` и `PHOTOS_PATH/*` НИКОГДА не совпадают с одним путём
    (префиксу нужен ещё сегмент), поэтому на них порядок разрешения конфликта
    недоказуем — перестановка циклов в `_limit_for` не краснит такой тест.
    Доказать порядок можно только ключом, который подпадает под ОБА правила
    сразу: конкретным `uuid`-путём внутри префикса.
    """
    return BodySizeLimitMiddleware(
        app,
        max_body_bytes=GLOBAL_LIMIT,
        overrides={PHOTO_ITEM_PATH: EXACT_LIMIT, PHOTOS_PATH + "/*": PHOTO_LIMIT},
    )


# --- общий лимит остаётся прежним ------------------------------------------------


async def test_profile_route_keeps_global_limit() -> None:
    """AC-08: на прочих маршрутах лимит 1 МиБ сохраняется без изменений.

    Тело в 2× общего лимита на `POST /profiles` отбивается `413` и не читается.
    Подними кто-то общий лимит «чтобы фото проходили» — тест краснеет,
    а вместе с ним вернулся бы OOM (фикс 2026-10-22).
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    await _middleware(app)(_scope(PROFILES_PATH, content_length=GLOBAL_LIMIT * 2), receive, send)

    assert app.called is False
    assert _status_of(sent) == 413
    assert _body_of(sent) == {"code": BODY_TOO_LARGE_CODE}


async def test_unknown_path_gets_global_limit() -> None:
    """Task 8.1: путь не из карты — общий лимит, поведение не изменилось."""
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться")

    await _middleware(app)(
        _scope("/api/v1/profiles/other", content_length=GLOBAL_LIMIT + 1), receive, send
    )

    assert app.called is False
    assert _status_of(sent) == 413


# --- маршрут фото получает свой лимит -------------------------------------------


async def test_photos_route_allows_large_body() -> None:
    """AC-08: тело больше общего лимита доходит до приложения на маршруте фото.

    `!= 413` (а не `== 200`): на реальном приложении без cookie ответит `401`,
    и это тоже доказывает, что гард не отбил запрос по размеру.
    """
    app = _Recorder()
    sent, send = _collector()
    body = b"x" * (GLOBAL_LIMIT * 2)

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    await _middleware(app)(_scope(PHOTOS_PATH, content_length=len(body)), receive, send)

    assert app.called is True
    assert _status_of(sent) != 413


async def test_photos_route_over_its_own_limit_is_413() -> None:
    """AC-08: тело больше лимита upload-маршрута по-прежнему отбивается `413`.

    Лимит маршрута — не «безлимит»: превышение на байт отбивается до чтения
    в память, иначе OOM вернулся бы через один маршрут.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    await _middleware(app)(_scope(PHOTOS_PATH, content_length=PHOTO_LIMIT + 1), receive, send)

    assert app.called is False
    assert _status_of(sent) == 413
    assert _body_of(sent) == {"code": BODY_TOO_LARGE_CODE}


async def test_photos_route_exactly_at_limit_passes() -> None:
    """AC-08: тело ровно в лимит маршрута проходит.

    Без этого теста `>=` вместо `>` не ловится: файл ровно в лимит (плюс
    обвязка multipart) отбивался бы `413` — прямое нарушение AC-08.
    """
    app = _Recorder()
    sent, send = _collector()
    body = b"x" * PHOTO_LIMIT

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    await _middleware(app)(_scope(PHOTOS_PATH, content_length=len(body)), receive, send)

    assert app.called is True
    assert _status_of(sent) != 413


async def test_trailing_slash_uses_route_limit() -> None:
    """Task 8.1: хвостовой `/` не отменяет лимит маршрута.

    FastAPI с `redirect_slashes` принимает `/me/photos/` как тот же маршрут,
    и точное сравнение отбивало бы такой запрос общим лимитом — легальная
    загрузка падала бы `413` из-за слеша.
    """
    app = _Recorder()
    sent, send = _collector()
    body = b"x" * (GLOBAL_LIMIT * 2)

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    await _middleware(app)(_scope(PHOTOS_PATH + "/", content_length=len(body)), receive, send)

    assert app.called is True
    assert _status_of(sent) != 413


# --- стори 2.5: префиксный ключ для пути с `{uuid}` -------------------------------


async def test_photo_item_path_gets_photo_limit() -> None:
    """Task 2.4 (а), D-5: `PUT /me/photos/<uuid>` получает лимит фото.

    §18.1: без префиксного правила `_limit_for` возвращает общий лимит,
    и тело вдвое больше него отбивается `413` — тест краснеет. Именно этот
    дефект возник бы на замене фото: карта `overrides` сравнивает путь точно.
    """
    app = _Recorder()
    sent, send = _collector()
    body = b"x" * (GLOBAL_LIMIT * 2)

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    await _middleware(app)(_scope(PHOTO_ITEM_PATH, content_length=len(body)), receive, send)

    assert app.called is True
    assert _status_of(sent) != 413


async def test_exact_match_wins_over_prefix() -> None:
    """Task 2.4 (б), D-5: точный ключ главнее префиксного.

    `POST /me/photos` остаётся на своём лимите: префикс добавляет маршрут,
    а не переписывает существующий. Проверяется наблюдаемо — тело больше
    лимита фото отбивается `413`.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    await _middleware(app)(_scope(PHOTOS_PATH, content_length=PHOTO_LIMIT + 1), receive, send)

    assert app.called is False
    assert _status_of(sent) == 413


async def test_prefix_does_not_leak_to_sibling_path() -> None:
    """Task 2.4 (в): префикс не расширяется на похожие пути.

    Сравнение по границе сегмента: `/me/photosX` не совпадает с
    `/me/photos`. Иначе `startswith` без границы открыл бы OOM на любом
    маршруте с похожим префиксом.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться")

    await _middleware(app)(
        _scope(PHOTOS_PATH + "X/abc", content_length=GLOBAL_LIMIT + 1), receive, send
    )

    assert app.called is False
    assert _status_of(sent) == 413


async def test_photo_item_path_over_photo_limit_is_413() -> None:
    """Task 2.4, D-5: у префикса тот же предел, а не «безлимит».

    Иначе замена фото стала бы каналом для OOM в обход общего лимита.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    await _middleware(app)(_scope(PHOTO_ITEM_PATH, content_length=PHOTO_LIMIT + 1), receive, send)

    assert app.called is False
    assert _status_of(sent) == 413


async def test_photo_item_path_with_root_path_uses_prefix() -> None:
    """Task 2.4 (г): `root_path` не ломает префиксное совпадение.

    Под прокси путь может прийти с префиксом монтирования. Без учёта
    `root_path` маршрут замены молча падал бы на общий лимит — тот же
    `413` на легальном фото, только на стенде.
    """
    app = _Recorder()
    sent, send = _collector()
    body = b"x" * (GLOBAL_LIMIT * 2)
    scope = _scope("/prefix" + PHOTO_ITEM_PATH, content_length=len(body))
    scope["root_path"] = "/prefix"

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    await _middleware(app)(scope, receive, send)

    assert app.called is True
    assert _status_of(sent) != 413


async def test_photo_item_path_trailing_slash_uses_prefix() -> None:
    """Task 2.4 (г): хвостовой `/` не отменяет префиксное правило.

    `redirect_slashes` FastAPI принимает `/me/photos/<uuid>/` как тот же
    маршрут; отказ по размеру из-за слеша был бы ложным.
    """
    app = _Recorder()
    sent, send = _collector()
    body = b"x" * (GLOBAL_LIMIT * 2)

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    await _middleware(app)(_scope(PHOTO_ITEM_PATH + "/", content_length=len(body)), receive, send)

    assert app.called is True
    assert _status_of(sent) != 413


# --- рубеж 2: chunked-загрузка ---------------------------------------------------


async def test_chunked_photos_body_over_own_limit_aborted() -> None:
    """Task 8.2: chunked-тело на маршруте фото отбивается СВОИМ лимитом.

    Рубеж 2 обязан использовать выбранный лимит, а не общий. Иначе загрузка
    без `Content-Length` считалась бы по 1 МиБ, и легальное фото в 3 МиБ
    получало бы `413`.
    """
    app = _Recorder()
    sent, send = _collector()
    chunk = b"x" * 512
    served = 0
    total_chunks = PHOTO_LIMIT // len(chunk) + 2

    async def receive() -> Message:
        nonlocal served
        served += 1
        return {"type": "http.request", "body": chunk, "more_body": served < total_chunks}

    await _middleware(app)(_scope(PHOTOS_PATH), receive, send)

    assert _status_of(sent) == 413
    assert _body_of(sent) == {"code": BODY_TOO_LARGE_CODE}
    # Приложение получило РОВНО лимит маршрута, ни байтом больше.
    assert app.received == PHOTO_LIMIT


# --- связь карты с реальными маршрутами (патч 7 CR) ------------------------------


def test_photos_override_matches_real_route() -> None:
    """AC-08: путь в карте `overrides` — существующий маршрут загрузки фото.

    §18.1: без этой связи опечатка или смена префикса в `main.py` не краснит
    ни один тест. Остальные тесты гарда подают путь сами и потому проверяют
    только логику `_limit_for`, а не то, что карта вообще применяется к живому
    маршруту. Следствие такого расхождения — `413` на легальном фото в проде
    при зелёном CI (исходный дефект AC-08).
    """
    from app.main import PHOTOS_UPLOAD_PATH, app

    paths = app.openapi()["paths"]
    assert PHOTOS_UPLOAD_PATH in paths, (
        f"путь {PHOTOS_UPLOAD_PATH} из карты overrides не существует в приложении"
    )
    assert "post" in paths[PHOTOS_UPLOAD_PATH]


# --- константа обвязки multipart -------------------------------------------------


def test_multipart_overhead_is_64_kib() -> None:
    """Task 8.3: `MULTIPART_OVERHEAD = 64 КиБ` — запас на обвязку multipart.

    Тело multipart больше файла на границы и заголовки частей. Без запаса
    файл ровно в `MAX_PHOTO_BYTES` получал бы `413` (AC-08). Значение —
    контракт: уменьшение до нуля вернуло бы этот дефект.
    """
    assert MULTIPART_OVERHEAD == 64 * 1024


# --- CR 2.5: порядок разрешения конфликта ключей (патч P-3) ----------------------


async def test_exact_match_uses_exact_limit_not_prefix_limit() -> None:
    """CR 2.5, P-3: точный ключ выбирает СВОЙ лимит, а не префиксный.

    `_middleware` даёт обоим ключам один лимит, поэтому порядок циклов
    в `_limit_for` там недоказуем: перестановка «сначала префиксы» оставляла
    прежний тест зелёным. Здесь лимиты разные, и тело между ними проходит
    только при точном совпадении.

    §18.1: доказано откатом — при перестановке циклов `POST /me/photos`
    получает `PHOTO_LIMIT` и отвечает `413` на легальное фото.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await _middleware_distinct(app)(
        _scope(PHOTO_ITEM_PATH, content_length=EXACT_LIMIT), receive, send
    )

    assert app.called is True
    assert _status_of(sent) == 200


async def test_prefix_path_uses_prefix_limit() -> None:
    """CR 2.5, P-3: путь с `{uuid}` берёт префиксный лимит, а не точный.

    Тот же вход, что выше, но путь другой: тело в пределах точного лимита
    и больше префиксного должно отбиваться `413`. Обратная перестановка
    циклов сделала бы этот тест красным, предыдущий — тоже.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    # Другой `uuid`-путь: точного ключа для него нет — работает префикс.
    other_item = PHOTOS_PATH + "/11111111-2222-4333-8444-555555555555"
    await _middleware_distinct(app)(
        _scope(other_item, content_length=PHOTO_LIMIT + 1), receive, send
    )

    assert app.called is False
    assert _status_of(sent) == 413


# --- CR 2.5: `..`/двойной слэш не уносят лимит фото (патч P-6) -------------------


async def test_dot_dot_path_gets_global_limit() -> None:
    """CR 2.5, P-6: путь с `..` не получает лимит фото.

    Сегментная проверка `startswith(prefix + '/')` пропускала
    `/me/photos/../users`: такой путь уносил бы лимит 6 МиБ на маршрут,
    к фото не относящийся. Сам путь даёт `404`, но гард — единственный
    рубеж ДО чтения тела и не должен зависеть от внешнего нормализатора.

    §18.1: доказано откатом — без `_is_plain_path` тело больше общего лимита
    проходит в приложение.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    await _middleware(app)(
        _scope(PHOTOS_PATH + "/../users", content_length=GLOBAL_LIMIT + 1), receive, send
    )

    assert app.called is False
    assert _status_of(sent) == 413


async def test_double_slash_path_gets_global_limit() -> None:
    """CR 2.5, P-6: двойной слэш — тоже общий лимит.

    Некоторые нормализаторы схлопывают `//` ПОСЛЕ гарда, и путь
    превращается в маршрут вне префикса. Лимит фото на нём — это допуск
    тела 6 МиБ туда, где ожидается 1 МиБ.
    """
    app = _Recorder()
    sent, send = _collector()

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться при превышении лимита")

    await _middleware(app)(
        _scope(PHOTOS_PATH + "//x", content_length=GLOBAL_LIMIT + 1), receive, send
    )

    assert app.called is False
    assert _status_of(sent) == 413


# --- CR 2.5: нормализация ключей карты (патч P-5) --------------------------------


async def test_override_key_with_trailing_slash_still_matches() -> None:
    """CR 2.5, P-5: ключ карты с хвостовым `/` совпадает с путём.

    Путь нормализуется (`rstrip('/')`), а ключи карты раньше — нет. Ключ,
    записанный с хвостовым слэшем, не совпал бы НИКОГДА, и легальное фото
    получало бы `413`; ни один тест гарда этого не видел, потому что карту
    подают сами тесты, а `main.py` пишет ключи без слэша.

    §18.1: доказано откатом — без нормализации в `__init__` приходит общий
    лимит, и тело больше него отбивается `413`.
    """
    app = _Recorder()
    sent, send = _collector()
    guard = BodySizeLimitMiddleware(
        app,
        max_body_bytes=GLOBAL_LIMIT,
        overrides={PHOTOS_PATH + "/": PHOTO_LIMIT},
    )

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await guard(_scope(PHOTOS_PATH, content_length=PHOTO_LIMIT), receive, send)

    assert app.called is True
    assert _status_of(sent) == 200


# --- CR 2.5: связь с живыми маршрутами и CORS (патч P-7) -------------------------


def test_photos_prefix_override_matches_real_put_route() -> None:
    """CR 2.5, P-7: префиксный ключ связан с РЕАЛЬНЫМ маршрутом замены.

    Прежний тест проверял только точный путь. Если из `overrides` убрать
    префиксную строку, все тесты гарда оставались зелёными (`_prefixes`
    собирается из карты, которую подают сами тесты), а прод получал `413`
    на легальное фото при замене — ровно дефект, ради которого писался D-5.

    §18.1: доказано откатом — удаление строки `PHOTOS_ITEM_PATH_PREFIX`
    из `main.py` краснит этот тест.
    """
    from app.main import PHOTOS_ITEM_PATH_PREFIX, PHOTOS_UPLOAD_PATH, app

    paths = app.openapi()["paths"]
    item_path = f"{PHOTOS_UPLOAD_PATH}/{{photo_uuid}}"
    assert item_path in paths, f"маршрута {item_path} нет в приложении"
    assert "put" in paths[item_path]
    # Ключ `.../photos/*` покрывает `.../photos/{photo_uuid}` по границе сегмента.
    assert f"{PHOTOS_UPLOAD_PATH}/*" == PHOTOS_ITEM_PATH_PREFIX


def test_cors_allows_put() -> None:
    """CR 2.5, P-7: `PUT` разрешён в CORS вместе с маршрутом замены.

    Через BFF preflight не возникает (вызов серверный, same-origin), поэтому
    дефект латентный: прямой кросс-origin `PUT` получал бы отказ preflight
    при рабочем маршруте. Проверяется фактический список методов middleware.
    """
    from starlette.middleware.cors import CORSMiddleware

    from app.main import app as real_app

    cors = [m for m in real_app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "CORS-middleware не найден"
    methods = cors[0].kwargs["allow_methods"]
    assert "PUT" in methods
