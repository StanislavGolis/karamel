"""Гард на размер тела запроса (фикс OOM, 2026-10-22).

Дефект, который закрывает модуль: `POST /api/v1/profiles` с телом ~10 МБ
ронял контейнер `api` по OOM. Причина не в БД и не в Pydantic — тело целиком
читалось в память, а затем нормализация шла синхронно в async-обработчике
(`profile_normalize._strip_invisible` идёт посимвольно), блокируя event loop.
Тело на 5 МБ проходило с `422` за ~1 с только потому, что успевало
нормализоваться; 10 МБ не успевало и убивало процесс.

Почему ASGI-middleware, а не `BaseHTTPMiddleware`: `BaseHTTPMiddleware`
оборачивает `receive` и при чтении тела не даёт раннего отказа — решение
принимается уже после того, как приложение запросило тело. Здесь отказ
происходит ДО чтения: `Content-Length` известен из заголовков.

Два рубежа:
  1. `Content-Length` больше лимита — ответ `413` без единого `receive()`.
     Тело не попадает в память вообще.
  2. `Content-Length` нет (chunked) или он врёт — считаем реально
     прочитанные байты и обрываем запрос на превышении: буфер не растёт
     выше лимита.

Стори 2.2 (AC-08): лимит теперь не один на всё приложение, а карта
`overrides` — «точный путь → лимит». Появился маршрут загрузки фото
(`/api/v1/profiles/me/photos`), чьё тело multipart законно больше анкеты
(файл до 5 МиБ), и общий лимит в 1 МиБ отбивал бы легальную загрузку
`413 request_too_large`. Поднимать общий лимит нельзя: это возвращает
исходный OOM на `POST /profiles` с телом в 10 МБ. Путь, которого нет
в карте, получает `max_body_bytes` — поведение всех прочих маршрутов
не изменилось.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Доменный код в общем конверте проекта (`{"code": ...}`) — как у остальных
# ошибок в `main.py`. Клиент разбирает только `code`.
BODY_TOO_LARGE_CODE = "request_too_large"

# Стори 2.2, Task 8.3 (AC-08): запас на обвязку multipart. Тело multipart
# больше самого файла на границы частей и заголовки (`Content-Disposition`,
# `Content-Type`), и лимит гарда, равный `MAX_PHOTO_BYTES`, отбивал бы `413`
# легальный файл ровно в лимит. 64 КиБ — с запасом на любую обвязку: одна
# часть multipart даёт десятки байт заголовков, а не килобайты.
#
# Точный размер ФАЙЛА проверяет `photo_validation` (Task 4.2), а не гард:
# гард видит тело целиком и не отличает файл от обвязки.
MULTIPART_OVERHEAD = 64 * 1024

# RFC 9110: `Content-Length = 1*DIGIT`. `int()` мягче — принимает `+5`,
# ` 5 ` и `1_048_576`; h11 такие значения отбивает сам, но гард не должен
# зависеть от парсера сервера: нестрогий разбор молча пропустил бы
# `Content-Length: +99999999` в приложение.
_CONTENT_LENGTH = re.compile(r"\A[0-9]+\Z")


def _is_plain_path(path: str) -> bool:
    """CR 2.5, патч P-6: путь без `.`/`..`-сегментов и двойных слэшей.

    Префиксное правило сравнивает строку как есть, поэтому `..` внутри пути
    проходило бы сегментную проверку и получало лимит фото. Само приложение
    такой путь не обслуживает (`404`), но защита не должна зависеть от того,
    что перед ним стоит: гард — единственный рубеж до чтения тела.
    """
    if "//" in path:
        return False
    return all(segment not in {".", ".."} for segment in path.split("/"))


def _declared_length(scope: Scope) -> int | None:
    """`Content-Length` из заголовков или `None`, если его нет/он битый.

    Битое значение не ошибка запроса: клиент мог прислать мусор, и отказ
    по длине был бы ложным. Такой запрос проходит во второй рубеж — счётчик
    прочитанных байт.
    """
    for raw_name, raw_value in scope["headers"]:
        if raw_name == b"content-length":
            try:
                text = raw_value.decode("ascii")
            except UnicodeDecodeError:
                return None
            if not _CONTENT_LENGTH.match(text):
                return None
            return int(text)
    return None


async def _send_too_large(send: Send) -> None:
    """`413` в едином конверте ошибок проекта.

    `cache-control: no-store` — как в `profiles.py`: ответ на ошибку не должен
    кэшироваться, иначе следующий запрос с того же устройства получит чужой
    отказ из промежуточного кэша.
    """
    body = json.dumps({"code": BODY_TOO_LARGE_CODE}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Отклоняет тело больше допустимого лимита до его чтения в память.

    Подключается в `main.py` с лимитом из `core.config`
    (`MAX_REQUEST_BODY_BYTES`, дефолт 1 МиБ).

    Стори 2.2, Task 8.1 (AC-08): `overrides` — карта «точный путь → лимит».
    Загрузка фото законно больше анкеты (multipart с файлом до 5 МиБ), и
    поднимать общий лимит ради одного маршрута значило бы открыть OOM
    на анкете. Путь не из карты получает `max_body_bytes` — поведение
    остальных маршрутов не меняется ни на байт.

    Ключ сравнивается с `scope["path"]` точно, с нормализацией хвостового
    `/`: `POST /api/v1/profiles/me/photos/` — тот же маршрут для FastAPI
    (redirect_slashes), и гард не должен отбивать его общим лимитом.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        overrides: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        # CR 2.5, патч P-5: ключи нормализуются так же, как путь запроса
        # (срез хвостового `/`). Раньше сравнение шло по нормализованному
        # пути против СЫРЫХ ключей: ключ, записанный с хвостовым `/`,
        # не совпал бы никогда и молча отдал общий лимит 1 МиБ — легальное
        # фото получило бы `413 request_too_large`, и причину не нашли бы
        # ни один тест (карту подают сами тесты гарда).
        self.overrides: dict[str, int] = {
            (key.rstrip("/") or "/"): limit for key, limit in (overrides or {}).items()
        }
        # Стори 2.5, Task 2.2 (D-5): префиксные ключи вида
        # `/api/v1/profiles/me/photos/*` — для путей с параметром (`{uuid}`),
        # которые нельзя перечислить точной картой. Хранятся отдельно и без
        # звёздочки: сравнение идёт по границе сегмента пути, а не по
        # `startswith` произвольной строки (иначе `/photosX` совпал бы с
        # `/photos`). Сортировка по убыванию длины даёт longest-prefix match,
        # если когда-нибудь появятся вложенные префиксы.
        self._prefixes: list[tuple[str, int]] = sorted(
            (
                (key[:-1].rstrip("/"), limit)
                for key, limit in self.overrides.items()
                if key.endswith("/*")
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def _limit_for(self, scope: Scope) -> int:
        """Task 8.1: лимит для пути запроса — из карты или общий.

        Стори 2.5, Task 2.2 (D-5): сначала ТОЧНОЕ совпадение, затем префиксные
        ключи, затем общий лимит. Порядок принципиален: он сохраняет прежнее
        поведение всех маршрутов из карты (`POST /me/photos` остаётся со своим
        лимитом) и не расширяет общий лимит — иначе вернулся бы OOM на анкете.

        `root_path` учитывается: при монтировании приложения под префиксом
        (ASGI-`root_path`, прокси) `scope["path"]` в некоторых сборках
        Starlette приходит без него, и маршрут из карты не совпал бы.
        """
        if not self.overrides:
            return self.max_body_bytes
        path = scope.get("path", "")
        root_path = scope.get("root_path") or ""
        candidates = [path]
        if root_path and path.startswith(root_path):
            candidates.append(path[len(root_path) :])
        for candidate in candidates:
            normalized = candidate.rstrip("/") or "/"
            limit = self.overrides.get(normalized)
            if limit is not None:
                return limit
            if not _is_plain_path(normalized):
                # CR 2.5, патч P-6: путь с `..`/`.` или двойным слэшем НЕ
                # получает префиксный лимит. Иначе `/me/photos/../../x`
                # проходил бы сегментную проверку и уносил лимит фото
                # (6 МиБ вместо 1 МиБ) на путь, к фото не относящийся. Сам
                # по себе такой путь даёт `404`, но при появлении
                # нормализующего прокси повышенный лимит уехал бы на чужой
                # маршрут. Точное совпадение выше уже отработало — здесь
                # сознательно отдаём общий лимит.
                continue
            for prefix, prefix_limit in self._prefixes:
                # Граница сегмента: `/photos` совпадает с `/photos/<uuid>`,
                # но не с `/photosX`. Хвостовой `/` уже срезан выше.
                if normalized.startswith(prefix + "/"):
                    return prefix_limit
        return self.max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Рубеж 1 — заголовок; рубеж 2 — счётчик прочитанных байт.

        Task 8.2 (AC-08): оба рубежа используют ОДИН выбранный лимит.
        Проверка только по `Content-Length` оставила бы лазейку: chunked-загрузка
        того же файла шла бы мимо гарда, и OOM вернулся бы через curl без
        заголовка длины.
        """
        if scope["type"] != "http":
            # WebSocket и lifespan не имеют тела запроса в этом смысле.
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(scope)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            # Рубеж 1: тело не читается вообще — OOM невозможен.
            await _send_too_large(send)
            return

        received = 0
        limit_exceeded = False
        response_started = False

        async def counting_receive() -> Message:
            """Рубеж 2: чанки считаются, чтение обрывается на превышении.

            `Content-Length` может отсутствовать (chunked) или врать (клиент
            заявил 100 байт и прислал 10 МБ) — заголовку верить нельзя.

            Почему НЕ исключение: FastAPI ловит ЛЮБОЕ исключение при чтении
            тела и превращает его в `400 There was an error parsing the body`.
            Проверено на стенде: chunked-тело на 2 МБ давало `400` вместо `413`.
            Поэтому чтение обрывается пустым финальным чанком, а приложение
            получает усечённое тело и отвечает своей ошибкой — её мы подавим
            и отправим `413` сами. Память при этом ограничена лимитом.
            """
            nonlocal received, limit_exceeded
            if limit_exceeded:
                # Тело уже отвергнуто, но `http.disconnect` обязан дойти:
                # приложение, ждущее разрыва (`request.is_disconnected`),
                # иначе будет крутиться на фальшивых пустых чанках. Тело
                # следующего сообщения отбрасывается — в памяти живёт не больше
                # одного чанка сервера (uvicorn отдаёт ~64 КБ), не весь запрос.
                message = await receive()
                if message["type"] == "http.disconnect":
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    limit_exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def tracking_send(message: Message) -> None:
            """Подавляет ответ приложения, если тело превысило лимит.

            Приложение уже начало формировать ответ на усечённое тело
            (`400` от парсера JSON). Пропустить его нельзя: клиент получил бы
            `400` вместо `413` и не понял бы, что дело в размере.

            Если ответ уже начат (статус ушёл клиенту), заменить его на `413`
            невозможно. Тогда ответ НЕ подавляется: выброшенное финальное
            сообщение оставило бы соединение незакрытым до `timeout_keep_alive`,
            а клиент видел бы валидный `2xx` без тела. Ветка латентна для
            текущего API (FastAPI читает тело до вызова обработчика), но гард
            общий — стриминговый роут обязан закрыть ответ.
            """
            nonlocal response_started
            if message["type"] == "http.response.start":
                if limit_exceeded:
                    # Ответ приложения на уже усечённое тело: не начинаем его.
                    # Клиенту уйдёт `413` после возврата из приложения.
                    return
                response_started = True
                await send(message)
                return
            if limit_exceeded and not response_started:
                # Тело ответа на усечённый запрос — тоже подавляется.
                return
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except Exception:
            if not (limit_exceeded and not response_started):
                raise
            # Приложение упало, прочитав усечённое тело. Не глотаем молча:
            # иначе настоящий `500` (ошибка БД, `AttributeError`) маскируется
            # под клиентский `413` и исчезает из мониторинга.
            logger.warning(
                "application failed on truncated body; answering 413",
                exc_info=True,
            )
        if limit_exceeded and not response_started:
            await _send_too_large(send)
