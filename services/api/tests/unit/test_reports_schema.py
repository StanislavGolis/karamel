"""Модель `Report` против миграций `0023`+`0025` (стори 7.4, расширено в 8.1).

**Обновлено в 8.1.** Жалоба получила второй объект (`message`, FR36/FR37):
`attendance_id` стал nullable, домен `target_type` расширен, добавлены
`chat_id`/`event_card_id`/`resolution` и три ограничения. Проверки ниже
описывают итоговую схему после `0025`, а не состояние 7.4: `alembic check`
сравнивает модель с БД и не прощает расхождения.

**Почему unit, а не integration.** Здесь проверяется СОГЛАСОВАННОСТЬ модели и
миграции на уровне объектов: набор `CHECK`, имена по конвенции, типы колонок.
Фактическое поведение ограничений на живой БД проверяет
`tests/integration/test_reports_migration_cycle.py` — иначе расхождение
модели и миграции (класс дефекта 3.1/4.1) было бы видно только с одной стороны.

**Почему сравниваются ОБА источника.** `alembic check` сравнивает схему с
моделью и молчит, если обе совпадают неверно (например, `CHECK` объявлен
только в модели и не попал в миграцию — тогда `DROP`/`CREATE` цикла покажет
это, но не тип). Явное сравнение текстов ограничений ловит подмену
(`(status = 'open') = (resolved_at IS NULL)` вместо двух раздельных `CHECK`).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models.report import (
    REPORT_STATUSES,
    REPORT_TARGET_TYPES,
    Report,
)


def test_table_name_and_absence_of_timestamp_mixin() -> None:
    """AC-01 (X-7.1-1): таблица `reports`, `created_at`/`updated_at` НЕ наследуются.

    Путь записи — сырой `INSERT ... ON CONFLICT`: `onupdate` ORM на нём не
    срабатывает, и `updated_at` осталась бы `NULL`/старой навсегда — ложный
    аудит «когда менялось».
    """
    assert Report.__tablename__ == "reports"
    columns = Report.__table__.c
    assert "created_at" not in columns
    assert "updated_at" not in columns
    assert "reported_at" in columns


def test_column_types_and_nullability() -> None:
    """AC-01: типы и `nullable` совпадают с миграцией.

    `reporter_id`/`attendance_id`/`uuid`/`reported_at` NOT NULL: жалоба без
    автора, без пары, без адреса или без времени не адресуется и не считается
    в очереди 8.3. `resolved_at` NULL — разбор ещё не начат.
    """
    columns = Report.__table__.c
    assert str(columns.id.type) == "BIGINT"
    assert str(columns.uuid.type) == "UUID"
    assert str(columns.reporter_id.type) == "BIGINT"
    assert str(columns.attendance_id.type) == "BIGINT"
    assert str(columns.target_type.type) == "VARCHAR(16)"
    assert str(columns.status.type) == "VARCHAR(24)"
    assert str(columns.reason.type) == "TEXT"
    # `DateTime(timezone=True)` рендерится SQLAlchemy как `DATETIME` с флагом
    # `timezone=True`, а не как `TIMESTAMP`: сравнение со строкой `TIMESTAMP`
    # падало бы на КОРРЕКТНОЙ модели (поймано первым прогоном). Важно здесь
    # именно `timezone=True` — без зоны сравнение с `now()` даёт сдвиг.
    assert str(columns.reported_at.type) == "DATETIME"
    assert columns.reported_at.type.timezone is True  # type: ignore[attr-defined]
    assert str(columns.resolved_at.type) == "DATETIME"
    assert columns.resolved_at.type.timezone is True  # type: ignore[attr-defined]
    assert columns.resolved_at.nullable is True
    # AC-01, D-15 (8.1): `attendance_id` стал nullable — у жалобы на переписку
    # пары явки нет. `event_card_id`/`chat_id`/`resolution` тоже nullable:
    # заполняются по типу объекта и по факту разбора.
    assert columns.attendance_id.nullable is True
    required_not_null = (
        "uuid",
        "reporter_id",
        "target_type",
        "status",
        "reported_at",
    )
    for required in required_not_null:
        assert columns[required].nullable is False, required


def test_domain_values_are_exactly_the_checks() -> None:
    """AC-02 (8.1): домен `target_type` — `message` и `no_show`.

    В 7.4 было единственное значение; стори 8.1 добавила жалобу на переписку.
    Расширение домена обязано менять и этот кортеж, и `CHECK` — расхождение
    ловит `test_checks_declared_in_model` ниже.
    """
    assert set(REPORT_TARGET_TYPES) == {"message", "no_show"}
    assert REPORT_STATUSES == ("open", "resolved_warning", "resolved_ban", "dismissed")
    assert Report.__table__.c.status.server_default is not None
    assert "open" in str(Report.__table__.c.status.server_default.arg)


def test_checks_declared_in_model() -> None:
    """AC-01, AC-11: `CHECK`-и модели — ровно ожидаемый набор формулировок.

    Бикондиционал разбора проверяется как СТРОКА: если его заменить двумя
    раздельными `CHECK` (класс ошибки CR 7.3 H-2 — `NULL` слева даёт
    `UNKNOWN`, а `CHECK` отвергает только `FALSE`), набор текстов изменится.
    """
    texts = {str(c.sqltext) for c in Report.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "id > 0" in texts
    assert "target_type IN ('message', 'no_show')" in texts
    # AC-06 (8.1): исход разбора и его согласованность со `status`.
    assert "resolution IN ('warning', 'ban', 'dismissed')" in texts
    assert "(status = 'open') = (resolution IS NULL)" in texts
    assert "status IN ('open', 'resolved_warning', 'resolved_ban', 'dismissed')" in texts
    assert "(status = 'open') = (resolved_at IS NULL)" in texts
    assert "resolved_at >= reported_at" in texts


def test_uniques_match_on_conflict_targets() -> None:
    """AC-01, AC-07: `uq_reports_attendance_id_reporter_id` — цель `ON CONFLICT`.

    Имя строится конвенцией `models/base.py`, и репозиторий полагается на
    колонки этого ограничения. Пропажа `UNIQUE` убрала бы ЕДИНСТВЕННУЮ
    защиту от дубля (P-3), и `ON CONFLICT` упал бы с «no unique constraint».
    """
    uniques = [
        tuple(col.name for col in c.columns)
        for c in Report.__table__.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert ("attendance_id", "reporter_id") in uniques
    assert ("uuid",) in uniques


def test_index_covers_status_then_reported_at() -> None:
    """AC-10 (NFR52): составной индекс `(status, reported_at)`.

    Очередь 8.3 фильтрует по `status` и сортирует по `reported_at`; индекс
    только по времени не покрыл бы фильтр, и выборка шла бы по всей таблице.
    """
    index = next(
        (i for i in Report.__table__.indexes if i.name == "ix_reports_status_reported_at"),
        None,
    )
    assert index is not None
    assert [col.name for col in index.columns] == ["status", "reported_at"]


def test_foreign_keys_cascade() -> None:
    """AC-01 (ADR-07): обе FK — `ON DELETE CASCADE`.

    Жалоба без жалобщика и жалоба без факта явки не адресуются и не
    проверяются; ретенция уносит их вместе с субъектом.
    """
    for column_name, target in (("reporter_id", "users.id"), ("attendance_id", "attendance.id")):
        fks = list(Report.__table__.c[column_name].foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == target
        assert fks[0].ondelete == "CASCADE"
