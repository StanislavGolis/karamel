"""add event_cards audience_skipped_at marker (story 12.6, CR fixes H-1/H-3)

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-30

**Зачем маркер, а не терминальный статус.** AC-5 стори 12.6 требует, чтобы
карточка с рамками, отсекшими всех, осталась `published`: заявки живы, автор
вправе снять рамку, и человек не должен терять заявку без решения. Значит
`status` менять нельзя. Но предикат воркера (`status='published' AND
expires_at <= now`) после этого совпадает с карточкой ВЕЧНО: CR 12.6 H-1
замерил повтор `skipped: 1` на каждом прогоне, а `count_overdue_windows`
(предикат `status='published' AND expires_at < now - 5 min`) давал **1** —
то есть ложный алерт «воркер лежит» каждый час на здоровом воркере.

Маркер `audience_skipped_at` — дедупликация без смены статуса: воркер
пропускает помеченные карточки (`H-1`), они больше не занимают окно батча
(`H-3`: зависшая карточка имеет самый старый `expires_at` и стояла в начале
очереди `order_by(expires_at, id).limit(N)`), и `count_overdue_windows` их
не считает (ложный алерт снят).

**Маркер самоисцеляется.** Он снимается в ДВУХ случаях, оба — «появился шанс
на решение»: (1) автор изменил рамки карточки (`PATCH`, любое из трёх полей)
— `event_card_service.update_card`; (2) на карточку подана новая заявка —
`join_request_service.submit_join_request`, потому что новый заявитель может
проходить рамки, и пропускать его до истечения суток было бы потерей выбора.

**Почему NULL-able и без `server_default`.** Существующие карточки маркера не
имеют и не должны: `NULL` = «рамки никого не отсекали» — законное состояние.
`server_default` здесь означал бы «все старые карточки уже отсеяны», то есть
ложь в поле, влияющем на работу воркера.

**Транзакционность.** `ADD COLUMN` без `server_default` в PostgreSQL 11+
меняет только каталог (таблица не переписывается), поэтому миграция атомарна
и мгновенна. `downgrade` удаляет колонку: данные карточки и заявок не
затрагиваются, теряется только признак «уже отсеяно» — после отката воркер
один раз повторит попытку, что безопасно (повторное решение идемпотентно).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_cards",
        sa.Column("audience_skipped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Порядок один: колонка одна и ограничений на неё нет.
    op.drop_column("event_cards", "audience_skipped_at")
