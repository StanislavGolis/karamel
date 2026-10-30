"""AC-1 стори 12.7: у анкеты НЕТ постоянной витрины `/profiles/{uuid}`.

Замер CS (M1–M4) показал, что маршрута нет: `grep '@router\.' profiles.py`
даёт 9 маршрутов, и `/{uuid}` среди них нет; живой запрос с валидным UUID
и с числом оба дают `404 {"code":"not_found"}`. AC-1 выполнен ДО стори,
поэтому здесь не реализация, а ЗАМОК: тест обязан покраснеть, если маршрут
появится в любой форме.

Почему параметризованы две формы пути:

- **типизированный** `/{uuid}` (`uuid_lib.UUID`) даёт `422` на `/profiles/1`
  и `401`/`403` на валидный UUID без сессии — оба кода отличны от 404
  маршрута, и тест красит оба случая;
- **строковый** `/{value}` (`str`) даёт либо `200`, либо 404 ПОЛЬЗОВАТЕЛЯ
  (`code` отличается от `not_found` маршрута) — тоже отличимо.

`test_routes_no_numeric_ids.py` НЕ дублируется: там проверяются числовые
идентификаторы в маршрутах, валидного UUID в пути нет (Task 1.4).

§18.1: тест краснеет от появления маршрута, а не от смены текста. §18.2:
ассерт на `code` вместе со статусом — иначе `404` от отсутствия маршрута и
`404` от отсутствия пользователя неотличимы.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

_VALID_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def anon_client() -> AsyncClient:
    """Клиент БЕЗ сессии.

    Намеренно без авторизации: маршрута нет, поэтому FastAPI отвечает `404`
    до всякой проверки сессии. Если бы маршрут появился, запрос без сессии
    дал бы `401` — тест обязан это заметить.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_showcase_route_does_not_exist_for_valid_uuid(anon_client: AsyncClient) -> None:
    """AC-1: `GET /profiles/{uuid}` нет — валидный UUID даёт 404 маршрута.

    Мутация U1 из таблицы P-29 (добавить `@router.get("/{uuid}")`) делает
    этот тест красным: появится `401` (без сессии) или `200`/`404` пользователя.
    """
    response = await anon_client.get(f"/api/v1/profiles/{_VALID_UUID}")

    assert response.status_code == 404
    # AC-1: тело именно маршрутное. `404` без сверки `code` прошёл бы и на
    # 404 ПОЛЬЗОВАТЕЛЯ, то есть на появившемся маршруте с валидным UUID
    # несуществующего человека — мутация U1 осталась бы незамеченной.
    assert response.json() == {"code": "not_found"}


async def test_showcase_route_does_not_exist_for_numeric_path(anon_client: AsyncClient) -> None:
    """AC-1: `/profiles/1` — тот же 404 маршрута, не `422`.

    Мутация U2 (типизировать маршрут `str`, чтобы число прошло) делает тест
    красным: `422` от Pydantic или `200`/404 пользователя вместо 404 маршрута.
    """
    response = await anon_client.get("/api/v1/profiles/1")

    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}
