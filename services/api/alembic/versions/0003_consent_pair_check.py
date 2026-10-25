"""add users consent pair CHECK

Решение 1 код-ревью стори 1.6 (вариант 2): пара `age_confirmed` /
`consent_given_at` не может разойтись. До этой миграции инвариант держался
только в коде сервиса: любая другая запись (админ-скрипт, будущий отзыв
согласия, ручная правка) могла оставить учётку с подтверждённым возрастом
без согласия на обработку ПД — прямое нарушение 152-ФЗ.

Три гарды на фронтенде используют три разных предиката, и это уже дало
цикл редиректов `/app` ↔ `/consent`. Ограничение в БД — единственный
уровень, который нельзя обойти клиентским кодом.

CHECK допускает оба согласованных состояния: `false`+`NULL` (новая учётка)
и `true`+`NOT NULL` (согласие дано). Состояния `false`+дата и `true`+`NULL`
невозможны.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 22:45:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Стори 6.1 (AC-07, долг X-5a.4-2): `op.f` вызывается ТОЛЬКО внутри
# `upgrade()`/`downgrade()`. На уровне модуля прокси `Operations` ещё не
# установлен, и `op.f(...)` падал `NameError: Can't invoke function 'f', as
# the proxy object has not yet been established` при простом импорте цепочки
# ревизий — то есть ломал `alembic heads`/`alembic history` и любую
# диагностику миграций. `alembic check` при этом оставался зелёным, поэтому
# дефект жил незамеченным.
#
# `op.f` помечает имя как финальное: без него Alembic применяет naming
# convention из `models/base.py` (`ck_%(table_name)s_%(constraint_name)s`)
# и получается `ck_users_ck_users_consent_pair` — двойной префикс, который
# не совпадает с именем в модели и делает `downgrade` неработающим.
# Так же оформлены ограничения в миграции 0001.
#
# Голые строки на уровне модуля безопасны: это литералы, а не прокси.
CONSTRAINT_NAME = "ck_users_consent_pair"
EXPRESSION = "age_confirmed = (consent_given_at IS NOT NULL)"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_check_constraint(op.f(CONSTRAINT_NAME), "users", EXPRESSION)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f(CONSTRAINT_NAME), "users", type_="check")
