"""API настроек канала уведомлений (стори 9.3; AC-01, AC-02; Task 4, 6.3).

**Почему `422` и `409` проверяются на РЕАЛЬНЫХ запросах через `TestClient`.**
Проверка констант («в сервисе брошено исключение нужного класса») не доказывает,
что клиент получит нужный код: маппинг живёт в роуте, и его отсутствие видно
только по HTTP-ответу (Task 5 из Testing Requirements).

**Почему `enabled` сравнивается по имени типа, а не по индексу.** Порядок 11
типов — часть контракта (D-9.3-8), но тест, читающий `items[3]`, падал бы при
добавлении типа в середину перечня, ничего не сказав о дефекте. Порядок
проверяется ОТДЕЛЬНО и ровно один раз.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NOTIFICATION_TYPES, REQUIRED_NOTIFICATION_TYPES
from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _settings(client: AsyncClient, token: str, *, user_id: int) -> dict[str, Any]:
    """`GET /notifications/settings` → `{type: (enabled, required)}`.

    `user_id` в подписи намеренно: тест обязан явно сказать, ЧЬИ настройки он
    читает, иначе проверка «настройка сохраняется» ничего не доказывает.
    """
    del user_id
    response = await client.get("/api/v1/notifications/settings", headers=cookie_headers(token))
    assert response.status_code == 200, response.text
    return {item["type"]: (item["enabled"], item["required"]) for item in response.json()["items"]}


async def test_get_returns_all_eleven_types_in_order(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, M10: `GET` отдаёт ПОЛНЫЙ перечень в порядке `NOTIFICATION_TYPES`.

    Мутация M10 («отдаёт только отключённые») роняет первую же проверку: у
    нового пользователя отключённых нет, и ответ был бы пустым — клиент
    показал бы экран без единого переключателя.
    """
    user, token = await make_user(session)

    response = await client.get("/api/v1/notifications/settings", headers=cookie_headers(token))
    assert response.status_code == 200, response.text

    items = response.json()["items"]
    assert [item["type"] for item in items] == [str(t) for t in NOTIFICATION_TYPES]
    assert response.headers["cache-control"] == "no-store"

    # CR 9.3: вторая половина контракта — ЗНАЧЕНИЯ `enabled`, а не только
    # состав перечня. Мутация M10 («всегда enabled=true») проходила проверку
    # типов: ответ содержал все 11 элементов, и тест был зелёным. Теперь у
    # пользователя с ОДНИМ отключённым типом ровно один `false` — это ловит
    # и M10, и M11 (инверсия дефолта), и «отдаёт только отключённые».
    await client.patch(
        "/api/v1/notifications/settings",
        headers=cookie_headers(token),
        json={"type": "report.created", "enabled": False},
    )
    after = (
        await client.get("/api/v1/notifications/settings", headers=cookie_headers(token))
    ).json()["items"]
    assert [item["enabled"] for item in after].count(False) == 1, after
    assert {item["type"]: item["enabled"] for item in after}["report.created"] is False


async def test_new_user_has_everything_enabled(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, M11: дефолт — «всё включено» (opt-out, D-9.3-3).

    Мутация M11 (дефолт `enabled=false` при отсутствии строки) делает нового
    пользователя глухим ко всем уведомлениям без единой строки в настройках —
    и заметить это в UI он не сможет: переключатели просто все выключены.
    """
    user, token = await make_user(session)
    settings = await _settings(client, token, user_id=user.id)

    assert len(settings) == len(NOTIFICATION_TYPES)
    assert all(enabled for enabled, _ in settings.values()), settings


async def test_required_types_flagged_and_cannot_be_disabled(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, D-9.3-5, M9: неотключаемые отмечены `required` и дают `409`.

    Первая половина (флаг `required`) краснеет, если перечень обязательных
    потерян в ответе; вторая (`409`) — если снять гард: клиент увидел бы
    «недоступен», а API молча выключил бы санкцию, нарушив FR38/FR39.
    """
    user, token = await make_user(session)
    settings = await _settings(client, token, user_id=user.id)

    required_names = {str(t) for t in REQUIRED_NOTIFICATION_TYPES}
    assert {name for name, (_, required) in settings.items() if required} == required_names

    for required_type in REQUIRED_NOTIFICATION_TYPES:
        response = await client.patch(
            "/api/v1/notifications/settings",
            headers=cookie_headers(token),
            json={"type": str(required_type), "enabled": False},
        )
        assert response.status_code == 409, response.text
        assert response.json() == {"code": "notification_type_required"}

    # Ни одна строка не создана: гард сработал ДО записи, а не «записал и откатил».
    rows = await session.execute(
        text("SELECT count(*) FROM notification_mutes WHERE user_id = :u"), {"u": user.id}
    )
    assert int(rows.scalar_one()) == 0


async def test_disable_then_enable_is_visible_in_get(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, M5: отключение видно в `GET`, включение возвращает `true`.

    Мутация M5 (инверсия `enabled` в `PATCH`) сохраняет статус `200` и правдоподобное
    тело — ловится только сверкой с `GET`: пользователь нажимал бы «выключить»
    и получал включённое, при этом запрос «успешен».
    """
    user, token = await make_user(session)
    target = "report.created"

    response = await client.patch(
        "/api/v1/notifications/settings",
        headers=cookie_headers(token),
        json={"type": target, "enabled": False},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"type": target, "enabled": False, "required": False}

    settings = await _settings(client, token, user_id=user.id)
    assert settings[target] == (False, False)
    # Остальные не тронуты: иначе «отключил один тип» глушило бы всё.
    assert sum(1 for enabled, _ in settings.values() if not enabled) == 1

    restore = await client.patch(
        "/api/v1/notifications/settings",
        headers=cookie_headers(token),
        json={"type": target, "enabled": True},
    )
    assert restore.status_code == 200, restore.text
    assert (await _settings(client, token, user_id=user.id))[target] == (True, False)


async def test_patch_is_idempotent_and_keeps_one_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-01, M6, D-9.3-12: два одинаковых `PATCH` — `200`+`200`, одна строка.

    Мутация M6 (снять `ON CONFLICT DO NOTHING`) даёт `500` на втором запросе:
    пользователь, дважды нажавший переключатель (или обновивший вкладку),
    видел бы ошибку на корректном действии.
    """
    user, token = await make_user(session)
    body = {"type": "request.received", "enabled": False}

    for _ in range(2):
        response = await client.patch(
            "/api/v1/notifications/settings", headers=cookie_headers(token), json=body
        )
        assert response.status_code == 200, response.text

    rows = await session.execute(
        text("SELECT count(*) FROM notification_mutes WHERE user_id = :u AND type = :t"),
        {"u": user.id, "t": body["type"]},
    )
    assert int(rows.scalar_one()) == 1

    # То же для включения: повторное включение уже включённого — no-op.
    for _ in range(2):
        response = await client.patch(
            "/api/v1/notifications/settings",
            headers=cookie_headers(token),
            json={"type": body["type"], "enabled": True},
        )
        assert response.status_code == 200, response.text

    left = await session.execute(
        text("SELECT count(*) FROM notification_mutes WHERE user_id = :u"), {"u": user.id}
    )
    assert int(left.scalar_one()) == 0


async def test_unknown_type_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01: неизвестный тип — `422 invalid_notification_type` в конверте проекта.

    Проверяется и статус, и ТЕЛО: `422` от FastAPI с `{"detail": [...]}` прошёл
    бы проверку статуса, но клиент, разбирающий `code`, не смог бы показать
    текст и молча ничего не сделал бы.
    """
    user, token = await make_user(session)

    for bad_type in ("foo.bar", "request_received", ""):
        response = await client.patch(
            "/api/v1/notifications/settings",
            headers=cookie_headers(token),
            json={"type": bad_type, "enabled": False},
        )
        assert response.status_code == 422, (bad_type, response.text)
        assert response.json() == {"code": "invalid_notification_type"}


async def test_settings_persist_across_sessions(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: настройка переживает перезапуск сессии.

    Проверяется через НОВУЮ пару cookie (второй `make_user` не нужен): тот же
    пользователь входит заново и видит своё отключение. Без сохранения в БД
    настройка жила бы в памяти запроса и терялась бы при первом же рестарте
    процесса — то есть AC-02 не выполнялся бы, а AC-01 выглядел бы рабочим.
    """
    user, token = await make_user(session)
    target = "company.assembled"

    response = await client.patch(
        "/api/v1/notifications/settings",
        headers=cookie_headers(token),
        json={"type": target, "enabled": False},
    )
    assert response.status_code == 200, response.text

    # Новый токен того же пользователя — эмуляция повторного входа.
    from app.services.session_service import issue_session

    fresh = await issue_session(session, user)
    settings = await _settings(client, fresh.access_token, user_id=user.id)
    assert settings[target][0] is False


async def test_settings_require_authentication(client: AsyncClient) -> None:
    """AC-01, D-9.3-9: без сессии — `401`; настройки не публичны.

    Настройка уведомлений привязана к человеку: анонимный `GET` показал бы
    дефолт «всё включено» и позволил бы `PATCH` от чужого имени.
    """
    get_response = await client.get("/api/v1/notifications/settings")
    assert get_response.status_code == 401

    patch_response = await client.patch(
        "/api/v1/notifications/settings", json={"type": "report.created", "enabled": False}
    )
    assert patch_response.status_code == 401
