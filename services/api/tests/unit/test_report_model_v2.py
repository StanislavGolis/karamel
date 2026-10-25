"""Модель `reports` после 8.1 (AC-01, AC-02, AC-06, AC-11).

Стори 8.1 расширяет `reports` до двух типов объектов. Тесты проверяют
ИМЕННО набор колонок и ограничений: лишняя колонка — красный, потому что
схема должна быть ровно такой, как описана в AC-01.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models import Report
from app.models.report import REPORT_RESOLUTIONS, REPORT_TARGET_TYPES

#: AC-01: точный набор колонок `reports` после 8.1 И 8.5.
#:
#: `accused_id` добавлен стори 8.5 (AC-12, D-64) — материализованный
#: обвиняемый для карточки модератора. Набор по-прежнему ТОЧНЫЙ: лишняя
#: колонка обязана краснить этот тест, иначе схема разойдётся с описанием
#: молча. Обновление набора — часть расширения схемы, а не ослабление
#: проверки.
EXPECTED_COLUMNS = {
    "id",
    "uuid",
    "reporter_id",
    "accused_id",
    "attendance_id",
    "chat_id",
    "event_card_id",
    "target_type",
    "status",
    "resolution",
    "reason",
    "reported_at",
    "resolved_at",
}


def test_reports_columns_exact() -> None:
    """AC-01: ровно ожидаемые колонки, ни одной лишней."""
    actual = {c.name for c in Report.__table__.columns}
    assert actual == EXPECTED_COLUMNS


def test_reports_nullable_flags() -> None:
    """AC-01: `attendance_id` теперь nullable, `chat_id`/`event_card_id`/`resolution` — тоже."""
    cols = Report.__table__.columns
    assert cols["attendance_id"].nullable is True
    assert cols["chat_id"].nullable is True
    assert cols["event_card_id"].nullable is True
    assert cols["resolution"].nullable is True
    # `reporter_id` и `target_type` остаются обязательными.
    assert cols["reporter_id"].nullable is False
    assert cols["target_type"].nullable is False


def test_report_target_types_domain() -> None:
    """AC-02: домен расширен ровно одним значением, `no_show` сохранено."""
    assert set(REPORT_TARGET_TYPES) == {"message", "no_show"}
    assert len(REPORT_TARGET_TYPES) == 2


def test_report_resolutions_domain() -> None:
    """AC-06: `dismissed` — закрытие без санкции, но входит в домен."""
    assert set(REPORT_RESOLUTIONS) == {"warning", "ban", "dismissed"}


def test_report_has_target_scope_check() -> None:
    """AC-03: одна формула `target_scope`, а не два раздельных `CHECK`."""
    names = {c.name for c in Report.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "ck_reports_target_scope" in names
    # Дефект, который поймал бы «два раздельных CHECK»: их было бы два.
    assert not any("chat_id_scope" in n or "attendance_scope" in n for n in names)


def test_report_resolution_checks_present() -> None:
    """AC-06: согласованность `status` ↔ `resolution` объявлена в модели."""
    names = {c.name for c in Report.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "ck_reports_status_resolution_consistency" in names
    assert "ck_reports_status_resolution_mapping" in names
    assert "ck_reports_resolution" in names


def test_report_composite_fk_declared() -> None:
    """AC-04: составной FK на пару `(id, card_id)`, а не одиночный."""
    from sqlalchemy import ForeignKeyConstraint

    composite = [
        c
        for c in Report.__table__.constraints
        if isinstance(c, ForeignKeyConstraint) and len(c.columns) > 1
    ]
    assert len(composite) == 1
    fk = composite[0]
    assert {c.key for c in fk.columns} == {"attendance_id", "event_card_id"}
    assert fk.name == "fk_reports_attendance_pair_attendance"
    assert fk.ondelete == "CASCADE"


def test_report_partial_unique_index() -> None:
    """AC-08: частичный `UNIQUE (chat_id, reporter_id)` — по одному на диалог."""
    idx = {i.name: i for i in Report.__table__.indexes}
    assert "uq_reports_chat_id_reporter_id" in idx
    index = idx["uq_reports_chat_id_reporter_id"]
    assert index.unique is True
    assert str(index.dialect_options["postgresql"]["where"]) == "chat_id IS NOT NULL"


def test_report_keeps_attendance_unique() -> None:
    """D-19: старый `UNIQUE (attendance_id, reporter_id)` сохраняется."""
    uniques = {
        tuple(sorted(c.name for c in u.columns))
        for u in Report.__table__.constraints
        if isinstance(u, UniqueConstraint) and len(u.columns) > 1
    }
    assert ("attendance_id", "reporter_id") in uniques
