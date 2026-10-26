"""API удаления аккаунта (стори 10.2; AC-01, AC-03, AC-05, AC-06; Task 7.2).

**Проверки идут через РЕАЛЬНЫЙ роутер и РЕАЛЬНУЮ БД** (урок CR 9.4: моки
доказывают попытку, а не результат). Ни один тест здесь не подменяет сервис
или репозиторий: `200` проверяется строкой `users.deletion_requested_at`,
`409` — неизменностью этой строки, `204` — её обнулением.

**AC-06 проверяется по времени, не по факту удаления.** Пометка ставится
`now - 8 days` прямым `UPDATE`, воркер не запускается: доступ закрывает
гард `CurrentUser`, а не воркер. Это ровно то требование, которое отличает
«окно закрывается по времени» от «окно закрывается после прогона».

**Даты — ISO 8601 UTC с `Z`** (project-context §4): формат проверяется
строкой ответа, а не разбором в `datetime`. Клиент парсит именно `Z`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

_DELETE_URL = "/api/v1/me/delete"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _markup(session: AsyncSession, user_id: int) -> datetime | None:
    """Пометка удаления из БД — источник правды, не поле объекта."""
    row = await session.execute(
        text("SELECT deletion_requested_at FROM users WHERE id = :uid"), {"uid": user_id}
    )
    return row.scalar_one()


# --- AC-01: POST ставит пометку ---


async def test_post_sets_markup_and_returns_purge_after(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01: `POST` помечает учётку и отдаёт `purge_after` для кнопки отмены.

    Красным станет, если сервис не запишет `deletion_requested_at` или
    роутер не отдаст `purge_after` (клиенту неоткуда взять дату).
    """
    user, token = await make_user(session)

    response = await client.post(_DELETE_URL, headers=cookie_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["deletion_requested_at"].endswith("Z")
    assert body["purge_after"].endswith("Z")
    stored = await _markup(session, user.id)
    assert stored is not None
    requested = datetime.fromisoformat(body["deletion_requested_at"].replace("Z", "+00:00"))
    purge = datetime.fromisoformat(body["purge_after"].replace("Z", "+00:00"))
    assert purge - requested == timedelta(days=7)


async def test_post_twice_gives_409_and_does_not_extend(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, D-10.2-7: повторный `POST` — `409`, окно НЕ продлевается.

    D-10.2-7 требует `409`, а не `200`: клиент не должен решать, продлилось
    окно или нет. Красным станет, если сервис перезапишет метку — тогда
    `purge_after` во втором ответе сдвинется вперёд.
    """
    user, token = await make_user(session)
    first = await client.post(_DELETE_URL, headers=cookie_headers(token))
    assert first.status_code == 200

    second = await client.post(_DELETE_URL, headers=cookie_headers(token))

    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "deletion_already_requested"
    # Дата окончания та же, что в первом ответе: окно не продлено.
    assert body["purge_after"] == first.json()["purge_after"]


# --- AC-05: GET отдаёт статус окна ---


async def test_get_without_markup_gives_nulls(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: без пометки оба поля `null` — клиент различает «окна нет»."""
    _user, token = await make_user(session)

    response = await client.get(_DELETE_URL, headers=cookie_headers(token))

    assert response.status_code == 200
    assert response.json() == {"deletion_requested_at": None, "purge_after": None}


async def test_get_with_markup_gives_both_dates(client: AsyncClient, session: AsyncSession) -> None:
    """AC-05: с пометкой отдаются обе даты — другого источника у клиента нет."""
    user, token = await make_user(session)
    await client.post(_DELETE_URL, headers=cookie_headers(token))

    response = await client.get(_DELETE_URL, headers=cookie_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["deletion_requested_at"] is not None
    assert body["purge_after"] is not None
    assert await _markup(session, user.id) is not None


# --- AC-03: DELETE снимает пометку, идемпотентно ---


async def test_delete_cancels_markup(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03: `DELETE` снимает пометку, данные целы, `204`."""
    user, token = await make_user(session)
    await client.post(_DELETE_URL, headers=cookie_headers(token))

    response = await client.delete(_DELETE_URL, headers=cookie_headers(token))

    assert response.status_code == 204
    assert await _markup(session, user.id) is None
    # Данные целы: учётка на месте.
    row = await session.execute(
        text("SELECT count(*) FROM users WHERE id = :uid"), {"uid": user.id}
    )
    assert row.scalar_one() == 1


async def test_delete_twice_is_idempotent(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, D-10.2-7: повторный `DELETE` тоже `204`, не `404`.

    Пользователь мог нажать дважды; `404` показал бы ошибку на успешной
    операции.
    """
    _user, token = await make_user(session)

    first = await client.delete(_DELETE_URL, headers=cookie_headers(token))
    second = await client.delete(_DELETE_URL, headers=cookie_headers(token))

    assert first.status_code == 204
    assert second.status_code == 204


# --- AC-01 против AC-06: активное окно доступ не закрывает ---


async def test_access_allowed_within_window(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: активное окно НЕ блокирует доступ — иначе отмена недостижима.

    Красным станет, если предикат вернут к `is not None` (D-10.2-1):
    `GET` отдал бы `403 user_blocked` сразу после `POST`.
    """
    _user, token = await make_user(session)
    assert (await client.post(_DELETE_URL, headers=cookie_headers(token))).status_code == 200

    response = await client.get(_DELETE_URL, headers=cookie_headers(token))

    assert response.status_code == 200


async def test_access_denied_after_window_expires(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06: после 7 суток доступ закрыт `403 user_blocked` — по времени.

    Воркер НЕ запускается: окно закрывает гард `CurrentUser`, а не факт
    удаления. Красным станет, если гард перестанет звать
    `deletion_window_expired`.
    """
    user, token = await make_user(session)
    await session.execute(
        text("UPDATE users SET deletion_requested_at = :ts WHERE id = :uid"),
        {"ts": datetime.now(UTC) - timedelta(days=8), "uid": user.id},
    )
    await session.flush()

    response = await client.get(_DELETE_URL, headers=cookie_headers(token))

    assert response.status_code == 403
    assert response.json() == {"code": "user_blocked"}


# --- AC-03 против AC-06: отмена доступна только внутри окна ---


async def test_cancel_available_inside_window_only(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-03, AC-06: отмена возможна внутри окна, после — `403` от гарда.

    **P-13 (CR 10.2): тест делает РЕАЛЬНЫЕ HTTP-запросы.** До правки он жил
    в интеграционном файле, назывался по поведению эндпоинта, но не принимал
    ни `client`, ни `session` и дёргал предикат напрямую — то есть дублировал
    unit-тест и НЕ защищал AC-03/AC-06 на уровне API. Если бы гард перестал
    звать `deletion_window_expired`, тест остался бы зелёным.

    Доказывается граница по обе стороны: пометка «6 суток 23 часа» — отмена
    проходит (`204`, метка обнулена); пометка «8 суток» — `403 user_blocked`,
    и метка остаётся на месте (отменить нечем, доступ уже закрыт).
    """
    # Сторона 1: окно активно — `DELETE` доступен и реально снимает метку.
    user, token = await make_user(session)
    await session.execute(
        text("UPDATE users SET deletion_requested_at = :ts WHERE id = :uid"),
        {"ts": datetime.now(UTC) - timedelta(days=6, hours=23), "uid": user.id},
    )
    await session.flush()

    inside = await client.delete(_DELETE_URL, headers=cookie_headers(token))

    assert inside.status_code == 204
    assert await _markup(session, user.id) is None

    # Сторона 2: окно истекло — `CurrentUser` отсекает раньше роутера.
    expired_user, expired_token = await make_user(session)
    await session.execute(
        text("UPDATE users SET deletion_requested_at = :ts WHERE id = :uid"),
        {"ts": datetime.now(UTC) - timedelta(days=8), "uid": expired_user.id},
    )
    await session.flush()

    outside = await client.delete(_DELETE_URL, headers=cookie_headers(expired_token))

    assert outside.status_code == 403
    assert outside.json() == {"code": "user_blocked"}
    # Метка НЕ снята: отмена после окна недоступна по решению продукта.
    assert await _markup(session, expired_user.id) is not None


# --- cache-control ---


async def test_responses_are_not_cacheable(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, AC-03: удаление — ПД-операция, ни один ответ не кешируется."""
    _user, token = await make_user(session)

    post = await client.post(_DELETE_URL, headers=cookie_headers(token))
    get = await client.get(_DELETE_URL, headers=cookie_headers(token))
    delete = await client.delete(_DELETE_URL, headers=cookie_headers(token))

    assert post.headers["cache-control"] == "no-store"
    assert get.headers["cache-control"] == "no-store"
    assert delete.headers["cache-control"] == "no-store"


# --- 401 без сессии ---


async def test_no_session_gives_401(client: AsyncClient) -> None:
    """Гард: без сессии — `401`, а не `403` и не `500`."""
    response = await client.post(_DELETE_URL)

    assert response.status_code == 401


# --- P-16 (CR 10.2): схема OpenAPI соответствует реальному ответу ---


def test_openapi_declares_string_dates_and_409_model() -> None:
    """P-16 (CR 10.2): `response_model` не декоративен — схема честная.

    До правки модель объявляла `datetime` (OpenAPI `date-time`), а обработчик
    возвращал `JSONResponse` с уже готовой строкой `...Z`: FastAPI отдаёт
    `JSONResponse` как есть, поэтому `response_model` не применялся вовсе.
    Клиент, сгенерированный по схеме, ждал `date-time` и получал строку.
    Плюс `409` не был описан вообще — поле `purge_after` клиент узнавал
    только из текста ошибки.

    Красным станет, если вернуть `datetime` в модель (первое поле) или
    убрать `responses=` у `POST` (второе).
    """
    from app.main import app

    schema = app.openapi()
    post = schema["paths"]["/api/v1/me/delete"]["post"]

    # 200: обе даты — строки, а не `date-time`.
    ok_schema = post["responses"]["200"]["content"]["application/json"]["schema"]
    ref = ok_schema["$ref"].rsplit("/", 1)[-1]
    properties = schema["components"]["schemas"][ref]["properties"]
    for field in ("deletion_requested_at", "purge_after"):
        variants = properties[field].get("anyOf", [])
        types = {variant.get("type") for variant in variants}
        assert "string" in types, f"{field}: OpenAPI обещает {types}, приходит строка"
        # Ключевое: `datetime` в OpenAPI — тоже `type: string`, но с
        # `format: date-time`. Фактический ответ — строка БЕЗ format, потому что
        # `_iso_utc` форматирует её сам. Проверка одного `type` не отличала бы
        # дефект от исправления (поймано доказательством откатом: тест был
        # зелёным на откате к `datetime`).
        formats = {variant.get("format") for variant in variants}
        assert "date-time" not in formats, (
            f"{field}: OpenAPI обещает date-time, а приходит строка — response_model декоративен"
        )

    # 409: тело описано моделью, а не только кодом ответа.
    conflict = post["responses"]["409"]["content"]["application/json"]["schema"]
    conflict_ref = conflict["$ref"].rsplit("/", 1)[-1]
    conflict_props = schema["components"]["schemas"][conflict_ref]["properties"]
    assert set(conflict_props) == {"code", "purge_after"}
