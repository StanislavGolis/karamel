"""Конверт ошибок FastAPI (CR 2.3, патчи 13–14).

§18.2: проверяется форма тела и заголовок ответа, а не факт вызова
обработчика. Оба свойства наблюдаемы клиентом: по `code` он выбирает текст
ошибки, по `cache-control` — можно ли ответ кэшировать.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from app.main import http_error_handler, validation_error_handler

app = FastAPI()
app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]


@app.get("/plain-401")
async def plain_401() -> None:
    """Зависимость авторизации бросает исключение с текстом, а не с конвертом."""
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


@app.get("/plain-403")
async def plain_403() -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@app.get("/plain-500")
async def plain_500() -> None:
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="boom: secret")


@app.get("/coded")
async def coded() -> None:
    """Готовый конверт проходит без изменений — он уже закрытый."""
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "profile_exists"})


client = TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/plain-401", "unauthenticated"),
        ("/plain-403", "forbidden"),
        ("/plain-500", "internal_error"),
    ],
)
def test_plain_detail_maps_to_closed_code(path: str, expected: str) -> None:
    """CR 2.3, патч 13: код выводится из статуса, а не из текста `detail`.

    Прежний обработчик собирал третий конверт `{"code": "error", "message":
    str(detail)}`: клиент, разбирающий только `code`, получал бессмысленный
    литерал `error`, а `str(detail)` выносил наружу внутренний текст.
    Тест краснеет, если вернуть `error` или `message`.
    """
    response = client.get(path)

    body = response.json()
    assert body == {"code": expected}
    assert "message" not in body


def test_plain_detail_text_is_not_leaked() -> None:
    """CR 2.3, патч 13: текст исключения не попадает в ответ.

    `detail` может содержать SQL, имя таблицы или внутренний идентификатор;
    клиенту он не нужен и наружу выходить не должен.
    """
    response = client.get("/plain-500")

    assert "secret" not in response.text
    assert "boom" not in response.text


def test_prepared_envelope_is_passed_through() -> None:
    """Патч 13, обратная сторона: готовый `{"code": ...}` не переписывается.

    Роуты (`profiles.py`, `auth.py`) уже отдают закрытый конверт. Если бы
    обработчик подменял его кодом из статуса, `409 profile_exists` стал бы
    `forbidden`-подобным значением, и клиент потерял бы различие.
    """
    response = client.get("/coded")

    assert response.status_code == 409
    assert response.json() == {"code": "profile_exists"}


@pytest.mark.parametrize("path", ["/plain-401", "/plain-403", "/plain-500", "/coded"])
def test_errors_are_not_cacheable(path: str) -> None:
    """CR 2.3, патч 14: `cache-control: no-store` ставится и на ошибках.

    `profiles.py` объявляет инвариант «заголовок на каждом ответе профиля»,
    но 401/403 рождаются здесь, а не в роуте. Без заголовка промежуточный кэш
    мог бы отдать 401 другому пользователю того же устройства.
    """
    response = client.get(path)

    assert response.headers["cache-control"] == "no-store"


def test_validation_error_is_not_cacheable() -> None:
    """CR 2.3, патч 14: `422` от Pydantic тоже не кэшируется.

    Ответ 422 зависит от тела конкретного запроса; кэш, отдавший его
    повторно, скрыл бы успешное сохранение после исправления формы.
    """
    from fastapi.exceptions import RequestValidationError
    from pydantic import BaseModel

    validation_app = FastAPI()
    validation_app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]

    class Body(BaseModel):
        value: int

    @validation_app.post("/body")
    async def body(payload: Body) -> dict[str, int]:
        return {"value": payload.value}

    validation_client = TestClient(validation_app, raise_server_exceptions=False)
    response = validation_client.post("/body", json={"value": "not-a-number"})

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
