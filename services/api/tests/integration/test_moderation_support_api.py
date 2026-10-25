"""Кабинет модератора: очередь и ответ (стори 9.6; AC-01…AC-04).

**Проверки идут через РЕАЛЬНЫЙ роутер и РЕАЛЬНУЮ БД** (урок CR 9.4: моки
доказывают попытку, а не результат). Ни один тест здесь не подменяет сервис
или репозиторий: `200` проверяется строкой в таблице, «отвеченное уходит из
очереди» — составом ответа при строках, созданных ПРЯМЫМ `INSERT` (переходов
статуса в API создания нет).

**Сид `open`-строки с МЕНЬШИМ `id` ПЕРВОЙ** (урок `L-9.5-7`). Порядок
очереди — `created_at ASC, id ASC`, и тест, где проверяемая строка создана
раньше всех, доказывает порядок; тест, где она последняя, прошёл бы и при
обратном порядке.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.support_tickets import SupportTicketsRepository
from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

_QUEUE_URL = "/api/v1/moderation/support/tickets"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


async def _insert_ticket(
    session: AsyncSession,
    *,
    user_id: int,
    body: str,
    status: str = "open",
    answer: str | None = None,
    answered_at: datetime | None = None,
    created_at: datetime | None = None,
) -> tuple[uuid_lib.UUID, int]:
    """Прямой `INSERT` мимо API: единственный способ создать `answered`.

    AC-04 доказывается ЧТЕНИЕМ строки в статусе, который API пока не ставит:
    если бы тест создавал её через сервис ответа, он проверял бы собственный
    сид, а не предикат очереди.
    """
    result = await session.execute(
        text(
            "INSERT INTO support_tickets "
            "(user_id, body, status, answer, answered_at, created_at) "
            "VALUES (:user_id, :body, :status, :answer, :answered_at, "
            "COALESCE(:created_at, now())) "
            "RETURNING uuid, id"
        ),
        {
            "user_id": user_id,
            "body": body,
            "status": status,
            "answer": answer,
            "answered_at": answered_at,
            "created_at": created_at,
        },
    )
    row = result.one()
    return row.uuid, int(row.id)


async def _reply(
    client: AsyncClient, token: str, ticket_uuid: Any, answer: str = "Ответ поддержки"
) -> Any:
    return await client.post(
        f"{_QUEUE_URL}/{ticket_uuid}/reply",
        headers=cookie_headers(token),
        json={"answer": answer},
    )


# ——— AC-01, AC-04: очередь ———


async def test_queue_excludes_answered(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, AC-04, M1: в очереди РОВНО открытые, `total` = «сколько открытых».

    M1 (убрать `WHERE t.status='open'` из `list_open`) роняет обе проверки:
    отвеченное обращение появляется в `items`, а `total` (у которого свой
    предикат, M1 его не трогает) перестаёт совпадать с числом элементов.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    now = datetime.now(UTC)

    first, first_id = await _insert_ticket(
        session, user_id=author.id, body="Первое", created_at=now - timedelta(hours=3)
    )
    second, _second_id = await _insert_ticket(
        session, user_id=author.id, body="Второе", created_at=now - timedelta(hours=2)
    )
    answered, _answered_id = await _insert_ticket(
        session,
        user_id=author.id,
        body="Отвеченное",
        status="answered",
        answer="Готово",
        answered_at=now - timedelta(hours=1),
        created_at=now - timedelta(hours=4),
    )
    # Урок L-9.5-7: `open`-строка с МЕНЬШИМ `id` создана ПЕРВОЙ.
    assert first_id < _second_id

    response = await client.get(_QUEUE_URL, headers=cookie_headers(moderator_token))

    assert response.status_code == 200, response.text
    payload = response.json()
    uuids = [item["uuid"] for item in payload["items"]]
    assert uuids == [str(first), str(second)], "очередь не «только открытые»"
    assert str(answered) not in uuids, "отвеченное обращение осталось в очереди (M1)"
    assert payload["total"] == 2, "total считает не только открытые"


async def test_queue_oldest_first(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, M2: «старые сверху» — по `created_at ASC`.

    M2 (`ORDER BY created_at DESC`) роняет порядок: первым придёт `Второе`.
    Проверяется именно ПОРЯДОК, а не состав: состав уже доказан тестом выше,
    и объединение двух проверок скрыло бы, какая из них сломалась.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    now = datetime.now(UTC)

    older, _older_id = await _insert_ticket(
        session, user_id=author.id, body="Старое", created_at=now - timedelta(hours=5)
    )
    newer, _newer_id = await _insert_ticket(
        session, user_id=author.id, body="Новое", created_at=now - timedelta(hours=1)
    )

    response = await client.get(_QUEUE_URL, headers=cookie_headers(moderator_token))

    assert response.status_code == 200, response.text
    uuids = [item["uuid"] for item in response.json()["items"]]
    assert uuids == [str(older), str(newer)], "порядок очереди не «старые сверху» (M2)"


async def test_queue_item_shape(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, D-9.6-11: элемент — ровно семь полей, `author_uuid` вместо `user_id`.

    Краснеет, если в схему попадёт `user_id` или `answered_by_id` (§7): внутренний
    `bigint` наружу не выходит, а адрес автора — публичный `uuid`.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid, _ticket_id = await _insert_ticket(
        session, user_id=author.id, body="Тело обращения"
    )

    response = await client.get(_QUEUE_URL, headers=cookie_headers(moderator_token))

    assert response.status_code == 200, response.text
    # Task 6.2, M12: тело обращения — ПД автора, промежуточный кэш не должен
    # его сохранять. Без этого ассерта снятие заголовка в GET-ветке проходит
    # зелёным (мутация M12 SURVIVED) — §18.2 (D-98).
    assert response.headers.get("cache-control") == "no-store", "тело обращения кэшируется"
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert set(item) == {
        "uuid",
        "body",
        "status",
        "created_at",
        "answer",
        "answered_at",
        "author_uuid",
    }
    assert item["uuid"] == str(ticket_uuid)
    assert item["author_uuid"] == str(author.uuid)
    # §18.11: пара ответа проверяется в ОБОИХ значениях — здесь `null`.
    assert item["answer"] is None
    assert item["answered_at"] is None


# ——— AC-03: гард ———


async def test_queue_forbidden_for_regular_user(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, M10: без роли модератора очередь — `403 forbidden`.

    M10 (`ModeratorUser` → `CurrentUser`) роняет статус: обычный пользователь
    получил бы `200` со списком чужих обращений.
    """
    author, author_token = await make_user(session)
    await _insert_ticket(session, user_id=author.id, body="Чужое обращение")

    response = await client.get(_QUEUE_URL, headers=cookie_headers(author_token))

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden", response.json()


async def test_reply_forbidden_for_regular_user(client: AsyncClient, session: AsyncSession) -> None:
    """AC-03, M10: ответ без роли модератора — `403` и НИЧЕГО не записано.

    Мутация M10 роняет статус (`200` вместо `403`). Вторая половина — строка
    в БД: гард обязан сработать ДО чтения и записи, иначе отказ приходил бы
    после изменения состояния.
    """
    author, author_token = await make_user(session)
    ticket_uuid, ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await _reply(client, author_token, ticket_uuid, "Сам себе ответ")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden", response.json()
    row = (
        await session.execute(
            text("SELECT status, answer, answered_at FROM support_tickets WHERE id = :i"),
            {"i": ticket_id},
        )
    ).one()
    assert row.status == "open", "отказ изменил статус обращения"
    assert row.answer is None
    assert row.answered_at is None


# ——— AC-02: ответ ———


async def test_reply_sets_answered_status(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, M4, M5: `200`; статус `answered`, автор ответа записан.

    M4 (не ставить `status='answered'`) роняет первую проверку, M5 (не писать
    `answered_by_id`) — последнюю: строка осталась бы без «кто ответил».
    """
    author, _author_token = await make_user(session)
    moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid, ticket_id = await _insert_ticket(
        session, user_id=author.id, body="Не работает вход"
    )

    response = await _reply(client, moderator_token, ticket_uuid, "  Проверьте SMS  ")

    assert response.status_code == 200, response.text
    # Task 6.2, M13: текст ответа — ПД, кэшу его хранить нельзя. Без этого
    # ассерта снятие заголовка в POST-ветке проходит зелёным (M13 SURVIVED).
    assert response.headers.get("cache-control") == "no-store", "текст ответа кэшируется"
    payload = response.json()
    assert payload["status"] == "answered"
    # D-9.6-4: нормализация снимает внешние пробелы и края строк.
    assert payload["answer"] == "Проверьте SMS"
    # D-9.6-11: «кто ответил» наружу не выходит.
    assert "answered_by_id" not in payload
    assert "user_id" not in payload

    row = (
        await session.execute(
            text(
                "SELECT status, answer, answered_at, answered_by_id, created_at "
                "FROM support_tickets WHERE id = :i"
            ),
            {"i": ticket_id},
        )
    ).one()
    assert row.status == "answered"
    assert row.answer == "Проверьте SMS"
    assert row.answered_at is not None
    assert row.answered_at >= row.created_at, "ответ раньше создания обращения"
    assert row.answered_by_id == moderator.id, "не записан автор ответа (M5)"


async def test_reply_removes_ticket_from_queue(client: AsyncClient, session: AsyncSession) -> None:
    """AC-04: после ответа обращение уходит из очереди открытых.

    Проверяется СОСТАВОМ очереди после ответа, а не `status` в ответе: AC-04
    сформулирован про сортировку/выдачу, и `status` в теле доказал бы только
    запись поля.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid, _ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")

    reply = await _reply(client, moderator_token, ticket_uuid)
    assert reply.status_code == 200, reply.text

    queue = await client.get(_QUEUE_URL, headers=cookie_headers(moderator_token))

    assert queue.status_code == 200, queue.text
    payload = queue.json()
    assert payload["items"] == []
    assert payload["total"] == 0


# ——— AC-02: отказы ———


async def test_second_reply_conflicts_409(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, D-9.6-5, M6, M7: повтор — `409`, ответ НЕ перезаписан.

    M7 (сервис всегда отдаёт `404`) роняет статус: клиент получил бы
    «обращения нет» вместо «уже отвечено». M6 (убрать `AND status='open'` из
    `UPDATE`) роняет СОСТОЯНИЕ: без CAS второй ответ молча перезаписал бы
    текст и автора.
    """
    author, _author_token = await make_user(session)
    moderator, moderator_token = await make_user(session, role="moderator")
    second_moderator, second_token = await make_user(session, role="moderator")
    ticket_uuid, ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")

    # `id` читается ДО запроса: после `commit` внутри роута атрибут просроченной
    # ORM-сущности превратился бы в ленивую загрузку в sync-контексте
    # (`MissingGreenlet`), и тест упал бы не по делу.
    moderator_id = moderator.id
    second_moderator_id = second_moderator.id

    first = await _reply(client, moderator_token, ticket_uuid, "Первый ответ")
    assert first.status_code == 200, first.text

    second = await _reply(client, second_token, ticket_uuid, "Второй ответ")

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "ticket_already_answered", second.json()

    row = (
        await session.execute(
            text("SELECT answer, answered_by_id FROM support_tickets WHERE id = :i"),
            {"i": ticket_id},
        )
    ).one()
    assert row.answer == "Первый ответ", "ответ перезаписан повтором (M6)"
    assert row.answered_by_id == moderator_id, "автор ответа перезаписан (M6)"
    assert row.answered_by_id != second_moderator_id


async def test_reply_unknown_ticket_returns_404(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, D-9.6-7: несуществующее обращение — `404 ticket_not_found`.

    Отдельный код от `409`: слитый код заставил бы кабинет перечитывать
    очередь ради объяснения отказа.
    """
    _moderator, moderator_token = await make_user(session, role="moderator")

    response = await _reply(client, moderator_token, uuid_lib.uuid4())

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "ticket_not_found", response.json()


async def test_reply_non_uuid_path_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: не-`uuid` в пути — `422` от валидации, не `500`.

    Тип `ticket_uuid: UUID` отбивает мусор в Pydantic: иначе строка `abc`
    дошла бы до SQL и упала бы приведением типа (`500`).
    """
    _moderator, moderator_token = await make_user(session, role="moderator")

    response = await client.post(
        f"{_QUEUE_URL}/not-a-uuid/reply",
        headers=cookie_headers(moderator_token),
        json={"answer": "Ответ"},
    )

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "answer",
    ["", "   ", "\t\n", "\u00a0\u200b"],
    ids=["empty", "spaces", "tabs-newlines", "nbsp-zwsp"],
)
async def test_empty_answer_rejected_by_schema(
    client: AsyncClient, session: AsyncSession, answer: str
) -> None:
    """AC-02, D-9.6-4: пустой ответ — `422 invalid_request` ОТ СХЕМЫ.

    Код важен: отказ из сервиса был бы тем же `422`, и мутация «снять
    `min_length`/`mode='before'` в схеме» выжила бы, потому что рубеж
    сместился бы на второй, а не исчез.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid, ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await _reply(client, moderator_token, ticket_uuid, answer)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_request", response.json()
    row = (
        await session.execute(
            text("SELECT status, answer FROM support_tickets WHERE id = :i"), {"i": ticket_id}
        )
    ).one()
    assert row.status == "open"
    assert row.answer is None


async def test_reply_extra_field_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02: серверные поля в теле — `422`, а не молчаливый выброс.

    `extra="forbid"`: иначе модератор считал бы, что назначил `status` или
    `answered_by_id` запросом, и расхождение с БД всплыло бы позже.
    """
    author, _author_token = await make_user(session)
    _moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid, _ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")

    response = await client.post(
        f"{_QUEUE_URL}/{ticket_uuid}/reply",
        headers=cookie_headers(moderator_token),
        json={"answer": "Ответ", "answered_by_id": 1},
    )

    assert response.status_code == 422, response.text


async def test_reply_records_moderator_id(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, D-9.6-6, M5: в БД записан автор ответа; наружу он не выходит.

    M5 (не писать `answered_by_id` в `UPDATE`) роняет `answered_by_id`:
    без него спор «кто ответил» неразрешим, а `answer`/`answered_at` описывают
    только ЧТО и КОГДА. Вторая половина — ответ API: «кто ответил» не
    раскрывается (§7, D-9.6-11).
    """
    author, _author_token = await make_user(session)
    moderator, moderator_token = await make_user(session, role="moderator")
    ticket_uuid, ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")
    # `id` читается ДО запроса: после `commit` внутри роута обращение к атрибуту
    # просроченной ORM-сущности стало бы ленивой загрузкой (`MissingGreenlet`).
    moderator_id = moderator.id

    response = await _reply(client, moderator_token, ticket_uuid, "Ответ")

    assert response.status_code == 200, response.text
    answered_by_id = (
        await session.execute(
            text("SELECT answered_by_id FROM support_tickets WHERE id = :i"),
            {"i": ticket_id},
        )
    ).scalar_one()
    assert answered_by_id == moderator_id, "автор ответа не записан (M5)"
    assert "answered_by_id" not in response.json(), "внутренний id наружу (§7)"


async def test_repository_reply_cas_blocks_second_write(session: AsyncSession) -> None:
    """AC-02, D-9.6-5, M6: CAS в `UPDATE` — ВТОРАЯ линия обороны, отдельно.

    Проверка идёт на РЕПОЗИТОРИИ, а не через API: сервис отдаёт `409` по
    предпроверке под `FOR UPDATE`, и до `UPDATE` второй запрос не доходит.
    Значит HTTP-тест НЕ различает «CAS есть» и «CAS нет» — он краснеет от
    мутации в сервисе, а не в предикате. Мутация `M6` (убрать `AND status='open'`
    из `UPDATE`) ловится только здесь.

    Сценарий ровно тот, ради которого предикат написан: между предпроверкой и
    записью строку мог ответить другой модератор. Эмулируем это вторым прямым
    вызовом `reply` — он ОБЯЗАН вернуть `None`, а не перезаписать ответ.
    """
    author, _author_token = await make_user(session)
    first_moderator, _first_token = await make_user(session, role="moderator")
    second_moderator, _second_token = await make_user(session, role="moderator")
    ticket_uuid, ticket_id = await _insert_ticket(session, user_id=author.id, body="Обращение")
    first_moderator_id = first_moderator.id
    second_moderator_id = second_moderator.id

    first = await SupportTicketsRepository.reply(
        session,
        ticket_uuid=ticket_uuid,
        answer="Первый ответ",
        moderator_id=first_moderator_id,
    )
    assert first is not None, "первый ответ не записан — CAS сломан в другую сторону"

    second = await SupportTicketsRepository.reply(
        session,
        ticket_uuid=ticket_uuid,
        answer="Второй ответ",
        moderator_id=second_moderator_id,
    )

    assert second is None, "CAS не сработал: повтор прошёл через UPDATE (M6)"
    row = (
        await session.execute(
            text("SELECT answer, answered_by_id FROM support_tickets WHERE id = :i"),
            {"i": ticket_id},
        )
    ).one()
    assert row.answer == "Первый ответ", "CAS пропустил перезапись текста (M6)"
    assert row.answered_by_id == first_moderator_id
