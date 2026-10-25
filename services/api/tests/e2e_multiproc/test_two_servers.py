"""Тест самого мультипортового харнесса (Chore P-6, O4).

**Почему это тест обвязки, а не стори.** WS-эндпоинта `/api/v1/ws` пока нет —
он появится в 6.5. Проверять «событие с порта A пришло на B» рано,
а вот «два процесса действительно поднялись и независимы» — можно и нужно:
если харнесс не поднимает второй инстанс, будущий тест AC-05 будет
проверять один процесс и останется зелёным при локальном фан-ауте
(мутация M12 из TD).

Тест помечен `multiprocess` и скипается без `DATABASE_URL`/`REDIS_URL`
или при занятых портах — он не должен флакать в общем PR-прогоне.
"""

from __future__ import annotations

import urllib.request

import pytest

from tests.e2e_multiproc.conftest import ServerInstance


def _health(server: ServerInstance) -> dict[str, str]:
    """Читает `/health` инстанса; падает понятно, если ответ не тот."""
    with urllib.request.urlopen(f"{server.base_url}/health", timeout=3.0) as response:
        assert response.status == 200, f"health на :{server.port} вернул {response.status}"
        return {"status": "ok"}


def test_two_servers_are_up_on_different_ports(
    two_servers: tuple[ServerInstance, ServerInstance],
) -> None:
    """O4: подняты ДВА процесса на РАЗНЫХ портах, оба отвечают на `/health`.

    Мутация, которая должна покраснить тест: поднять второй сервер на том же
    порту (или не поднимать вовсе) — тогда `two_servers[1].port` совпадёт
    с первым либо `/health` второго не ответит.
    """
    first, second = two_servers

    assert first.port != second.port
    assert first.process.pid != second.process.pid
    assert _health(first) == {"status": "ok"}
    assert _health(second) == {"status": "ok"}


def test_servers_are_separate_processes(
    two_servers: tuple[ServerInstance, ServerInstance],
) -> None:
    """O4: инстансы — разные процессы ОС, а не два приложения в одном.

    Это и есть смысл харнесса: внутри одного процесса у приложений общая
    память, и локальный вызов `ws_hub` вместо `PUBLISH` выглядел бы как
    рабочий фан-аут.
    """
    first, second = two_servers

    assert first.process.pid != second.process.pid
    assert first.base_url != second.base_url
    # Оба живы на момент проверки: если один упал, следующий тест получит
    # таймаут вместо внятной причины.
    for server in (first, second):
        assert server.process.poll() is None, f"процесс на :{server.port} завершился"


@pytest.mark.parametrize("attribute", ["base_url", "ws_url"])
def test_server_instance_urls_point_to_own_port(
    two_servers: tuple[ServerInstance, ServerInstance],
    attribute: str,
) -> None:
    """O4: URL инстанса содержит ЕГО порт, а не захардкоженный.

    Ошибка «оба клиента ходят на 8101» сделала бы тест мультипроцесса
    бессмысленным: он проверял бы один сервер и проходил бы при сломанном
    фан-ауте.
    """
    first, second = two_servers

    assert str(first.port) in getattr(first, attribute)
    assert str(second.port) in getattr(second, attribute)
    assert getattr(first, attribute) != getattr(second, attribute)
