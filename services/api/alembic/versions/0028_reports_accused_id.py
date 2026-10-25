"""reports.accused_id for the moderator user card (story 8.5, AC-12, AC-13)

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-25

**Зачем колонка.** FR54 требует в карточке модератора «жалобы, поданные И на
него». До сих пор `reports` хранила только `reporter_id`: обвиняемый
вычислялся на лету (`_ACCUSED_SQL` в 8.4, четырёхветвевой `UNION`). Для
решения по ОДНОЙ жалобе это было приемлемо — один вызов на действие
модератора. Для карточки это неверно: запрос идёт на КАЖДОЕ открытие, а
`UNION` по `attendance`/`event_cards`/`join_requests` на объёме стенда
(40k жалоб) — три seq scan при ручном действии.

**D-57 предсказал это дословно.** В DS 8.4 записано: «`reports` не хранит
`accused_id` … материализация потребовала бы миграции с backfill по всем
прошлым жалобам, а потребителя не было». Потребитель появился — колонка
вводится здесь.

**Backfill повторяет правило D-57 буквально**, иначе `reports.against`
разойдётся с тем, что 8.4 считает обвиняемым при выдаче санкции:

- `no_show` — вторая сторона пары: участник `attendance.participant_id`,
  если жалобщик — автор карточки; и `event_cards.author_id`, если жалобщик —
  участник;
- `message` — вторая сторона диалога: `join_requests.applicant_id`
  (`status='selected'`), если жалобщик — автор; и `event_cards.author_id`,
  если жалобщик — участник.

Форма `CASE WHEN автор = reporter THEN вторая_сторона ELSE автор END`
эквивалентна четырём ветвям `UNION` из `_ACCUSED_SQL`, но выполняется одним
проходом и не требует `DISTINCT`: на `no_show` ровно один участник пары, на
`message` ровно один `selected` (ADR-04).

**Почему `ON DELETE SET NULL`, а не `CASCADE`.** Жалоба — факт разбора, она
переживает удаление обвиняемого; `CASCADE` снёс бы жалобу из истории
жалобщика вместе с чужим аккаунтом. `SET NULL` сохраняет строку, и карточка
покажет её как «на удалённого пользователя» (поле останется `NULL`).

**Почему индексы `CONCURRENTLY`** (D-4). `reports` на стенде непуста;
обычный `CREATE INDEX` взял бы `ACCESS EXCLUSIVE` и заблокировал подачу
жалоб на всё время построения. Внутри транзакции `CONCURRENTLY` запрещён —
отсюда `autocommit_block`.

**Почему `downgrade` БЕЗ `CONCURRENTLY`** (CR 8.2, H-1). `autocommit_block`
коммитит оператор вне транзакции ревизии: когда откат ниже `0028`
останавливает guard `0027`/`0026`, транзакция возвращается к `0028`, а
индекс остаётся снесённым навсегда — схема дрейфует при формально верном
`alembic_version`.

**Offline-safe** (D-7). В `--sql` режиме `indisvalid` не проверить, а
backfill без guard'а мог бы молча оставить жалобы без обвиняемого. Поэтому
обе функции падают явно, а не генерируют скрипт.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028"

down_revision: str | None = "0027"

branch_labels: str | Sequence[str] | None = None

depends_on: str | Sequence[str] | None = None

#: AC-12: выборка `reports.against` карточки — `WHERE accused_id = :id ORDER BY
#: reported_at DESC`. Составной, с `DESC`: без второго столбца планировщик
#: добавил бы `Sort` на каждом открытии карточки.
ACCUSED_INDEX = "ix_reports_accused_id_reported_at"

#: AC-12: выборка `reports.filed` — `WHERE reporter_id = :id`.
#: Существующий `UNIQUE(attendance_id, reporter_id)` ведёт с `attendance_id`,
#: а у `message`-жалоб он `NULL` — для поиска по репортёру не годится.
REPORTER_INDEX = "ix_reports_reporter_id_reported_at"


#: AC-12: backfill жалоб на неявку. Обвиняемый — вторая сторона пары.
#: `attendance.participant_id` — участник; `event_cards.author_id` — автор.
#: `CASE` выбирает ту, которая НЕ является жалобщиком.
_BACKFILL_NO_SHOW_SQL = """
UPDATE reports AS r
   SET accused_id = CASE
                      WHEN ec.author_id = r.reporter_id THEN a.participant_id
                      ELSE ec.author_id
                    END
  FROM attendance AS a
  JOIN event_cards AS ec ON ec.id = a.card_id
 WHERE r.attendance_id = a.id
   AND r.target_type = 'no_show'
   AND r.accused_id IS NULL
"""

#: AC-12: backfill жалоб на переписку. Обвиняемый — вторая сторона диалога.
#: `join_requests.applicant_id` со `status='selected'` — участник, выбранный
#: автором (`UNIQUE(event_card_id)` на `chats` гарантирует один чат на
#: карточку, ADR-04).
_BACKFILL_MESSAGE_SQL = """
UPDATE reports AS r
   SET accused_id = CASE
                      WHEN ec.author_id = r.reporter_id THEN jr.applicant_id
                      ELSE ec.author_id
                    END
  FROM chats AS c
  JOIN event_cards AS ec ON ec.id = c.event_card_id
  JOIN join_requests AS jr
    ON jr.card_id = c.event_card_id AND jr.status = 'selected'
 WHERE r.chat_id = c.id
   AND r.target_type = 'message'
   AND r.accused_id IS NULL
"""


def _assert_offline_safe(action: str) -> None:
    """D-7: в `--sql`-режиме guard непроверяем — падаем явно.

    Без этого `alembic upgrade --sql` выдал бы скрипт без `indisvalid`-проверки
    и без guard'а backfill, то есть применил бы миграцию, которая на живой БД
    могла молча оставить жалобы без обвиняемого.
    """
    if op.get_context().as_sql:
        raise RuntimeError(
            f"`{action} --sql` для 0028 запрещён: `CREATE INDEX CONCURRENTLY` "
            "не выражается в offline-скрипте, а guard по `indisvalid` и guard "
            "backfill требуют соединения с БД. Выполните онлайн."
        )


def _assert_index_valid(name: str) -> None:
    """D-18: прерванное `CONCURRENTLY`-построение оставляет невалидный индекс.

    Невалидный индекс не используется планировщиком, но и не удаляется сам:
    миграция считалась бы применённой без работающего индекса, и карточка
    модератора деградировала бы до seq scan молча.

    CR 8.3, H-3: `to_regclass`, а НЕ `'name'::regclass` — приведение на
    несуществующем имени БРОСАЕТ `UndefinedTableError`, и ветка «индекс не
    создан» ушла бы мимо guard'а сырой ошибкой драйвера.
    """
    valid = (
        op.get_bind()
        .execute(
            sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": name},
        )
        .scalar_one_or_none()
    )
    if valid is not True:
        raise RuntimeError(
            f"Индекс {name} не построен или невалиден (indisvalid={valid!r}). "
            "Пересоздайте индекс вручную и повторите `upgrade`."
        )


def upgrade() -> None:
    """AC-12: колонка → backfill → guard → два индекса (AC-12)."""
    _assert_offline_safe("upgrade")

    # (1) Колонка nullable: `NOT NULL` отверг бы все существующие строки до
    # backfill (урок D-18 в 8.1 — там `CHECK` ставился ПОСЛЕ заполнения).
    op.add_column("reports", sa.Column("accused_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_reports_accused_id_users",
        "reports",
        "users",
        ["accused_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # (2) Backfill: две ветви по типу жалобы. Порядок безразличен (предикаты
    # взаимоисключающие — `target_scope` из 8.1), но разделение на два
    # оператора делает ошибку в одной ветви видимой, а не размытой общим CASE.
    op.execute(_BACKFILL_NO_SHOW_SQL)
    op.execute(_BACKFILL_MESSAGE_SQL)

    # (3) Guard: жалоба без обвиняемого не должна тихо выпасть из карточки.
    # `RuntimeError`, а не `print`: молчаливый пропуск оставил бы дыру в FR54.
    missing = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM reports WHERE accused_id IS NULL"))
        .scalar_one()
    )
    if missing:
        worst = (
            op.get_bind()
            .execute(sa.text("SELECT min(id) FROM reports WHERE accused_id IS NULL"))
            .scalar_one()
        )
        raise RuntimeError(
            f"Backfill 0028 не заполнил accused_id у {missing} жалоб (min(id)={worst}). "
            "Проверьте состав чата/пары: жалоба без обвиняемого не должна попасть в карточку."
        )

    # (4) Индексы — каждый отдельным `autocommit_block`: `CONCURRENTLY` не
    # живёт внутри транзакции ревизии.
    for name, columns in (
        (ACCUSED_INDEX, "(accused_id, reported_at DESC)"),
        (REPORTER_INDEX, "(reporter_id, reported_at DESC)"),
    ):
        with op.get_context().autocommit_block():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON reports {columns}")
        _assert_index_valid(name)


def downgrade() -> None:
    """Снимает индексы АТОМАРНО, затем колонку (CR 8.2, H-1)."""
    _assert_offline_safe("downgrade")

    # Без `CONCURRENTLY` и без `autocommit_block`: откат обязан быть атомарным,
    # иначе остановка guard'ом ниже оставит индексы снесёнными навсегда.
    op.execute(f"DROP INDEX IF EXISTS {ACCUSED_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {REPORTER_INDEX}")

    op.drop_constraint("fk_reports_accused_id_users", "reports", type_="foreignkey")
    op.drop_column("reports", "accused_id")
