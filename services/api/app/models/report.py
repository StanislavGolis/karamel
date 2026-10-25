"""Модель жалобы `reports` (стори 7.4, расширена в 8.1).

**Два объекта жалобы.** В 7.4 домен был один — неявка на встречу (`no_show`).
Стори 8.1 добавляет жалобу на собеседника в переписке (`message`, FR36/FR37),
и обе живут в одной таблице: модератор работает с одной очередью (FR53).

**`message_id` отсутствует осознанно** (D-13). Жалоба подаётся на человека,
а не на реплику: фрагмент 8.3 берёт 10 последних сообщений диалога. Ссылка на
сообщение дала бы вторую ось валидации («сообщение принадлежит чату») и ложные
`404` при удалении одной реплики.

**`event_card_id` задаёт PostgreSQL, не приложение** (D-14). Колонка нужна
AC-02 шарда, но денормализация без ограничения разошлась бы с `attendance.card_id`
при переносе пары. Составной FK `(attendance_id, event_card_id) →
attendance (id, card_id)` делает рассинхронизацию невыразимой.

**`status` не переименован** (D-16). Закрытая 7.4 использует четыре значения
и 2299 тестов; `resolution` добавляет «какая санкция вынесена» отдельным полем,
а пара ограничений держит их согласованными.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ReportResolution, ReportTargetType

#: AC-02: домен объекта жалобы. Два значения вместо одного (7.4).
REPORT_TARGET_TYPES: tuple[str, ...] = tuple(t.value for t in ReportTargetType)

#: AC-06: домен исхода разбора. `dismissed` — закрытие БЕЗ санкции (FR89).
REPORT_RESOLUTIONS: tuple[str, ...] = tuple(r.value for r in ReportResolution)

#: AC-06: состояния разбора. `open` создаёт код, остальные — модератор (8.3/8.4).
REPORT_STATUSES: tuple[str, ...] = (
    "open",
    "resolved_warning",
    "resolved_ban",
    "dismissed",
)

#: AC-03, D-15: ОДНА формула цели. Два раздельных `CHECK` допустили бы
#: «жалобу без объекта» (`attendance_id` и `chat_id` оба `NULL`) и «жалобу на
#: два объекта сразу». Одна дизъюнкция из двух полных конъюнкций делает оба
#: состояния невыразимыми — несогласованность невозможна по построению.
TARGET_SCOPE_SQL = (
    "(target_type = 'no_show' AND attendance_id IS NOT NULL "
    "AND event_card_id IS NOT NULL AND chat_id IS NULL) "
    "OR (target_type = 'message' AND chat_id IS NOT NULL "
    "AND attendance_id IS NULL AND event_card_id IS NULL)"
)

#: AC-06: «разбор не начат ⟺ решения нет». Бикондиционал, а не два `CHECK`:
#: `(status = 'open') = (resolution IS NULL)` запрещает «разобрана без решения».
STATUS_RESOLUTION_CONSISTENCY_SQL = "(status = 'open') = (resolution IS NULL)"

#: AC-06: соответствие состояния и вида решения. Ветка `open` — TRUE
#: независимо от `resolution`: непустоту при `open` уже запрещает
#: бикондиционал выше (разделение обязанностей, а не дублирование).
STATUS_RESOLUTION_MAPPING_SQL = (
    "status = 'open' "
    "OR (status = 'resolved_warning' AND resolution = 'warning') "
    "OR (status = 'resolved_ban' AND resolution = 'ban') "
    "OR (status = 'dismissed' AND resolution = 'dismissed')"
)

#: AC-11: \"разбор начат ⟺ метка разбора пуста\" (7.4, сохранено).
STATUS_RESOLVED_CONSISTENCY_SQL = "(status = 'open') = (resolved_at IS NULL)"

#: AC-11: `NULL` слева даёт `UNKNOWN`, а `CHECK` отвергает только `FALSE` —
#: поэтому ограничение работает лишь вместе с бикондиционалом выше.
RESOLVED_AFTER_REPORTED_SQL = "resolved_at >= reported_at"


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений.

    Восьмая копия функции — существующий долг `deferred-work.md`
    (см. `models/attendance.py`). Вынос в общий модуль затронул бы семь
    закрытых стори, поэтому внутри 8.1 не делается.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Report(Base):
    """Жалоба участника: на неявку пары (`no_show`) или на собеседника (`message`)."""

    __tablename__ = "reports"

    # AC-01, D-TD-2: `Identity()` + `CHECK (id > 0)`. Оба обязательны.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # AC-03, AC-08 (7.4): публичный адрес жалобы.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )

    # AC-01: жалобщик. `CASCADE` — жалоба без автора не адресуется (ADR-07).
    reporter_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # AC-01: у `message` пары нет — колонка nullable (D-15).
    # FK на `attendance` объявляется составным в `__table_args__`.
    attendance_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # AC-01, D-13: диалог, на который подана жалоба. Nullable: у `no_show` его нет.
    chat_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=True
    )

    # AC-01, D-14: карточка встречи. Заполняется только backfill-ом и FK-ом;
    # приложение значение не пишет.
    event_card_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # AC-02, D-7.4-8: объект жалобы. `String(16)` с запасом: `message` = 7.
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # AC-06, NFR52: состояние разбора. `open` — единственное, что создаёт код.
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="open", server_default=text("'open'")
    )

    # AC-06, D-16: какая санкция вынесена. `NULL` ⟺ `status='open'`.
    resolution: Mapped[str | None] = mapped_column(String(16))

    # AC-05, X-7.4-3: причина — текст модератора (8.4). Жалобщик её не пишет.
    reason: Mapped[str | None] = mapped_column(Text)

    # AC-05, NFR52, D-7.4-5: время жалобы. Имя по домену, не `created_at`:
    # очередь 8.3 сортирует по нему и считает по нему же 20 часов (NFR63).
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # AC-11: метка разбора. `NULL` ⟺ `status='open'`.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Стори 8.5 (AC-12, D-64): обвиняемый. До 8.5 вычислялся на лету
    # (`_ACCUSED_SQL` в 8.4) — приемлемо для одного решения, но карточка
    # модератора читает `WHERE accused_id = :id` на КАЖДОЕ открытие.
    #
    # `SET NULL`, а не `CASCADE` (D-64): жалоба — факт разбора и переживает
    # удаление обвиняемого; `CASCADE` снёс бы её из истории жалобщика вместе
    # с чужим аккаунтом. Приложение пишет это поле при подаче (AC-13) —
    # иначе FR54 работал бы только для жалоб, попавших под backfill `0028`.
    accused_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # AC-01: имя PK строится конвенцией (`pk_reports`).
        PrimaryKeyConstraint("id"),
        # AC-01, D-TD-2: единственная защита от явной вставки неположительного id.
        CheckConstraint("id > 0", name="id_positive"),
        # AC-01: публичный адрес уникален (`uq_reports_uuid`).
        UniqueConstraint("uuid"),
        # AC-07 (7.4), D-19: идемпотентность жалобы на пару. СОХРАНЯЕТСЯ:
        # `NULL` в `attendance_id` не конфликтует, `message`-ветке не мешает.
        UniqueConstraint("attendance_id", "reporter_id"),
        # AC-04, D-14: составной FK. Имя задаётся явно через `op.f()` в миграции;
        # конвенция `fk` берёт только `column_0_name` и дала бы то же имя, что
        # у снимаемого одиночного FK — коллизия в `pg_constraint`.
        ForeignKeyConstraint(
            ["attendance_id", "event_card_id"],
            ["attendance.id", "attendance.card_id"],
            name="fk_reports_attendance_pair_attendance",
            ondelete="CASCADE",
        ),
        # AC-02: объект жалобы — переписка или неявка.
        CheckConstraint(_in_clause("target_type", REPORT_TARGET_TYPES), name="target_type"),
        # AC-06: состояния разбора.
        CheckConstraint(_in_clause("status", REPORT_STATUSES), name="status"),
        # AC-06: исход разбора.
        CheckConstraint(_in_clause("resolution", REPORT_RESOLUTIONS), name="resolution"),
        # AC-03, D-15: одна формула цели.
        CheckConstraint(TARGET_SCOPE_SQL, name="target_scope"),
        # AC-06: «не разобрана ⟺ решения нет».
        CheckConstraint(STATUS_RESOLUTION_CONSISTENCY_SQL, name="status_resolution_consistency"),
        # AC-06: соответствие состояния и вида решения.
        CheckConstraint(STATUS_RESOLUTION_MAPPING_SQL, name="status_resolution_mapping"),
        # AC-11 (7.4): разбор начат ⟺ метка разбора пуста.
        CheckConstraint(STATUS_RESOLVED_CONSISTENCY_SQL, name="status_resolved_consistency"),
        # AC-11 (7.4): разбор не раньше жалобы.
        CheckConstraint(RESOLVED_AFTER_REPORTED_SQL, name="resolved_after_reported"),
        # AC-08, D-19: идемпотентность жалобы на диалог. Частичный: у `no_show`
        # `chat_id IS NULL`, и такой строке уникальность не нужна.
        Index(
            "uq_reports_chat_id_reporter_id",
            "chat_id",
            "reporter_id",
            unique=True,
            postgresql_where=text("chat_id IS NOT NULL"),
        ),
        # AC-10 (7.4), NFR52: очередь модератора 8.3.
        Index("ix_reports_status_reported_at", "status", "reported_at"),
        # Стори 8.3 (AC-01, AC-04, D-31): очередь читает ОТКРЫТЫЕ жалобы по
        # возрасту. Существующий `(status, reported_at)` покрывает фильтр, но
        # не второй ключ сортировки: при tie-группе планировщик добавит
        # `Sort`. Частичный — закрытые жалобы в очередь не попадают (D-31),
        # поэтому в индексе им места нет.
        Index(
            "ix_reports_open_reported_at_uuid",
            "reported_at",
            "uuid",
            postgresql_where=text("status = 'open'"),
        ),
        # Стори 8.5 (AC-12, D-64, P-13): `reports.against` карточки модератора —
        # `WHERE accused_id = :id ORDER BY reported_at DESC`. Имя и состав
        # ДОЛЖНЫ совпадать с `0028` дословно: модель и миграция — две копии
        # одной схемы, и расхождение ловит только `alembic check` на живой БД.
        Index("ix_reports_accused_id_reported_at", "accused_id", text("reported_at DESC")),
        # Стори 8.5 (AC-12, D-64): `reports.filed` — `WHERE reporter_id = :id`.
        # Существующий `UNIQUE(attendance_id, reporter_id)` ведёт с
        # `attendance_id`, а у `message`-жалоб он `NULL`, поэтому для поиска
        # по репортёру не годится.
        Index("ix_reports_reporter_id_reported_at", "reporter_id", text("reported_at DESC")),
    )


__all__ = [
    "REPORT_RESOLUTIONS",
    "REPORT_STATUSES",
    "REPORT_TARGET_TYPES",
    "Report",
]
