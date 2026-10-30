"""`/health/deep` — процесс + БД + Redis (стори 10.7, Task 2.3; AC-01).

**Почему unit, а не integration.** Маршрут не трогает доменные таблицы: он
делает `SELECT 1` и `PING`. Дорогая обвязка integration (миграции, транзакция,
`TRUNCATE`) не добавила бы ни одной проверки — добавляла бы только время.

**Что здесь проверяется и чего не проверяется.** Проверяется КОНТРАКТ
маршрута: форма тела, код ответа и ИЗОЛЯЦИЯ проверок (отказ Redis не должен
делать `checks.db` отказом). Не проверяется реальная недоступность
PostgreSQL: это состояние не воспроизводится подменой — оно воспроизводится
остановкой контейнера, и такой тест жил бы в `docker-compose`, а не в pytest.

**Почему `/health` здесь не проверяется.** Его контракт — стори 1.1, он
защищён `tests/unit/test_health.py:19-36` и в 10.7 НЕ меняется (D-10.7-1).
Дублирование той же проверки здесь выглядело бы как «покрыли больше».
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from app.main import app


@pytest.fixture
async def client() -> Any:
    """Клиент к приложению без сети: `ASGITransport` дёргает ASGI напрямую."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_deep_health_ok_when_dependencies_alive(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: обе зависимости живы → 200 `ok` и оба `checks` равны `ok`.

    Проверки подменяются, а не выполняются: тест обязан быть воспроизводимым
    на машине без PostgreSQL и Redis, а проверяемое здесь — форма ответа,
    не работоспособность стенда.
    """
    from app.api.v1 import health as health_module

    async def _ok() -> str:
        return health_module.OK

    async def _backup_ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "_check_db", _ok)
    monkeypatch.setattr(health_module, "_check_redis", _ok)
    monkeypatch.setattr(health_module, "backup_freshness", _backup_ok)

    response = await client.get("/health/deep")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Стори 11.10 (AC-4, З-11): третий признак. Точное равенство словаря из
    # стори 10.7 стало ложным осознанно — иначе тест краснел бы «по факту»,
    # а не по решению. Формулировка ниже проверяет те же два ключа и наличие
    # третьего, не привязываясь к его значению в dev-окружении теста.
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert set(body["checks"]) == {"db", "redis", "backup"}


async def test_deep_health_reports_redis_failure_without_breaking_db(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: отказ Redis → 503, `checks.redis` = fail, `checks.db` = ok.

    Это проверка ИЗОЛЯЦИИ: без неё диагноз «лежит всё» появлялся бы там, где
    лёг один Redis, и дежурный шёл бы чинить базу. Код `fail:` — не `500`:
    `500` от health-маршрута неотличим от падения процесса, и алерт соврал бы
    о причине.
    """
    from app.api.v1 import health as health_module

    async def _redis_down() -> str:
        raise RedisError("connection refused")

    async def _ok() -> str:
        return health_module.OK

    async def _backup_ok() -> str:
        return "ok"

    # `_check_db` подменяется на `ok` НАМЕРЕННО: тест про ИЗОЛЯЦИЮ Redis, а не
    # про доступность PostgreSQL. Без подмены он требовал бы живой базы (в
    # локальном прогоне — `fail:gaierror`), то есть проверял бы стенд, а не
    # маршрут. Тот же класс, что `W-11.8-a`.
    monkeypatch.setattr(health_module, "_check_db", _ok)
    monkeypatch.setattr(health_module, "backup_freshness", _backup_ok)
    monkeypatch.setattr(health_module, "_check_redis", _redis_down)

    response = await client.get("/health/deep")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "fail:RedisError"
    # Изоляция проверок: отказ Redis НЕ делает базу отказом.
    assert body["checks"]["db"] == "ok"
    # И не делает отказом признак бэкапа: три проверки независимы.
    assert "backup" in body["checks"]


async def test_deep_health_reports_db_failure_without_breaking_redis(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-6 (CR 10.7): отказ БД при живом Redis → 503 и `checks.db = fail:...`.

    **Зачем отдельный тест.** Первая версия файла проверяла изоляцию в одну
    сторону: отказ Redis не делал отказом базу. Обратной ветки не было, и
    `_check_db` можно было заменить на хардкод `return OK` — все четыре
    теста оставались зелёными, хотя половина AC-01 (БД) не проверялась.

    **Почему ловушка, а не подмена `_check_db`.** Подмена самой проверки
    доказывала бы, что маршрут читает её результат, но НЕ что проверка
    вообще способна сообщить об отказе. Здесь `_check_db` работает
    по-настоящему: ломается только движок, ниже его. Если тело `_check_db`
    когда-нибудь станет `return OK`, тест покраснеет.
    """
    from app.api.v1 import health as health_module

    class _BrokenEngine:
        """Движок, соединение с которым невозможно (БД лежит)."""

        def connect(self) -> Any:
            class _Ctx:
                async def __aenter__(self) -> Any:
                    raise OSError("connection refused")

                async def __aexit__(self, *args: Any) -> None:
                    return None

            return _Ctx()

    async def _ok() -> str:
        return health_module.OK

    async def _backup_ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "get_engine", lambda: _BrokenEngine())
    monkeypatch.setattr(health_module, "_check_redis", _ok)
    monkeypatch.setattr(health_module, "backup_freshness", _backup_ok)

    response = await client.get("/health/deep")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"] == "fail:OSError"
    # Изоляция в обратную сторону: отказ БД НЕ делает Redis отказом.
    assert body["checks"]["redis"] == "ok"
    assert "backup" in body["checks"]


async def test_deep_health_timeout_becomes_failure_not_500(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: зависшая проверка → `fail:TimeoutError`, а не 500 и не вечное ожидание.

    `wait_for` срабатывает по `_CHECK_TIMEOUT_SECONDS`; тест подменяет предел
    сотыми долями секунды, чтобы не ждать три секунды на каждом прогоне.
    Проверяется именно превращение таймаута в СТРОКУ диагноза — ветка, из-за
    которой маршрут возвращает 503, а не падает.
    """
    from app.api.v1 import health as health_module

    async def _hanging() -> str:
        import asyncio

        await asyncio.sleep(10)
        return health_module.OK

    async def _ok() -> str:
        return health_module.OK

    async def _backup_ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "_check_db", _hanging)
    monkeypatch.setattr(health_module, "_check_redis", _ok)
    monkeypatch.setattr(health_module, "backup_freshness", _backup_ok)
    monkeypatch.setattr(health_module, "_CHECK_TIMEOUT_SECONDS", 0.01)

    response = await client.get("/health/deep")

    assert response.status_code == 503
    assert response.json()["checks"]["db"] == "fail:TimeoutError"


async def test_plain_health_still_does_not_touch_database(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-10.7-1: `/health` не изменился — БД в нём по-прежнему не создаётся.

    Дубль контракта 1.1 стоит здесь намеренно: расширение `/health` — самая
    вероятная «улучшайзинговая» правка следующей стори, и этот тест падает
    первой строкой (`AssertionError` из подменённого `get_engine`).
    """
    import app.core.db as db_module

    def _broken_engine() -> None:
        raise AssertionError("health не должен создавать движок БД")

    monkeypatch.setattr(db_module, "get_engine", _broken_engine)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_stale_backup_keeps_http_200(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A-9 (AC-4 стори 11.10): `backup: stale` НЕ меняет HTTP-код и `status`.

    **Почему это отдельный тест, а не следствие предыдущих.** Все прежние
    тесты держат `backup` в `ok`. Мутация «`stale` → 503» проходила бы их
    все: код 200 остаётся 200 при `ok`, а при `stale` никто не смотрел.

    **Почему именно 200.** `503` погасил бы `healthcheck` контейнера `api`
    (compose начнёт перезапускать api при живом сервисе), а `health-probe`
    послал бы алерт «всё лежит» — то есть устаревший бэкап маскировался бы
    под отказ продукта. Деградация бэкапа — не отказ сервиса.
    """
    from app.api.v1 import health as health_module

    async def _ok() -> str:
        return health_module.OK

    async def _stale() -> str:
        return "stale"

    monkeypatch.setattr(health_module, "_check_db", _ok)
    monkeypatch.setattr(health_module, "_check_redis", _ok)
    monkeypatch.setattr(health_module, "backup_freshness", _stale)

    response = await client.get("/health/deep")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["backup"] == "stale"
