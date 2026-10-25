"""Запрос подтверждения явки: основное поведение (стори 7.3).

Покрывает AC-03 (запрос после начала), AC-04 (адресность), AC-07 (окно 15 мин —
латентность, не отсечка), AC-08 (`cancelled`), AC-12 (карточка без пар) и
SQL-gate адресности (S40, R6 — самый дорогой риск стори).

**Фикстура адресности — 1 автор + 2 принятых + 1 отклонённый** (R15, score 9).
На одном участнике `UNION`/`UNION ALL`, `participant_id <> author_id` и
«автор ровно один раз» неразличимы — TD 7.3 называет это причиной пяти HIGH,
пропущенных в 7.2.

Время — только инъекцией `now` (`run_attendance_prompts(now=...)`), `sleep` нет.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.attendance import AttendanceRepository
from app.repositories.event_cards import EventCardRepository
from app.repositories.notifications import ATTENDANCE_REQUEST_TYPE
from app.workers import attendance_prompts as prompts_module
from app.workers.attendance_prompts import run_attendance_prompts
from tests.integration.attendance_prompt_helpers import (
    _cleanup_seed,
    attendance_rows,
    company,
    count_notifications,
    factory,
    insert_card,
    make_place,
    marker,
    new_user,
    notification_rows,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]


# ——— AC-03: запрос после начала встречи ————————————————————————————————


async def test_prompt_sent_exactly_at_start(factory: async_sessionmaker[AsyncSession]) -> None:
    """AC-03, AC-07 (S09/S24): `starts_at == now` — запрос уходит.

    Граница включительная: ровно в момент начала подтверждать уже уместно.
    Краснеет от `starts_at < :now` (строгое неравенство).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment)
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    # 9.4 (Task 2.5): исход запроса прежний; доставка считается отдельно.
    assert counters["prompted"] == 1
    assert counters["skipped"] == 0
    assert counters["failed"] == 0
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is not None
        rows = await attendance_rows(check, seeded["card_id"])
        # AC-03: 2 принятых участника = 2 строки `attendance` (отклонённый не входит).
        assert len(rows) == 2
        assert all(status == "pending" for _, status in rows)


async def test_no_prompt_before_start(factory: async_sessionmaker[AsyncSession]) -> None:
    """AC-07 (S10, M3): `starts_at = now + 1 с` — запроса нет, маркер `NULL`.

    D-TD-5: уведомлений НОЛЬ и payload не собирается вовсе — `starts_at` для
    payload берётся из выборки, а не из `RETURNING` (иначе при мутации M3
    payload собрался бы с `None`).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment + timedelta(seconds=1))
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 0
    assert counters["skipped"] == 0
    assert counters["failed"] == 0
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is None
        assert await count_notifications(check, seeded["card_uuid"]) == 0


async def test_attendance_rows_created_via_ensure_rows(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03 (S11, M4): строки `attendance` создаёт `ensure_rows` 7.2, не второй INSERT.

    Перехват: сервис обязан позвать `AttendanceRepository.ensure_rows`.
    Своя вставка по `join_requests` потеряла бы `ON CONFLICT DO NOTHING`
    и отсечку `participant_id <> author_id` — это и есть мутация M4.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment)
        await seed.commit()

    calls: list[tuple[int, int]] = []
    original = AttendanceRepository.ensure_rows

    async def _spy(self: AttendanceRepository, *, card_id: int, author_id: int) -> int:
        calls.append((card_id, author_id))
        return await original(self, card_id=card_id, author_id=author_id)

    monkeypatch.setattr(AttendanceRepository, "ensure_rows", _spy)

    await run_attendance_prompts(factory, now=moment)

    assert calls == [(seeded["card_id"], int(seeded["author"].id))]


# ——— AC-04: адресность ————————————————————————————————————————————————


async def test_addressing_author_plus_two_participants(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (S12/S13/S14): ровно 3 строки — 1 автор + 2 принятых, отклонённому 0.

    Три «неудобных» случая фикстуры: автор получает ОДНУ строку (не по паре),
    второй участник проверяет `UNION`, отклонённый — `ensure_rows` его не создал,
    значит и в `SELECT` его нет. Без отклонённого тест прошёл бы при
    `INSERT ... FROM users` — «уведомить всех» вместо адресатов.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=1)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        rows = await notification_rows(check, seeded["card_uuid"])
        recipients = [user_id for user_id, _, _ in rows]
        author_id = int(seeded["author"].id)
        accepted_ids = {int(member.id) for member in seeded["accepted"]}
        refused_ids = {int(member.id) for member in seeded["refused"]}

        assert len(rows) == 3
        # AC-04: автор РОВНО один раз, хотя пар у него две (D-7.3-3).
        assert recipients.count(author_id) == 1
        assert accepted_ids <= set(recipients)
        # AC-04: отклонённый не получает ничего.
        assert refused_ids & set(recipients) == set()
        assert all(type_ == ATTENDANCE_REQUEST_TYPE for _, type_, _ in rows)


async def test_author_gets_one_row_with_three_pairs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (S13/S15, M5/M6): с ТРЕМЯ парами автор получает ОДНУ строку.

    `UNION ALL` дал бы автору 3 строки; отсутствие `participant_id <> :author_id`
    дало бы 2 (как участник + как владелец). Тест краснеет от обеих мутаций.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=3, rejected=0)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        rows = await notification_rows(check, seeded["card_uuid"])
        author_id = int(seeded["author"].id)
        assert len(rows) == 4
        assert [user_id for user_id, _, _ in rows].count(author_id) == 1


# ——— AC-07: 15 минут — латентность, НЕ отсечка (D-7.3-4) ——————————————


@pytest.mark.parametrize(
    "delay",
    [
        timedelta(minutes=14, seconds=59),
        timedelta(minutes=16),
        timedelta(hours=3),
    ],
    ids=["inside-window", "past-window", "three-hours-late"],
)
async def test_late_cards_still_prompted(
    factory: async_sessionmaker[AsyncSession], delay: timedelta
) -> None:
    """AC-07 (S20/S21/S22, R7): поздний запрос вместо пропуска факта.

    Отсечка `starts_at > :now - interval '15 min'` превратила бы лежачего
    воркера в НЕОБРАТИМЫЙ пропуск: карточка не будет запрошена никогда,
    история 7.5 и санкция FR109 останутся без данных (нарушение AC-06).
    Поэтому 16 минут и 3 часа обязаны давать `prompted == 1`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment - delay)
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is not None


async def test_one_second_before_start_not_prompted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07 (S23): `starts_at = now - 1 с`… наоборот: `now` ещё не наступил."""
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment + timedelta(seconds=1))
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 0
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is None


# ——— AC-08: отменённая карточка —————————————————————————————————————


async def test_cancelled_card_not_prompted(factory: async_sessionmaker[AsyncSession]) -> None:
    """AC-08 (S25, D-7.3-5): `cancelled` — запроса нет, маркер НЕ двигается.

    Двигать маркер значило бы утверждать «запрос отправлен», чего не было,
    и потерять ответ на вопрос «почему по карточке нет фактов» при разборе 7.4.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment - timedelta(hours=1), status="cancelled")
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 0
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is None
        assert await attendance_rows(check, seeded["card_id"]) == []
        assert await count_notifications(check, seeded["card_uuid"]) == 0


# ——— AC-12: карточка без принятых заявок ——————————————————————————————


async def test_card_without_pairs_marks_but_publishes_nothing(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-12 (S35, D-7.3-8, M10, M17): маркер встаёт, уведомлений ноль,
    И WS-публикации нет даже при наличии `chat_uuid`.

    **Карточка создаётся С чатом намеренно** (CR 7.3 H-1). Прежняя версия
    теста звала `company(...)` без `chat_uuid`, поэтому путь «0 адресатов +
    есть чат» не исполнялся ни разу, и дефект «публикуем при `recipients == 0`»
    прошёл все 84 теста. Гард `recipients > 0` проверяется только так.

    Смысл: приглашение «подтвердите явку» по встрече без компании ведёт
    участника чата в `409 ATTENDANCE_NO_PAIR` (7.2). Маркер при этом
    ОБЯЗАН встать — иначе карточка выбирается каждые 60 с вечно.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(
            seed, starts_at=moment, selected=0, rejected=1, chat_uuid=uuid_lib.uuid4()
        )
        await seed.commit()

    published: list[Any] = []

    async def _spy(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        published.append((chat_uuid, envelope.type))

    monkeypatch.setattr(prompts_module, "publish_to_chat", _spy)
    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    assert published == [], "публикация при нуле адресатов (CR H-1)"
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is not None
        assert await count_notifications(check, seeded["card_uuid"]) == 0


async def test_card_without_pairs_prompted_once_in_ten_runs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-12 (S36, M10): 10 прогонов → 1 попытка и 9 пропусков.

    Без маркера карточка без пар выбиралась бы каждые 60 с вечно.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        await company(seed, starts_at=moment, selected=0, rejected=0)
        await seed.commit()

    attempts = [await run_attendance_prompts(factory, now=moment) for _ in range(10)]

    assert sum(counter["prompted"] for counter in attempts) == 1


# ——— S40 / R6: SQL-gate адресности ————————————————————————————————


async def test_sql_gate_sends_when_rows_created_lazily(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """S40 (R6, score 9, M13): строки уже созданы 7.2 → `ensure_rows` вернёт 0,
    но уведомления ОБЯЗАНЫ уйти.

    Это самый ценный тест стори. `rowcount == 0` означает ДВА разных
    состояния: «пар нет» (AC-12) и «пары уже созданы ленивым путём 7.2».
    Различает их только `EXISTS` ВНУТРИ `INSERT`. Python-gate
    `if ensure_rows(...) > 0` даёт здесь ноль уведомлений — тихая потеря
    факта: маркер встал, повторить нельзя.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0)
        # Ленивый путь 7.2: подтверждение создаёт строки ДО воркера.
        await AttendanceRepository(seed).ensure_rows(
            card_id=seeded["card_id"], author_id=int(seeded["author"].id)
        )
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    async with factory() as check:
        # 3 = автор + 2 участника; при Python-gate было бы 0.
        assert await count_notifications(check, seeded["card_uuid"]) == 3


# ——— A01/A02/A03/A04/A05 ———————————————————————————————————————————


async def test_full_and_closed_statuses_both_prompted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A01: `full` И `closed` обрабатываются, а не только `finished`.

    Проверяет, что кортеж статусов реально читается целиком, а не один литерал.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        full = await company(seed, starts_at=moment, status="full", selected=1, rejected=0)
        closed = await company(seed, starts_at=moment, status="closed", selected=1, rejected=0)
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 2
    async with factory() as check:
        assert await marker(check, full["card_id"]) is not None
        assert await marker(check, closed["card_id"]) is not None


async def test_draft_and_published_not_selected(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A02 (S38, M9): `draft`/`published` с прошлым `starts_at` — не в выборке.

    Краснеет от `status <> 'cancelled'` в SELECT: тогда сюда попали бы обе.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        draft = await company(seed, starts_at=moment, status="draft", selected=1, rejected=0)
        published = await company(
            seed, starts_at=moment, status="published", selected=1, rejected=0
        )
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 0
    async with factory() as check:
        assert await marker(check, draft["card_id"]) is None
        assert await marker(check, published["card_id"]) is None


async def test_batch_limit_processes_one_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A03: `limit=1` при двух пригодных — ровно одна, вторая в следующем прогоне."""
    moment = datetime.now(UTC)
    async with factory() as seed:
        first = await company(seed, starts_at=moment - timedelta(minutes=2), selected=1, rejected=0)
        second = await company(
            seed, starts_at=moment - timedelta(minutes=1), selected=1, rejected=0
        )
        await seed.commit()

    assert (await run_attendance_prompts(factory, now=moment, limit=1))["prompted"] == 1
    assert (await run_attendance_prompts(factory, now=moment, limit=1))["prompted"] == 1
    async with factory() as check:
        assert await marker(check, first["card_id"]) is not None
        assert await marker(check, second["card_id"]) is not None


async def test_payload_identical_for_all_recipients(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A08: payload побайтово одинаков у всех адресатов одной карточки.

    Payload собирается ОДИН раз на карточку (Task 3.3), а не на адресата:
    иначе `starts_at` мог бы разойтись между строками одного запроса.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        rows = await notification_rows(check, seeded["card_uuid"])
        payloads = [payload for _, _, payload in rows]
        assert len(payloads) == 3
        assert all(payload == payloads[0] for payload in payloads)


async def test_count_undelivered_before_and_after(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A05 + 9.4 AC-02: `count_undelivered` — граница доставки (Redis жив).

    Тест 7.3 утверждал «после прогона строки ждут доставки»: тогда
    `delivered_at` не заполнялось ничем. С 9.4 (FR111) личный канал доставляет,
    и значение признака изменилось — но не перестало проверяться: доставка
    отмечается, `count_undelivered` = 0. Обратное значение (Redis лежит →
    строки остаются `NULL`) доказывает отдельный тест
    `test_count_undelivered_when_publish_fails`, §18.11.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    async with factory() as check:
        from app.repositories.notifications import NotificationsRepository

        assert (
            await NotificationsRepository(check).count_undelivered(user_id=int(seeded["author"].id))
            == 0
        )

    await run_attendance_prompts(factory, now=moment)

    # AC-02 9.4: Redis доступен — событие ушло в личный канал, отметка стоит.
    async with factory() as check:
        from app.repositories.notifications import NotificationsRepository

        repo = NotificationsRepository(check)
        assert await repo.count_undelivered(user_id=int(seeded["author"].id)) == 0
        assert await repo.count_undelivered(user_id=int(seeded["accepted"][0].id)) == 0
        # Считаем ТОЛЬКО строки этой карточки: `count(*)` по всей таблице
        # захватил бы чужие закоммиченные строки (тесты делят одну БД).
        delivered = await check.execute(
            text(
                "SELECT count(*) FROM notifications "
                "WHERE delivered_at IS NOT NULL AND payload->>'card_uuid' = :u"
            ),
            {"u": seeded["card_uuid"]},
        )
        # 2 адресата: автор карточки и единственный принятый участник —
        # у каждого личный канал, и у обеих строк `delivered_at` заполнен.
        assert int(delivered.scalar_one()) == 2


async def test_count_undelivered_when_publish_fails(
    factory: async_sessionmaker[AsyncSession], dead_redis: Any
) -> None:
    """A05 + 9.4 AC-02, D-9.2-5: публикация не ушла — строки остаются `NULL`.

    Второе значение того же признака (§18.11). Без него `count_undelivered`
    проходил бы и при константе «всегда отметить»: мутация M9 осталась бы
    зелёной. Здесь Redis недоступен, `publish_to_user` возвращает `False`,
    и отметка НЕ ставится — «в шину не ушло» остаётся честным сигналом.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        from app.repositories.notifications import NotificationsRepository

        repo = NotificationsRepository(check)
        assert await repo.count_undelivered(user_id=int(seeded["author"].id)) == 1
        assert await repo.count_undelivered(user_id=int(seeded["accepted"][0].id)) == 1


async def test_fk_cascade_removes_notifications(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A06: `ON DELETE CASCADE` — удаление пользователя уносит его уведомления."""
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    member_id = int(seeded["accepted"][0].id)
    async with factory() as check:
        before = await count_notifications(check, seeded["card_uuid"])
        assert before == 2
        await check.execute(text("DELETE FROM users WHERE id = :id"), {"id": member_id})
        await check.commit()
        after = await count_notifications(check, seeded["card_uuid"])
        assert after == 1


async def test_participant_rejected_gets_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """S14 (CR 7.3 LOW): отклонённая заявка не создаёт пару и не даёт уведомления.

    Прежняя версия проверяла `assert insert_request is not None` — тавтология,
    зелёная при любом регрессe адресности. Теперь проверяется ФАКТ: у
    `not_selected` нет строки `attendance` и нет строки `notifications`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=2)
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    refused_ids = {int(member.id) for member in seeded["refused"]}
    async with factory() as check:
        # Пары не созданы: ensure_rows берёт только `status='selected'`.
        pairs = await attendance_rows(check, seeded["card_id"])
        assert refused_ids & {participant for participant, _ in pairs} == set()
        # И уведомлений им тоже нет.
        recipients = {
            user_id for user_id, _, _ in await notification_rows(check, seeded["card_uuid"])
        }
        assert refused_ids & recipients == set()
        # Санити: принятый и автор уведомление получили (иначе тест пустой).
        assert int(seeded["accepted"][0].id) in recipients
        assert int(seeded["author"].id) in recipients


# ——— Изоляция слоёв адресности (M5, M6, M9) ————————————————————————
#
# Мутационный прогон показал: тесты через воркер не различают `UNION` и
# `UNION ALL` (вторая ветка — скаляр автора, дубля не даёт) и не видят
# потерю `participant_id <> :author_id`, если в `attendance` нет подделанной
# строки. Ниже — прямой вызов SQL-слоя и подделка строки.


async def test_forged_attendance_row_does_not_duplicate_author(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-04 (M6): подделанная строка `participant_id = author_id` не дублирует автора.

    Такая строка физически возможна (прямой SQL, будущая схема) — тогда
    первая ветка `UNION` вернула бы автора как «участника», и без
    `participant_id <> :author_id` он получил бы ДВЕ строки: как владелец и
    как участник собственной встречи.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        author_id = int(seeded["author"].id)
        # Подделка: автор записан участником своей же карточки.
        await seed.execute(
            text(
                "INSERT INTO attendance (card_id, participant_id) "
                "VALUES (:c, :a) ON CONFLICT DO NOTHING"
            ),
            {"c": seeded["card_id"], "a": author_id},
        )
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        rows = await notification_rows(check, seeded["card_uuid"])
        recipients = [user_id for user_id, _, _ in rows]
        assert recipients.count(author_id) == 1, recipients


async def test_direct_select_excludes_draft_and_published(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A02/M9: `due_prompt_ids` НАПРЯМУЮ не отдаёт `draft`/`published`.

    Воркер мог бы «спастись» вторым слоем (CAS), поэтому предикат выборки
    проверяется без воркера: расхождение `status IN (...)` с `status <>
    'cancelled'` видно только так.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        draft = await company(seed, starts_at=moment, status="draft", selected=1, rejected=0)
        published = await company(
            seed, starts_at=moment, status="published", selected=1, rejected=0
        )
        finished = await company(seed, starts_at=moment, status="finished", selected=1, rejected=0)
        await seed.commit()

    async with factory() as session:
        card_ids = {
            card_id
            for card_id, *_ in await EventCardRepository(session).due_prompt_ids(
                now=moment, limit=100
            )
        }

    assert draft["card_id"] not in card_ids
    assert published["card_id"] not in card_ids
    assert finished["card_id"] in card_ids


async def test_forged_author_only_row_is_not_a_company(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-12 (M6 в EXISTS-ветке): строка «автор как участник» — НЕ компания.

    Если в `attendance` лежит только подделанная строка автора, у карточки
    нет компании, и запрос «подтвердите явку» автору бессмыслен: он получит
    `409 ATTENDANCE_NO_PAIR`. Отсечка `participant_id <> :author_id` стоит
    и в `EXISTS`, и именно она не даёт этой строке сойти за пару.

    Мутация «убрать отсечку ТОЛЬКО из первой ветки» эквивалентна: дубль
    автора гасит `UNION`. Различима только отсечка в `EXISTS` — этот тест
    её и закрепляет (обнаружено мутационным прогоном: M6 «выжила» на
    обычной фикстуре).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await new_user(seed)
        place_id = await make_place(seed)
        card_uuid, card_id = await insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            starts_at=moment,
            status="finished",
        )
        # Единственная строка attendance — сам автор. Компании нет.
        await seed.execute(
            text(
                "INSERT INTO attendance (card_id, participant_id) "
                "VALUES (:c, :a) ON CONFLICT DO NOTHING"
            ),
            {"c": card_id, "a": int(author.id)},
        )
        await seed.commit()

    await run_attendance_prompts(factory, now=moment)

    async with factory() as check:
        # AC-12: маркер стоит (карточка обработана), но адресатов НЕТ.
        assert await marker(check, card_id) is not None
        assert await count_notifications(check, card_uuid) == 0
