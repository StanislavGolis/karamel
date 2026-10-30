"""add profiles.birth_date (story 12.2)

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-29

Стори 12.2 (AC-1…AC-6; ADR-17, ADR-18; NFR36, NFR66, NFR52).

**Почему `0036`, а не `0035`.** Эпик (`epic-12.md`) назначает этой стори
`0035`, исходя из порядка «12.1, потом 12.2». Замер `alembic heads`
2026-10-29: голова — `0035`, её заняла стори 12.1
(`0035_rename_community_to_city.py`). Свободны `0036`–`0038`.
`down_revision = "0035"`. Отклонение от эпика зафиксировано замером (P-28).

**Почему `age` не удаляется.** ADR-17: 40 008 существующих анкет имеют число
лет и не имеют даты рождения. Восстановить дату из числа математически
невозможно (год известен только как разность), поэтому бэкфилл отсутствует
осознанно, а `age` остаётся legacy-fallback: `compute_age()` берёт
`birth_date`, если он есть, иначе `age`. Удаление колонки лишило бы
доступа всех старых пользователей — прямое нарушение AC-5.

**Почему `ADD COLUMN` без `NOT NULL` и без дефолта.** Новое поле необязательно
при миграции: у существующих строк его нет и выдумывать его нельзя (та же
логика, что в `0034` для `consent_version`). PostgreSQL 11+ добавляет
такую колонку без переписывания таблицы — только каталог.

**⚠️ Почему переопределяется CHECK `completeness`.** Существующее ограничение
(миграция `0003`, модель `profile.py:177`):

    status <> 'complete' OR num_nulls(name, city, age, bio) = 0

Оно требует **именно `age`**. Новый пользователь заполняет `birth_date`,
а `age` у него `NULL` → `status='complete'` нарушил бы CHECK →
`IntegrityError` → `500` в момент сохранения анкеты. Без этой правки
AC-2 недостижим физически, а не «стилистически».

Новая форма сохраняет смысл ограничения («complete при заполненном
возрасте»), но принимает любой из двух источников:

    status <> 'complete' OR (num_nulls(name, city, bio) = 0
                             AND (age IS NOT NULL OR birth_date IS NOT NULL))

**Порядок в `downgrade` обязателен: сначала CHECK, потом `DROP COLUMN`.**
Обратный порядок уронил бы откат: старое ограничение ссылается на `age`
(не на `birth_date`), но новый CHECK ссылается на `birth_date`, и удаление
колонки до снятия ограничения даст «cannot drop column ... other objects
depend on it».

**Транзакционность.** `ADD COLUMN` без дефолта и `DROP CONSTRAINT` /
`ADD CONSTRAINT` — быстрые операции каталога. PostgreSQL исполняет DDL
транзакционно, поэтому `upgrade()` целиком атомарен: сбой на любом шаге
оставляет схему на `0035`. Алембик заворачивает ревизию в транзакцию,
отдельный `BEGIN` не нужен.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

#: Имя ограничения полноты анкеты **без префикса**.
#:
#: Замер `pg_constraint` 2026-10-29: фактическое имя — `ck_profiles_completeness`,
#: его строит naming convention Alembic из `name="completeness"` в модели.
#: Если передать в `create_check_constraint` полное имя, convention добавит
#: префикс второй раз — получается `ck_profiles_ck_profiles_completeness`,
#: и `upgrade` падает на несуществующем ограничении (проверено прогоном).
COMPLETENESS_CONSTRAINT = "completeness"

#: Исходное ограничение (`0003`): требует именно `age`.
_OLD_COMPLETENESS = "status <> 'complete' OR num_nulls(name, city, age, bio) = 0"

#: Новое ограничение (ADR-17): возраст из любого источника.
_NEW_COMPLETENESS = (
    "status <> 'complete' OR (num_nulls(name, city, bio) = 0 "
    "AND (age IS NOT NULL OR birth_date IS NOT NULL))"
)


def upgrade() -> None:
    # AC-1: колонка `date NULL`, без дефолта и без индекса.
    # `nullable=True` — не небрежность: у 40 008 строк даты рождения нет
    # и выдумывать её нельзя (ADR-17, «бэкфилл невозможен»).
    op.add_column("profiles", sa.Column("birth_date", sa.Date(), nullable=True))

    # AC-2 + AC-5: без этого `complete` с `birth_date` и `age = NULL`
    # падает на старом CHECK — см. докстринг ревизии.
    # `op.f(...)` — полное имя из convention: снять ограничение, созданное моделью.
    op.drop_constraint(op.f(f"ck_profiles_{COMPLETENESS_CONSTRAINT}"), "profiles", type_="check")
    op.create_check_constraint(COMPLETENESS_CONSTRAINT, "profiles", _NEW_COMPLETENESS)


def downgrade() -> None:
    """Откат: **данные важнее схемы**, поэтому `age` заполняется до CHECK.

    CR 12.2, находка H-1 (замер на живой БД). Наивный порядок — снять новый
    CHECK, вернуть старый, удалить колонку — падал:

        ERROR: check constraint "ck_profiles_completeness" of relation
               "profiles" is violated by some row

    Причина: старый CHECK требует `age IS NOT NULL` у `complete`, а любой,
    кто заполнил анкету после 12.2, имеет `birth_date` и `age = NULL`.
    То есть `upgrade` проходил всегда, а `downgrade` — только на пустой
    базе. Продукт был бы неоткатываемым — блокер выпуска.

    **Почему заполнение законно именно здесь.** Из даты рождения число лет
    восстанавливается однозначно на дату отката; обратное — нет (ADR-17,
    «бэкфилл невозможен»). После отката продукт снова живёт на числе лет,
    и оно вычислено тем же правилом, что действовало в 12.2:
    полные годы на `CURRENT_DATE`, день рождения сегодня — уже полный год.

    Формула продублирована на SQL, а не вызвана из Python: миграция обязана
    оставаться исполнимой сама по себе (`alembic` зовёт её в отдельном
    процессе), а импорт `app.services.age_service` связал бы ревизию
    с текущим кодом приложения — при следующем рефакторинге откат старой
    ревизии сломался бы.
    """
    # 1. Заполняем `age` у всех, у кого есть дата и нет числа.
    #    Порядок шагов важен: CHECK ниже проверяет СУЩЕСТВУЮЩИЕ строки.
    op.execute(
        "UPDATE profiles "
        "SET age = date_part('year', age(CURRENT_DATE, birth_date)) "
        "WHERE birth_date IS NOT NULL AND age IS NULL"
    )
    # 2. Снимаем новый CHECK (он ссылается на `birth_date`).
    op.drop_constraint(op.f(f"ck_profiles_{COMPLETENESS_CONSTRAINT}"), "profiles", type_="check")
    # 3. Возвращаем старый CHECK — теперь он проходит на всех строках.
    op.create_check_constraint(COMPLETENESS_CONSTRAINT, "profiles", _OLD_COMPLETENESS)
    # 4. И только теперь удаляем колонку: до снятия CHECK это упало бы
    #    на зависимостях, а до заполнения `age` — на самом CHECK.
    op.drop_column("profiles", "birth_date")
