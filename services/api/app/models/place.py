"""Модель справочника мест `places` (стори 3.1, Task 3.1–3.7; AC-01, AC-03, AC-05).

AC-01 (FR67, NFR34): место хранится как название, категория и адрес текстом.
Колонок с координатами (`lat`, `lng`, `latitude`, `longitude`, `coords`, `geo`)
нет ни в каком виде: точные координаты — трансграничная передача и лишние ПД,
которых PRD не требует. Геокодер не вызывается вовсе.

D-2: `id` — внутренний bigint и наружу не выходит никогда; публичный
идентификатор — `uuid`.

D-8: `city_slug` — `varchar(64)` + regex-CHECK, без FK на `communities`:
таблицы сообществ нет (ADR-01), в пилоте одно сообщество (NFR46). Переход
к мультисообществу — одна миграция с FK; запросы уже фильтруют по срезу.

D-5: уникальность `(city_slug, name)` — без учёта регистра НЕ приводится.
`normalize_text` снимает края и схлопывает пробелы, поэтому
`'Кафе  „Прага“ '` и `'Кафе „Прага“'` — одно место. «Прага» и «прага» — два
разных места осознанно: функциональный индекс по `lower(name)` не добавлен,
и если появится требование — это отдельная миграция, а не правка «по месту».
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Identity,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import PlaceCategory


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений StrEnum — единый источник списка.

    AC-02, D-2: список категорий не дублируется строкой ни здесь, ни в миграции.
    Иначе добавление значения в `PlaceCategory` не изменило бы ограничение
    в БД, и категория, прошедшая Pydantic, отвергалась бы PostgreSQL.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


# AC-02, Task 3.4: `btrim(x)` без второго аргумента режет ТОЛЬКО пробел U+0020.
# Название из одного таба прошло бы CHECK и навсегда выпало из поиска.
# Набор «пробельных» задан явно — тот же, что в `profile.py` и `profile_photo.py`.
#
# CR 3.1, патч 8: NBSP (U+00A0) и узкий NBSP (U+202F) ВХОДЯТ в набор.
# Раньше их не было, и `CHECK` пропускал `name = 'Кафе Прага\u00a0'` из
# сырого SQL/воркера: сервис нормализует вход и такой дубль не видит, поэтому
# в таблице оказывались две визуально одинаковые строки, а `uq_places_city_name`
# их не отбивала (проверено вставкой на живой БД). `normalize_text` заменяет
# NBSP пробелом до `strip()`, так что вывод сервиса этому CHECK соответствует.
_WHITESPACE = "E' \\t\\r\\n\\f\\v\u00a0\u202f'"


def _trimmed(column: str) -> str:
    """`btrim(col, <пробельные>)` — единая запись для строковых CHECK."""
    return f"btrim({column}, {_WHITESPACE})"


class Place(TimestampMixin, Base):
    """Проверенное публичное место сообщества (AC-01).

    Место — справочная сущность: карточка события ссылается на него вместо
    свободного текста (эпик 4), поэтому адрес хранится одной строкой, а
    «скрыть» (FR68) реализуется флагом `is_active`, а не удалением строки.
    """

    __tablename__ = "places"

    # AC-01, D-2: внутренний PK, наружу не выходит.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # AC-01: публичный идентификатор. `default` в Python нужен, чтобы объект
    # был готов до flush; `server_default` — для вставок вне ORM.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid_lib.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # AC-01: название места. Предел 120 — карточка события показывает его
    # в списке; длиннее в интерфейс не влезает и не диктуется продуктом.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # AC-02: категория из белого списка. `String(32)` + CHECK, не PG-enum —
    # добавление категории остаётся миграцией, а не `ALTER TYPE`.
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    # AC-01, FR67: адрес одной строкой. Координат нет: NFR34 запрещает
    # трансграничную передачу, а геокодер для адреса не вызывается.
    address_text: Mapped[str] = mapped_column(String(255), nullable=False)
    # AC-03, D-8: срез сообщества. FK нет — таблицы `communities` не существует
    # (ADR-01); допустимость значения держит regex-CHECK + конфиг сервиса.
    city_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    # AC-05, FR68: «скрыть» ≠ «удалить». Строка остаётся, карточки на неё
    # ссылаться могут, а выпадающий список (FR98) её не показывает.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        # AC-01: `PrimaryKeyConstraint` объявлен явно, чтобы имя строилось
        # по convention (`pk_places`) и совпадало с миграцией.
        PrimaryKeyConstraint("id"),
        # AC-01: публичный идентификатор уникален — адресация им однозначна.
        UniqueConstraint("uuid"),
        # AC-04 (третий AC эпика), D-4: запрет дубля держит БД, а не проверка
        # перед вставкой. Две параллельные вставки одного имени в одном
        # сообществе не проходят: вторую отбивает это ограничение.
        # Имя задано ПОЛНЫМ. У конвенции `uq` нет токена `%(constraint_name)s`
        # (в отличие от `ck`), поэтому короткое `name="city_name"` давало
        # ровно `city_name` — без префикса `uq_places_`. Миграция с
        # `op.f("uq_places_city_name")` тогда разошлась бы с моделью,
        # и `downgrade` падал бы на несуществующем имени.
        UniqueConstraint("city_slug", "name", name="uq_places_city_name"),
        # AC-02: категория — только значения `PlaceCategory`, собранные
        # из класса, а не переписанные строкой.
        CheckConstraint(_in_clause("category", tuple(PlaceCategory)), name="category"),
        # AC-01: название не пустое и БЕЗ пробельных по краям. Проверка
        # не только в сервисе — вставка сырым SQL и из воркера тоже обязана
        # пройти нормализацию, иначе место навсегда выпадает из поиска.
        # CR 3.1, патч 8: равенство, а не `<> ''`. `<> ''` отбивал только
        # пустоту: `' Прага'` и `'Прага'` проходили оба и сосуществовали,
        # потому что `uq_places_city_name` сравнивает байты, а сервис
        # ищет дубль по нормализованному значению (проверено на живой БД).
        CheckConstraint(f"name = {_trimmed('name')}", name="name"),
        # AC-01: то же для адреса.
        CheckConstraint(f"address_text = {_trimmed('address_text')}", name="address_text"),
        # AC-03: тот же шаблон, что в `Settings.city_slugs`. Две копии
        # намеренно: конфиг проверяет `.env` до подключения к БД, CHECK держит
        # инвариант для всех путей записи.
        CheckConstraint("city_slug ~ '^[a-z0-9_-]{3,64}$'", name="city_slug"),
        # AC-05, FR98: частичный индекс под выпадающий список выбора места
        # и админский фильтр. `is_active` в ключе не нужен: предикат уже
        # отбирает активные, а категория сужает выдачу.
        Index(
            "idx_places_city_active",
            "city_slug",
            "category",
            postgresql_where=text("is_active"),
        ),
        # Стори 4.1, AC-07 (X-02, решение пользователя 2026-10-23): индекс под
        # курсорную выдачу `GET /api/v1/places` (3.3). Выдача сортирует по
        # `(created_at, uuid)` внутри среза сообщества и фильтрует
        # `is_active`; прежний `idx_places_city_active` покрывает фильтр,
        # но НЕ порядок — PostgreSQL делал `Sort` в памяти на каждой странице.
        #
        # Объявлен здесь, потому что `alembic check` сверяет БД с моделями:
        # индекс, созданный миграцией `0008` и не описанный в модели, выглядит
        # как лишний объект в БД, и autogenerate предлагает его УДАЛИТЬ
        # (`New upgrade operations detected: remove_index`). Модель — источник
        # истины для схемы, миграция лишь применяет её.
        #
        # `uuid` в ключе, а не `id`: публичная выдача адресуется `uuid` (D-2),
        # и порядок `(created_at, uuid)` — ровно тот, что даёт курсор 3.3.
        Index(
            "idx_places_city_cursor",
            "city_slug",
            "created_at",
            "uuid",
            postgresql_where=text("is_active"),
        ),
    )
