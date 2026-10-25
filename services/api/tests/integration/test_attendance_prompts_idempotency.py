"""Идемпотентность запроса подтверждения (стори 7.3; AC-05, AC-06, AC-09).

Контракт «пропусков 0, дублей 0» доказывается ТРЕМЯ независимыми тестами,
каждый из которых краснеет от своей мутации:

- последовательные прогоны (`S18`) → `[1, 0, 0]`, уведомлений ровно N (M1);
- «рестарт» (новый `session_factory` над той же БД, `S19`) → 0; ловит любой
  маркер вне БД: in-memory set/Redis-ключ (M15);
- параллельные воркеры (`S44`, `asyncio.Barrier`) → сумма 1.

Гонки — только реальными соединениями с настоящими `commit` (§18.3). Фикстура
`session` (SAVEPOINT) здесь не годится: её `commit` не виден из другого
соединения вообще, и тест гонки стал бы тавтологией (R17).
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.attendance_prompt_service import prompt_attendance
from app.workers import attendance_prompts as prompts_module
from app.workers.attendance_prompts import run_attendance_prompts
from tests.integration.attendance_prompt_helpers import (
    _cleanup_seed,
    attendance_rows,
    company,
    count_notifications,
    database_url,
    factory,
    insert_card,
    make_place,
    marker,
    new_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

__all__ = ["factory", "_cleanup_seed"]


# ——— AC-05, AC-06: последовательные прогоны (S16, S18) ————————————————————


async def test_three_runs_prompt_once(factory: async_sessionmaker[AsyncSession]) -> None:
    """AC-05/AC-06 (S16/S18, M1): три прогона → `[1, 0, 0]`, уведомлений ровно N.

    Краснеет от удаления `AND attendance_prompted_at IS NULL` из CAS: тогда
    каждый прогон создавал бы новый комплект строк (3N вместо N).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0)
        await seed.commit()

    counters = [await run_attendance_prompts(factory, now=moment) for _ in range(3)]

    assert [counter["prompted"] for counter in counters] == [1, 0, 0]
    async with factory() as check:
        # 3 = автор + 2 участника; при M1 было бы 9.
        assert await count_notifications(check, seeded["card_uuid"]) == 3


async def test_restart_with_new_factory_prompts_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-06 (S19, M15): маркер живёт в БД, а не в памяти процесса.

    Рестарт эмулируется ДВУМЯ действиями: новый `async_sessionmaker` и
    `importlib.reload` модуля воркера. Второе обязательно: без перезагрузки
    модуля in-memory-фильтр (`_seen: set[int]`) пережил бы «рестарт» и тест
    остался бы зелёным при мутации M15 (проверено — первая версия теста
    именно так и вела себя).

    Мутация M15 («маркер только в памяти») после reload теряет маркер
    полностью: в БД он `NULL`, память пуста → второй прогон даёт
    `prompted == 1` и удваивает строки.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    assert (await run_attendance_prompts(factory, now=moment))["prompted"] == 1

    restarted = async_sessionmaker(bind=factory.kw["bind"], expire_on_commit=False)
    # P-13/M15: перезагрузка модуля сбрасывает любые in-memory-структуры —
    # ровно то, что делает рестарт процесса. Без неё мутация «маркер в
    # памяти» выжила бы (set остался бы в том же объекте модуля).
    import importlib

    from app.workers import attendance_prompts as prompts_module

    importlib.reload(prompts_module)
    assert (await prompts_module.run_attendance_prompts(restarted, now=moment))["prompted"] == 0

    async with factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 2
        assert await marker(check, seeded["card_id"]) is not None


# ——— AC-09: атомарность карточки (S27, M8) ————————————————————————————


async def test_failure_between_cas_and_insert_rolls_back_marker(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-09 (S27, M8): падение вставки откатывает и маркер, и `attendance`.

    Это и есть «атомарность карточки»: либо помечено и записано, либо ничего
    и карточка попадает в следующий прогон. Альтернатива (маркер отдельной
    транзакцией) оставляла бы 0 уведомлений НАВСЕГДА.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    from app.repositories import notifications as notifications_module

    async def _boom(self: Any, **kwargs: Any) -> int:
        raise RuntimeError("падение между CAS и вставкой (тест)")

    monkeypatch.setattr(
        notifications_module.NotificationsRepository, "create_attendance_requests", _boom
    )

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["failed"] == 1
    assert counters["prompted"] == 0
    async with factory() as check:
        # Маркер откатился, пары откатились, уведомлений нет.
        assert await marker(check, seeded["card_id"]) is None
        assert await attendance_rows(check, seeded["card_id"]) == []
        assert await count_notifications(check, seeded["card_uuid"]) == 0

    # Следующий прогон (после восстановления) доставляет РОВНО один раз.
    monkeypatch.undo()
    assert (await run_attendance_prompts(factory, now=moment))["prompted"] == 1
    async with factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 2


# ——— AC-08: отмена между выборкой и CAS (S26, M2) ————————————————————


async def test_cancel_between_select_and_cas_gives_empty_returning(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-08 (S26, M2): карточку отменили после выборки → CAS пуст, уведомлений 0.

    Без `status IN (...)` в предикате CAS маркер встал бы на отменённой
    карточке (урок CR 5б.4, патч 1): запрос «подтвердите явку» по отменённой
    встрече — утверждение, которого не было.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        # Между выборкой и CAS карточку отменили (второе соединение уже
        # закоммитило изменение — гонка на уровне реальных транзакций).
        await seed.execute(
            text("UPDATE event_cards SET status = 'cancelled' WHERE id = :id"),
            {"id": seeded["card_id"]},
        )
        await seed.commit()

    async with factory() as session:
        result = await prompt_attendance(
            session,
            card_id=seeded["card_id"],
            author_id=int(seeded["author"].id),
            card_uuid=uuid_lib.UUID(seeded["card_uuid"]),
            chat_uuid=None,
            starts_at=moment,
            now=moment,
        )
        await session.commit()

    assert result.outcome.value == "skipped"
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is None
        assert await count_notifications(check, seeded["card_uuid"]) == 0


# ——— S17: точное число SQL (D-TD-4) ———————————————————————————————


async def test_exactly_four_statements_per_card(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S17 (D-TD-4, M4/D-3) + 9.4 D-9.4-10: 4 SQL в транзакции карточки.

    Контракт назван явно, потому что «число запросов» — единственный способ
    заметить второй `SELECT` (например, за `starts_at`, которого нет в
    выборке). Ловим ПЕРЕХВАТОМ исполнения, а не предположением.

    **Считаются только запросы ДО публикации** (транзакция карточки):
    (1) выборка кандидатов, (2) CAS, (3) `ensure_rows`, (4) `INSERT ... SELECT`
    уведомлений. `commit` — не SQL; публикация — Redis, не SQL.

    **9.4 добавляет ПЯТЫЙ запрос — и это не нарушение контракта** (D-9.4-10).
    Отметка `delivered_at` ставится ОДНИМ `UPDATE ... ANY` ПОСЛЕ публикации и
    в отдельной транзакции: строки уже закоммичены вместе с маркером, и
    отметка не может быть их частью. Тест поэтому проверяет не «всего 4»,
    а «4 ДО публикации и ровно 1 отметка ПОСЛЕ». Прежняя формулировка
    («всего 4») стала бы ложной в обе стороны: она краснела бы на корректной
    реализации и молчала бы, если бы отметка влезла ВНУТРЬ транзакции.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        await company(seed, starts_at=moment, selected=1, rejected=0, chat_uuid=uuid_lib.uuid4())
        await seed.commit()

    events: list[tuple[str, str]] = []
    engine = factory.kw["bind"]

    def _before(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        stripped = " ".join(statement.split())
        if "event_cards" in stripped or "attendance" in stripped or "notifications" in stripped:
            events.append(("sql", stripped))

    async def _publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> bool:
        events.append(("publish", "chat"))
        return True

    async def _publish_user(*, user_uuid: uuid_lib.UUID, envelope: Any) -> bool:
        events.append(("publish", "user"))
        return True

    monkeypatch.setattr(prompts_module, "publish_to_chat", _publish)
    monkeypatch.setattr(prompts_module, "publish_to_user", _publish_user)

    event.listen(engine.sync_engine, "before_cursor_execute", _before)
    try:
        await run_attendance_prompts(factory, now=moment)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _before)

    # Граница — ПЕРВАЯ публикация: до неё идёт транзакция карточки, после неё
    # только отметки доставки (по одной транзакции на пачку адресатов).
    first_publish = next(index for index, item in enumerate(events) if item[0] == "publish")
    before = [payload for kind, payload in events[:first_publish] if kind == "sql"]
    after = [payload for kind, payload in events[first_publish:] if kind == "sql"]

    # Ровно четыре содержательных запроса ДО публикации: выборка, CAS,
    # `ensure_rows`, вставка уведомлений. Пятый = второй `SELECT` (дефект D-3).
    assert len(before) == 4, before
    assert any("FROM event_cards" in s for s in before)
    assert any(s.startswith("UPDATE event_cards") for s in before)
    assert any("INSERT INTO attendance" in s for s in before)
    assert any("INSERT INTO notifications" in s for s in before)

    # D-9.4-10: отметка доставки — ОДИН `UPDATE notifications` на пачку, и он
    # ПОСЛЕ публикации. Внутри транзакции карточки его быть не может: строки
    # к этому моменту уже закоммичены.
    assert len(after) == 1, after
    assert after[0].startswith("UPDATE notifications"), after[0]
    assert "delivered_at" in after[0]


# ——— R9: starvation батча (S42, M14) ————————————————————————————————


async def test_starvation_fresh_card_processed_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R9 (S42, M14): 250 обработанных старых карточек не вытесняют свежую.

    `ORDER BY starts_at, id` ставит старые первыми. Если предикат выборки
    потеряет `attendance_prompted_at IS NULL`, обработанные карточки вечно
    занимают `LIMIT 200`, и свежая не обрабатывается — тихая деградация
    без единой ошибки.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await new_user(seed)
        place_id = await make_place(seed)
        for index in range(250):
            await insert_card(
                seed,
                author_id=int(author.id),
                place_id=place_id,
                starts_at=moment - timedelta(days=2, minutes=index),
                status="finished",
                prompted_at=moment - timedelta(days=2, minutes=index),
            )
        fresh = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    counters = await run_attendance_prompts(factory, now=moment)

    assert counters["prompted"] == 1
    async with factory() as check:
        assert await marker(check, fresh["card_id"]) is not None


# ——— R18: два параллельных воркера (S44) ——————————————————————————


@pytest.fixture
async def race_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Отдельный движок для гонки: два соединения живут одновременно."""
    engine = create_async_engine(database_url(), pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def test_two_workers_prompt_once_total(
    race_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R18 (S44): два параллельных воркера → суммарно `prompted == 1`.

    `asyncio.Barrier` обязателен: без него второй воркер стартует после
    `commit` первого, и CAS отработает «по честному» — тест был бы зелёным
    при любой реализации. Барьер ставит оба воркера в выборку ДО того, как
    кто-то сделает CAS.

    Второй воркер видит пустой `RETURNING` — норма, а не ошибка (AC-05).
    """
    moment = datetime.now(UTC)
    async with race_factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=2, rejected=0)
        await seed.commit()

    barrier = asyncio.Barrier(2)

    async def _worker() -> dict[str, int]:
        await barrier.wait()
        return await run_attendance_prompts(race_factory, now=moment)

    first, second = await asyncio.gather(_worker(), _worker())

    assert first["prompted"] + second["prompted"] == 1
    assert first["failed"] == 0 and second["failed"] == 0
    async with race_factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 3


# ——— Изоляция CAS от SELECT (M1, M3) ———————————————————————————————
#
# Воркер защищён ДВУМЯ слоями: `SELECT` не отдаёт карточку и `CAS` не ставит
# маркер. Тест через воркер мутацию одного слоя не видит (второй слой
# спасает) — это обнаружено мутационным прогоном: M1 и M3 «выжили» на
# `test_three_runs_prompt_once` и `test_no_prompt_before_start`. Поэтому
# каждый слой проверяется НАПРЯМУЮ через сервис, минуя выборку.


async def test_service_second_call_is_skipped_without_select(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-05 (M1): CAS-слой сам по себе идемпотентен — БЕЗ защиты `SELECT`.

    Два прямых вызова `prompt_attendance` по одной карточке: второй обязан
    вернуть `SKIPPED`. Краснеет от удаления `AND attendance_prompted_at IS
    NULL` из `UPDATE`: тогда второй вызов создаёт второй комплект строк.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment, selected=1, rejected=0)
        await seed.commit()

    for _ in range(2):
        async with factory() as session:
            await prompt_attendance(
                session,
                card_id=seeded["card_id"],
                author_id=int(seeded["author"].id),
                card_uuid=uuid_lib.UUID(seeded["card_uuid"]),
                chat_uuid=None,
                starts_at=moment,
                now=moment,
            )
            await session.commit()

    # Второй вызов обязан быть пропуском: 2 строки, не 4.
    async with factory() as check:
        assert await count_notifications(check, seeded["card_uuid"]) == 2


async def test_service_call_before_start_is_skipped_without_select(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07 (M3): граница начала держится и в САМОМ CAS, не только в SELECT.

    Карточка с `starts_at = now + 1 час`, вызов сервиса напрямую: `SKIPPED`.
    Краснеет от удаления `AND starts_at <= :now` из `UPDATE` — тогда маркер
    встал бы до начала встречи, и запрос ушёл бы «подтвердите явку» заранее.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        seeded = await company(seed, starts_at=moment + timedelta(hours=1), selected=1, rejected=0)
        await seed.commit()

    async with factory() as session:
        result = await prompt_attendance(
            session,
            card_id=seeded["card_id"],
            author_id=int(seeded["author"].id),
            card_uuid=uuid_lib.UUID(seeded["card_uuid"]),
            chat_uuid=None,
            starts_at=moment + timedelta(hours=1),
            now=moment,
        )
        await session.commit()

    assert result.outcome.value == "skipped"
    async with factory() as check:
        assert await marker(check, seeded["card_id"]) is None
        assert await count_notifications(check, seeded["card_uuid"]) == 0
