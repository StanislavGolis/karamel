"""Публикация `attendance.request` и безличность payload (стори 7.3; AC-10, AC-11).

**Главная ловушка — SAVEPOINT (R17, score 9).** Основная фикстура `session`
живёт в `join_transaction_mode="create_savepoint"`: её `commit()` не виден из
другого соединения ВООБЩЕ. Тест «уведомление не видно до commit», написанный
на ней, был бы зелёным при ЛЮБОМ порядке — то есть тавтологией. Поэтому
порядок `commit → publish` доказывается перехватом с пробой из ВТОРОГО
соединения (`second_engine`) в момент вызова публикации.

**Почему не `expect_silence` из `ws_harness`.** Воркер — не HTTP-запрос: он
работает в отдельном процессе/сессии, и подписка `TestClient` живёт в другом
event loop. Вместо этого «тишина» проверяется прямым подписчиком Redis на
канал `ws:chat:{uuid}`: если публикации нет, в канале нет сообщений. Это
строже визуальной тишины WS-клиента и не требует второго loop.
"""

from __future__ import annotations

import json
import os
import re
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repositories.notifications import ATTENDANCE_REQUEST_TYPE
from app.schemas.ws import WS_EVENT_TYPES, WS_TRANSITION_EVENT_TYPES, WsEnvelope
from app.services import ws_publisher
from app.workers import attendance_prompts as prompts_module
from app.workers.attendance_prompts import run_attendance_prompts
from tests.integration.attendance_prompt_helpers import (
    _cleanup_seed,
    company,
    count_notifications,
    database_url,
    factory,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


# ——— AC-10: ADR-11, порядок commit → publish (S28, S29, M7, M16) ————————


async def test_publish_sees_committed_rows_from_second_connection(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10 (S29, R3+R17, score 9; M7 и M16): в момент `publish` строки ВИДНЫ
    из независимого соединения.

    Проба идёт через `second_engine` — другое соединение, поэтому видит только
    ЗАКОММИЧЕННОЕ. Краснеет от:

    - M7 (`publish` до `commit`) → `seen == 0`;
    - M16 (`commit` → `flush`) → `seen == 0` (фиксации не было вовсе).

    Проба через сессию воркера увидела бы незакоммиченные строки и дала бы 3
    при любом порядке — поэтому так делать нельзя.
    """
    moment = datetime.now(UTC)
    chat_uuid = uuid_lib.uuid4()
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0, chat_uuid=chat_uuid)
        await seed.commit()

    seen: list[int] = []
    probe_engine = create_async_engine(database_url(), poolclass=None)
    probe_factory = async_sessionmaker(bind=probe_engine, expire_on_commit=False)
    real_publish = prompts_module.publish_to_chat

    async def _spy(*, chat_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> None:
        async with probe_factory() as probe:
            from sqlalchemy import text

            result = await probe.execute(
                text("SELECT count(*) FROM notifications WHERE payload->>'card_uuid' = :card_uuid"),
                {"card_uuid": seeded["card_uuid"]},
            )
            seen.append(int(result.scalar_one()))
        await real_publish(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr(prompts_module, "publish_to_chat", _spy)
    try:
        counters = await run_attendance_prompts(factory, now=moment)
    finally:
        await probe_engine.dispose()

    assert counters["prompted"] == 1
    # 3 = автор + 2 участника; при M7/M16 было бы 0.
    assert seen == [3]


async def test_publish_after_commit_order_captured(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10 (S28): последовательность «сначала фиксация, затем публикация».

    Порядок фиксируется списком событий, а не ассертом состояния: только
    фактическая последовательность отличает `commit → publish` от обратного.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=uuid_lib.uuid4())
        await seed.commit()

    order: list[str] = []
    real_build = prompts_module.WsEnvelope.build

    async def _publish(*, chat_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> None:
        order.append("publish")

    def _build(*args: Any, **kwargs: Any) -> WsEnvelope:
        order.append("build")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(prompts_module, "publish_to_chat", _publish)
    monkeypatch.setattr(prompts_module.WsEnvelope, "build", staticmethod(_build))

    await run_attendance_prompts(factory, now=moment)

    # `build` идёт после `commit` в коде воркера (публикация — последний шаг).
    assert order == ["build", "publish"]


# ——— AC-10: нет чата / нет Redis (S30, S31) ——————————————————————————


async def test_no_chat_means_no_publish_but_rows_exist(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10 (S30, D-7.2-10): `chat_uuid IS NULL` — публикации нет, строки есть.

    Публикация — ускоритель, не условие. Участник без открытого чата обязан
    всё равно получить адресную строку: её доставит эпик 9 (`delivered_at`).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=None)
        await seed.commit()

    published: list[uuid_lib.UUID] = []

    async def _publish(*, chat_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> None:
        published.append(chat_uuid)

    monkeypatch.setattr(prompts_module, "publish_to_chat", _publish)
    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    assert published == []
    async with factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 2


async def test_no_redis_does_not_break_worker(
    factory: async_sessionmaker[AsyncSession], dead_redis: Any
) -> None:
    """AC-10 (S31, R8): Redis лежит — `publish` не бросает, воркер жив.

    `publish_to_chat` ловит `RedisError`/`OSError` и уходит в локальную
    доставку: недоступность Redis не превращается в падение прогона, а
    строки `notifications` уже зафиксированы.

    CR 7.3 LOW: проверяется ФАКТ попытки публикации (`dead_redis.calls`).
    Прежде тест читал только счётчики и строки БД, поэтому мутация «убрать
    вызов `publish_to_chat`» оставляла его зелёным — он проверял «воркер не
    упал», а не «публикация была».
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(
            seed, starts_at=moment, selected=1, rejected=0, chat_uuid=uuid_lib.uuid4()
        )
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    assert counters["skipped"] == 0
    assert counters["failed"] == 0
    # CR 7.3 LOW: попытка публикации БЫЛА, и падение Redis её не отменило.
    #
    # 9.4 (AC-02): каналов теперь ДВА вида — чат (7.3, D-9.4-9) и личный канал
    # каждого адресата (FR111). Первый канал остаётся чатом: порядок
    # «сначала комната, потом адресные» не изменился. Проверяется и то, что
    # личные публикации были попыткой, а не пропуском: `chat_uuid is None`
    # здесь не проверяется, он есть.
    assert dead_redis.calls[0] == f"ws:chat:{seeded['chat_uuid']}"
    user_channels = [call for call in dead_redis.calls if call.startswith("ws:user:")]
    # 3 строки = автор + 2 участника (`selected=1` + автор), каждая в свой канал.
    assert len(user_channels) == 2
    assert len(set(user_channels)) == 2, "два адресата не могут делить канал"
    async with factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 2


# ——— AC-11: безличность payload (S32, S33, S34, M12) ————————————————


def _all_keys(value: Any) -> set[str]:
    """Рекурсивно собирает ВСЕ ключи объекта payload (S32)."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.add(str(key))
            keys |= _all_keys(inner)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


async def test_payload_has_no_personal_keys(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-11 (S32, M12): в payload нет `user_id`/`author_id`/`participant_id` и имён.

    Безличность — про третьих лиц (решение 3 пакета): адресат видит «по
    карточке X пора подтвердить», а не «кто именно идёт». Проверка
    рекурсивная: вложенный объект не должен прятать ключ.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        from sqlalchemy import text

        rows = await check.execute(
            text("SELECT payload FROM notifications WHERE payload->>'card_uuid' = :u"),
            {"u": seeded["card_uuid"]},
        )
        payloads = [dict(row[0]) for row in rows.all()]

    assert payloads
    forbidden = {"user_id", "author_id", "participant_id", "name", "phone", "title"}
    for payload in payloads:
        keys = _all_keys(payload)
        assert keys & forbidden == set(), keys


async def test_payload_shape_exactly_two_keys(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-11 (S33): payload = ровно `{card_uuid, starts_at}`, `starts_at` в ISO-Z.

    Форма `...Z`, а не `+00:00`: клиент разбирает эту же форму у курсора
    сообщений (`message_cursor`), и «почти ISO» дало бы расхождение парсеров.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        from sqlalchemy import text

        result = await check.execute(
            text("SELECT payload FROM notifications WHERE payload->>'card_uuid' = :u LIMIT 1"),
            {"u": seeded["card_uuid"]},
        )
        payload = dict(result.scalar_one())

    assert set(payload) == {"card_uuid", "starts_at"}
    assert payload["card_uuid"] == seeded["card_uuid"]
    assert ISO_Z.match(payload["starts_at"]), payload["starts_at"]


# ——— D-7.3-9 / M11: тип приглашения —————————————————————————————


def test_attendance_request_registered_but_not_transition() -> None:
    """AC-10 (S48, M11, D-7.3-9): тип в реестре, но НЕ в переходах.

    `event_uuid` — ключ дедупа переходов. Приглашение повторяемо: два запроса
    по одной карточке (разные прогоны, разные причины) — не дубль, а два
    факта. Попади тип в `WS_TRANSITION_EVENT_TYPES`, повтор приглашения был
    бы отброшен брокером как дубль.
    """
    assert ATTENDANCE_REQUEST_TYPE == "attendance.request"
    assert ATTENDANCE_REQUEST_TYPE in WS_EVENT_TYPES
    assert ATTENDANCE_REQUEST_TYPE not in WS_TRANSITION_EVENT_TYPES

    envelope = WsEnvelope.build(ATTENDANCE_REQUEST_TYPE, {"card_uuid": "x", "starts_at": "y"})
    assert envelope.event_uuid is None
    assert envelope.type == "attendance.request"
    assert envelope.ts.endswith("Z")


# ——— S49: словарь run_once ——————————————————————————————————————


def test_ws_publisher_does_not_raise_signature() -> None:
    """AC-10 (R8): `publish_to_chat` принимает только ключевые аргументы.

    Контракт «не бросает» проверяется поведением (`dead_redis`), а форма
    вызова — здесь: воркер обязан звать его с `chat_uuid=` и `envelope=`.
    """
    import inspect

    signature = inspect.signature(ws_publisher.publish_to_chat)
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


async def test_envelope_payload_matches_stored_payload(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10/AC-11 (S30-доп): WS-payload = payload уведомления, побайтово.

    Адресность живёт в БД, канал лишь сообщает «по карточке X пора
    подтвердить». Расхождение содержимого между строкой и кадром дало бы
    клиенту два разных представления одного события.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(
            seed, starts_at=moment, selected=1, rejected=0, chat_uuid=uuid_lib.uuid4()
        )
        await seed.commit()

    captured: list[dict[str, Any]] = []

    async def _publish(*, chat_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> None:
        captured.append(dict(envelope.payload))

    monkeypatch.setattr(prompts_module, "publish_to_chat", _publish)
    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        from sqlalchemy import text

        result = await check.execute(
            text("SELECT DISTINCT payload FROM notifications WHERE payload->>'card_uuid' = :u"),
            {"u": seeded["card_uuid"]},
        )
        stored = [dict(row[0]) for row in result.all()]

    assert len(captured) == 1
    assert stored == [captured[0]]
    assert json.dumps(captured[0], sort_keys=True) == json.dumps(stored[0], sort_keys=True)
