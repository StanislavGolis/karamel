"""extend reports for message complaints and create sanctions (story 8.1)

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-25

**Почему `0025`, а не `0012` из шарда.** Шард эпика 8 ссылается на
`0012 extend_reports_for_no_show.py` — наследие v1-нумерации. В v2 под этим
номером живёт `0012_create_join_requests.py`, а `reports` создана в `0023`
(стори 7.4). `alembic heads` на 2026-10-25 = `0024` (D-12).

**Что расширяется.** `reports` получает второй объект жалобы — переписку
(`message`, FR36/FR37). Домен `target_type` расширяется до
`('message','no_show')`, `attendance_id` становится nullable (у жалобы на
диалог пары нет), добавляются `chat_id`, `event_card_id`, `resolution`.

**Почему `message_id` НЕТ** (D-13). Жалоба подаётся на человека, а не на
реплику; фрагмент 8.3 берёт 10 последних сообщений диалога. Ссылка на
сообщение дала бы вторую ось валидации и ложные `404` при удалении реплики.

**Почему `event_card_id` через составной FK** (D-14). Колонка нужна AC-02
шарда, но без ограничения она разошлась бы с `attendance.card_id` при
переносе пары, и очередь 8.3 показала бы жалобу на несуществующую встречу.
Составной FK `(attendance_id, event_card_id) → attendance (id, card_id)`
требует `UNIQUE (id, card_id)` на `attendance` — он создаётся здесь же.

**Почему `resolution` — отдельная колонка при сохранённом `status`** (D-16).
Закрытая 7.4 использует четыре значения `status` и 2299 тестов; переименование
сломало бы её без пользы. Пара ограничений держит их согласованными, поэтому
«разобрана без решения» невыразимо.

**Порядок `upgrade` не переставлять.** `CREATE INDEX CONCURRENTLY` требует
autocommit, и `SET LOCAL lock_timeout` внутри такого блока не живёт — поэтому
настройка идёт ПОСЛЕ блока, а guard `indisvalid` проверяет, что индекс реально
построен: на невалидном индексе составной FK упал бы посреди окна.
Backfill — до `CHECK`: `CHECK` валидирует существующие строки при создании.

**Что НЕ создаётся.** Нет `revoked_at` (в эпике 8 нет стори о снятии; D-17),
нет индекса по `target_type` (два значения на всю таблицу), нет
`moderator_id` в `reports` (модератор фиксируется на санкции).

**Откат отличается от `0024`.** `0024` удаляет один индекс; здесь снимается
целая таблица `sanctions` и пять ограничений — и непустая `sanctions`
останавливает откат: санкции это данные (§12.3). В offline-режиме откат
падает явно: проверить наличие строк невозможно (урок CR 7.3).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
_down_revision = "0024"
down_revision: str | Sequence[str] | None = _down_revision
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: AC-08: индекс под составной FK. Имя литералом: `op.f()` дал бы префикс
#: конвенции модели, а индекс создаётся сырым SQL в autocommit-блоке.
ATTENDANCE_ID_CARD_INDEX = "uq_attendance_id_card_id"

#: AC-08, D-19: частичный `UNIQUE` жалобы на диалог.
REPORTS_CHAT_INDEX = "uq_reports_chat_id_reporter_id"

#: AC-08: история санкций пользователя для карточки 8.5.
SANCTIONS_HISTORY_INDEX = "ix_sanctions_user_id_created_at"

#: AC-06: домен исхода разбора. Дублируется из `app/models/report.py`
#: осознанно: миграция не импортирует приложение, расхождение ловит тест 4.3.
_RESOLUTIONS = ("warning", "ban", "dismissed")

#: AC-07: домен вида санкции. `dismissed` здесь нет — это не санкция.
_SANCTION_TYPES = ("warning", "ban")

#: AC-03, D-15: ОДНА формула цели. Копия `TARGET_SCOPE_SQL` модели.
_TARGET_SCOPE_SQL = (
    "(target_type = 'no_show' AND attendance_id IS NOT NULL "
    "AND event_card_id IS NOT NULL AND chat_id IS NULL) "
    "OR (target_type = 'message' AND chat_id IS NOT NULL "
    "AND attendance_id IS NULL AND event_card_id IS NULL)"
)

#: AC-06: «не разобрана ⟺ решения нет».
_STATUS_RESOLUTION_CONSISTENCY_SQL = "(status = 'open') = (resolution IS NULL)"

#: AC-06: соответствие состояния и вида решения.
_STATUS_RESOLUTION_MAPPING_SQL = (
    "status = 'open' "
    "OR (status = 'resolved_warning' AND resolution = 'warning') "
    "OR (status = 'resolved_ban' AND resolution = 'ban') "
    "OR (status = 'dismissed' AND resolution = 'dismissed')"
)


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """`col IN ('a','b')` из кортежа значений."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _create_index_concurrently(
    name: str, table: str, columns: str, *, unique: bool = False, where: str = ""
) -> None:
    """`CREATE [UNIQUE] INDEX CONCURRENTLY` в autocommit-блоке.

    Отдельная функция, а не `op.create_index`: `CONCURRENTLY` не работает
    внутри транзакции, и Alembic для него требует `autocommit_block`.
    """
    unique_sql = "UNIQUE " if unique else ""
    where_sql = f" WHERE {where}" if where else ""
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE {unique_sql}INDEX CONCURRENTLY {name} ON {table} ({columns}){where_sql}"
        )


def upgrade() -> None:
    """Расширение `reports` до двух объектов + `sanctions` (AC-01…AC-08)."""
    # AC-09: в offline-режиме (`--sql`) guard-и проверить невозможно —
    # нет соединения, а `CREATE INDEX CONCURRENTLY` вообще не выражается
    # в транзакционном скрипте. Падаем явно, а не генерируем скрипт без
    # единой проверки: защита отсутствовала бы ровно там, где его отдают
    # оператору (урок CR 7.3, D-7).
    if op.get_context().as_sql:
        raise RuntimeError(
            "`upgrade --sql` для 0025 запрещён: guard по `indisvalid` и guard "
            "backfill требуют соединения с БД, а `CREATE INDEX CONCURRENTLY` "
            "не выражается в offline-скрипте. Выполните upgrade онлайн."
        )

    # --- 1. Индекс под составной FK (AC-04, AC-08) -------------------------
    # `attendance` может быть большой (стенд: 40k). Обычный `CREATE INDEX`
    # взял бы `ACCESS EXCLUSIVE` и заблокировал запись явки на всё время
    # построения — `0024` уже показал цену этого (D-4).
    # `IF NOT EXISTS` через предварительный DROP: `CONCURRENTLY` вне транзакции,
    # и прерванный прогон оставляет индекс в базе.
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {ATTENDANCE_ID_CARD_INDEX}")
    _create_index_concurrently(ATTENDANCE_ID_CARD_INDEX, "attendance", "id, card_id", unique=True)
    # Guard: `CONCURRENTLY` может оставить НЕВАЛИДНЫЙ индекс (например, при
    # прерванном построении). FK на такой индекс упал бы на шаге 3 посреди
    # окна, поэтому проверка обязательна.
    valid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = 'uq_attendance_id_card_id'::regclass"
            )
        )
        .scalar_one_or_none()
    )
    if valid is not True:
        raise RuntimeError(
            "Индекс uq_attendance_id_card_id не построен или невалиден "
            f"(indisvalid={valid!r}). Составной FK на него невозможен. "
            "Пересоздайте индекс вручную и повторите `upgrade`."
        )

    # --- 2. Таймаут блокировок (AC-09) ------------------------------------
    # `SET LOCAL` действует только внутри транзакции и НЕ переживает
    # autocommit-блок выше — поэтому выставляется здесь, а не «первым».
    # Построенный индекс преобразуется в ОГРАНИЧЕНИЕ: FK может ссылаться
    # только на unique constraint / PK, но не на «просто» уникальный индекс.
    # `USING INDEX` принимает готовый индекс за O(1): `ADD CONSTRAINT ... UNIQUE
    # (id, card_id)` построил бы его заново под `ACCESS EXCLUSIVE`. Имя
    # сохраняется, `alembic check` видит ограничение и не считает индекс лишним.
    op.execute(
        "ALTER TABLE attendance ADD CONSTRAINT "
        f"{ATTENDANCE_ID_CARD_INDEX} UNIQUE USING INDEX {ATTENDANCE_ID_CARD_INDEX}"
    )

    op.execute("SET LOCAL lock_timeout = '3s'")

    # --- 3. `reports`: nullable + снятие старого FK ------------------------
    # Одиночный FK `attendance_id → attendance.id` заменяется составным:
    # составной подразумевает тот же столбец и добавляет `card_id`.
    # `op.f()`: имя уже полное, конвенция не должна применять префикс второй раз
    # (иначе Alembic ищет `ck_reports_ck_reports_target_type` — дефект 3.1/4.1/6.7-D3).
    op.drop_constraint(op.f("fk_reports_attendance_id_attendance"), "reports", type_="foreignkey")
    op.alter_column("reports", "attendance_id", existing_type=sa.BigInteger(), nullable=True)

    # --- 4. Новые колонки (AC-01) -----------------------------------------
    op.add_column("reports", sa.Column("chat_id", sa.BigInteger(), nullable=True))
    op.add_column("reports", sa.Column("event_card_id", sa.BigInteger(), nullable=True))
    op.add_column("reports", sa.Column("resolution", sa.String(length=16), nullable=True))

    # --- 5. Внешние ключи (AC-01, AC-04) ----------------------------------
    op.create_foreign_key(
        "fk_reports_chat_id_chats",
        "reports",
        "chats",
        ["chat_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # `MATCH SIMPLE` (по умолчанию): при `message` оба столбца NULL и FK
    # не проверяется — это и нужно.
    op.create_foreign_key(
        "fk_reports_attendance_pair_attendance",
        "reports",
        "attendance",
        ["attendance_id", "event_card_id"],
        ["id", "card_id"],
        ondelete="CASCADE",
    )

    # --- 6. Backfill (AC-05) ----------------------------------------------
    # До `CHECK`: ограничение валидирует существующие строки при создании,
    # и на объёме стенда любая непереписанная жалоба уронила бы миграцию.
    op.execute(
        "UPDATE reports r SET event_card_id = a.card_id "
        "FROM attendance a "
        "WHERE r.attendance_id = a.id AND r.event_card_id IS NULL"
    )
    broken = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM reports "
                "WHERE target_type = 'no_show' "
                "AND (event_card_id IS NULL OR chat_id IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if broken:
        first = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT min(id) FROM reports "
                    "WHERE target_type = 'no_show' "
                    "AND (event_card_id IS NULL OR chat_id IS NOT NULL)"
                )
            )
            .scalar_one()
        )
        raise RuntimeError(
            f"Backfill 0025 неполон: {broken} жалоб(ы) на неявку без карточки "
            f"или с chat_id (первая: id={first}). Молчаливый NULL дал бы в "
            "очереди модератора 8.3 «пустую» жалобу. Разберите строки вручную "
            "и повторите `upgrade`."
        )

    # --- 7. Ограничения (AC-02, AC-03, AC-06) ----------------------------
    op.drop_constraint(op.f("ck_reports_target_type"), "reports", type_="check")
    op.create_check_constraint(
        "target_type",
        "reports",
        _in_clause("target_type", ("message", "no_show")),
    )
    op.create_check_constraint("target_scope", "reports", _TARGET_SCOPE_SQL)
    op.create_check_constraint("resolution", "reports", _in_clause("resolution", _RESOLUTIONS))
    op.create_check_constraint(
        "status_resolution_consistency", "reports", _STATUS_RESOLUTION_CONSISTENCY_SQL
    )
    op.create_check_constraint(
        "status_resolution_mapping", "reports", _STATUS_RESOLUTION_MAPPING_SQL
    )

    # --- 8. `sanctions` (AC-07) ------------------------------------------
    op.create_table(
        "sanctions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "uuid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("moderator_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # AC-07: обвиняемый удаляется каскадом — санкция без субъекта не адресуется.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sanctions_user_id_users"),
            ondelete="CASCADE",
        ),
        # AC-07: предмет решения; удаление жалобы уносит санкцию по ней.
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_sanctions_report_id_reports"),
            ondelete="CASCADE",
        ),
        # AC-07: модератор — SET NULL: решение остаётся фактом.
        sa.ForeignKeyConstraint(
            ["moderator_id"],
            ["users.id"],
            name=op.f("fk_sanctions_moderator_id_users"),
            ondelete="SET NULL",
        ),
        # AC-07, P-10: защита от явной вставки неположительного id.
        sa.CheckConstraint("id > 0", name="id_positive"),
        # AC-07: публичный адрес.
        sa.UniqueConstraint("uuid", name=op.f("uq_sanctions_uuid")),
        # AC-07: одно решение на жалобу. `ON CONFLICT (report_id)` в 8.4
        # резолвится по этому ограничению.
        sa.UniqueConstraint("report_id", name=op.f("uq_sanctions_report_id")),
        # AC-07: вид санкции. `dismissed` сюда не входит.
        sa.CheckConstraint(_in_clause("type", _SANCTION_TYPES), name="type"),
    )

    # --- 9. Индексы (AC-08) ----------------------------------------------
    # Оба — на уже существующих таблицах с данными: `CONCURRENTLY`.
    _create_index_concurrently(SANCTIONS_HISTORY_INDEX, "sanctions", "user_id, created_at DESC")
    _create_index_concurrently(
        REPORTS_CHAT_INDEX,
        "reports",
        "chat_id, reporter_id",
        unique=True,
        where="chat_id IS NOT NULL",
    )


def downgrade() -> None:
    """Откат в обратном порядке; непустая `sanctions` останавливает удаление.

    Guard НЕ обходится в offline-режиме: при `downgrade --sql` генерировался бы
    `DROP TABLE sanctions` без единой проверки — защита отсутствовала бы ровно
    там, где скрипт отдают оператору (урок CR 7.3, D-7).
    """
    if op.get_context().as_sql:
        raise RuntimeError(
            "Откат 0025 в offline-режиме (`--sql`) запрещён: проверить наличие "
            "строк в sanctions и reports невозможно, а `DROP TABLE` уничтожил бы "
            "санкции молча. Выполните откат онлайн (без `--sql`)."
        )

    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM sanctions")).scalar_one()
    if rows:
        raise RuntimeError(
            f"Откат 0025 остановлен: в sanctions {rows} строк(и). Санкции — это "
            "данные, и `DROP TABLE` уничтожил бы их молча. Перенесите строки "
            "или удалите их осознанно, затем повторите откат (§12.3)."
        )

    # Обратный порядок: сначала то, что создано последним.
    #
    # Без `CONCURRENTLY` — и это НЕ упущение. `autocommit_block` коммитит
    # оператор вне транзакции ревизии: когда откат ниже `0025` останавливает
    # guard `0024`/`0023`, транзакция откатывается к `0026`, а индексы 8.1
    # остаются снесёнными навсегда (head `0026` при отсутствующих индексах).
    # `alembic check` ловит это как «New upgrade operations detected».
    # Поймано на P-14 прогоне 8.2; откат обязан быть атомарным.
    for name in (REPORTS_CHAT_INDEX, SANCTIONS_HISTORY_INDEX):
        op.execute(f"DROP INDEX IF EXISTS {name}")

    op.drop_table("sanctions")

    op.drop_constraint(op.f("ck_reports_status_resolution_mapping"), "reports", type_="check")
    op.drop_constraint(op.f("ck_reports_status_resolution_consistency"), "reports", type_="check")
    op.drop_constraint(op.f("ck_reports_resolution"), "reports", type_="check")
    op.drop_constraint(op.f("ck_reports_target_scope"), "reports", type_="check")

    # Домен `target_type` возвращается к единственному значению 7.4. Если в
    # таблице уже есть `message`-жалобы, ограничение не создастся — и это
    # правильно: откат не должен молча терять данные нового домена.
    op.drop_constraint(op.f("ck_reports_target_type"), "reports", type_="check")
    op.create_check_constraint("target_type", "reports", "target_type IN ('no_show')")

    op.drop_constraint(op.f("fk_reports_attendance_pair_attendance"), "reports", type_="foreignkey")
    op.drop_constraint(op.f("fk_reports_chat_id_chats"), "reports", type_="foreignkey")
    op.drop_column("reports", "resolution")
    op.drop_column("reports", "event_card_id")
    op.drop_column("reports", "chat_id")

    op.alter_column("reports", "attendance_id", existing_type=sa.BigInteger(), nullable=False)
    op.create_foreign_key(
        "fk_reports_attendance_id_attendance",
        "reports",
        "attendance",
        ["attendance_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # ПОСЛЕДНИМ: `uq_attendance_id_card_id` — ограничение, на которое
    # ссылается составной FK `reports`. Пока FK жив, PostgreSQL не даст
    # снять ограничение (`cannot drop constraint ... because other objects
    # depend on it`), поэтому шаг идёт после `DROP CONSTRAINT fk_...`.
    op.drop_constraint(op.f(ATTENDANCE_ID_CARD_INDEX), "attendance", type_="unique")
