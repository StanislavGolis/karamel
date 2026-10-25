"""profiles: composite gender FK, strict whitespace CHECK, completeness flag

CR 2.1, Решение 1: `profiles.gender` — зеркало `users.gender`, и расхождение
теперь запрещено БД. Простой FK проверял только существование учётки:
`gender='male'` у женской учётки проходил, и выборка эпика 4 показала бы
однополые анкеты. Составной FK `(user_id, gender) -> users(id, gender)`
закрывает это и для записей сырым SQL, и для будущего воркера.

Для FK нужен уникальный ключ на целевой паре: `UNIQUE (id, gender)`.
Составной UNIQUE, первая колонка которого — PK, в PostgreSQL допустим.

CR 2.1, патчи 1 и 2: `btrim(x)` без второго аргумента режет только пробел,
поэтому `city = E'Томск\t'` проходил CHECK и навсегда выпадал из выборки.
Пробельный набор задан явно; `length(x) <= N` удалены — предел держит
`varchar(n)`, и до CHECK по длине дело не доходит (мёртвый предикат давал
ложную гарантию при будущем расширении типа).

CR 2.1, патч 14: колонка `status` — признак полноты анкеты. Выбран отдельный
столбец, а не CHECK «все четыре поля NULL или все заполнены»: онбординг
пошаговый (фото в 2.2 приезжают раньше текста), и связка `num_nulls IN (0,4)`
запретила бы сохранить частично заполненную анкету. Фильтр выборки эпика 4
становится явным: `status = 'complete'` вместо надежды на `city IS NOT NULL`.
Недопустимое состояние «complete с пустыми полями» отбивает `ck_profiles_completeness`.

Все изменения собраны в одну ревизию, а не правятся в 0004: 0004 уже
применена на стенде, и её изменение потребовало бы ручного `downgrade 0003`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# CR 2.1, патч 1: явный набор «пробельных» символов для btrim. Без второго
# аргумента btrim режет только пробел (U+0020).
_WS = "E' \t\r\n\f\v'"


def upgrade() -> None:
    """Заменяет простой FK составным и ужесточает CHECK-и (CR 2.1)."""
    # Решение 1: цель составного FK. Имя через `op.f` (§18.7), иначе
    # naming convention даёт двойной префикс. Составной UNIQUE на (PK, col)
    # допустим в PostgreSQL и обязателен: без него FK не создать (42830).
    op.create_unique_constraint(op.f("uq_users_id_gender"), "users", ["id", "gender"])

    # Простой FK больше не нужен: составной проверяет и существование
    # учётки, и совпадение пола.
    op.drop_constraint(op.f("fk_profiles_user_id_users"), "profiles", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_profiles_user_gender_users"),
        "profiles",
        "users",
        ["user_id", "gender"],
        ["id", "gender"],
        ondelete="CASCADE",
    )

    # Патч 14: признак полноты. NOT NULL DEFAULT 'draft' — существующие строки
    # становятся черновиками, что безопасно: в выборку они не попадают.
    op.add_column(
        "profiles",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
    )
    op.create_check_constraint(
        op.f("ck_profiles_status"),
        "profiles",
        "status IN ('draft', 'complete')",
    )
    # `draft` разрешает любую заполненность (пошаговый онбординг), `complete`
    # требует все четыре поля. Обратное («complete с пустым городом») невозможно.
    op.create_check_constraint(
        op.f("ck_profiles_completeness"),
        "profiles",
        "status <> 'complete' OR num_nulls(name, city, age, bio) = 0",
    )

    # Патчи 1 и 2: пересоздание строковых CHECK с явным пробельным набором
    # и без мёртвых предикатов длины. DROP+ADD, а не ALTER: PostgreSQL не
    # умеет менять выражение CHECK.
    op.drop_constraint(op.f("ck_profiles_name"), "profiles", type_="check")
    op.create_check_constraint(
        op.f("ck_profiles_name"),
        "profiles",
        f"name IS NULL OR (btrim(name, {_WS}) <> '')",
    )

    op.drop_constraint(op.f("ck_profiles_city"), "profiles", type_="check")
    # AC-07: `city` без пробельных символов по краям — иначе '  Томск' не
    # найдётся запросом по 'Томск'. Регистр по-прежнему не проверяется (D-5).
    op.create_check_constraint(
        op.f("ck_profiles_city"),
        "profiles",
        f"city IS NULL OR (btrim(city, {_WS}) <> '' AND city = btrim(city, {_WS}))",
    )

    op.drop_constraint(op.f("ck_profiles_bio"), "profiles", type_="check")
    # CR 2.1, патч 2: `text` не ограничивает размер — предвалидация в схеме 2.3,
    # CHECK по длине был бы недостижим и создавал ложную гарантию.
    op.create_check_constraint(
        op.f("ck_profiles_bio"),
        "profiles",
        f"bio IS NULL OR (btrim(bio, {_WS}) <> '')",
    )


def downgrade() -> None:
    """Возвращает простой FK и прежние CHECK-и."""
    op.drop_constraint(op.f("ck_profiles_bio"), "profiles", type_="check")
    op.create_check_constraint(
        op.f("ck_profiles_bio"),
        "profiles",
        "bio IS NULL OR (btrim(bio) <> '' AND length(bio) <= 500)",
    )

    op.drop_constraint(op.f("ck_profiles_city"), "profiles", type_="check")
    op.create_check_constraint(
        op.f("ck_profiles_city"),
        "profiles",
        "city IS NULL OR (btrim(city) <> '' AND city = btrim(city) AND length(city) <= 100)",
    )

    op.drop_constraint(op.f("ck_profiles_name"), "profiles", type_="check")
    op.create_check_constraint(
        op.f("ck_profiles_name"),
        "profiles",
        "name IS NULL OR (btrim(name) <> '' AND length(name) <= 50)",
    )

    op.drop_constraint(op.f("ck_profiles_completeness"), "profiles", type_="check")
    op.drop_constraint(op.f("ck_profiles_status"), "profiles", type_="check")
    op.drop_column("profiles", "status")

    op.drop_constraint(op.f("fk_profiles_user_gender_users"), "profiles", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_profiles_user_id_users"),
        "profiles",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(op.f("uq_users_id_gender"), "users", type_="unique")
