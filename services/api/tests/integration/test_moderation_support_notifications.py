"""Уведомление `support.replied` (стори 9.6; AC-02, FR81, ADR-11, ADR-13).

**Проверка идёт через РЕАЛЬНЫЙ Redis и реальную БД** (урок CR 9.4): мок
`publish_to_user` доказал бы только факт вызова. Здесь `publish_to_user`
работает по-настоящему (Redis поднят), поэтому `delivered_at` заполняется
именно потому, что событие ушло в канал `ws:user:{uuid}`.

**Безличность payload'а проверяется РЕКУРСИВНО** (§18.11, FR81): `ticket_uuid`
— единственный ключ, и вложенный объект не должен прятать `user_id`,
`answered_by_id` или текст ответа.

**Порядок `commit` → уведомление** (ADR-11, M11): тест с падением создания
строки уведомления доказывает, что доменный ответ остаётся зафиксированным.
Если бы уведомление шло ДО `commit`, его `rollback` откатил бы и ответ.
"""

from __future__ import annotations

import json
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationType
from app.repositories import notifications as notifications_repo_module
from app.services import notification_service
from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("DATABASE_URL") and os.environ.get("REDIS_URL")),
        reason="нужны DATABASE_URL и REDIS_URL",
    ),
]

_QUEUE_URL = "/api/v1/moderation/support/tickets"


def _key_paths(value: Any, prefix: str = "") -> list[str]:
    """Все пути ключей рекурсивно — вложенный объект не должен прятать личность."""
    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(_key_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            paths.extend(_key_paths(nested, f"{prefix}[{index}]"))
    return paths


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _insert_ticket(session: AsyncSession, *, user_id: int, body: str) -> uuid_lib.UUID:
    result = await session.execute(
        text("INSERT INTO support_tickets (user_id, body) VALUES (:u, :b) RETURNING uuid"),
        {"u": user_id, "b": body},
    )
    return result.scalar_one()


async def _reply(client: AsyncClient, token: str, ticket_uuid: Any, answer: str) -> Any:
    return await client.post(
        f"{_QUEUE_URL}/{ticket_uuid}/reply",
        headers=cookie_headers(token),
        json={"answer": answer},
    )


async def test_reply_creates_support_replied_notification(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, M8: ответ создаёт РОВНО ОДНУ строку `support.replied` автору.

    M8 (не вызывать уведомление в роутере) роняет число строк: автор не узнал
    бы об ответе. Адресат проверяется `user_id` строки: уведомление участнику
    или модератору означало бы ошибку адресации.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await _reply(client, moderator_token, ticket_uuid, "Ответ")

    assert response.status_code == 200, response.text
    rows = (
        await session.execute(
            text("SELECT user_id, type, payload, delivered_at FROM notifications WHERE type = :t"),
            {"t": NotificationType.SUPPORT_REPLIED.value},
        )
    ).all()
    assert len(rows) == 1, f"ожидалась одна строка support.replied, получено {len(rows)} (M8)"
    assert rows[0].user_id == author.id, "уведомление ушло не автору обращения"
    # §18.11: `delivered_at` заполнен, потому что публикация РЕАЛЬНАЯ (Redis жив).
    assert rows[0].delivered_at is not None, "delivered_at пуст при живой публикации"


async def test_notification_payload_has_only_ticket_uuid(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-02, M9, FR81/ADR-13: payload — РОВНО `{ticket_uuid}`.

    M9 (`{ticket_uuid, user_id}`) роняет первую проверку. Рекурсивный обход
    путей (§18.11) нужен, потому что вложенный объект спрятал бы личность от
    проверки «плоского» множества ключей.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await _reply(client, moderator_token, ticket_uuid, "Секретный ответ")
    assert response.status_code == 200, response.text

    raw = (
        await session.execute(
            text("SELECT payload FROM notifications WHERE type = :t"),
            {"t": NotificationType.SUPPORT_REPLIED.value},
        )
    ).scalar_one()
    payload = raw if isinstance(raw, dict) else json.loads(raw)

    assert set(payload) == {"ticket_uuid"}, payload
    assert payload["ticket_uuid"] == str(ticket_uuid)

    # FR81: ни личности, ни текста — ни в ключах, ни во вложенных объектах.
    paths = _key_paths(payload)
    for forbidden in ("user_id", "answered_by_id", "author_uuid", "answer", "body", "moderator_id"):
        assert not any(path.endswith(forbidden) for path in paths), (forbidden, paths)
    # Текст ответа не должен встречаться и как ЗНАЧЕНИЕ (payload — не копия ответа).
    assert "Секретный ответ" not in json.dumps(payload, ensure_ascii=False)


async def test_reply_succeeds_when_type_muted(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, gate 9.3: тип отключён — строки НЕТ, ответ всё равно `200`.

    Gate стоит внутри `INSERT` (`mute_gate_sql`), поэтому отключение не даёт
    ни строки, ни ошибки. Краснеет, если `support.replied` попадёт в
    `REQUIRED_NOTIFICATION_TYPES` (тогда gate перестал бы работать) или если
    отключение начнёт ломать ответ модератора.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid = await _insert_ticket(session, user_id=author.id, body="Обращение")
    await session.execute(
        text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
        {"u": author.id, "t": NotificationType.SUPPORT_REPLIED.value},
    )

    response = await _reply(client, moderator_token, ticket_uuid, "Ответ")

    assert response.status_code == 200, response.text
    count = (
        await session.execute(
            text("SELECT count(*) FROM notifications WHERE user_id = :u AND type = :t"),
            {"u": author.id, "t": NotificationType.SUPPORT_REPLIED.value},
        )
    ).scalar_one()
    assert count == 0, "уведомление создано при отключённом типе"
    row = (
        await session.execute(
            text("SELECT status FROM support_tickets WHERE uuid = :u"), {"u": ticket_uuid}
        )
    ).scalar_one()
    assert row == "answered", "отключение уведомления отменило ответ"


async def test_notification_failure_does_not_rollback_reply(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-02, M11, ADR-11, D-9.2-12: сбой уведомления не откатывает ответ.

    M11 (уведомление ДО `commit`) роняет СОСТОЯНИЕ: `_create_publish_mark`
    на ошибке делает `rollback`, и без предварительного коммита он снял бы
    сам ответ — обращение вернулось бы в `open`.
    """

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(notifications_repo_module.NotificationsRepository, "create_one", _boom)

    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await _reply(client, moderator_token, ticket_uuid, "Ответ")

    assert response.status_code == 200, response.text
    row = (
        await session.execute(
            text("SELECT status, answer, answered_at FROM support_tickets WHERE uuid = :u"),
            {"u": ticket_uuid},
        )
    ).one()
    assert row.status == "answered", "сбой уведомления откатил ответ (M11)"
    assert row.answer == "Ответ"
    assert row.answered_at is not None


async def test_notification_failure_logs_error(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-02, D-9.2-12: сбой уведомления пишется `notification.create.failed`.

    Без строки в логе сбой доставки был бы неотличим от «тип отключён» —
    два разных исхода настроек и инфраструктуры, которые обслуживаются
    по-разному. `capture_logs` из `structlog.testing` (как в 8.6), а не фикстура:
    перехват идёт вокруг ВЫЗОВА сервиса, потому что `_create_publish_mark`
    логирует ошибку до того, как роутер вернёт ответ.
    """
    from structlog.testing import capture_logs

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(notifications_repo_module.NotificationsRepository, "create_one", _boom)

    author, _author_token = await make_user(session)

    with capture_logs() as captured:
        result = await notification_service.notify_support_replied(
            session,
            user_uuid=author.uuid,
            user_id=author.id,
            ticket_uuid=uuid_lib.uuid4(),
        )

    assert result is False, "сбой создания уведомления не должен считаться доставкой"
    messages = [event.get("event") for event in captured]
    assert "notification.create.failed" in messages, messages


async def test_when_publish_refused_delivered_at_stays_null(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-02, D-9.2-5: публикация не удалась — `delivered_at IS NULL`, ответ `200`.

    §18.11: `delivered_at` проверяется в ОБОИХ значениях (успех — тест выше,
    неуспех — здесь). Заполненная отметка при неудаче соврала бы остальным
    инстансам: событие до них не дошло.
    """

    async def _refuse(**kwargs: Any) -> bool:
        return False

    # Патч в МОДУЛЕ-ПОТРЕБИТЕЛЕ: `notification_service` импортирует имя напрямую.
    monkeypatch.setattr(notification_service, "publish_to_user", _refuse)

    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await _reply(client, moderator_token, ticket_uuid, "Ответ")

    assert response.status_code == 200, response.text
    row = (
        await session.execute(
            text("SELECT delivered_at FROM notifications WHERE type = :t"),
            {"t": NotificationType.SUPPORT_REPLIED.value},
        )
    ).one()
    assert row.delivered_at is None, "delivered_at заполнен при неуспешной публикации"


async def test_support_replied_is_not_required_type() -> None:
    """AC-02, gate 9.3: `support.replied` — ОТКЛЮЧАЕМЫЙ тип.

    Мутация «добавить тип в `REQUIRED_NOTIFICATION_TYPES`» отключила бы для
    него gate: пользователь не смог бы выключить канал, а пункт PRD «ответ
    поддержки» не отменяется отключением уведомления — ответ виден в списке.
    """
    from app.models.enums import REQUIRED_NOTIFICATION_TYPES

    assert NotificationType.SUPPORT_REPLIED not in REQUIRED_NOTIFICATION_TYPES
