"""Каскадные удаления вокруг фактов явки (стори 10.6; Task 4.1, 4.2).

**Task 4.1 — AC-02.** Карточка удалена ДО истечения срока хранения
(`DELETE FROM event_cards` прямой командой, воркер не участвует) → строки
`attendance` этого `card_id` исчезают каскадом FK (`0020:156-160`). Это не
поведение воркера, а свойство схемы: AC-02 выполняется базой, и ретенция
`attendance` при удалении карточки не нужна.

**Task 4.2 — D-10.6-3, W-10.6-c.** Удаление строки `attendance` сносит жалобу
`reports` на пару (составной FK `(attendance_id, event_card_id)`,
`0025`) и каскадом — санкцию по ней (`sanctions.report_id`, CASCADE).
Это ИЗМЕНЕНИЕ наблюдаемой истории модератора, зафиксированное тестом,
а не открытие при первом разборе: FK в 10.6 НЕ меняются (D-10.6-3),
долг W-10.6-c с триггером «перед публичным запуском».
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceStatus
from tests.integration.attendance_history_helpers import add_attendance, make_card, make_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _count(session: AsyncSession, table: str, where: str, params: dict[str, object]) -> int:
    """Число строк таблицы по предикату — без ORM и identity map."""
    row = await session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)
    return int(row.scalar_one())


# --- Task 4.1: каскад при удалении карточки (AC-02) ---


async def test_deleting_card_cascades_attendance(session: AsyncSession) -> None:
    """AC-02: `DELETE FROM event_cards` уносит строки `attendance` каскадом.

    Воркер здесь НЕ участвует: проверяется `ON DELETE CASCADE` FK
    `attendance.card_id → event_cards.id` (`models/attendance.py:179`,
    `0020:156-160`). Если каскад потеряется, строки явки переживут карточку
    и останутся навсегда — ретенция их уже не увидит (нет карточки, нет
    `starts_at`).
    """
    author, _ = await make_user(session)
    participant, _ = await make_user(session)
    _, card_id = await make_card(
        session, author_id=author.id, starts_at=datetime.now(UTC) - timedelta(days=1)
    )
    await add_attendance(
        session, card_id=card_id, participant_id=participant.id, status="confirmed"
    )
    await session.flush()
    assert await _count(session, "attendance", "card_id = :c", {"c": card_id}) == 1

    await session.execute(text("DELETE FROM event_cards WHERE id = :c"), {"c": card_id})
    await session.flush()

    assert await _count(session, "attendance", "card_id = :c", {"c": card_id}) == 0


# --- Task 4.2: каскад на reports и sanctions (D-10.6-3) ---


async def test_deleting_attendance_cascades_report_and_sanction(session: AsyncSession) -> None:
    """D-10.6-3, W-10.6-c: удаление факта явки уносит жалобу и её санкцию.

    Фиксирует ТЕКУЩЕЕ поведение схемы: `reports.attendance_id` составным FK
    `(attendance_id, event_card_id) → attendance (id, card_id)` с CASCADE
    (`models/report.py:185-190`, `0025`), `sanctions.report_id → reports.id`
    тоже CASCADE (`models/sanction.py:82`).

    На пилоте безвредно (жалобы живут неделями, ретенция — 6 месяцев), но
    история модератора меняется: исчезнувшую санкцию нельзя объяснить
    постфактум. В 10.6 FK НЕ меняются (D-10.6-3) — тест нужен, чтобы каскад
    не стал открытием при первом вопросе «почему исчезла санкция».
    """
    author, _ = await make_user(session)
    accused, _ = await make_user(session)
    reporter, _ = await make_user(session)
    moderator, _ = await make_user(session, role="moderator")
    _, card_id = await make_card(
        session, author_id=author.id, starts_at=datetime.now(UTC) - timedelta(days=1)
    )
    attendance_id = await add_attendance(
        session,
        card_id=card_id,
        participant_id=accused.id,
        status=AttendanceStatus.NO_SHOW,
    )
    await session.flush()

    report_row = await session.execute(
        text(
            "INSERT INTO reports (target_type, reporter_id, attendance_id, event_card_id, "
            "reason, status) "
            "VALUES ('no_show', :r, :a, :c, 'не пришёл', 'open') RETURNING id"
        ),
        {"r": reporter.id, "a": attendance_id, "c": card_id},
    )
    report_id = int(report_row.scalar_one())
    await session.execute(
        text(
            "INSERT INTO sanctions (user_id, report_id, moderator_id, type, reason) "
            "VALUES (:u, :rep, :mod, 'warning', 'первое предупреждение')"
        ),
        {"u": accused.id, "rep": report_id, "mod": moderator.id},
    )
    await session.flush()
    assert await _count(session, "sanctions", "report_id = :r", {"r": report_id}) == 1

    # Ретенция удаляет именно строку `attendance` — как в проде.
    await session.execute(text("DELETE FROM attendance WHERE id = :a"), {"a": attendance_id})
    await session.flush()

    assert await _count(session, "reports", "id = :r", {"r": report_id}) == 0
    assert await _count(session, "sanctions", "report_id = :r", {"r": report_id}) == 0
