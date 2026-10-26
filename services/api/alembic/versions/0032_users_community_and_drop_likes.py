"""add users.community_slug and drop likes mechanics (story 10.1)

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-26

Стори 10.1 (AC-01, AC-02, AC-03; FR16; NFR34; ADR-01, ADR-13).

**`community_slug` добавляется как `NOT NULL DEFAULT '<default_community_slug>'`
с последующим `DROP DEFAULT`** (§4.3 требование 4). Это безопасно для
существующих строк и не оставляет «магический» дефолт в схеме: новая вставка
без слага падает, что и требуется — сообщество обязательно.

**Значение — литерал `'tomsk'`, не импорт из `app.core.config`.** Историческая
ревизия обязана быть воспроизводимой при смене конфига (правило из `0031`).
Расхождение литерала с `Settings.default_community_slug` ловится тестом
сверки трёх источников.

**Почему `tomsk` для существующих строк, а не маппинг по городу.** ADR-01
отвергает город как ось показа: `places` и `event_cards` уже несут
`community_slug`, город — отдельное поле профиля v1. Пилот — одно сообщество
(NFR46), все существующие строки принадлежат ему. Маппинг создал бы вторую
ось и таблицу соответствия, которую ADR-01 отвергает.

**Удаление лайков — последним** (§4.3 требование 5), чтобы откат любой более
ранней ревизии не требовал их восстановления. Колонки `likes_used_today`,
`likes_counted_on` и CHECK `likes_non_negative` созданы в `0001`; механика
снята разворотом Action-first, обращений в коде не осталось.

**`downgrade()` реально откатывает** (§4.3 требование 1): восстанавливает обе
колонки, CHECK и снимает `community_slug`. Данные лайков при откате — нули:
исходные значения негде хранить, колонок нет. §4.3 требует «данные
непротиворечивы», а не «восстановлены».
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


# Литерал, не импорт из конфига (см. docstring).
DEFAULT_COMMUNITY_SLUG = "tomsk"
COMMUNITY_SLUG_PATTERN = "^[a-z0-9_-]{3,64}$"


def upgrade() -> None:
    # AC-01: колонка появляется с дефолтом — существующие строки заполняются
    # одним ALTER, без отдельного UPDATE по каждой строке.
    op.add_column(
        "users",
        sa.Column(
            "community_slug",
            sa.String(64),
            nullable=False,
            server_default=DEFAULT_COMMUNITY_SLUG,
        ),
    )
    # AC-01: дефолт снимается — схема не хранит «магическое» значение, а новая
    # вставка без слага обязана упасть.
    op.alter_column("users", "community_slug", server_default=None)

    # AC-01: та же форма, что у `ck_places_community_slug` (0007). Имя через
    # naming convention: `create_check_constraint('community_slug', ...)` даёт
    # `ck_users_community_slug`. Полное имя в `name=` дало бы двойной префикс
    # `ck_users_ck_users_community_slug` (прецедент 0003_consent_pair_check).
    op.create_check_constraint(
        "community_slug",
        "users",
        f"community_slug ~ '{COMMUNITY_SLUG_PATTERN}'",
    )

    # AC-02: механика лайков удаляется последней (§4.3 п.5).
    op.drop_constraint(op.f("ck_users_likes_non_negative"), "users", type_="check")
    op.drop_column("users", "likes_used_today")
    op.drop_column("users", "likes_counted_on")


def downgrade() -> None:
    # AC-03: зеркало в обратном порядке. Колонки лайков восстанавливаются
    # с теми же типами и дефолтом, что в `0001`; данные — нули.
    op.add_column(
        "users",
        sa.Column(
            "likes_used_today",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("users", sa.Column("likes_counted_on", sa.Date(), nullable=True))
    op.create_check_constraint("likes_non_negative", "users", "likes_used_today >= 0")

    # AC-03: `community_slug` снимается последним — сначала CHECK, потом колонка.
    op.drop_constraint(op.f("ck_users_community_slug"), "users", type_="check")
    op.drop_column("users", "community_slug")
