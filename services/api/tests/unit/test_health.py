"""AC-01: /health отвечает 200 без обращения к БД и Redis.

**Почему гард стоит на КЭШЕ движка, а не на `get_engine`** (P-2 из CR 10.7).
Первая версия теста патчила `app.core.db.get_engine` и держалась на форме
импорта в `main.py`: пока геттер импортировался ВНУТРИ функции, подмена
работала. Как только `main.py` получает `from app.core.db import ... get_engine`
на уровне модуля, ссылка копируется в пространство имён `main` до подмены, и
реальный `SELECT 1` в `/health` проходит ЗЕЛЁНЫМ. Замерено ревьюером:
мутация M5b (модульный импорт + `SELECT 1`) дала `2 passed`.

Подмена `_engine` от формы импорта не зависит: и `get_engine()`, и прямой
`from app.core.db import _engine` читают один и тот же модульный атрибут в
момент обращения. Дополнительно закрыт `create_async_engine` — путь «движка
ещё нет, создадим» тоже не должен срабатывать в `/health`.

**Redis** закрыт третьим слоем: `get_redis` подменяется функцией, бросающей
`AssertionError`. `/health` не имеет права ходить и туда (контракт 1.1:
никаких внешних вызовов).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


class _EngineTrap:
    """Объект на месте `app.core.db._engine`: любое обращение — провал теста.

    Ловушка на ВСЕ методы, а не только `connect`: `SELECT 1` может пойти и
    через `engine.begin()`, и через `engine.raw_connection()`. Проверяется
    факт «движок тронут», а не конкретный способ.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"health не должен трогать движок БД (обращение к .{name})")


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ломает ТРИ пути к БД сразу: кэш, создание движка, фабрику сессий.

    Подмена одного `get_engine` недостаточна: она ловится только при
    локальном импорте. Здесь `/health` не может дойти до PostgreSQL ни
    через кэш, ни через создание нового движка, ни через фабрику сессий.
    """
    import app.core.db as db_module

    monkeypatch.setattr(db_module, "_engine", _EngineTrap())
    monkeypatch.setattr(db_module, "_session_factory", _EngineTrap())

    def _no_engine_creation(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("health не должен СОЗДАВАТЬ движок БД")

    monkeypatch.setattr(db_module, "create_async_engine", _no_engine_creation)


@pytest.fixture
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/health` не имеет права ходить в Redis: контракт 1.1 — без вызовов."""
    import app.api.v1.health as health_module

    def _no_redis() -> Any:
        raise AssertionError("health не должен создавать клиент Redis")

    monkeypatch.setattr(health_module, "get_redis", _no_redis)


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_survives_broken_database(no_database: None, no_redis: None) -> None:
    """AC-01, D-10.7-1: БД и Redis недоступны → `/health` всё равно 200 `ok`.

    Краснеет от ЛЮБОГО похода в БД в `/health`, включая модульный импорт
    `get_engine` (P-2, мутация M5b) и прямое обращение к `_engine`.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_added_database_call_turns_red(
    no_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-2: гард обязан ловить поход в БД ЛЮБЫМ способом импорта.

    Тест — контроль качества самого гарда: он подменяет маршрут `/health`
    на заведомо «плохой» (модульный импорт + `SELECT 1`) и требует, чтобы
    такой маршрут НЕ отдал 200. Если этот тест зелёный при плохом маршруте,
    значит гард снова слеп — ровно то, что нашло ревью (M5b `2 passed`).

    Проверка идёт через ловушку напрямую, а не через HTTP: тест доказывает
    СИЛУ ловушки, а не поведение реального `/health` (у него своё покрытие
    выше). Так он не зависит от того, каким маршрут будет в следующей стори.
    """
    from sqlalchemy import text

    import app.core.db as db_module

    async def _bad_health_body() -> str:
        # Тот же путь, что у M5b: движок берётся через модульный атрибут.
        engine = db_module.get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return "ok"

    with pytest.raises(AssertionError, match="health не должен трогать движок БД"):
        await _bad_health_body()
