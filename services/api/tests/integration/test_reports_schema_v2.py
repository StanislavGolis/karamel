"""Схема `reports`/`sanctions` после 8.1 (AC-01…AC-08).

Проверки идут через `information_schema`/`pg_constraint`/`pg_indexes` и через
ФАКТИЧЕСКИЕ вставки, а не через модель: модель описывает намерение, а AC
говорит о реальной схеме (§18.1). Каждое ограничение проверяется НАРУШЕНИЕМ:
наличие `CHECK` в каталоге не доказывает, что он что-то запрещает.

§18.8: каждый ожидаемый отказ — в своём `begin_nested()`. После
`IntegrityError` транзакция аборчена, и следующая проверка была бы не про
ограничение, а про её состояние.

P-8: `target_scope` — 4 столбца × 2 значения = 16 комбинаций, из них
валидны ДВЕ, запрещены ЧЕТЫРНАДЦАТЬ. Перебор полный, не выборка: иначе
мутация одного из четырёх предикатов осталась бы зелёной.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-01: точный состав `reports` после 8.1 И 8.5.
#:
#: `accused_id` добавлен стори 8.5 (AC-12, D-64) — материализованный
#: обвиняемый. Состав остаётся ТОЧНЫМ: лишняя колонка краснит этот тест.
REPORTS_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "reporter_id": ("bigint", "NO"),
    "accused_id": ("bigint", "YES"),
    "attendance_id": ("bigint", "YES"),
    "chat_id": ("bigint", "YES"),
    "event_card_id": ("bigint", "YES"),
    "target_type": ("character varying", "NO"),
    "status": ("character varying", "NO"),
    "resolution": ("character varying", "YES"),
    "reason": ("text", "YES"),
    "reported_at": ("timestamp with time zone", "NO"),
    "resolved_at": ("timestamp with time zone", "YES"),
}

#: AC-07: точный состав `sanctions`.
SANCTIONS_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "user_id": ("bigint", "NO"),
    "report_id": ("bigint", "NO"),
    "moderator_id": ("bigint", "YES"),
    "type": ("character varying", "NO"),
    "reason": ("text", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
}


def _phone() -> str:
    return f"+7909{uuid_lib.uuid4().int % 10**7:07d}"


async def _user(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO users (uuid, provider, provider_user_id, phone, role, status, "
            "age_confirmed, consent_given_at) "
            "VALUES (gen_random_uuid(), 'phone', :p, :p, 'user', 'active', true, now()) "
            "RETURNING id"
        ),
        {"p": _phone()},
    )
    return int(row.scalar_one())


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
    )
    return int(row.scalar_one())


async def _card(session: AsyncSession, *, author_id: int, place_id: int) -> int:
    now = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at) "
            "VALUES (:a, :p, 'tomsk', 'Встреча', NULL, 'cafe', :s, 4, 2880, :e, "
            "'published', 0, 'approved', :pub) RETURNING id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "s": now + timedelta(days=3),
            "e": now + timedelta(days=1),
            "pub": now,
        },
    )
    return int(row.scalar_one())


async def _chat(session: AsyncSession, *, card_id: int) -> int:
    row = await session.execute(
        text("INSERT INTO chats (uuid, event_card_id) VALUES (gen_random_uuid(), :c) RETURNING id"),
        {"c": card_id},
    )
    return int(row.scalar_one())


async def _attendance(session: AsyncSession, *, card_id: int, participant_id: int) -> int:
    row = await session.execute(
        text(
            "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
            "participant_confirmed, status, confirmed_at) "
            "VALUES (:c, :p, false, false, 'no_show', now()) RETURNING id"
        ),
        {"c": card_id, "p": participant_id},
    )
    return int(row.scalar_one())


class Fixture:
    """Всё нужное для вставок: пара, карточка пары, диалог, пользователи."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


@pytest.fixture
async def fixtures(session: AsyncSession) -> Fixture:
    reporter = await _user(session)
    accused = await _user(session)
    author = await _user(session)
    place = await _place(session)

    pair_card = await _card(session, author_id=author, place_id=place)
    attendance = await _attendance(session, card_id=pair_card, participant_id=accused)

    # Диалог принадлежит ДРУГОЙ карточке: `chats.event_card_id` уникален, а
    # жалоба на переписку и жалоба на неявку — разные предметы.
    chat_card = await _card(session, author_id=author, place_id=place)
    chat = await _chat(session, card_id=chat_card)

    await session.flush()
    return Fixture(
        reporter=reporter,
        accused=accused,
        attendance=attendance,
        pair_card=pair_card,
        chat=chat,
    )


async def _columns(session: AsyncSession, table: str) -> dict[str, tuple[str, str]]:
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0]: (row[1], row[2]) for row in result}


# --- AC-01, AC-07: состав колонок -------------------------------------------


async def test_reports_columns_exact(session: AsyncSession) -> None:
    """AC-01: равенство, а не подмножество — лишняя колонка тоже красная."""
    assert await _columns(session, "reports") == REPORTS_COLUMNS


async def test_sanctions_columns_exact(session: AsyncSession) -> None:
    """AC-07: `reason NOT NULL`, `moderator_id` nullable."""
    assert await _columns(session, "sanctions") == SANCTIONS_COLUMNS


# --- AC-02…AC-07: имена ограничений -----------------------------------------


async def test_constraint_names(session: AsyncSession) -> None:
    """AC-02/03/06/07: имена — часть контракта `downgrade` и `alembic check`."""
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid IN ('reports'::regclass, 'sanctions'::regclass)"
        )
    )
    names = {row[0] for row in result}
    expected = {
        "ck_reports_target_scope",
        "ck_reports_status_resolution_consistency",
        "ck_reports_status_resolution_mapping",
        "ck_reports_resolution",
        "ck_reports_target_type",
        "ck_sanctions_type",
        "ck_sanctions_id_positive",
        "fk_reports_attendance_pair_attendance",
        "fk_reports_chat_id_chats",
        "fk_sanctions_report_id_reports",
        "fk_sanctions_user_id_users",
        "fk_sanctions_moderator_id_users",
        "uq_sanctions_report_id",
        "uq_sanctions_uuid",
    }
    missing = expected - names
    assert not missing, f"нет ограничений: {sorted(missing)}"


# --- AC-03, D-15: полная матрица `target_scope` ------------------------------


async def _insert_report(
    session: AsyncSession,
    *,
    reporter: int,
    target_type: str,
    attendance_id: int | None,
    event_card_id: int | None,
    chat_id: int | None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO reports (reporter_id, attendance_id, event_card_id, chat_id, "
            "target_type, status) VALUES (:r, :a, :c, :ch, :t, 'open')"
        ),
        {
            "r": reporter,
            "a": attendance_id,
            "c": event_card_id,
            "ch": chat_id,
            "t": target_type,
        },
    )


async def test_two_valid_combinations_accepted(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-03: валидны РОВНО две комбинации из 16."""
    await _insert_report(
        session,
        reporter=fixtures.reporter,
        target_type="no_show",
        attendance_id=fixtures.attendance,
        event_card_id=fixtures.pair_card,
        chat_id=None,
    )
    await _insert_report(
        session,
        reporter=fixtures.reporter,
        target_type="message",
        attendance_id=None,
        event_card_id=None,
        chat_id=fixtures.chat,
    )
    result = await session.execute(text("SELECT count(*) FROM reports"))
    assert result.scalar_one() == 2


#: AC-03, D-15: ВСЕ 14 запрещённых комбинаций. Строка — `(target_type,
#: attendance, event_card, chat)`; `1` = задано, `0` = NULL. Полный перебор:
#: 2 типа × 2^3 = 16, минус две валидные (`no_show/1/1/0`, `message/0/0/1`).
FORBIDDEN: tuple[tuple[str, int, int, int], ...] = (
    # `no_show` — 7 запрещённых из 8.
    ("no_show", 0, 0, 0),  # жалоба без объекта
    ("no_show", 0, 0, 1),  # только диалог
    ("no_show", 0, 1, 0),  # только карточка, без пары
    ("no_show", 0, 1, 1),  # карточка + диалог
    ("no_show", 1, 0, 0),  # пара без карточки
    ("no_show", 1, 0, 1),  # пара + диалог, без карточки
    ("no_show", 1, 1, 1),  # все три сразу
    # `message` — 7 запрещённых из 8.
    ("message", 0, 0, 0),  # жалоба без объекта
    ("message", 0, 1, 0),  # только карточка
    ("message", 0, 1, 1),  # карточка + диалог
    ("message", 1, 0, 0),  # только пара
    ("message", 1, 0, 1),  # пара + диалог
    ("message", 1, 1, 0),  # пара + карточка
    ("message", 1, 1, 1),  # все три сразу
)


@pytest.mark.parametrize(("target_type", "has_att", "has_card", "has_chat"), FORBIDDEN)
async def test_forbidden_scope_combinations_rejected(
    session: AsyncSession,
    fixtures: Fixture,
    target_type: str,
    has_att: int,
    has_card: int,
    has_chat: int,
) -> None:
    """AC-03: каждая из 14 запрещённых комбинаций отвергается.

    P-8: полнота важна — мутация любого из четырёх предикатов `target_scope`
    должна ронять ХОТЯ БЫ ОДИН параметр этого теста.
    """
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_report(
                session,
                reporter=fixtures.reporter,
                target_type=target_type,
                attendance_id=fixtures.attendance if has_att else None,
                event_card_id=fixtures.pair_card if has_card else None,
                chat_id=fixtures.chat if has_chat else None,
            )


# --- AC-04, D-14: составной FK ----------------------------------------------


async def test_composite_fk_rejects_foreign_card(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-04: `event_card_id` из ЧУЖОЙ пары отвергается.

    Одиночный FK `event_card_id → event_cards.id` пропустил бы такую строку:
    карточка существует, но паре она не принадлежит. Именно поэтому FK
    составной.
    """
    other_card = await _card(session, author_id=fixtures.accused, place_id=await _place(session))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_report(
                session,
                reporter=fixtures.reporter,
                target_type="no_show",
                attendance_id=fixtures.attendance,
                event_card_id=other_card,
                chat_id=None,
            )


async def test_delete_card_cascades_to_reports(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-04: `DELETE` карточки уносит `attendance` и каскадом жалобу."""
    await _insert_report(
        session,
        reporter=fixtures.reporter,
        target_type="no_show",
        attendance_id=fixtures.attendance,
        event_card_id=fixtures.pair_card,
        chat_id=None,
    )
    await session.flush()

    await session.execute(text("DELETE FROM event_cards WHERE id = :c"), {"c": fixtures.pair_card})
    await session.flush()

    left = await session.execute(text("SELECT count(*) FROM reports"))
    assert left.scalar_one() == 0, "жалоба пережила удаление карточки"
    att = await session.execute(
        text("SELECT count(*) FROM attendance WHERE id = :a"), {"a": fixtures.attendance}
    )
    assert att.scalar_one() == 0


# --- AC-06, D-16: матрица `resolution` --------------------------------------


async def _resolve(
    session: AsyncSession, *, report_uuid: Any, status: str, resolution: str | None
) -> None:
    await session.execute(
        text(
            "UPDATE reports SET status = :s, resolution = :r, resolved_at = now() WHERE uuid = :u"
        ),
        {"s": status, "r": resolution, "u": report_uuid},
    )


async def _open_report(session: AsyncSession, fixtures: Fixture) -> Any:
    row = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, attendance_id, event_card_id, target_type, status) "
            "VALUES (:r, :a, :c, 'no_show', 'open') RETURNING uuid"
        ),
        {"r": fixtures.reporter, "a": fixtures.attendance, "c": fixtures.pair_card},
    )
    return row.scalar_one()


async def test_resolution_matching_status_accepted(
    session: AsyncSession, fixtures: Fixture
) -> None:
    """AC-06: `resolved_warning` + `warning` проходит."""
    report_uuid = await _open_report(session, fixtures)
    await _resolve(
        session, report_uuid=report_uuid, status="resolved_warning", resolution="warning"
    )
    await session.flush()
    result = await session.execute(
        text("SELECT resolution FROM reports WHERE uuid = :u"), {"u": report_uuid}
    )
    assert result.scalar_one() == "warning"


@pytest.mark.parametrize(
    ("status", "resolution"),
    [
        ("open", "warning"),  # решение при неразобранной
        ("resolved_warning", None),  # разобрана без решения
        ("resolved_warning", "ban"),  # несоответствие вида
        ("resolved_ban", "warning"),
        ("dismissed", "ban"),
        ("dismissed", None),
    ],
)
async def test_resolution_mismatches_rejected(
    session: AsyncSession, fixtures: Fixture, status: str, resolution: str | None
) -> None:
    """AC-06: шесть несогласованных пар отвергаются."""
    report_uuid = await _open_report(session, fixtures)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _resolve(session, report_uuid=report_uuid, status=status, resolution=resolution)


async def test_open_report_cannot_have_resolution(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-06: `resolution` у `open` запрещён бикондиционалом."""
    report_uuid = await _open_report(session, fixtures)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE reports SET resolution = 'warning' WHERE uuid = :u"),
                {"u": report_uuid},
            )


# --- AC-07: `sanctions` ------------------------------------------------------


async def _sanction(
    session: AsyncSession,
    *,
    user_id: int,
    report_id: int,
    moderator_id: int | None,
    type_: str = "warning",
) -> None:
    await session.execute(
        text(
            "INSERT INTO sanctions (user_id, report_id, moderator_id, type, reason) "
            "VALUES (:u, :r, :m, :t, 'причина')"
        ),
        {"u": user_id, "r": report_id, "m": moderator_id, "t": type_},
    )


async def _report_id(session: AsyncSession, fixtures: Fixture) -> int:
    """Новая жалоба на пару. Репортёр — НОВЫЙ.

    `UNIQUE (attendance_id, reporter_id)` (7.4) не даёт одному человеку
    пожаловаться на одну пару дважды: без нового репортёра второй вызов
    падал бы ДО проверки `sanctions`, и тест проверял бы не то ограничение.
    """
    reporter = await _user(session)
    row = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, attendance_id, event_card_id, target_type, status) "
            "VALUES (:r, :a, :c, 'no_show', 'open') RETURNING id"
        ),
        {"r": reporter, "a": fixtures.attendance, "c": fixtures.pair_card},
    )
    return int(row.scalar_one())


async def test_sanctions_unique_report_id(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-07: два решения по одной жалобе невозможны (защита `409` в 8.4)."""
    report_id = await _report_id(session, fixtures)
    await _sanction(session, user_id=fixtures.accused, report_id=report_id, moderator_id=None)
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _sanction(
                session,
                user_id=fixtures.accused,
                report_id=report_id,
                moderator_id=None,
                type_="ban",
            )


async def test_sanctions_type_domain(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-07: `warning`/`ban` проходят, `mute` отвергается."""
    first = await _report_id(session, fixtures)
    second = await _report_id(session, fixtures)
    await _sanction(session, user_id=fixtures.accused, report_id=first, moderator_id=None)
    await _sanction(
        session, user_id=fixtures.accused, report_id=second, moderator_id=None, type_="ban"
    )

    third = await _report_id(session, fixtures)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _sanction(
                session, user_id=fixtures.accused, report_id=third, moderator_id=None, type_="mute"
            )


async def test_moderator_delete_sets_null(session: AsyncSession, fixtures: Fixture) -> None:
    """AC-07: удаление модератора оставляет санкцию с `moderator_id IS NULL`."""
    report_id = await _report_id(session, fixtures)
    moderator = await _user(session)
    await _sanction(session, user_id=fixtures.accused, report_id=report_id, moderator_id=moderator)
    await session.flush()

    await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": moderator})
    await session.flush()

    result = await session.execute(text("SELECT moderator_id FROM sanctions"))
    assert result.scalar_one() is None


# --- AC-08: индексы ----------------------------------------------------------


async def test_sanctions_history_index(session: AsyncSession) -> None:
    """AC-08: `(user_id, created_at DESC)`."""
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_sanctions_user_id_created_at'")
    )
    definition = result.scalar_one()
    assert "user_id" in definition
    assert "created_at DESC" in definition


async def test_reports_chat_index_is_partial_unique(
    session: AsyncSession, fixtures: Fixture
) -> None:
    """AC-08, D-19: частичный `UNIQUE`, а не полный.

    P-8: предикат проверяется НАРУШЕНИЕМ в ОБОИХ значениях. Чтения `indexdef`
    недостаточно: индекс с предикатом и индекс без него дают одинаковую
    строку «UNIQUE», а различие — в поведении. `no_show`-жалобы имеют
    `chat_id IS NULL` и не должны конфликтовать между собой: полный `UNIQUE`
    запретил бы второму участнику компании пожаловаться на другую неявку.
    """
    # (1) Поведение: второй `message` на тот же диалог от того же автора — отказ.
    await _insert_report(
        session,
        reporter=fixtures.reporter,
        target_type="message",
        attendance_id=None,
        event_card_id=None,
        chat_id=fixtures.chat,
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_report(
                session,
                reporter=fixtures.reporter,
                target_type="message",
                attendance_id=None,
                event_card_id=None,
                chat_id=fixtures.chat,
            )
    # (2) Поведение: две `no_show` с `chat_id IS NULL` не конфликтуют.
    #     Полный `UNIQUE` на `(chat_id, reporter_id)` запретил бы вторую.
    await _insert_report(
        session,
        reporter=fixtures.reporter,
        target_type="no_show",
        attendance_id=fixtures.attendance,
        event_card_id=fixtures.pair_card,
        chat_id=None,
    )
    # (3) Каталог: предикат записан в индексе (иначе проверка (2) прошла бы
    #     на полном UNIQUE при отсутствии строк-дублей).
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_reports_chat_id_reporter_id'")
    )
    definition = result.scalar_one()
    assert "UNIQUE" in definition
    assert "WHERE (chat_id IS NOT NULL)" in definition


async def test_attendance_pair_index_is_valid(session: AsyncSession) -> None:
    """AC-08: `CONCURRENTLY` может оставить НЕВАЛИДНЫЙ индекс."""
    result = await session.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'uq_attendance_id_card_id'"
        )
    )
    assert result.scalar_one() is True
