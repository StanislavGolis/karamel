"""Идемпотентность жалобы на неявку (стори 7.4; AC-07, D-7.4-3, P-3).

**Почему повтор — `409`, а не тихий `200`** (D-7.4-3). Клиент обязан
отличать «принята сейчас» от «я уже жаловался»: второй успех создал бы
ложное впечатление новой жалобы, и человек ждал бы второго разбора.

**Почему защита — только `UNIQUE` + `ON CONFLICT`, без предпроверки** (P-3).
`SELECT EXISTS` перед `INSERT` открывает окно между чтением и записью, и два
одновременных запроса создают две строки (урок D-7.2-1). Тест
`test_no_select_on_reports_before_insert` фиксирует отсутствие такого
`SELECT` по тексту исполненных запросов, а не по чтению кода.

**Почему гонка проверяется на ДВУХ НАСТОЯЩИХ соединениях.** Фикстура
`session` живёт в SAVEPOINT: второй запрос видит незакоммиченные изменения
первого, и гонку на ней доказать нельзя (§18.3, `test_attendance_races.py`).
Сервис вызывается напрямую: HTTP-слой — тонкая обёртка (проверен отдельно),
а состязается здесь именно `INSERT ... ON CONFLICT`.

**Разные жалобщики на одну пару проходят оба** — это следствие формы
ограничения `UNIQUE (attendance_id, reporter_id)`: дублем считается жалоба
ТОГО ЖЕ человека на ТУ ЖЕ пару. Тест фиксирует фактическое поведение, чтобы
его нельзя было изменить молча (вопрос из постановки DS).
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.report_service import (
    ReportAlreadyExistsError,
    report_no_show,
)
from tests.integration.report_helpers import (
    ReportSettings,
    build_client,
    make_company,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


async def _row_count(session: AsyncSession, card_id: int) -> int:
    """Число жалоб по карточке (через пары, а не по `card_id` в `reports`)."""
    result = await session.execute(
        text(
            "SELECT count(*) FROM reports r "
            "  JOIN attendance a ON a.id = r.attendance_id "
            " WHERE a.card_id = :c"
        ),
        {"c": card_id},
    )
    return int(result.scalar_one())


async def test_repeat_report_gives_409_and_one_row(session: AsyncSession) -> None:
    """AC-07: повторная жалоба тем же участником → `409`, строка одна.

    Мутация M8 (`ON CONFLICT ... DO UPDATE` вместо `DO NOTHING`) делает тест
    красным: второй запрос вернул бы строку `RETURNING`, и клиент получил бы
    `201` на повтор — то есть два «успеха» на одну жалобу.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        first = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )
        second = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"code": "ATTENDANCE_ALREADY_REPORTED"}
    assert await _row_count(session, company["card_id"]) == 1


async def test_repeat_does_not_change_first_report(session: AsyncSession) -> None:
    """AC-07: повтор не переписывает `uuid`/`reported_at` первой жалобы.

    Иначе адрес жалобы, выданный клиенту, перестал бы указывать на неё же,
    и связь «показанная жалоба ↔ строка в очереди модератора» порвалась бы.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        first = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )
        second = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    row = await session.execute(
        text(
            "SELECT uuid, reported_at FROM reports r "
            "  JOIN attendance a ON a.id = r.attendance_id WHERE a.card_id = :c"
        ),
        {"c": company["card_id"]},
    )
    stored_uuid, stored_at = row.one()
    body = first.json()
    assert str(stored_uuid) == body["uuid"]
    assert stored_at.isoformat().replace("+00:00", "Z") == body["reported_at"]
    assert second.json() != body


async def test_different_reporters_on_same_pair_both_pass(session: AsyncSession) -> None:
    """AC-07: РАЗНЫЕ жалобщики на одну пару — обе жалобы проходят.

    Фактическое поведение `UNIQUE (attendance_id, reporter_id)`: дубль — это
    жалоба того же человека на ту же пару. Две стороны пары могут пожаловаться
    друг на друга (при `disputed` это ожидаемо, D-7.4-10), и запрет второй
    жалобы скрыл бы от модератора половину конфликта.
    """
    company = await make_company(session, pair_status="disputed", members=2)
    member = company["members"][0]

    async with build_client(session) as client:
        by_author = await post_report(
            client, company, token=company["author_token"], accused_uuid=member["user"].uuid
        )
        by_member = await post_report(
            client, company, token=member["token"], accused_uuid=company["author"].uuid
        )

    assert by_author.status_code == 201
    assert by_member.status_code == 201
    assert await _row_count(session, company["card_id"]) == 2


async def test_no_select_on_reports_before_insert(session: AsyncSession) -> None:
    """AC-07 (P-3, D-7.4-11): перед `INSERT` нет `SELECT` по `reports`.

    Доказательство — по ФАКТИЧЕСКИ исполненным запросам, а не по чтению кода:
    перехватывается `before_cursor_execute` и проверяется, что ни один запрос
    с `FROM reports` не предшествует `INSERT INTO reports`. Именно этот
    `SELECT` открыл бы окно гонки (урок D-7.2-1).

    Чтения по `users`/`attendance`/`event_cards` допустимы и ожидаемы: они не
    создают окна, потому что решение принимается самим `INSERT`.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any, **kwargs: Any) -> None:
        statements.append(statement)

    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", _capture)
    try:
        async with build_client(session) as client:
            response = await post_report(
                client, company, token=company["author_token"], accused_uuid=accused.uuid
            )
    finally:
        event.remove(listen_engine, "before_cursor_execute", _capture)

    assert response.status_code == 201
    inserts = [i for i, s in enumerate(statements) if "INSERT INTO reports" in s]
    assert len(inserts) == 1, statements
    before = statements[: inserts[0]]
    assert not [s for s in before if "FROM reports" in s], before


# ——— Гонка: два настоящих соединения (R-CAS-подобная проверка) ————————


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на собственном движке: коммиты настоящие.

    Пул ограничен четырьмя соединениями: две параллельные транзакции плюс
    одна для проверки результата после гонки.
    """
    engine = create_async_engine(DATABASE_URL, pool_size=4, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Удаляет созданные строки в порядке, обратном FK-зависимостям.

    Гонка коммитит по-настоящему, поэтому строки переживают прогон и ломали
    бы `count(*)` в посторонних тестах (CR 5а.6: 45 ложных падений).
    """
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    async with factory() as session:
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM reports WHERE reporter_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text("DELETE FROM event_cards WHERE author_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
        if _CREATED_PLACES:
            await session.execute(
                text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
            )
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
            )
        await session.commit()


async def test_concurrent_reports_on_same_pair(factory: async_sessionmaker[AsyncSession]) -> None:
    """AC-07 (P-3): два одновременных запроса → `201` + `409`, строка ОДНА.

    Гонка запускается барьером ДО вызова (не `sleep`, запрещён R1): проигравший
    блокируется на уникальном индексе, после коммита победителя получает
    пустой `RETURNING` и `409`. Без `UNIQUE` в схеме оба запроса дали бы `201`
    и две строки — то есть два разбора одной неявки.
    """
    async with factory() as setup:
        company = await make_company(setup, pair_status="no_show", members=2)
        _CREATED_USERS.append(company["author"].id)
        for member in company["members"]:
            _CREATED_USERS.append(member["user"].id)
        place = await setup.execute(
            text("SELECT place_id FROM event_cards WHERE id = :c"), {"c": company["card_id"]}
        )
        _CREATED_PLACES.append(int(place.scalar_one()))
        await setup.commit()
        card_uuid = uuid_lib.UUID(company["card_uuid"])
        author_id = company["author"].id
        accused_uuid = company["members"][0]["user"].uuid
        card_id = company["card_id"]

    barrier = asyncio.Barrier(2)
    results: list[str] = []

    async def _attempt() -> None:
        async with factory() as attempt_session:
            await barrier.wait()
            try:
                await report_no_show(
                    attempt_session,
                    card_uuid=card_uuid,
                    viewer_id=author_id,
                    accused_uuid=accused_uuid,
                    settings=ReportSettings(),
                )
            except ReportAlreadyExistsError:
                results.append("conflict")
                return
            # `commit` делает роут; здесь он же — иначе блокировка не снимется.
            await attempt_session.commit()
            results.append("created")

    await asyncio.gather(_attempt(), _attempt())

    assert sorted(results) == ["conflict", "created"]
    async with factory() as check:
        count = await check.execute(
            text(
                "SELECT count(*) FROM reports r "
                "  JOIN attendance a ON a.id = r.attendance_id WHERE a.card_id = :c"
            ),
            {"c": card_id},
        )
        assert int(count.scalar_one()) == 1


async def test_concurrent_reports_different_pairs_both_created(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-07: разные пары одной карточки — обе жалобы создаются параллельно.

    Ограничение обязано быть по ПАРЕ, а не по карточке: иначе автор не смог бы
    пожаловаться на второго неявившегося, и модератор увидел бы одну неявку
    из двух. Мутация M9 (`UNIQUE (attendance_id)` без `reporter_id` не ловится,
    а `UNIQUE(reporter_id)` — ловится этим тестом).
    """
    async with factory() as setup:
        company = await make_company(setup, pair_status="no_show", members=2)
        _CREATED_USERS.append(company["author"].id)
        for member in company["members"]:
            _CREATED_USERS.append(member["user"].id)
        place = await setup.execute(
            text("SELECT place_id FROM event_cards WHERE id = :c"), {"c": company["card_id"]}
        )
        _CREATED_PLACES.append(int(place.scalar_one()))
        await setup.commit()
        card_uuid = uuid_lib.UUID(company["card_uuid"])
        author_id = company["author"].id
        accused = [m["user"].uuid for m in company["members"]]
        card_id = company["card_id"]

    async def _attempt(accused_uuid: uuid_lib.UUID) -> None:
        async with factory() as attempt_session:
            await report_no_show(
                attempt_session,
                card_uuid=card_uuid,
                viewer_id=author_id,
                accused_uuid=accused_uuid,
                settings=ReportSettings(),
            )
            await attempt_session.commit()

    await asyncio.gather(_attempt(accused[0]), _attempt(accused[1]))

    async with factory() as check:
        count = await check.execute(
            text(
                "SELECT count(*) FROM reports r "
                "  JOIN attendance a ON a.id = r.attendance_id WHERE a.card_id = :c"
            ),
            {"c": card_id},
        )
        assert int(count.scalar_one()) == 2
