"""Мультипортовый харнесс (Chore P-6, O4; TD 6.5, AC-05/R4).

**Зачем два процесса, а не два `TestClient`.** Внутри одного процесса у двух
приложений общая память: если «фан-аут» на самом деле локальный вызов
`ws_hub`, тест этого не заметит. Публикация через Redis `PUBLISH` работает
только между НЕЗАВИСИМЫМИ подписчиками, поэтому нужны два процесса `uvicorn`
с одним `REDIS_URL` (мутация M12 из TD ловится только здесь).

**Почему порты фиксированные и со skip.** Динамический порт (`bind(0)`)
пришлось бы передавать в `uvicorn` и в URL клиента, а диагностика падений
становится нечитаемой: в логе нет порта — нет понимания, какой инстанс упал.
Занятый порт — это НЕ падение теста, а отсутствие условий (параллельный
прогон, чужой процесс), поэтому `pytest.skip`, а не `fail`: флакающий
тест хуже отсутствующего (§18.2, Murat).

**Почему `skipif` вместо `xfail`.** Харнесс требует реальных PostgreSQL и
Redis и не должен «тихо зеленеть»: без окружения он скипается с причиной,
как и остальные integration-тесты проекта.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")

pytestmark = [
    pytest.mark.multiprocess,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="нужны реальные PostgreSQL и Redis: DATABASE_URL + REDIS_URL",
    ),
]

#: Порты фиксированы: в отчёте о падении сразу видно, какой инстанс сломался.
PORT_A = 8101
PORT_B = 8102

STARTUP_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 10.0


@dataclass(frozen=True)
class ServerInstance:
    """Один запущенный инстанс API."""

    port: int
    process: subprocess.Popen[bytes]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/ws"


def _port_is_free(port: int) -> bool:
    """Проверяет, что порт свободен прямо сейчас.

    `SO_REUSEADDR` не ставится намеренно: иначе проверка считала бы свободным
    порт в `TIME_WAIT` и `uvicorn` упал бы на старте с `EADDRINUSE` — тест
    вместо skip получал бы таймаут готовности.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _wait_until_ready(server: ServerInstance) -> None:
    """Поллит `GET /health` до готовности или таймаута.

    Без поллинга тест гонялся бы со стартом сервера: `uvicorn` открывает
    сокет раньше, чем приложение готово отвечать (lifespan ещё идёт), и
    первый WS-handshake получал бы отказ по ложной причине.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error: str = "нет попыток"
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            raise RuntimeError(
                f"uvicorn на :{server.port} завершился с кодом "
                f"{server.process.returncode} до готовности"
            )
        try:
            with urllib.request.urlopen(f"{server.base_url}/health", timeout=1.0) as response:
                if response.status == 200:
                    return
                last_error = f"health вернул {response.status}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = repr(exc)
        time.sleep(0.1)
    raise RuntimeError(
        f"uvicorn на :{server.port} не поднялся за {STARTUP_TIMEOUT} с ({last_error})"
    )


def _start_server(port: int) -> ServerInstance:
    """Поднимает `uvicorn app.main:app` отдельным процессом.

    Переменные окружения передаются копией `os.environ`: процесс обязан
    видеть те же `DATABASE_URL`/`REDIS_URL`, что и тест, иначе он поднимется
    с настройками из `.env` (там docker-имена `postgres`/`redis`) и упадёт.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(API_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return ServerInstance(port=port, process=process)


def _stop_server(server: ServerInstance) -> None:
    """Останавливает процесс; `kill` — если `terminate` не помог.

    Без этого шага процессы переживают прогон, держат порты и Redis-подписки,
    и следующий запуск падает на занятом порту. Это самый дорогой класс
    ошибок в тестах реального времени.
    """
    if server.process.poll() is not None:
        return
    server.process.terminate()
    try:
        server.process.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        server.process.kill()
        server.process.wait(timeout=SHUTDOWN_TIMEOUT)


@pytest.fixture(scope="module")
def two_servers() -> Iterator[tuple[ServerInstance, ServerInstance]]:
    """Два инстанса API на разных портах, один PostgreSQL и один Redis.

    Scope `module`, а не `function`: старт двух процессов занимает секунды,
    и поднимать их на каждый тест — прямая дорога к минутам в наборе.
    """
    busy = [port for port in (PORT_A, PORT_B) if not _port_is_free(port)]
    if busy:
        pytest.skip(f"порты заняты ({busy}) — мультипроцессный тест пропущен")

    servers: list[ServerInstance] = []
    try:
        for port in (PORT_A, PORT_B):
            servers.append(_start_server(port))
        for server in servers:
            _wait_until_ready(server)
        yield servers[0], servers[1]
    finally:
        for server in servers:
            _stop_server(server)
