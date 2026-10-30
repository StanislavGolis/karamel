"""add event_cards audience bounds (story 12.6)

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-29

Стори 12.6 (AC-1, AC-1a, AC-1b; FR119, ADR-19).

**Почему `0038`, а не `0037`.** Эпик (`epic-12.md:122`) и
`sprint-status.yaml:181` назначают этой стори `0037`. Замер `alembic heads`
2026-10-29 (перед созданием файла): голова — `0037`, её заняла стори 12.4
(`0037_event_cards_kind_activity.py`). Свободна `0038`.
`down_revision = "0037"`. Отклонение от эпика зафиксировано замером (P-28).
Это четвёртый случай подряд: 12.1 → `0035`, 12.2 → `0036`, 12.4 → `0037`,
12.6 → `0038`. Причина одна: номера в эпике зашиты до выполнения волн.

**Почему все три колонки NULL-able и без `server_default`.** Рамки аудитории
необязательны: карточка без них — легальная карточка «ищу любого» (AC-2
эпика: мягкий фильтр, не требование). `server_default` здесь не нужен даже
на время `ADD COLUMN`: NULL — законное значение для существующих строк
(в отличие от `0037`, где `NOT NULL` требовал заполнить 40 019 карточек).

**Почему CHECK `age_min <= age_max` не запрещает полуоткрытые пары.**
`NULL <= 5` в SQL даёт `NULL`, а CHECK считает `NULL` пройденной проверкой
(«не FALSE»). Значит `age_min = 30, age_max = NULL` вставляется — так и
требуется: «ищу от 30, без верхней границы» — легальный запрос (AC-1a).
Ужесточение до «обе границы или ни одной» было бы новым требованием и
отклонило бы этот случай. Замок — пара тестов (P-21): `40/30` падает,
`30/NULL` проходит. Один тест не отличит настоящий CHECK от `TRUE`.

**Почему `gender_filter` проверяется через `IN`, а не через тип ENUM.**
Прецедент `kind` (`0037`): строковая колонка + CHECK — то же поведение,
что у ENUM, но без `ALTER TYPE` (который в PostgreSQL не откатывается
транзакционно и требует отдельной миграции при добавлении значения).
Список продублирован строкой осознанно: миграция обязана оставаться
исполнимой сама по себе, а импорт `app.models.enums` связал бы ревизию
с текущим кодом приложения — при следующем рефакторинге откат старой
ревизии сломался бы (тот же довод, что в `0037`).

**Порядок в `downgrade` обязателен: сначала `drop_constraint`, потом
`drop_column`.** Обратный порядок даёт «cannot drop column ... other objects
depend on it» — класс дефекта CR 12.2 H-1, где продукт оказался
неоткатываемым. Гарда на потерю данных нет и не нужно: `downgrade` удаляет
только сами рамки, карточки и заявки остаются нетронутыми. `upgrade`
после отката вернёт NULL-колонки — рамки будут потеряны, и это осознанно:
альтернатива (отказ от отката при заданных рамках) сделала бы миграцию
необратимой без причины. Данные карточки (название, время, место, тип,
активность) не затрагиваются ни в одну сторону.

**Транзакционность.** `ADD COLUMN` без `server_default` в PostgreSQL 11+
меняет только каталог (таблица не переписывается). Создание CHECK по
NULL-able колонкам с пустым множеством невалидных строк — быстрая операция
каталога. PostgreSQL исполняет DDL транзакционно, поэтому `upgrade()`
целиком атомарен: сбой на любом шаге оставляет схему на `0037`.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

#: Имена ограничений **без префикса**: naming convention Alembic строит полное
#: имя `ck_event_cards_<name>` из `name="..."` в модели. Если передать полное
#: имя, convention добавит префикс второй раз и `upgrade` упадёт на
#: несуществующем ограничении (проверено прогоном в CR 12.2).
AGE_RANGE_CONSTRAINT = "age_range"
GENDER_FILTER_CONSTRAINT = "gender_filter"

#: AC-1a: CHECK действует только когда ОБЕ границы заданы. Полуоткрытые пары
#: (`30/NULL`, `NULL/35`) легальны — `NULL` в сравнении даёт `NULL`, а CHECK
#: пропускает `NULL` как «не FALSE». Это поведение требуется, а не терпится.
_AGE_RANGE_CHECK = "age_min <= age_max"

#: AC-1b: пол — `female`/`male` или NULL (рамка не задана).
#: Список строкой: миграция не должна зависеть от `app.models.enums`.
_GENDER_FILTER_CHECK = "gender_filter IS NULL OR gender_filter IN ('female', 'male')"


def upgrade() -> None:
    # AC-1: три необязательные колонки. NULL-able ⇒ `server_default` не нужен,
    # существующие карточки остаются «ищу любого» (NULL = рамка не задана).
    op.add_column("event_cards", sa.Column("gender_filter", sa.String(16), nullable=True))
    op.add_column("event_cards", sa.Column("age_min", sa.SmallInteger(), nullable=True))
    op.add_column("event_cards", sa.Column("age_max", sa.SmallInteger(), nullable=True))

    # AC-1a/AC-1b: белые списки и порядок границ. Имена короткие — префикс
    # добавит convention.
    op.create_check_constraint(AGE_RANGE_CONSTRAINT, "event_cards", _AGE_RANGE_CHECK)
    op.create_check_constraint(GENDER_FILTER_CONSTRAINT, "event_cards", _GENDER_FILTER_CHECK)


def downgrade() -> None:
    """Откат ничего не теряет, кроме самих рамок.

    **Порядок важен:** CHECK ссылаются на колонки, поэтому снимаются первыми.
    Обратный порядок даёт «cannot drop column ... other objects depend on it»
    (класс CR 12.2 H-1).

    Карточки и заявки не затрагиваются: удаляются только три NULL-able
    колонки, которых до `0038` не существовало. Заданные авторами рамки
    при откате пропадут — это осознанно, иначе откат был бы невозможен.
    """
    op.drop_constraint(
        op.f(f"ck_event_cards_{GENDER_FILTER_CONSTRAINT}"), "event_cards", type_="check"
    )
    op.drop_constraint(op.f(f"ck_event_cards_{AGE_RANGE_CONSTRAINT}"), "event_cards", type_="check")
    op.drop_column("event_cards", "age_max")
    op.drop_column("event_cards", "age_min")
    op.drop_column("event_cards", "gender_filter")
