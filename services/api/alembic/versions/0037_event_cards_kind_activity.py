"""add event_cards.kind and event_cards.activity (story 12.4)

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-29

Стори 12.4 (AC-1…AC-8; FR117, FR118, ADR-20).

**Почему `0037`, а не `0036`.** Эпик (`epic-12.md:81`) назначает этой стори
`0036`, исходя из порядка «12.1, потом 12.2, потом 12.4». Замер `alembic
heads` 2026-10-29: голова — `0036`, её заняла стори 12.2
(`0036_profiles_birth_date.py`). Свободны `0037`–`0038`.
`down_revision = "0036"`. Отклонение от эпика зафиксировано замером (P-28).
Это третий случай подряд: 12.1 → `0035`, 12.2 → `0036`, 12.4 → `0037`.

**Почему `other` есть в CHECK активности.** AC-2 эпика требует бэкфилл
существующих строк значением `company`/`other`, а AC-1 перечисляет белый
список FR118 (`coffee/walk/bar/cinema/boardgames/sport`), где `other` нет.
Миграция в такой формулировке физически невыполнима: `UPDATE ... activity =
'other'` нарушил бы CHECK.

Решение (вариант C, обоснование — в стори, §«Решение по `other`»):
`other` остаётся **легаси-значением в БД**, но недоступен на входе.
Границу держат валидаторы схем (`ACTIVITY_INPUT_VALUES`), а не ограничение
БД. Бэкфилл в конкретную активность отклонён: он объявил бы «кофе»
у карточек, где активности не было (тот же класс, что отклонённый
бэкфилл `consent_version='1.0'` в 11.4). `activity = NULL` отклонён:
ломает `NOT NULL` AC-1 и FR118, а фильтр ленты терял бы строки.

**Почему `server_default` затем снимается.** AC-2 прямо требует, чтобы
в схеме не осталось «магического» значения: новая карточка обязана
получить тип и активность от автора, а не унаследовать `company`/`other`.
`server_default` нужен ровно на время `ADD COLUMN` на непустой таблице
(иначе `NOT NULL` не применить), после чего `DROP DEFAULT` возвращает
колонке честную обязательность.

**Порядок шагов обязателен:** `ADD COLUMN` (с `server_default`) → `CHECK` →
`DROP DEFAULT`. Ранний `DROP DEFAULT` дал бы `NOT NULL violation` на
существующих строках, поздний `CHECK` — `check constraint is violated`
на бэкфилле, если бы значение дефолта не входило в список.

**Порядок в `downgrade` тоже обязателен: сначала `drop_constraint`,
потом `drop_column`.** Обратный порядок даёт «cannot drop column ... other
objects depend on it» — тот же класс дефекта, что CR 12.2 H-1, где
продукт оказался неоткатываемым.

**Транзакционность.** `ADD COLUMN` с константным дефолтом в PostgreSQL 11+
меняет только каталог (таблица не переписывается), `DROP DEFAULT` и
переопределение CHECK — быстрые операции каталога. PostgreSQL исполняет
DDL транзакционно, поэтому `upgrade()` целиком атомарен: сбой на любом
шаге оставляет схему на `0036`.
"""

from __future__ import annotations

import logging
import os

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

#: Имена ограничений **без префикса**: naming convention Alembic строит полное
#: имя `ck_event_cards_<name>` из `name="..."` в модели. Если передать полное
#: имя, convention добавит префикс второй раз (`ck_event_cards_ck_event_cards_kind`)
#: и `upgrade` упадёт на несуществующем ограничении — проверено прогоном
#: в CR 12.2 (`COMPLETENESS_CONSTRAINT`).
KIND_CONSTRAINT = "kind"
ACTIVITY_CONSTRAINT = "activity"

#: AC-1 (FR117): тип встречи — ровно два значения.
_KIND_CHECK = "kind IN ('date', 'company')"

#: AC-1 (FR118 + легаси `other`): белый список активности.
#: Список продублирован строкой осознанно: миграция обязана оставаться
#: исполнимой сама по себе (alembic зовёт её в отдельном процессе), а импорт
#: `app.models.enums` связал бы ревизию с текущим кодом приложения — при
#: следующем рефакторинге откат старой ревизии сломался бы.
_ACTIVITY_CHECK = (
    "activity IN ('coffee', 'walk', 'bar', 'cinema', 'boardgames', 'sport', 'other')"
)

#: CR 12.4 H-1 (долг `W-12.4-h`): ценности, которые `upgrade()` восстанавливает
#: бэкфиллом. Откат сохраняет смысл только для строк, уже равных этой паре:
#: для всех остальных `downgrade` + `upgrade` молча превратили бы «свидание,
#: кино» в «компания, активность неизвестна».
BACKFILL_KIND = "company"
BACKFILL_ACTIVITY = "other"

#: Переменная осознанного выбора: оператор знает, что смысл будет потерян,
#: и подтверждает это явно. Без неё откат останавливается ДО операции.
ALLOW_MEANING_LOSS_ENV = "ALEMBIC_ALLOW_MEANING_LOSS"

#: Класс CR 12.2 H-1 / гарда `0024`: в offline-режиме (`--sql`) соединения
#: нет, посчитать строки невозможно. Генерировать разрушительный `DROP COLUMN`
#: без проверки значило бы отдать оператору скрипт без защиты ровно там, где
#: её не видно.
_OFFLINE_REFUSAL = (
    "Откат 0037 в offline-режиме (`--sql`) запрещён: посчитать карточки, у которых "
    "откат сотрёт тип встречи и активность, невозможно без соединения. "
    "Выполните откат онлайн."
)


def upgrade() -> None:
    # AC-1: колонки обязательные; дефолт нужен только чтобы применить NOT NULL
    # к уже существующим строкам (AC-2).
    op.add_column(
        "event_cards",
        sa.Column("kind", sa.String(16), nullable=False, server_default="company"),
    )
    op.add_column(
        "event_cards",
        sa.Column("activity", sa.String(32), nullable=False, server_default="other"),
    )

    # AC-1: белые списки. Имена короткие — префикс добавит convention.
    op.create_check_constraint(KIND_CONSTRAINT, "event_cards", _KIND_CHECK)
    op.create_check_constraint(ACTIVITY_CONSTRAINT, "event_cards", _ACTIVITY_CHECK)

    # AC-2: дефолт снимается — в схеме нет магического значения.
    op.alter_column("event_cards", "kind", server_default=None)
    op.alter_column("event_cards", "activity", server_default=None)


def downgrade() -> None:
    """Откат с гардом против МОЛЧАЛИВОЙ потери смысла (AC-8; CR 12.4 H-1).

    **Что было не так.** `downgrade` удалял колонки, `upgrade` создавал их
    заново с бэкфиллом `company`/`other`. Цикл `0037 → 0036 → 0037` проходил,
    строки не терялись (AC-8 формально выполнялся), но карточка «свидание,
    кино» становилась «компания, активность неизвестна» — без единого
    сообщения. Падение видно, затирание — нет (класс CR 12.2 H-1, но хуже).

    **Решение.** Гард считает строки, чей смысл бэкфилл НЕ восстановит:
    `kind <> 'company' OR activity <> 'other'`. Если такие есть — откат
    останавливается ДО изменения схемы, с числом строк в сообщении. Оператор
    узнаёт о потере до операции, а не после повторного наката.

    **Почему не отказ по любым строкам.** Замер: три чужих теста
    (`test_attendance_history_index`, `test_attendance_migration_cycle`,
    `test_reports_accused_id_migration`) откатываются ниже `0037` при непустых
    `event_cards`. Отказ по всем строкам сделал бы их неработоспособными,
    хотя смысла в тех карточках откат не несёт: они засеяны парой бэкфилла.
    Гард различает «строки есть» и «строки со смыслом есть».

    **Обход.** `ALEMBIC_ALLOW_MEANING_LOSS=1` — осознанный выбор оператора
    (например, откат пилота, где тип и активность не нужны). Обход печатает
    предупреждение: молчаливого стирания не остаётся ни в одном пути.
    """
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    rows_at_risk = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM event_cards "
            "WHERE kind IS DISTINCT FROM :kind "
            "   OR activity IS DISTINCT FROM :activity"
        ),
        {"kind": BACKFILL_KIND, "activity": BACKFILL_ACTIVITY},
    ).scalar_one()

    if rows_at_risk:
        if os.environ.get(ALLOW_MEANING_LOSS_ENV) != "1":
            raise RuntimeError(
                f"Откат 0037 остановлен: у {rows_at_risk} карточек тип встречи или "
                "активность не совпадают с тем, что вернёт повторный накат "
                f"(`{BACKFILL_KIND}`/`{BACKFILL_ACTIVITY}`). После `downgrade 0036` "
                "и `upgrade 0037` эти карточки станут `компания`/`активность "
                "неизвестна` — смысл встречи будет стёрт без возможности "
                "восстановления. Если потеря смысла допустима, повторите откат с "
                f"`{ALLOW_MEANING_LOSS_ENV}=1`."
            )
        # Прецедент `0021`: stdlib-logger, а не `structlog` — миграция не должна
        # зависеть от конфигурации логирования приложения.
        logging.getLogger("alembic.runtime.migration").warning(
            "осознанный откат 0037: у %s карточек тип/активность будут стёрты "
            "повторным накатом (%s=1)",
            rows_at_risk,
            ALLOW_MEANING_LOSS_ENV,
        )

    # AC-8: порядок важен — CHECK ссылаются на колонки, поэтому снимаются первыми.
    op.drop_constraint(op.f(f"ck_event_cards_{KIND_CONSTRAINT}"), "event_cards", type_="check")
    op.drop_constraint(
        op.f(f"ck_event_cards_{ACTIVITY_CONSTRAINT}"), "event_cards", type_="check"
    )
    op.drop_column("event_cards", "activity")
    op.drop_column("event_cards", "kind")
