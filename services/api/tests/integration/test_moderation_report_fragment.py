"""Фрагмент переписки для модератора (стори 8.3; AC-02, AC-03).

**Главная проверка — не «вернулось сколько-то сообщений», а КАКИЕ именно.**
Фрагмент обязан отдавать ХВОСТ диалога (конец разговора, из-за которого
подана жалоба), а не начало. Тест с 12 сообщениями различает эти два
поведения: `ASC LIMIT 10` вернул бы первые десять, `DESC LIMIT 10` +
переворот — последние десять.

**Расшифровка проверяется на отправленном тексте, а не на содержимом БД.**
Тест «вернули то, что лежит в БД» был бы зелёным и при полностью сломанном
`decrypt` (шифртекст вернулся бы шифртекстом). Поэтому тело сравнивается с
исходной строкой, а отдельно проверяется, что в БД лежит шифртекст.

**Лог проверяется по подстроке ТЕЛА, а не по отсутствию слова `body`.**
Отсутствие слова в коде ничего не доказывает: текст мог попасть в лог через
переменную с другим именем. Ищется сам секрет.
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.moderation_reports_service import FRAGMENT_LIMIT
from tests.integration.moderation_reports_helpers import (
    build_client,
    cookie_headers,
    make_message_report,
    make_moderator,
    make_no_show_report,
    make_plain_user,
    seed_messages,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _get_fragment(client, *, token: str, report_uuid: object, **params: object):
    return await client.get(
        f"/api/v1/moderation/reports/{report_uuid}/fragment",
        params=params,
        headers=cookie_headers(token),
    )


async def test_fragment_returns_last_ten_in_old_to_new_order(session: AsyncSession) -> None:
    """AC-02, D-32: не более 10 ПОСЛЕДНИХ сообщений, порядок — от старых к новым.

    Краснеет от мутации M3 (`ASC LIMIT 10`): вернулись бы первые десять, и
    первым элементом оказалось бы сообщение №1 вместо №3.
    """
    _, token = await make_moderator(session)
    seeded = await make_message_report(session)
    sender = seeded["chat"].author.id
    bodies = [f"сообщение {i}" for i in range(1, 13)]
    await seed_messages(session, chat_id=seeded["chat_id"], sender_id=sender, bodies=bodies)

    async with build_client(session) as client:
        response = await _get_fragment(client, token=token, report_uuid=seeded["report_uuid"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message_count"] == FRAGMENT_LIMIT == 10
    assert [item["body"] for item in body["items"]] == bodies[-10:]
    # Порядок именно «старые → новые», а не наоборот.
    assert body["items"][0]["body"] == "сообщение 3"
    assert body["items"][-1]["body"] == "сообщение 12"


async def test_fragment_ignores_client_limit(session: AsyncSession) -> None:
    """AC-02, FR40, NFR27: `?limit=1000` НЕ расширяет выдачу.

    Параметр не объявлен на маршруте, поэтому FastAPI его игнорирует: клиент
    не может обойти ограничение объёма раскрываемой переписки. Краснеет,
    если `limit` появится в сигнатуре роутера или в сервисе.
    """
    _, token = await make_moderator(session)
    seeded = await make_message_report(session)
    sender = seeded["chat"].author.id
    bodies = [f"строка {i}" for i in range(1, 13)]
    await seed_messages(session, chat_id=seeded["chat_id"], sender_id=sender, bodies=bodies)

    async with build_client(session) as client:
        response = await _get_fragment(
            client, token=token, report_uuid=seeded["report_uuid"], limit=1000
        )

    assert response.status_code == 200, response.text
    assert response.json()["message_count"] == FRAGMENT_LIMIT
    assert len(response.json()["items"]) == FRAGMENT_LIMIT


async def test_fragment_decrypts_real_text_and_db_holds_ciphertext(session: AsyncSession) -> None:
    """AC-02, NFR31: тело в ответе — ОТКРЫТЫЙ текст, в БД — шифртекст.

    Две проверки в одном тесте намеренно: только вместе они доказывают, что
    модератор получает расшифровку. Краснеет от мутации M4 (отдать `body` из
    БД без `decrypt`).
    """
    _, token = await make_moderator(session)
    seeded = await make_message_report(session)
    sender = seeded["chat"].author.id
    secret = "Он обещал прийти и не пришёл"
    await seed_messages(session, chat_id=seeded["chat_id"], sender_id=sender, bodies=[secret])

    stored = await session.execute(
        text("SELECT body FROM messages WHERE chat_id = :c"), {"c": seeded["chat_id"]}
    )
    blob = stored.scalar_one()
    assert isinstance(blob, bytes | bytearray | memoryview)
    assert secret.encode("utf-8") not in bytes(blob)

    async with build_client(session) as client:
        response = await _get_fragment(client, token=token, report_uuid=seeded["report_uuid"])

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["body"] == secret


async def test_fragment_of_no_show_report_is_404(session: AsyncSession) -> None:
    """AC-02: у жалобы на неявку фрагмента не существует — `404`, не `422`.

    `422` сообщил бы «жалоба есть, но другого типа», то есть подтвердил бы
    существование объекта; модератору это знание ничего не даёт.
    """
    _, token = await make_moderator(session)
    no_show = await make_no_show_report(session)

    async with build_client(session) as client:
        response = await _get_fragment(client, token=token, report_uuid=no_show["report_uuid"])

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "report_not_found"


async def test_fragment_of_missing_report_is_404(session: AsyncSession) -> None:
    """AC-02: несуществующая жалоба — тот же `404 report_not_found`."""
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await _get_fragment(client, token=token, report_uuid=uuid_lib.uuid4())

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "report_not_found"


async def test_non_moderator_cannot_read_fragment(session: AsyncSession) -> None:
    """AC-02: обычный пользователь — `403` до чтения переписки."""
    _, token = await make_plain_user(session)
    seeded = await make_message_report(session)

    async with build_client(session) as client:
        response = await _get_fragment(client, token=token, report_uuid=seeded["report_uuid"])

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


async def test_access_log_has_ids_and_no_message_text(session: AsyncSession) -> None:
    """AC-03, FR60, NFR32: в лог доступа — кто/куда/сколько, БЕЗ текста.

    Проверяется наличие всех обязательных полей (включая `card_id`, D-39) и
    ОТСУТСТВИЕ тела сообщения. Поиск идёт по подстроке самого секрета: тест
    на «в коде нет слова body» был бы зелёным при утечке через `repr`.
    """
    moderator, token = await make_moderator(session)
    seeded = await make_message_report(session)
    sender = seeded["chat"].author.id
    secret = "уникальный-маркер-утечки-8-3"
    await seed_messages(session, chat_id=seeded["chat_id"], sender_id=sender, bodies=[secret])

    with structlog.testing.capture_logs() as logs:
        async with build_client(session) as client:
            response = await _get_fragment(client, token=token, report_uuid=seeded["report_uuid"])

    assert response.status_code == 200, response.text
    events = [entry for entry in logs if entry.get("event") == "moderation.fragment.read"]
    assert len(events) == 1, logs
    entry = events[0]
    assert entry["moderator_uuid"] == str(moderator.uuid)
    assert entry["report_uuid"] == str(seeded["report_uuid"])
    assert entry["chat_id"] == seeded["chat_id"]
    # `card_id` — id карточки встречи, к которой относится диалог.
    assert isinstance(entry["card_id"], int)
    assert entry["card_id"] > 0
    assert entry["message_count"] == 1
    assert entry["decrypted"] is True
    # Текста сообщений в выводе нет (AC-03, NFR27).
    assert secret not in repr(logs)


async def test_no_access_log_when_decryption_fails(session: AsyncSession) -> None:
    """AC-03, D-34, мутация M5: упавшая расшифровка НЕ пишет запись об успехе.

    Блоб подменяется чужим ключом: `InvalidTag` рвёт чтение, и лог «отдал N
    сообщений» был бы ложным следом в аудите. Краснеет, если `logger.info`
    перенести ДО цикла расшифровки.
    """
    from app.core.crypto import encrypt_message_body

    _, token = await make_moderator(session)
    seeded = await make_message_report(session)
    sender = seeded["chat"].author.id
    blob = encrypt_message_body(
        "чужой ключ", chat_id=seeded["chat_id"], sender_id=sender, key="0" * 64
    )
    await session.execute(
        text("INSERT INTO messages (uuid, chat_id, sender_id, body) VALUES (:u, :c, :s, :b)"),
        {"u": uuid_lib.uuid4(), "c": seeded["chat_id"], "s": sender, "b": blob},
    )
    await session.flush()

    # Повреждённый блоб НЕ перехватывается сервисом: `InvalidTag` — дефект
    # данных, а не ввод клиента, и в проде он даёт `500` (громко, в мониторинг).
    # Тест ловит само исключение: `ASGITransport` по умолчанию пробрасывает
    # необработанные исключения приложения, и это ровно то поведение, которое
    # проверяется. Ложный `200` с мусором в теле был бы хуже падения (NFR31).
    from cryptography.exceptions import InvalidTag

    with structlog.testing.capture_logs() as logs:
        async with build_client(session) as client:
            with pytest.raises(InvalidTag):
                await _get_fragment(client, token=token, report_uuid=seeded["report_uuid"])

    events = [entry for entry in logs if entry.get("event") == "moderation.fragment.read"]
    assert events == [], f"лог записан при упавшей расшифровке: {events}"
