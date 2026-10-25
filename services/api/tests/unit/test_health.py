"""AC-01: /health отвечает 200 без обращения к БД и Redis."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_survives_broken_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ломаем сам источник настроек БД — health обязан ответить 200.

    Патчим `settings.database_url` на недоступный адрес и создание движка:
    если health начнёт ходить в базу, запрос упадёт или зависнет.
    """
    import app.core.db as db_module

    def _broken_engine() -> None:
        raise AssertionError("health не должен создавать движок БД")

    monkeypatch.setattr(db_module, "get_engine", _broken_engine)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
