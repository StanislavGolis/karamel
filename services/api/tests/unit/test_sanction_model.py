"""Модель `sanctions` (AC-07, AC-11)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models import Sanction
from app.models.base import TimestampMixin
from app.models.enums import SANCTION_TYPES, SanctionType

EXPECTED_COLUMNS = {
    "id",
    "uuid",
    "user_id",
    "report_id",
    "moderator_id",
    "type",
    "reason",
    "created_at",
}


def test_sanctions_columns_exact() -> None:
    """AC-07: ровно ожидаемые колонки."""
    actual = {c.name for c in Sanction.__table__.columns}
    assert actual == EXPECTED_COLUMNS


def test_sanctions_nullable_flags() -> None:
    """AC-07: `moderator_id` nullable (SET NULL), остальные обязательные."""
    cols = Sanction.__table__.columns
    assert cols["moderator_id"].nullable is True
    for name in ("user_id", "report_id", "type", "reason", "created_at", "uuid"):
        assert cols[name].nullable is False, name


def test_sanctions_no_timestamp_mixin() -> None:
    """X-7.1-1: `TimestampMixin` дал бы вечно-`NULL` `updated_at`."""
    assert TimestampMixin not in Sanction.__mro__
    assert "updated_at" not in {c.name for c in Sanction.__table__.columns}


def test_sanction_types_domain() -> None:
    """AC-07: `dismissed` — не санкция, в домен не входит."""
    assert set(SANCTION_TYPES) == {"warning", "ban"}
    assert set(SanctionType) == {SanctionType.WARNING, SanctionType.BAN}


def test_sanctions_constraints() -> None:
    """AC-07: `id > 0`, `UNIQUE uuid`, `UNIQUE report_id`, `CHECK type`."""
    checks = {c.name for c in Sanction.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "ck_sanctions_id_positive" in checks
    assert "ck_sanctions_type" in checks

    uniques = {
        tuple(c.name for c in u.columns)
        if len(u.columns) == 1
        else tuple(sorted(c.name for c in u.columns))
        for u in Sanction.__table__.constraints
        if isinstance(u, UniqueConstraint)
    }
    assert ("uuid",) in uniques
    assert ("report_id",) in uniques


def test_sanctions_history_index() -> None:
    """AC-08: индекс истории `(user_id, created_at DESC)`."""
    idx = {i.name: i for i in Sanction.__table__.indexes}
    assert "ix_sanctions_user_id_created_at" in idx
