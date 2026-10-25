"""FR111 — доставка запроса подтверждения явки в канал пользователя (9.4; AC-02).

**Что было и чего не было.** Стори 7.3 создавала строки `attendance.request`
и публиковала событие в `ws:chat:{chat_uuid}`. Оставались три дефекта
доставки (D-9.4-6):

1. у карточки без чата не доставлялось НИЧЕГО (гард `chat_uuid is not None`);
2. личный канал `ws:user:{uuid}` не использовался вообще;
3. `delivered_at` не заполнялся ничем, то есть «доставлено» не отличалось
   от «создано».

Эти три свойства и проверяются. §18.11: `delivered_at` — в обоих значениях.
Порядок `commit → publish` доказан в `test_attendance_prompts_ws.py` (проба
из второго соединения); здесь его не дублируем.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.ws import WsEnvelope
from app.workers import attendance_prompts as prompts_module
from app.workers.attendance_prompts import run_attendance_prompts
from tests.integration.attendance_prompt_helpers import (
    _cleanup_seed,
    company,
    count_notifications,
    factory,
    marker,
    notification_rows,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]


async def _delivered_at(session: AsyncSession, card_uuid: str) -> list[Any]:
    result = await session.execute(
        text(
            "SELECT delivered_at FROM notifications "
            "WHERE payload->>'card_uuid' = :u ORDER BY user_id"
        ),
        {"u": card_uuid},
    )
    return [row[0] for row in result.all()]


class _Spy:
    """Собирает вызовы публикации: канал и событие."""

    def __init__(self) -> None:
        self.chat: list[str] = []
        self.user: list[str] = []
        self.envelopes: list[WsEnvelope] = []

    async def publish_chat(self, *, chat_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> bool:
        self.chat.append(str(chat_uuid))
        self.envelopes.append(envelope)
        return True

    async def publish_user(self, *, user_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> bool:
        self.user.append(str(user_uuid))
        self.envelopes.append(envelope)
        return True


# ——— AC-02: личный канал работает БЕЗ чата ————————————————————————————


async def test_card_without_chat_still_delivers_to_user_channels(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-02 (M7, M8): карточка без чата — событие всё равно доходит.

    До 9.4 при `chat_uuid is None` не публиковалось НИЧЕГО: строки в БД были,
    а запрос до пользователя не доходил. Краснеет при M7 (только `ws:chat`)
    и при M8 (гард чата, распространённый на личный канал).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0, chat_uuid=None)
        await seed.commit()

    spy = _Spy()
    monkeypatch.setattr(prompts_module, "publish_to_chat", spy.publish_chat)
    monkeypatch.setattr(prompts_module, "publish_to_user", spy.publish_user)

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    # Чата нет — в комнату не публикуем; личных каналов 3 (автор + 2 участника).
    assert spy.chat == []
    assert len(spy.user) == 3, spy.user
    assert len(set(spy.user)) == 3, "у адресатов каналы обязаны различаться"

    async with factory() as check:
        delivered = await _delivered_at(check, seeded["card_uuid"])

    assert len(delivered) == 3
    assert all(value is not None for value in delivered), "delivered_at не проставлен (M9)"


async def test_card_with_chat_publishes_to_both_channels(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-02 (D-9.4-9): при наличии чата публикация в `ws:chat` СОХРАНЯЕТСЯ.

    Замена чата личным каналом сломала бы регресс 7.3 и лишила бы комнату
    встречи события. Проверяются оба канала и общий `event_uuid` (R4).
    """
    moment = datetime.now(UTC)
    chat_uuid = uuid_lib.uuid4()
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=chat_uuid)
        await seed.commit()

    spy = _Spy()
    monkeypatch.setattr(prompts_module, "publish_to_chat", spy.publish_chat)
    monkeypatch.setattr(prompts_module, "publish_to_user", spy.publish_user)

    await run_attendance_prompts(factory, now=moment)

    assert spy.chat == [str(chat_uuid)]
    assert len(spy.user) == 2, spy.user
    # R4: у обоих каналов ОДИН `event_uuid` — клиент склеит доставки одного
    # события, а не покажет два одинаковых приглашения.
    uuids = {envelope.event_uuid for envelope in spy.envelopes}
    assert len(uuids) == 1, uuids
    assert None not in uuids, "ключ дедупа обязателен (R4)"

    async with factory() as check:
        delivered = await _delivered_at(check, seeded["card_uuid"])

    assert len(delivered) == 2
    assert all(value is not None for value in delivered)


# ——— AC-02: неуспешная публикация НЕ ставит delivered_at ————————————


async def test_delivered_at_not_set_when_publish_fails(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-02 (M9): `delivered_at` — только после успешной публикации.

    Отметка «на всякий случай» соврала бы остальным инстансам: событие в шину
    не ушло, повтора нет (D-9.2-5). Краснеет при M9.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=None)
        await seed.commit()

    async def _refuse(*, user_uuid: uuid_lib.UUID, envelope: WsEnvelope) -> bool:
        return False

    monkeypatch.setattr(prompts_module, "publish_to_chat", _Spy().publish_chat)
    monkeypatch.setattr(prompts_module, "publish_to_user", _refuse)

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["publish_failed"] == 2, counters
    async with factory() as check:
        delivered = await _delivered_at(check, seeded["card_uuid"])

    assert len(delivered) == 2
    assert all(value is None for value in delivered), "отметка без публикации (M9)"


async def test_mark_delivered_many_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-02 (M10, §18.11): повторная отметка не сдвигает `delivered_at`.

    Предикат `delivered_at IS NULL` — единственная защита от «второй отметки»:
    без него повторный вызов перезаписал бы время доставки, и
    `created_at <= delivered_at` держалось бы случайно. Краснеет при M10.

    Проверяется ТОЧНОЕ равенство сохранённого значения, а не «время не
    сдвинулось» (§18.2: сравнение с допуском — тавтология).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=None)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    from app.repositories.notifications import NotificationsRepository

    async with factory() as check:
        first = await _delivered_at(check, seeded["card_uuid"])
        rows = await check.execute(
            text(
                "SELECT uuid FROM notifications WHERE payload->>'card_uuid' = :u ORDER BY user_id"
            ),
            {"u": seeded["card_uuid"]},
        )
        uuids = [uuid_lib.UUID(str(row[0])) for row in rows.all()]

    async with factory() as again:
        updated = await NotificationsRepository(again).mark_delivered_many(notification_uuids=uuids)
        await again.commit()

    async with factory() as final:
        second = await _delivered_at(final, seeded["card_uuid"])

    assert all(value is not None for value in first)
    # Ни одна строка не обновлена повторно: предикат `IS NULL` отработал.
    assert updated == 0, "повторная отметка перезаписала delivered_at (M10)"
    assert second == first, "время доставки сдвинулось (M10)"


# ——— AC-02: mute одного адресата не глушит остальных ————————————————


async def test_muted_recipient_absent_from_requests_but_others_delivered(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-02 (M13-класс): отключённому не создаётся строка и не шлётся событие.

    Gate 9.3 отфильтровывает адресата ВНУТРИ `INSERT`, поэтому `RETURNING`
    (а значит и список `requests`) содержит только тех, кому строка реально
    создана. Второй адресат обязан получить своё: иначе ошибка gate,
    применённого ко всем сразу.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=None)
        author_id = int(seeded["author"].id)
        await seed.execute(
            text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
            {"u": author_id, "t": "attendance.request"},
        )
        await seed.commit()

    spy = _Spy()
    monkeypatch.setattr(prompts_module, "publish_to_chat", spy.publish_chat)
    monkeypatch.setattr(prompts_module, "publish_to_user", spy.publish_user)

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        rows = await notification_rows(check, seeded["card_uuid"])
        total = await count_notifications(check, seeded["card_uuid"])
        stamped = await marker(check, seeded["card_id"])

    assert total == 1, f"отключённому создана строка: {rows}"
    assert rows[0][0] == int(seeded["accepted"][0].id), "участник не получил уведомление"
    assert len(spy.user) == 1, "отключённому отправлено событие"
    assert stamped is not None, "маркер не встал (AC-12)"
