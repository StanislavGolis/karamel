"""create places table and active-list index

Стори 3.1 (AC-01, AC-02, AC-03, AC-04, AC-05). Справочник публичных мест:
карточка события ссылается на проверенное место (эпик 4), а не на свободный
текст. Координаты не хранятся ни в каком виде (NFR34): геокодер не вызывается,
трансграничная передача исключена.

Состав миграции — только новые объекты: таблица `places`, уникальное
ограничение `uq_places_community_name` (AC-04) и частичный индекс
`idx_places_community_active` (AC-05). Таблицы `communities` нет (ADR-01),
`event_cards` и `place_id` создаются в `0008` (эпик 4), `users.community_slug`
— в `0014` (10.1). Поэтому `downgrade` полностью откатывает изменение,
не задевая чужие таблицы.

Долг «индекс `profile_photos(created_at, id)`» в эту миграцию НЕ включён (D-7):
он к `places` отношения не имеет, а его добавление сломало бы атомарность
`0007` — откат уносил бы индекс чужой таблицы. Долг остаётся в
`deferred-work.md`.

Имена ограничений и индексов заданы через `op.f` (§18.7): без него naming
convention из `models/base.py` складывается с именем и даёт двойной префикс
(`ck_places_ck_places_category`), который не совпадает с моделью, ломает
`alembic check` и валит `downgrade` на несуществующем имени.

Список категорий собирается из `PlaceCategory`, а не переписывается строкой
(AC-02, D-2): иначе добавление значения в enum не изменило бы ограничение
в БД, и категория, прошедшая Pydantic, отвергалась бы PostgreSQL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op
from app.models.enums import PlaceCategory

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# AC-02, D-2: единственный источник белого списка — `PlaceCategory`. Строка
# ограничения собирается здесь, чтобы добавление категории было одной
# миграцией, а не правкой в двух местах.
_CATEGORIES: tuple[str, ...] = tuple(str(category) for category in PlaceCategory)
_CATEGORY_IN: str = "category IN (" + ", ".join(f"'{value}'" for value in _CATEGORIES) + ")"

# AC-01, Task 3.4: явный набор «пробельных» для btrim. `btrim(x)` без второго
# аргумента режет только U+0020, и `name` из одного таба прошёл бы CHECK,
# навсегда выпав из поиска.
#
# CR 3.1, патч 8: NBSP (U+00A0) и узкий NBSP (U+202F) входят в набор — иначе
# `CHECK` пропускал `'Кафе Прага\u00a0'` из сырого SQL/воркера, сервис такой
# дубль не видел (он нормализует вход), и в таблице сосуществовали две
# визуально одинаковые строки в обход `uq_places_community_name`
# (проверено вставкой на живой БД).
_WS = "E' \\t\\r\\n\\f\\v\u00a0\u202f'"


def upgrade() -> None:
    """Создаёт `places`, ограничения и частичный индекс (AC-01…AC-05)."""
    op.create_table(
        "places",
        # AC-01, D-2: внутренний PK, наружу не выходит никогда.
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        # AC-01: публичный идентификатор; server_default — для вставок вне ORM.
        sa.Column(
            "uuid",
            PGUUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # AC-01: название места. Предел держит `varchar`, CHECK — пустоту.
        sa.Column("name", sa.String(length=120), nullable=False),
        # AC-02: категория из белого списка; `text + CHECK`, не PG-enum.
        sa.Column("category", sa.String(length=32), nullable=False),
        # AC-01, FR67: адрес одной строкой. Координат нет (NFR34).
        sa.Column("address_text", sa.String(length=255), nullable=False),
        # AC-03, D-8: срез сообщества; FK нет — таблицы `communities` не существует.
        sa.Column("community_slug", sa.String(length=64), nullable=False),
        # AC-05, FR68: «скрыть» ≠ «удалить». Дефолт `true` — место сразу видимо
        # в выпадающем списке выбора, пока админ не скрыл его.
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_places")),
        # AC-01: публичный идентификатор уникален — адресация им однозначна.
        sa.UniqueConstraint("uuid", name=op.f("uq_places_uuid")),
        # AC-04, D-4: запрет дубля держится ЭТИМ ограничением, а не проверкой
        # перед вставкой. Проверка-перед-вставкой пропускает две параллельные
        # вставки одного имени (§18.3); уникальный индекс их отбивает.
        sa.UniqueConstraint("community_slug", "name", name=op.f("uq_places_community_name")),
        # AC-02: категория — только значения `PlaceCategory`.
        sa.CheckConstraint(_CATEGORY_IN, name=op.f("ck_places_category")),
        # AC-01: название без пробельных по краям — вставка сырым SQL и из
        # воркера идёт через то же ограничение, что и сервис.
        # CR 3.1, патч 8: РАВЕНСТВО, а не `<> ''`. `<> ''` отбивал только
        # пустоту, поэтому `' Прага'` и `'Прага'` сосуществовали (проверено на
        # живой БД), хотя сервис считает их дублем.
        sa.CheckConstraint(f"name = btrim(name, {_WS})", name=op.f("ck_places_name")),
        # AC-01: то же для адреса.
        sa.CheckConstraint(
            f"address_text = btrim(address_text, {_WS})",
            name=op.f("ck_places_address_text"),
        ),
        # AC-03: тот же шаблон, что в `Settings.community_slugs`. Конфиг
        # проверяет `.env` до подключения к БД, CHECK держит инвариант для всех
        # путей записи; расхождение дало бы `500` на сохранении.
        sa.CheckConstraint(
            "community_slug ~ '^[a-z0-9_-]{3,64}$'",
            name=op.f("ck_places_community_slug"),
        ),
    )
    # AC-05 (FR98): частичный индекс под выпадающий список выбора места и
    # админский фильтр. Имя полное — индексы в naming convention не участвуют.
    op.create_index(
        "idx_places_community_active",
        "places",
        ["community_slug", "category"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    """Удаляет индекс и таблицу (обратный порядок создания)."""
    op.drop_index("idx_places_community_active", table_name="places")
    op.drop_table("places")
