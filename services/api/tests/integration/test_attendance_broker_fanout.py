"""Фан-аут `attendance.request` через РЕАЛЬНЫЙ брокер (CR 9.4 HIGH-1).

**Почему этих тестов не хватало.** Все тесты 9.4 мокали `publish_to_chat` и
`publish_to_user`, то есть доказывали «воркер ПОПЫТАЛСЯ опубликовать». Реальная
доставка идёт через Redis Pub/Sub и `WsBroker.handle_message`, где живёт ДЕДУП.
Ключ дедупа `(type, event_uuid)` не различал каналы (R4: один `event_uuid` на
событие), поэтому первая публикация резервировала ключ, а все последующие
каналы схлопывались как `duplicate_dropped`: FR111 не доставлялся вовсе,
а `publish_to_user` возвращал `True` (Redis принял сообщение) — `delivered_at`
заполнялся ЛОЖНО.

**Что проверяется здесь.** Доставка на уровне брокера и сквозная доставка
через настоящий Redis: три адресата получают событие, чат получает событие,
дублей нет. Моки публикаторов не используются ВООБЩЕ.

§18.11: проверяются оба значения — «доставлено» и «дубль отброшен».
"""

from __future__ import annotations

import json
import os
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.redis import get_redis
from app.schemas.ws import WsEnvelope
from app.services.ws_broker import WsBroker
from app.services.ws_hub import WsConnection, ws_hub
from app.workers.attendance_prompts import run_attendance_prompts
from tests.integration.attendance_prompt_helpers import (
    _cleanup_seed,
    company,
    count_notifications,
    factory,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]


class _FakeWebSocket:
    """Сокет-заглушка: `WsConnection.send_frame` зовёт только `send_json`."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _connect(user_uuid: uuid_lib.UUID, *, chat_uuid: uuid_lib.UUID | None = None) -> WsConnection:
    """Регистрирует соединение как это делает WS-эндпоинт: user + (опц.) chat."""
    connection = WsConnection(websocket=_FakeWebSocket(), user_uuid=user_uuid)  # type: ignore[arg-type]
    ws_hub.register(connection)
    if chat_uuid is not None:
        ws_hub.subscribe(connection, chat_uuid)
    return connection


def _frames(connection: WsConnection) -> list[dict[str, Any]]:
    return connection.websocket.frames  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _clean_hub() -> Any:
    """Реестр — глобальный синглтон процесса; без очистки тесты протекают."""
    ws_hub.clear()
    yield
    ws_hub.clear()


# ——— 1. Уровень БРОКЕРА: один `event_uuid`, три канала ————————————————


async def test_broker_accepts_one_event_uuid_in_three_channels() -> None:
    """CR HIGH-1 (M13): один кадр в чат + 2 личных канала → 3 доставки.

    Это ровно тот случай, который схлопывался: ключ дедупа без канала
    резервировался первой публикацией. Краснеет на мутации M13 (ключ без
    канала) — вторая и третья доставки становятся нулевыми.
    """
    chat_uuid = uuid_lib.uuid4()
    user_a = uuid_lib.uuid4()
    user_b = uuid_lib.uuid4()

    chat_conn = _connect(uuid_lib.uuid4(), chat_uuid=chat_uuid)
    conn_a = _connect(user_a)
    conn_b = _connect(user_b)

    envelope = WsEnvelope.build(
        "attendance.request",
        {"card_uuid": str(uuid_lib.uuid4()), "starts_at": "2026-10-25T00:00:00Z"},
        event_uuid="e-high-1",
    )
    raw = json.dumps(envelope.to_frame(), ensure_ascii=False)
    broker = WsBroker()

    delivered_chat = await broker.handle_message(f"ws:chat:{chat_uuid}", raw)
    delivered_a = await broker.handle_message(f"ws:user:{user_a}", raw)
    delivered_b = await broker.handle_message(f"ws:user:{user_b}", raw)

    assert (delivered_chat, delivered_a, delivered_b) == (1, 1, 1)
    for connection in (chat_conn, conn_a, conn_b):
        assert len(_frames(connection)) == 1, "соединение не получило событие"

    # Обратное значение (§18.11): повтор в ТОТ ЖЕ канал — дубль, отброшен.
    assert await broker.handle_message(f"ws:user:{user_a}", raw) == 0
    assert len(_frames(conn_a)) == 1, "дубль доставлен повторно"


async def test_connection_in_both_channels_gets_one_frame() -> None:
    """CR 9.4 r1 (R4, M14): сокет в чате И в своём канале — ОДИН кадр.

    **Это правильное место R4.** Прежнее решение (общий `event_uuid` +
    дедуп брокера) схлопывало личные доставки и ломало FR111. Настоящая
    задача другая: один человек сидит и в комнате встречи, и в своём личном
    канале (ADR-06), поэтому событие, опубликованное в оба, пришло бы ЕМУ
    дважды. Решается на последней миле — в `WsConnection.send_frame`, где
    видно, что оба кадра идут в ОДИН сокет.

    Краснеет на мутации M14 (снять дедуп на соединении): соединение получит
    два одинаковых кадра.
    """
    chat_uuid = uuid_lib.uuid4()
    user_uuid = uuid_lib.uuid4()
    # Одно соединение: подписано на чат и зарегистрировано как пользователь.
    connection = _connect(user_uuid, chat_uuid=chat_uuid)

    envelope = WsEnvelope.build(
        "attendance.request",
        {"card_uuid": str(uuid_lib.uuid4()), "starts_at": "2026-10-25T00:00:00Z"},
        event_uuid="e-same-socket",
    )
    raw = json.dumps(envelope.to_frame(), ensure_ascii=False)
    broker = WsBroker()

    delivered_chat = await broker.handle_message(f"ws:chat:{chat_uuid}", raw)
    delivered_user = await broker.handle_message(f"ws:user:{user_uuid}", raw)

    # Оба канала ПРИНЯТЫ брокером (каналы разные — не дубль), но сокет
    # получил ровно один кадр: второй отброшен на соединении.
    assert (delivered_chat, delivered_user) == (1, 0)
    assert len(_frames(connection)) == 1, "сокет получил событие дважды"

    # Обратное значение (§18.11): событие БЕЗ `event_uuid` повторяемо
    # (D-2) — `chat.subscribed` обязан дойти дважды при двух подписках.
    repeatable = json.dumps(
        {
            "type": "chat.subscribed",
            "payload": {"chat_uuid": str(chat_uuid)},
            "ts": "2026-10-25T00:00:00Z",
        },
        ensure_ascii=False,
    )
    assert await broker.handle_message(f"ws:chat:{chat_uuid}", repeatable) == 1
    assert await broker.handle_message(f"ws:chat:{chat_uuid}", repeatable) == 1
    assert len(_frames(connection)) == 3, "повторяемое событие схлопнуто (D-2)"


# ——— 2. Сквозной: карточка БЕЗ чата, 3 адресата ————————————————————


async def _wait_subscribed(pubsub: Any) -> None:
    """Дожидается подтверждения `psubscribe` ДО публикации.

    `psubscribe` возвращается, когда команда отправлена, а не когда Redis её
    применил. Публикация, ушедшая в этот зазор, теряется навсегда (Pub/Sub не
    хранит историю), и тест падал бы на пустом словаре — дефект теста, а не
    кода. Подтверждений ровно столько, сколько шаблонов.
    """
    for _ in range(2):
        message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=2.0)
        assert message is not None, "psubscribe не подтверждён"
        assert str(message.get("type")) in {"psubscribe"}, message


async def _drain_pubsub(pubsub: Any, broker: WsBroker) -> dict[str, int]:
    """Читает всё, что успело прийти, и отдаёт брокеру как в `WsBroker._run`."""
    counts: dict[str, int] = {}
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None:
            break
        channel = message.get("channel")
        data = message.get("data")
        if isinstance(channel, bytes):
            channel = channel.decode()
        if isinstance(data, bytes):
            data = data.decode()
        if not isinstance(channel, str) or not isinstance(data, str):
            continue
        counts[channel] = await broker.handle_message(channel, data)
    return counts


async def test_card_without_chat_reaches_three_connections(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR HIGH-1 (AC-02, FR111): карточка без чата — 3 адресата реально получили.

    Сквозной путь без моков: воркер публикует в Redis, тест читает pub/sub и
    отдаёт кадры брокеру, как это делает `WsBroker._run`. Проверяется именно
    доставка до СОЕДИНЕНИЙ, а не факт вызова публикатора.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0, chat_uuid=None)
        await seed.commit()

    author_uuid = uuid_lib.UUID(str(seeded["author"].uuid))
    participant_uuids = [uuid_lib.UUID(str(member.uuid)) for member in seeded["accepted"]]
    all_uuids = [author_uuid, *participant_uuids]
    connections = {user_uuid: _connect(user_uuid) for user_uuid in all_uuids}

    client = get_redis()
    pubsub = client.pubsub()
    await pubsub.psubscribe("ws:user:*", "ws:chat:*")
    await _wait_subscribed(pubsub)
    try:
        await run_attendance_prompts(factory, now=moment)
        counts = await _drain_pubsub(pubsub, WsBroker())
    finally:
        await pubsub.punsubscribe("ws:user:*", "ws:chat:*")
        await pubsub.aclose()

    # Три личных канала, каждый — одна доставка; в чат публикации нет.
    assert len(counts) == 3, counts
    assert all(value == 1 for value in counts.values()), counts
    assert not any(channel.startswith("ws:chat:") for channel in counts)

    for user_uuid, connection in connections.items():
        frames = _frames(connection)
        assert len(frames) == 1, f"{user_uuid} не получил событие"
        assert frames[0]["type"] == "attendance.request"

    async with factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 3


# ——— 3. Сквозной: карточка С чатом — и чат, и личные каналы ————————


async def test_card_with_chat_reaches_chat_and_all_users(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR HIGH-1 (D-9.4-9): карточка с чатом — И чат, И все личные каналы.

    До фикса дедуп схлопывал: карточка с чатом не доставляла в личку ВООБЩЕ
    (чат публикуется первым и резервировал ключ). Проверяется «и», а не «или».
    """
    moment = datetime.now(UTC)
    chat_uuid = uuid_lib.uuid4()
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=chat_uuid)
        await seed.commit()

    author_uuid = uuid_lib.UUID(str(seeded["author"].uuid))
    participant_uuid = uuid_lib.UUID(str(seeded["accepted"][0].uuid))
    chat_conn = _connect(uuid_lib.uuid4(), chat_uuid=chat_uuid)
    author_conn = _connect(author_uuid)
    participant_conn = _connect(participant_uuid)

    client = get_redis()
    pubsub = client.pubsub()
    await pubsub.psubscribe("ws:user:*", "ws:chat:*")
    await _wait_subscribed(pubsub)
    try:
        await run_attendance_prompts(factory, now=moment)
        counts = await _drain_pubsub(pubsub, WsBroker())
    finally:
        await pubsub.punsubscribe("ws:user:*", "ws:chat:*")
        await pubsub.aclose()

    assert len(counts) == 3, counts
    assert counts[f"ws:chat:{chat_uuid}"] == 1, "чат не получил событие"
    assert counts[f"ws:user:{author_uuid}"] == 1, "автор не получил событие"
    assert counts[f"ws:user:{participant_uuid}"] == 1, "участник не получил событие"

    assert len(_frames(chat_conn)) == 1
    assert len(_frames(author_conn)) == 1
    assert len(_frames(participant_conn)) == 1
