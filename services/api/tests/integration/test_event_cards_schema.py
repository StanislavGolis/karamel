"""Схема `event_cards` (Task 8.1–8.7; AC-01, AC-05, AC-07, AC-09).

Проверки идут через `information_schema` и `pg_constraint`, а не через
модель: модель описывает намерение, а AC говорит о фактической схеме. Тест
на модели прошёл бы и при расхождении миграции `0008` с моделью — ровно
тот дефект, что был с `ProfilePhoto` (модель есть, таблицы нет).

§18.1: каждое ограничение проверяется НАРУШЕНИЕМ, а не чтением каталога:
наличие `CHECK` в `pg_constraint` не доказывает, что он что-то запрещает.
Каталог используется только для проверки ИМЁН — они часть контракта
(`op.f`, §18.7), и расхождение ломает `alembic check` и `downgrade`.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01: точный состав колонок с типами и nullability. Равенство, а не
# подмножество: тест падает и при лишней колонке, и при недостающей.
# ACHTUNG: ключевое свойство AC-01/NFR34 — здесь НЕТ ни одной колонки
# координат. Появление `lat`/`lng`/`geo`/`coords` ломает тест.
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "author_id": ("bigint", "NO"),
    "place_id": ("bigint", "NO"),
    "community_slug": ("character varying", "NO"),
    "title": ("character varying", "NO"),
    "description": ("text", "YES"),
    "category": ("character varying", "NO"),
    "starts_at": ("timestamp with time zone", "NO"),
    "capacity": ("smallint", "NO"),
    "decision_window_minutes": ("smallint", "NO"),
    "expires_at": ("timestamp with time zone", "NO"),
    "status": ("character varying", "NO"),
    "filled_count": ("smallint", "NO"),
    "verification_status": ("character varying", "NO"),
    "rejection_reason": ("text", "YES"),
    "verified_at": ("timestamp with time zone", "YES"),
    "verified_by": ("bigint", "YES"),
    "published_at": ("timestamp with time zone", "YES"),
    "closed_at": ("timestamp with time zone", "YES"),
    "attendance_prompted_at": ("timestamp with time zone", "YES"),
    "decision_reminded_at": ("timestamp with time zone", "YES"),
    # Стори 5б.2 (AC-01, D-5b.2-1, миграция `0013`): идентификатор чата
    # компании. `NULL` до выбора автора — компания ещё не собрана.
    "chat_uuid": ("uuid", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}

EXPECTED_CONSTRAINTS = (
    "pk_event_cards",
    "uq_event_cards_uuid",
    "ck_event_cards_status",
    "ck_event_cards_verification_status",
    "ck_event_cards_category",
    "ck_event_cards_capacity",
    "ck_event_cards_filled_count",
    "ck_event_cards_decision_window",
    "ck_event_cards_expires_at",
    "ck_event_cards_community_slug",
    "ck_event_cards_published",
    "ck_event_cards_rejection_reason",
    "ck_event_cards_full_capacity",
    "ck_event_cards_request_mode",
)

EXPECTED_INDEXES = (
    "ix_event_cards_community_starts",
    "ix_event_cards_author_active",
    "ix_event_cards_pending_verification",
    "ix_event_cards_due_windows",
    "ix_event_cards_prompt_due",
)

FORBIDDEN_COORDINATE_COLUMNS = ("lat", "lng", "latitude", "longitude", "coords", "geo")

STARTS_AT = datetime(2026, 11, 1, 19, 0, tzinfo=UTC)


async def _create_user(session: AsyncSession, *, phone: str = "+79009990001") -> int:
    """Создаёт учётку для FK `author_id` и возвращает её `id`.

    Через сырой SQL: тест схемы не должен зависеть от сервисов 1.x.
    """
    result = await session.execute(
        text(
            "INSERT INTO users (provider, phone, community_slug) "
            "VALUES ('phone', :phone, 'tomsk') RETURNING id"
        ),
        {"phone": phone},
    )
    return int(result.scalar_one())


async def _create_place(
    session: AsyncSession, *, community_slug: str = "tomsk", is_active: bool = True
) -> int:
    """Создаёт место для FK `place_id` и возвращает его `id`."""
    result = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES ('Кафе „Прага“', 'cafe', 'пр. Ленина, 1', :slug, :active) RETURNING id"
        ),
        {"slug": community_slug, "active": is_active},
    )
    return int(result.scalar_one())


def _card(**overrides: Any) -> dict[str, Any]:
    """Строка карточки для прямой вставки SQL: тест идёт мимо Pydantic.

    Проверяется, что белый список статусов и структурные инварианты живут
    в БД, а не только в схемах: воркер и админский скрипт пишут сырым SQL.
    """
    base: dict[str, Any] = {
        "community_slug": "tomsk",
        "title": "Настольные игры",
        "category": "cafe",
        "starts_at": STARTS_AT,
        "capacity": 3,
        "decision_window_minutes": 2880,
        "expires_at": STARTS_AT - timedelta(hours=48),
    }
    base.update(overrides)
    return base


def _insert_sql(values: dict[str, Any]) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """Собирает `INSERT` из словаря: возвращает SQL, параметры и список колонок.

    Явные колонки обязательны: `status`/`verification_status`/`filled_count`
    имеют `server_default`, и проверка дефолтов — отдельный тест, который
    вставляет строку БЕЗ этих колонок.
    """
    columns = tuple(values)
    placeholders = ", ".join(f":{name}" for name in columns)
    sql = f"INSERT INTO event_cards ({', '.join(columns)}) VALUES ({placeholders})"
    return sql, values, columns


async def _insert_card(session: AsyncSession, **overrides: Any) -> uuid_lib.UUID:
    """Вставляет карточку сырым SQL и возвращает её `uuid`."""
    values = _card(**overrides)
    sql, params, _columns = _insert_sql(values)
    result = await session.execute(text(f"{sql} RETURNING uuid"), params)
    return uuid_lib.UUID(str(result.scalar_one()))


# --- AC-01: колонки -------------------------------------------------------------


async def test_event_cards_columns_match_declared_schema(session: AsyncSession) -> None:
    """AC-01: состав колонок, типы и `NOT NULL` совпадают с §7 architecture.

    Краснеет при лишней колонке (например добавленной «на будущее») и при
    снятом `NOT NULL` с обязательного поля: карточка без `starts_at`
    сломала бы воркер архива (FR73).
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'event_cards'"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}

    assert actual == EXPECTED_COLUMNS


async def test_event_cards_has_no_coordinate_columns(session: AsyncSession) -> None:
    """AC-01, NFR34: координат нет ни в каком виде.

    Краснеет, если добавить `lat`, `lng`, `latitude`, `longitude`, `coords`
    или `geo`. Причина не стилистическая: точные координаты — трансграничная
    передача и лишние ПД, которых PRD не требует; местоположение карточки —
    ссылка на проверенное место (FR98), а не координаты.
    """
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'event_cards'"
        )
    )
    columns = {row[0] for row in result}

    assert not (columns & set(FORBIDDEN_COORDINATE_COLUMNS))


# --- AC-01, AC-05, AC-06: имена ограничений -------------------------------------


async def test_event_cards_constraint_names_match_model(session: AsyncSession) -> None:
    """§18.7: имена ограничений совпадают с моделью.

    Краснеет при двойном префиксе (`ck_event_cards_ck_event_cards_status`) —
    то есть когда в миграции забыт `op.f`. Такой diff не ловится `CHECK`-ом,
    но валит `alembic check` и `downgrade` (падает на несуществующем имени).
    """
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.event_cards'::regclass AND contype IN ('p','u','c')"
        )
    )
    names = {row[0] for row in result}

    assert names == set(EXPECTED_CONSTRAINTS), (
        f"лишние: {sorted(names - set(EXPECTED_CONSTRAINTS))}, "
        f"отсутствуют: {sorted(set(EXPECTED_CONSTRAINTS) - names)}"
    )


async def test_event_cards_foreign_keys_match_spec(session: AsyncSession) -> None:
    """AC-01, AC-04: три FK с правильными `ondelete`.

    Краснеет, если `place_id` сделать `NO ACTION` вместо `RESTRICT` — тогда
    физическое удаление места проходило бы и оставляло карточки-сироты
    (FR68 требует «скрыть», а не «удалить»).
    """
    result = await session.execute(
        text(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE conrelid = 'public.event_cards'::regclass AND contype = 'f'"
        )
    )
    # `confdeltype` — это `"char"` в PostgreSQL, и asyncpg отдаёт его как
    # `bytes` (`b'c'`), а не как `str`. Декодирование обязательно: без него
    # сравнение со строками падало бы ВСЕГДА, независимо от правильности FK,
    # и тест был бы «красным по дефекту теста, а не кода».
    constraints = {
        row[0]: row[1].decode() if isinstance(row[1], bytes) else row[1] for row in result
    }

    # `c` = CASCADE, `r` = RESTRICT, `n` = SET NULL (коды pg_constraint).
    assert constraints == {
        "fk_event_cards_author_id_users": "c",
        "fk_event_cards_place_id_places": "r",
        "fk_event_cards_verified_by_users": "n",
    }


# --- AC-01: дефолты -------------------------------------------------------------


async def test_status_and_verification_defaults(session: AsyncSession) -> None:
    """AC-01: новая карточка — `draft`, `pending`, `filled_count = 0`.

    Краснеет, если `server_default` убрать: вставка сырым SQL без этих
    колонок бросала бы `NOT NULL violation`, то есть воркер и импорт
    не смогли бы создать карточку.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    values = _card(author_id=author_id, place_id=place_id)
    sql, params, _columns = _insert_sql(values)
    await session.execute(
        text(f"{sql} RETURNING id, status, verification_status, filled_count"), params
    )

    row = (
        await session.execute(
            text("SELECT status, verification_status, filled_count FROM event_cards")
        )
    ).one()

    assert row[0] == "draft"
    assert row[1] == "pending"
    assert row[2] == 0


async def test_uuid_is_generated_by_database(session: AsyncSession) -> None:
    """AC-01, D-2: публичный `uuid` создаётся БД, а не приложением.

    Краснеет, если убрать `server_default=gen_random_uuid()`: вставка сырым
    SQL без `uuid` падала бы `NOT NULL violation`, а публичный идентификатор
    стал бы обязательным полем ввода (D-2: его не выбирает клиент).
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    values = _card(author_id=author_id, place_id=place_id)
    sql, params, _columns = _insert_sql(values)
    await session.execute(text(sql), params)

    uuid_value = (await session.execute(text("SELECT uuid FROM event_cards"))).scalar_one()

    assert isinstance(uuid_value, uuid_lib.UUID)
    assert str(uuid_value) != "00000000-0000-0000-0000-000000000000"


# --- AC-01, AC-05, AC-06: каждый CHECK проверяется нарушением -------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"capacity": 0}, "ck_event_cards_capacity"),
        ({"capacity": 7}, "ck_event_cards_capacity"),
        ({"filled_count": -1}, "ck_event_cards_filled_count"),
        ({"capacity": 2, "filled_count": 3}, "ck_event_cards_filled_count"),
        ({"decision_window_minutes": 14}, "ck_event_cards_decision_window"),
        ({"decision_window_minutes": 10081}, "ck_event_cards_decision_window"),
        ({"status": "unknown"}, "ck_event_cards_status"),
        ({"verification_status": "unknown"}, "ck_event_cards_verification_status"),
        ({"category": "bar"}, "ck_event_cards_category"),
        ({"community_slug": "Tomsk"}, "ck_event_cards_community_slug"),
        ({"community_slug": "to"}, "ck_event_cards_community_slug"),
        ({"status": "published"}, "ck_event_cards_published"),
        (
            {"verification_status": "rejected"},
            "ck_event_cards_rejection_reason",
        ),
        (
            {"capacity": 2, "filled_count": 2, "status": "published", "published_at": STARTS_AT},
            "ck_event_cards_full_capacity",
        ),
        (
            {"capacity": 1, "decision_window_minutes": 30, "filled_count": 2},
            "ck_event_cards_filled_count",
        ),
    ],
)
async def test_check_constraints_reject_invalid_rows(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    """AC-01, AC-05, AC-06: каждое нарушение отбивается своим CHECK.

    Краснеет, если ограничение снять из миграции: строка с недопустимым
    значением попала бы в БД, и дефект проявился бы в ленте (4.5), а не
    на записи. Проверяется ИМЯ ограничения, а не только факт отказа:
    иначе неверный CHECK маскировал бы отсутствующий.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    values = _card(author_id=author_id, place_id=place_id, **overrides)
    sql, params, _columns = _insert_sql(values)

    with pytest.raises(IntegrityError) as excinfo:
        await session.execute(text(sql), params)

    assert constraint in str(excinfo.value)


async def test_expires_at_after_starts_at_is_rejected(session: AsyncSession) -> None:
    """AC-01, ADR-02: окно решения не закрывается позже начала события.

    Отдельный тест, а не строка в параметризации: `expires_at` зависит от
    `starts_at`, и в общий словарь пришлось бы передавать оба поля, что
    сделало бы идентификатор теста неинформативным.

    Краснеет, если снять `ck_event_cards_expires_at`: окно «закрывается»
    после начала, то есть решение принимается по уже прошедшей встрече.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    values = _card(
        author_id=author_id,
        place_id=place_id,
        expires_at=STARTS_AT + timedelta(minutes=1),
    )
    sql, params, _columns = _insert_sql(values)

    with pytest.raises(IntegrityError) as excinfo:
        await session.execute(text(sql), params)

    assert "ck_event_cards_expires_at" in str(excinfo.value)


async def test_request_mode_with_two_filled_is_rejected(session: AsyncSession) -> None:
    """AC-03, FR70: режим заявок занимает ровно одно место.

    `filled_count = 2` при `capacity = 1` уже нарушает
    `ck_event_cards_filled_count` (`filled_count <= capacity`), поэтому
    отдельно проверяется `ck_event_cards_request_mode`: инвариант нужен на
    случай будущего расширения вместимости, когда `filled_count <= capacity`
    перестанет его покрывать.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    values = _card(
        author_id=author_id,
        place_id=place_id,
        capacity=1,
        decision_window_minutes=30,
        filled_count=2,
    )
    sql, params, _columns = _insert_sql(values)

    with pytest.raises(IntegrityError) as excinfo:
        await session.execute(text(sql), params)

    error = str(excinfo.value)
    assert "ck_event_cards_filled_count" in error or "ck_event_cards_request_mode" in error


async def test_every_status_is_accepted_by_database(session: AsyncSession) -> None:
    """AC-01, AC-06: ВСЕ значения `EventCardStatus` проходят CHECK.

    Краснеет, если CHECK собирается из захардкоженного списка, отставшего
    от enum: статус, добавленный в `EventCardStatus`, отвергался бы
    PostgreSQL при вставке — то есть `500` на легальном переходе.
    """
    from app.models.enums import EventCardStatus

    author_id = await _create_user(session)
    place_id = await _create_place(session)
    for index, status in enumerate(EventCardStatus):
        overrides: dict[str, Any] = {"status": str(status)}
        if status is EventCardStatus.PUBLISHED:
            overrides["published_at"] = STARTS_AT
        # Стори 4.6 (AC-07, X-35): CHECK стал ДВУСТОРОННИМ — `full` при
        # свободном месте теперь запрещён базой. Без этой строки тест падал бы
        # на `full` и требовал ослабить только что исправленный дефект.
        if status is EventCardStatus.FULL:
            overrides["capacity"] = 3
            overrides["filled_count"] = 3
        await _insert_card(
            session,
            author_id=author_id,
            place_id=place_id,
            community_slug="tomsk",
            title=f"Карточка {index}",
            **overrides,
        )

    count = (await session.execute(text("SELECT count(*) FROM event_cards"))).scalar_one()

    assert count == len(EventCardStatus)


async def test_every_category_is_accepted_by_database(session: AsyncSession) -> None:
    """AC-06, ADR-12: ВСЕ значения `PlaceCategory` проходят CHECK карточки.

    Краснеет, если список категорий карточки собирается отдельно от
    `PlaceCategory`: категория, добавленная в enum, принималась бы местом,
    но не карточкой — то есть фильтр ленты (4.5) по этой категории был бы
    пуст всегда.
    """
    from app.models.enums import PlaceCategory

    author_id = await _create_user(session)
    place_id = await _create_place(session)
    for index, category in enumerate(PlaceCategory):
        await _insert_card(
            session,
            author_id=author_id,
            place_id=place_id,
            title=f"Карточка {index}",
            category=str(category),
        )

    count = (await session.execute(text("SELECT count(*) FROM event_cards"))).scalar_one()

    assert count == len(PlaceCategory)


# --- AC-01, AC-04: поведение FK -------------------------------------------------


async def test_unknown_author_and_place_are_rejected(session: AsyncSession) -> None:
    """AC-01: FK не даёт сослаться на несуществующего автора или место.

    Краснеет, если FK убрать: карточка с `author_id = 999999` попала бы
    в БД, и лента (4.5) падала бы при попытке отдать автора.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)

    # §18.8: каждый ожидаемый отказ — в своём SAVEPOINT. После `IntegrityError`
    # транзакция PostgreSQL аборчена, и следующий запрос падает с
    # `InFailedSQLTransactionError` — то есть вторая проверка была бы не про
    # FK, а про состояние транзакции после первой.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_card(session, author_id=999_999_999, place_id=place_id)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_card(session, author_id=author_id, place_id=999_999_999)


async def test_deleting_author_cascades_to_cards(session: AsyncSession) -> None:
    """AC-01: удаление учётки уносит её карточки (CASCADE).

    Краснеет, если `ondelete` у `author_id` сделать `RESTRICT`: удаление
    аккаунта (FR «удаление данных — реальная функция») падало бы на любой
    карточке автора, и требование 152-ФЗ не выполнялось бы.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    await _insert_card(session, author_id=author_id, place_id=place_id)

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": author_id})

    count = (await session.execute(text("SELECT count(*) FROM event_cards"))).scalar_one()
    assert count == 0


async def test_deleting_place_with_card_is_restricted(session: AsyncSession) -> None:
    """AC-04, FR68: место с карточкой физически не удаляется.

    Краснеет, если `ondelete` у `place_id` сделать `CASCADE`: удаление места
    уносило бы карточки участников, то есть «скрыть место» (FR68) и «удалить
    место» стали бы одним действием с разрушительными последствиями.
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    await _insert_card(session, author_id=author_id, place_id=place_id)

    with pytest.raises(IntegrityError) as excinfo:
        await session.execute(text("DELETE FROM places WHERE id = :id"), {"id": place_id})

    assert "fk_event_cards_place_id_places" in str(excinfo.value)


async def test_hiding_place_keeps_card(session: AsyncSession) -> None:
    """AC-04, FR68: скрытое место остаётся доступным старым карточкам.

    Краснеет, если «скрыть» реализовать как `DELETE`: карточка потеряла бы
    место, и участники не увидели бы, куда идти. FR68 прямо разделяет
    «скрыть» и «удалить».
    """
    author_id = await _create_user(session)
    place_id = await _create_place(session)
    await _insert_card(session, author_id=author_id, place_id=place_id)

    await session.execute(
        text("UPDATE places SET is_active = false WHERE id = :id"), {"id": place_id}
    )

    count = (await session.execute(text("SELECT count(*) FROM event_cards"))).scalar_one()
    assert count == 1


async def test_deleting_moderator_sets_verified_by_null(session: AsyncSession) -> None:
    """AC-01, ADR-08: удаление модератора не уносит карточку.

    Краснеет, если `ondelete` у `verified_by` сделать `CASCADE`: увольнение
    модератора удаляло бы карточки, которые он проверял. `SET NULL`
    сохраняет карточку и факт проверки (`verified_at` остаётся).
    """
    author_id = await _create_user(session)
    moderator_id = await _create_user(session, phone="+79009990002")
    place_id = await _create_place(session)
    await _insert_card(
        session,
        author_id=author_id,
        place_id=place_id,
        verified_by=moderator_id,
        verified_at=STARTS_AT,
        verification_status="approved",
    )

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": moderator_id})

    row = (await session.execute(text("SELECT verified_by, verified_at FROM event_cards"))).one()

    assert row[0] is None
    assert row[1] is not None


# --- AC-07: индексы -------------------------------------------------------------


async def test_all_event_card_indexes_exist(session: AsyncSession) -> None:
    """AC-07: пять индексов карточки и X-02 на `places` созданы.

    Краснеет, если индекс потерять при правке миграции: лента (4.5) и
    воркеры (NFR20, NFR22) начнут сканировать таблицу целиком, и p95
    выйдет за бюджет §13.
    """
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'event_cards'"
        )
    )
    names = {row[0] for row in result}

    assert set(EXPECTED_INDEXES) <= names

    x02 = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = 'idx_places_community_cursor'"
        )
    )
    indexdef = x02.scalar_one()

    assert "(community_slug, created_at, uuid)" in indexdef
    assert "WHERE is_active" in indexdef


async def test_partial_index_predicates_are_exact(session: AsyncSession) -> None:
    """AC-07: предикаты частичных индексов совпадают с задуманными.

    Краснеет, если предикат убрать: индекс начнёт индексировать архивные
    карточки, и воркер окон (NFR20) потянет в план весь архив вместо
    нескольких просроченных строк.

    Сравнение нормализованное: PostgreSQL приводит предикат к виду
    `WHERE ((status)::text = 'published'::text)`, и побайтовое сравнение
    с исходной строкой падало бы всегда.
    """
    result = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname IN "
            "('ix_event_cards_author_active','ix_event_cards_pending_verification',"
            "'ix_event_cards_due_windows','ix_event_cards_prompt_due')"
        )
    )
    defs = {row[0]: row[1] for row in result}

    assert "WHERE" in defs["ix_event_cards_author_active"]
    assert "draft" in defs["ix_event_cards_author_active"]
    assert "pending" in defs["ix_event_cards_pending_verification"]
    assert "published" in defs["ix_event_cards_due_windows"]
    assert "attendance_prompted_at IS NULL" in defs["ix_event_cards_prompt_due"]


async def test_community_starts_index_is_not_partial(session: AsyncSession) -> None:
    """AC-07: индекс ленты — полный, по `(community_slug, starts_at, status)`.

    Краснеет, если его сделать частичным по статусу: лента 4.5 фильтрует
    `status IN ('published','full')`, но архивные карточки нужны участникам
    как история (FR73), и частичный индекс выкинул бы их из плана.
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = 'ix_event_cards_community_starts'"
        )
    )
    indexdef = result.scalar_one()

    assert "(community_slug, starts_at, status)" in indexdef
    assert "WHERE" not in indexdef


# --- AC-07 (X-02): план запроса -------------------------------------------------


async def test_places_cursor_query_uses_cursor_index(session: AsyncSession) -> None:
    """AC-07 (X-02): выдача мест 3.3 использует `idx_places_community_cursor`.

    `enable_seqscan = off` НЕДОСТАТОЧЕН (найдено CR 4.4, X-28): на пустой
    таблице планировщик сравнивает два индекса и выбирает
    `idx_places_community_active` + `Sort` (cost 8.16) вместо
    `idx_places_community_cursor` (cost 8.27) — тест падал в 4 прогонах из 5,
    то есть маскировал бы регресс плана (§18.2/18.3).

    `enable_sort = off` закрывает причину, а не симптом: без сортировки
    планировщик обязан искать индекс, отдающий `ORDER BY created_at, uuid`
    бесплатно. Такой индекс ровно один — `idx_places_community_cursor`.
    Проверено откатом: с `enable_indexscan/enable_indexonlyscan = off`
    (эмуляция отсутствия индекса) тест краснеет.

    Краснеет, если индекс удалить или переименовать: проверка идёт по
    фактическому плану, а не по каталогу.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    # CR 4.4, X-28: без этого на пустой таблице побеждает другой индекс
    # плюс `Sort` (см. docstring) — тест был флаком 1/5, а не проверкой.
    await session.execute(text("SET LOCAL enable_sort = off"))
    rows = (
        (
            await session.execute(
                text(
                    "EXPLAIN SELECT uuid FROM places "
                    "WHERE community_slug = :slug AND is_active "
                    "AND (created_at, uuid) > (:ts, :uuid) "
                    "ORDER BY created_at, uuid LIMIT 51"
                ),
                {
                    "slug": "tomsk",
                    "ts": STARTS_AT - timedelta(days=365),
                    "uuid": uuid_lib.UUID("00000000-0000-0000-0000-000000000000"),
                },
            )
        )
        .scalars()
        .all()
    )
    plan = "\n".join(rows)

    assert "idx_places_community_cursor" in plan
    assert "Sort" not in plan
