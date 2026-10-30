"""Схема `join_requests` (стори 5а.1, Task 6; AC-01…AC-05, AC-07, AC-10).

Проверки идут через `information_schema`/`pg_constraint`/`pg_indexes`,
а не через модель: модель описывает намерение, а AC говорит о фактической
схеме. Тест на модели прошёл бы и при расхождении миграции `0012` с
моделью — ровно тот дефект, что был с `ProfilePhoto` (модель есть,
таблицы нет).

§18.1: каждое ограничение проверяется НАРУШЕНИЕМ, а не чтением каталога:
наличие `CHECK` в `pg_constraint` не доказывает, что он что-то запрещает.
Каталог используется только для ИМЁН (`op.f`, §18.7) и предикатов
индексов — они часть контракта.

§18.8: каждый ожидаемый отказ — в своём `begin_nested()`: после
`IntegrityError` транзакция PostgreSQL аборчена, и следующая проверка
в той же транзакции была бы не про ограничение, а про её состояние.
"""

from __future__ import annotations

import itertools
import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01: точный состав колонок с типами и nullability. Равенство, а не
# подмножество: тест падает и при лишней колонке, и при недостающей.
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "card_id": ("bigint", "NO"),
    "applicant_id": ("bigint", "NO"),
    "message": ("character varying", "NO"),
    "status": ("character varying", "NO"),
    "submitted_at": ("timestamp with time zone", "NO"),
    "decided_at": ("timestamp with time zone", "YES"),
    "expires_at": ("timestamp with time zone", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}

EXPECTED_CONSTRAINTS = (
    "pk_join_requests",
    "uq_join_requests_uuid",
    "ck_join_requests_status",
    "ck_join_requests_message",
    "fk_join_requests_card_id_event_cards",
    "fk_join_requests_applicant_id_users",
)

EXPECTED_INDEXES = (
    "uq_join_requests_card_applicant_active",
    "ix_join_requests_applicant_pending",
    "ix_join_requests_card_queue",
    # Стори 6.1 (AC-06, долг X-5a.3-1): индекс страницы «мои заявки». Добавлен
    # миграцией `0014`; в наборе, потому что отсутствие индекса превращает
    # каждую страницу и `total` в seq scan + sort.
    "ix_join_requests_applicant_visible",
)

STARTS_AT = datetime(2026, 12, 1, 19, 0, tzinfo=UTC)


_PHONE_SEQ = itertools.count(1)


def _next_phone() -> str:
    """Детерминированный уникальный телефон.

    Патч CR 5а.1: раньше номера строились как
    `f"+7900777{abs(hash(status)) % 10000:04d}"`. `hash()` строки зависит от
    `PYTHONHASHSEED`, поэтому номер менялся между прогонами и мог совпасть
    с фиксированным `+79007770001`; на `uq_users_phone` это дало бы
    `IntegrityError` в случайном прогоне (флаки-тест, AC-02).
    """
    return f"+7900{next(_PHONE_SEQ):07d}"


async def _create_user(session: AsyncSession, *, phone: str | None = None) -> int:
    """Создаёт учётку для FK `applicant_id`/`author_id` и возвращает её `id`."""
    phone = phone or _next_phone()
    result = await session.execute(
        text(
            "INSERT INTO users (provider, phone, city_slug) "
            "VALUES ('phone', :phone, 'tomsk') RETURNING id"
        ),
        {"phone": phone},
    )
    return int(result.scalar_one())


async def _create_place(session: AsyncSession) -> int:
    """Создаёт место: карточке нужен FK `place_id`."""
    result = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES ('Кафе „Прага“', 'cafe', 'пр. Ленина, 1', 'tomsk', true) RETURNING id"
        )
    )
    return int(result.scalar_one())


async def _create_card(
    session: AsyncSession, *, author_id: int, capacity: int = 1, status: str = "published"
) -> int:
    """Вставляет карточку сырым SQL: тест схемы не зависит от сервисов 4.x.

    `capacity=1` — режим заявок (4.1). `published_at` обязателен при
    `published` (`ck_event_cards_published`), `expires_at <= starts_at`.
    """
    place_id = await _create_place(session)
    result = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, category, "
            "starts_at, capacity, decision_window_minutes, expires_at, status, published_at,"
            "kind, activity) "
            "VALUES (:author_id, :place_id, 'tomsk', 'Кофе', 'cafe', :starts_at, :capacity, "
            "2880, :expires_at, :status, :published_at, 'company', 'other') RETURNING id"
        ),
        {
            "author_id": author_id,
            "place_id": place_id,
            "starts_at": STARTS_AT,
            "capacity": capacity,
            "expires_at": STARTS_AT - timedelta(hours=48),
            "status": status,
            "published_at": STARTS_AT if status == "published" else None,
        },
    )
    return int(result.scalar_one())


async def _insert_request(
    session: AsyncSession, *, card_id: int, applicant_id: int, **overrides: Any
) -> uuid_lib.UUID:
    """Вставляет заявку сырым SQL и возвращает её `uuid`."""
    values: dict[str, Any] = {
        "card_id": card_id,
        "applicant_id": applicant_id,
        "message": "Хочу присоединиться",
        "expires_at": STARTS_AT - timedelta(hours=48),
    }
    values.update(overrides)
    columns = tuple(values)
    placeholders = ", ".join(f":{name}" for name in columns)
    result = await session.execute(
        text(
            f"INSERT INTO join_requests ({', '.join(columns)}) "
            f"VALUES ({placeholders}) RETURNING uuid"
        ),
        values,
    )
    return uuid_lib.UUID(str(result.scalar_one()))


# --- AC-01: колонки и ограничения ------------------------------------------------


async def test_join_requests_columns_match_declared_schema(session: AsyncSession) -> None:
    """AC-01: состав колонок, типы и `NOT NULL` совпадают с §7 architecture.

    Равенство, а не подмножество: лишняя колонка (например, `decided_at`
    без nullability или координаты) ломает контракт таблицы. Краснеет,
    если в `0012` потерять `decided_at` или сделать `message` nullable.
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'join_requests'"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}

    assert actual == EXPECTED_COLUMNS

    # Патч CR 5а.1: длина `varchar` — часть контракта §7 (`message` 300,
    # `status` 16). Раньше `data_type` сравнивался без длины, и `status`
    # `varchar(64)` проходил ВСЕ тесты (доказано мутацией M2); ловил только
    # `alembic check`, то есть тестовый слой контракт не защищал.
    lengths = await session.execute(
        text(
            "SELECT column_name, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'join_requests' "
            "AND character_maximum_length IS NOT NULL"
        )
    )

    assert dict(lengths.all()) == {"message": 300, "status": 16}


async def test_id_is_identity_and_uuid_is_not(session: AsyncSession) -> None:
    """AC-01, D-2: `id` — `bigint identity`, `uuid` — публичный идентификатор.

    Краснеет, если `id` объявить как `serial`/`sequence` без `Identity`
    или если `uuid` станет `identity`. Внутренний `id` наружу не выходит,
    адресация — только по `uuid` (иначе нумерация позволила бы перебор).
    """
    result = await session.execute(
        text(
            "SELECT column_name, is_identity "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'join_requests' "
            "AND column_name IN ('id','uuid')"
        )
    )
    identity = {row[0]: row[1] for row in result}

    assert identity == {"id": "YES", "uuid": "NO"}


async def test_join_requests_constraint_names_match_model(session: AsyncSession) -> None:
    """AC-01…AC-03, §18.7: имена ограничений в БД совпадают с моделью.

    Краснеет, если миграция задаст CHECK без `op.f(...)`: convention
    сложится с именем и даст `ck_join_requests_ck_join_requests_status`,
    `alembic check` покажет расхождение, а `downgrade` упадёт на
    несуществующем имени (дефект 4.1).
    """
    # `contype` в ('c','f','p','u') — только объявленные контрактом ограничения.
    # PostgreSQL 18 добавляет неявные NOT NULL-записи с `contype='n'`; они
    # не часть контракта и не должны влиять на равенство.
    # РАВЕНСТВО, а не `<=` (патч CR 5а.1): при подмножестве лишний CHECK
    # (например, `ck_join_requests_decided` на `decided_at`, запрещённый
    # OQ-5a.1-1) проходил незамеченным — доказано мутацией O (36 зелёных).
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.join_requests'::regclass "
            "AND contype IN ('c','f','p','u')"
        )
    )
    names = {row[0] for row in result}

    assert names == set(EXPECTED_CONSTRAINTS)


async def test_decided_at_check_absent_in_database(session: AsyncSession) -> None:
    """OQ-5a.1-1: в БД НЕТ CHECK, связывающего `decided_at` и `status`.

    Патч CR 5а.1: решение «не ставить» проверялось только на модели, и CHECK,
    добавленный ТОЛЬКО в миграцию `0012`, проходил 36 тестов (доказано
    мутацией O). Теперь контракт проверяется на фактической схеме: любой
    CHECK, упоминающий `decided_at`, краснеет и заставляет обсудить класс
    дефекта X-39 (500 на легальном переходе при забытом поле).
    """
    result = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conrelid = 'public.join_requests'::regclass AND contype = 'c'"
        )
    )

    assert [row[0] for row in result if "decided_at" in row[1]] == []


async def test_join_requests_foreign_keys_use_cascade(session: AsyncSession) -> None:
    """AC-01: оба FK — `ON DELETE CASCADE`.

    Краснеет, если `card_id` сделать `RESTRICT`: удаление карточки падало бы
    на любой заявке, и автор не смог бы её убрать (FR100). Краснеет и при
    `SET NULL`: заявка без карточки не имеет смысла.
    """
    result = await session.execute(
        text(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE conrelid = 'public.join_requests'::regclass AND contype = 'f'"
        )
    )
    # `c` = CASCADE, `r` = RESTRICT, `n` = SET NULL (коды pg_constraint).
    # asyncpg отдаёт `"char"` как `bytes` — без декодирования сравнение
    # с `"c"` падает всегда (проверено прогоном).
    deletions = {row[0]: row[1].decode() if isinstance(row[1], bytes) else row[1] for row in result}

    assert deletions == {
        "fk_join_requests_card_id_event_cards": "c",
        "fk_join_requests_applicant_id_users": "c",
    }


# --- AC-02: статусы --------------------------------------------------------------


async def test_every_status_is_accepted_by_database(session: AsyncSession) -> None:
    """AC-02: все пять значений `JoinRequestStatus` принимаются схемой.

    Краснеет, если CHECK собран без значения из enum (например, без
    `expired`): воркер 5б не смог бы перевести просроченную заявку,
    и она навсегда заняла бы слот лимита (NFR29).
    """
    author_id = await _create_user(session)
    card_id = await _create_card(session, author_id=author_id)

    for status in ("pending", "selected", "not_selected", "expired", "cancelled"):
        # Патч CR 5а.1: номер из `hash(status)` зависел от `PYTHONHASHSEED`
        # и мог совпасть с уже занятым (`uq_users_phone`) — флаки-тест.
        applicant_id = await _create_user(session)
        await _insert_request(session, card_id=card_id, applicant_id=applicant_id, status=status)

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 5


async def test_unknown_status_is_rejected(session: AsyncSession) -> None:
    """AC-02: статус вне `JoinRequestStatus` отбивает `ck_join_requests_status`.

    Краснеет, если CHECK снять: произвольная строка попала бы в БД, и
    частичные индексы (`status = 'pending'`) её бы не видели — лимит 2
    и очередь автовыбора молча теряли бы заявки.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770002")
    card_id = await _create_card(session, author_id=author_id)

    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await _insert_request(
                session, card_id=card_id, applicant_id=applicant_id, status="approved"
            )

    assert "ck_join_requests_status" in str(excinfo.value)


# --- AC-03: сообщение ------------------------------------------------------------


async def test_message_with_leading_or_trailing_spaces_is_rejected(
    session: AsyncSession,
) -> None:
    """AC-03: `message` без пробельных по краям — CHECK, а не только сервис.

    Краснеет, если в `0012` заменить равенство на `<> ''`: `'  привет'`
    прошёл бы, и сервис 5а.2, обрезающий вход, считал бы его тем же
    сообщением (класс дефекта `places.name`, CR 3.1, патч 8).
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770003")
    card_id = await _create_card(session, author_id=author_id)

    for bad in (" привет", "привет ", "\tпривет"):
        with pytest.raises(IntegrityError) as excinfo:
            async with session.begin_nested():
                await _insert_request(
                    session, card_id=card_id, applicant_id=applicant_id, message=bad
                )

        assert "ck_join_requests_message" in str(excinfo.value)


async def test_all_whitespace_message_is_rejected(session: AsyncSession) -> None:
    """AC-03, D-5a.1-3: сообщение из одних пробелов отбивается CHECK.

    `message = btrim(message, <пробельные>)` косвенно отвергает любую
    строку из одних пробельных: `btrim(' ')` = `''`, и равенство не
    выполняется. Это следствие инварианта «в БД только обрезанное»,
    а не отдельный CHECK на пустоту.

    Краснеет, если заменить равенство на `<> ''`: `' '` прошёл бы, и
    заявка в очереди автора выглядела бы пустой.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770004")
    card_id = await _create_card(session, author_id=author_id)

    for bad in (" ", " ", "		"):
        with pytest.raises(IntegrityError) as excinfo:
            async with session.begin_nested():
                await _insert_request(
                    session, card_id=card_id, applicant_id=applicant_id, message=bad
                )

        assert "ck_join_requests_message" in str(excinfo.value)


async def test_empty_message_passes_check_and_is_guarded_by_api(
    session: AsyncSession,
) -> None:
    """AC-03, D-5a.1-3: ПУСТАЯ строка схемой не отбивается — это 5а.2.

    `btrim('')` = `''`, равенство выполняется. Пустоту обязан отбить
    `JoinRequestCreate` в 5а.2 (`min_length=1` после `normalize_text`),
    как `title` у `EventCardCreate` (D-4.1-1). Проверка на уровне CHECK
    дала бы `500` из воркера на легальной вставке.

    Краснеет, если кто-то добавит `CHECK (message <> '')`: тест зафиксирует
    смену контракта и заставит обсудить класс дефекта X-39.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770004")
    card_id = await _create_card(session, author_id=author_id)

    await _insert_request(session, card_id=card_id, applicant_id=applicant_id, message="")

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 1


async def test_message_of_nbsp_is_rejected(session: AsyncSession) -> None:
    """AC-03, CR 3.1 патч 8: NBSP входит в набор `btrim`.

    Краснеет, если `_WS` потеряет `\u00a0`: `btrim(x)` без второго аргумента
    режет только U+0020, и сообщение из одного NBSP прошло бы CHECK —
    заявка в очереди автора выглядела бы пустой.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770005")
    card_id = await _create_card(session, author_id=author_id)

    for bad in ("\u00a0текст", "\u202fтекст"):
        with pytest.raises(IntegrityError) as excinfo:
            async with session.begin_nested():
                await _insert_request(
                    session, card_id=card_id, applicant_id=applicant_id, message=bad
                )

        assert "ck_join_requests_message" in str(excinfo.value)


async def test_message_length_limit_is_300(session: AsyncSession) -> None:
    """AC-03: 300 символов проходят, 301 — `DBAPIError` (не `IntegrityError`).

    Краснеет, если `String(300)` заменить на `Text` без CHECK длины.
    Фиксируется и КЛАСС ошибки: переполнение `varchar` даёт `DBAPIError`,
    не `IntegrityError`, — 5а.2 обязан валидировать длину до вставки
    (иначе `500` вместо `422`, класс дефекта 3.1).
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770006")
    card_id = await _create_card(session, author_id=author_id)

    await _insert_request(session, card_id=card_id, applicant_id=applicant_id, message="я" * 300)

    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await _insert_request(
                session,
                card_id=card_id,
                applicant_id=applicant_id,
                message="я" * 301,
                status="cancelled",
            )


# --- AC-01: дефолты ---------------------------------------------------------------


async def test_status_and_submitted_at_have_server_defaults(session: AsyncSession) -> None:
    """AC-01, AC-02: без `status`/`submitted_at` строка получает дефолты.

    Краснеет, если `server_default` потерять: вставка из воркера/скрипта
    без этих колонок упала бы на `NOT NULL`, хотя новая заявка обязана
    быть `pending` и иметь момент подачи.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770007")
    card_id = await _create_card(session, author_id=author_id)

    result = await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, expires_at) "
            "VALUES (:card_id, :applicant_id, 'привет', :expires_at) "
            "RETURNING status, submitted_at, uuid, decided_at"
        ),
        {
            "card_id": card_id,
            "applicant_id": applicant_id,
            "expires_at": STARTS_AT,
        },
    )
    row = result.one()

    assert row[0] == "pending"
    assert row[1] is not None
    assert row[2] is not None
    assert row[3] is None


async def test_uuid_is_generated_by_database(session: AsyncSession) -> None:
    """AC-01: `uuid` приходит от БД (`gen_random_uuid()`).

    Краснеет, если `server_default` у `uuid` потерять: вставка сырым SQL
    без `uuid` упала бы на `NOT NULL`, хотя публичный идентификатор обязан
    выдаваться БД, а не только ORM.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770008")
    card_id = await _create_card(session, author_id=author_id)

    first = await _insert_request(session, card_id=card_id, applicant_id=applicant_id)
    second = await _insert_request(
        session,
        card_id=card_id,
        applicant_id=await _create_user(session, phone="+79007770009"),
    )

    assert first != second
    assert isinstance(first, uuid_lib.UUID)


# --- AC-01: FK ----------------------------------------------------------------


async def test_unknown_card_and_applicant_are_rejected(session: AsyncSession) -> None:
    """AC-01: FK не даёт сослаться на несуществующую карточку или учётку.

    Краснеет, если FK убрать: заявка на `card_id = 999999999` попала бы
    в БД, и список заявившихся (5а.4) падал бы при чтении карточки.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770010")
    card_id = await _create_card(session, author_id=author_id)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_request(session, card_id=999_999_999, applicant_id=applicant_id)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _insert_request(session, card_id=card_id, applicant_id=999_999_999)


async def test_deleting_card_cascades_to_requests(session: AsyncSession) -> None:
    """AC-01: удаление карточки уносит её заявки (CASCADE).

    Краснеет, если `ondelete` у `card_id` сделать `RESTRICT`: карточку
    нельзя было бы удалить, пока на ней висит хоть одна заявка.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770011")
    card_id = await _create_card(session, author_id=author_id)
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id)

    await session.execute(text("DELETE FROM event_cards WHERE id = :id"), {"id": card_id})

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 0


async def test_deleting_applicant_cascades_to_requests(session: AsyncSession) -> None:
    """AC-01: удаление учётки уносит её заявки (CASCADE, ПД).

    Краснеет, если `ondelete` у `applicant_id` сделать `RESTRICT`:
    удаление аккаунта падало бы на любой заявке, и требование удаления
    ПД (NFR1–NFR4) не выполнялось бы.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770012")
    card_id = await _create_card(session, author_id=author_id)
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id)

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": applicant_id})

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 0


# --- AC-04: частичный уникальный индекс ------------------------------------------


async def test_second_pending_request_for_same_pair_is_rejected(
    session: AsyncSession,
) -> None:
    """AC-04, FR77: вторая `pending` на ту же пару отбивается `uq`.

    Краснеет, если `unique=True` потерять: у пользователя оказалось бы
    две активные заявки на одну карточку, и автовыбор (FR77) выбирал бы
    из двух одинаковых.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770013")
    card_id = await _create_card(session, author_id=author_id)
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id)

    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await _insert_request(
                session, card_id=card_id, applicant_id=applicant_id, message="Ещё раз"
            )

    assert "uq_join_requests_card_applicant_active" in str(excinfo.value)


async def test_pending_and_selected_for_same_pair_are_rejected(
    session: AsyncSession,
) -> None:
    """AC-04: `pending` и `selected` на одну пару не сосуществуют.

    Краснеет, если предикат `uq` сузить до `status = 'pending'`: после
    выбора заявителя повторная заявка на ту же карточку прошла бы, и на
    одну карточку оказалось бы две активные роли.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770014")
    card_id = await _create_card(session, author_id=author_id)
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id)

    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await _insert_request(
                session,
                card_id=card_id,
                applicant_id=applicant_id,
                status="selected",
                message="Повторно",
            )

    assert "uq_join_requests_card_applicant_active" in str(excinfo.value)


async def test_request_after_cancellation_is_allowed(session: AsyncSession) -> None:
    """AC-04, FR82: после `cancelled` повторная подача проходит.

    Краснеет, если `uq` сделать БЕЗУСЛОВНЫМ (снять предикат): терминальная
    заявка навсегда занимала бы пару, и отмена до решения (FR82) стала бы
    необратимой — пользователь не смог бы подать заявку снова.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770015")
    card_id = await _create_card(session, author_id=author_id)
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id)

    await session.execute(
        text(
            "UPDATE join_requests SET status = 'cancelled' "
            "WHERE card_id = :card_id AND applicant_id = :applicant_id"
        ),
        {"card_id": card_id, "applicant_id": applicant_id},
    )

    await _insert_request(
        session, card_id=card_id, applicant_id=applicant_id, message="Подаю снова"
    )

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 2


async def test_request_after_expiry_is_allowed(session: AsyncSession) -> None:
    """AC-04, ADR-09: после `expired` повторная подача проходит.

    Краснеет, если `expired` попадёт в предикат `uq`: просроченная заявка
    заблокировала бы пару, хотя срок истёк без участия пользователя,
    и он не смог бы подать заявку на новую карточку того же автора.
    """
    author_id = await _create_user(session)
    applicant_id = await _create_user(session, phone="+79007770016")
    card_id = await _create_card(session, author_id=author_id)
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id)

    await session.execute(
        text(
            "UPDATE join_requests SET status = 'expired' "
            "WHERE card_id = :card_id AND applicant_id = :applicant_id"
        ),
        {"card_id": card_id, "applicant_id": applicant_id},
    )
    await _insert_request(session, card_id=card_id, applicant_id=applicant_id, message="Снова")

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 2


async def test_two_applicants_on_one_card_are_allowed(session: AsyncSession) -> None:
    """AC-04: `uq` — по паре, а не по карточке.

    Краснеет, если в индексе оставить только `card_id`: на карточку
    могла бы подать заявку ровно одна персона, и автовыбор (FR77) стал бы
    бессмысленным.
    """
    author_id = await _create_user(session)
    first = await _create_user(session, phone="+79007770017")
    second = await _create_user(session, phone="+79007770018")
    card_id = await _create_card(session, author_id=author_id)

    await _insert_request(session, card_id=card_id, applicant_id=first)
    await _insert_request(session, card_id=card_id, applicant_id=second)

    count = (await session.execute(text("SELECT count(*) FROM join_requests"))).scalar_one()
    assert count == 2


# --- AC-05, AC-07: индексы ---------------------------------------------------------


async def test_all_join_request_indexes_exist(session: AsyncSession) -> None:
    """AC-05: три индекса созданы миграцией `0012`.

    Краснеет, если индекс потерять: подсчёт лимита (FR77) и очередь
    автовыбора (ADR-05) начнут сканировать таблицу целиком.
    """
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'join_requests'"
        )
    )
    names = {row[0] for row in result}

    # РАВЕНСТВО, а не `<=` (патч CR 5а.1): при подмножестве лишний индекс
    # проходил незамеченным. Доказано мутацией L — `ix_join_requests_expires_at`
    # (запрещён OQ-5a.1-2) давал 28 зелёных тестов, ловил только `alembic check`.
    # В наборе — три индекса контракта AC-05 плюс PK и `uq`-uuid: они тоже
    # индексы PostgreSQL, и их отсутствие/переименование ломает адресацию по `uuid`.
    assert names == set(EXPECTED_INDEXES) | {"pk_join_requests", "uq_join_requests_uuid"}


# Патч CR 5а.1: колонки и предикаты индексов — контракт AC-05. Раньше тест
# проверял только НАЛИЧИЕ подстрок, и две мутации прошли ЗЕЛЁНЫМИ:
#   (A) `ix_join_requests_applicant_pending` по `card_id` вместо `applicant_id`
#       — подсчёт лимита 2 по заявителю (FR77/NFR29) не работает;
#   (I) предикат `ix_join_requests_applicant_pending` расширен до
#       `IN ('pending','expired')` — индекс индексирует терминальные строки.
EXPECTED_INDEX_COLUMNS: dict[str, tuple[str, ...]] = {
    "uq_join_requests_card_applicant_active": ("card_id", "applicant_id"),
    "ix_join_requests_applicant_pending": ("applicant_id",),
    "ix_join_requests_card_queue": ("card_id", "submitted_at", "id"),
}


def _parse_indexdef(indexdef: str) -> tuple[tuple[str, ...], str]:
    """Возвращает `(колонки, предикат)` из текста `pg_indexes.indexdef`.

    AC-05: колонки — из скобок после `USING btree`, предикат — хвост после
    ` WHERE ` (пустая строка, если индекс не частичный).
    """
    body = indexdef.split("USING btree ", 1)[1]
    columns_part, _, predicate = body.partition(" WHERE ")
    columns = tuple(part.strip() for part in columns_part.strip().strip("()").split(","))

    return columns, predicate


async def test_partial_index_predicates_are_exact(session: AsyncSession) -> None:
    """AC-05: предикаты частичных индексов совпадают с задуманными.

    Краснеет, если предикат `ix_join_requests_card_queue` расширить до
    `status IN (...)` или снять: индекс начнёт индексировать историю, и
    очередь автовыбора потянет в план все терминальные заявки.

    Сравнение нормализованное: PostgreSQL приводит предикат к виду
    `WHERE ((status)::text = 'pending'::text)`, и побайтовое сравнение
    с исходной строкой падало бы всегда (приём теста 4.1).
    """
    result = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname IN "
            "('uq_join_requests_card_applicant_active','ix_join_requests_applicant_pending',"
            "'ix_join_requests_card_queue')"
        )
    )
    defs = {row[0]: row[1] for row in result}

    assert set(defs) == set(EXPECTED_INDEX_COLUMNS), defs.keys()

    for name, expected_columns in EXPECTED_INDEX_COLUMNS.items():
        columns, predicate = _parse_indexdef(defs[name])

        assert columns == expected_columns, (name, columns)
        assert predicate, f"{name}: индекс обязан быть частичным"

        if name == "uq_join_requests_card_applicant_active":
            # Ровно два статуса, занимающих пару (D-5a.1-1, FR82).
            assert "'pending'" in predicate, (name, predicate)
            assert "'selected'" in predicate, (name, predicate)
            for terminal in ("not_selected", "expired", "cancelled"):
                assert terminal not in predicate, (name, predicate)
        else:
            # Только `status = 'pending'` (ADR-09): терминальные статусы
            # освобождают слот лимита и в индексе не участвуют.
            assert predicate.count("'pending'") == 1, (name, predicate)
            for other in ("selected", "not_selected", "expired", "cancelled"):
                assert other not in predicate, (name, predicate)

    assert "UNIQUE" in defs["uq_join_requests_card_applicant_active"].upper()
    assert "UNIQUE" not in defs["ix_join_requests_applicant_pending"].upper()
    assert "UNIQUE" not in defs["ix_join_requests_card_queue"].upper()


async def test_no_index_on_expires_at(session: AsyncSession) -> None:
    """OQ-5a.1-2: индекса `ix_join_requests_expires_at` НЕТ.

    Патч CR 5а.1: решение «не добавлять» не было закреплено тестом, и лишний
    индекс проходил все 27 тестов схемы (доказано мутацией L). Предикат
    `status='pending'` уже покрыт `ix_join_requests_card_queue`; отдельный
    индекс под `expires_at` добавляется в 5б только по замеру.
    """
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'join_requests'"
        )
    )

    assert [row[0] for row in result if "expires_at" in row[0]] == []


async def test_no_separate_index_for_not_selected(session: AsyncSession) -> None:
    """AC-07, FR81: отдельного индекса под `not_selected` нет.

    Краснеет, если добавить `ix_join_requests_not_selected`: отказ
    фильтруется в репозитории 5а.3 по паре с заявителем, а отдельный
    индекс под историю отказов не нужен (объём пилота — единицы строк).
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'join_requests'"
        )
    )

    assert not any("not_selected" in row[0] for row in result)


# --- AC-09, X-39: достижимость `full` одним UPDATE --------------------------------


async def test_full_status_is_reachable_in_single_update(session: AsyncSession) -> None:
    """AC-09, X-39, D-5a.1-4: `full` достигается ОДНИМ `UPDATE`.

    На карточке `capacity=1, filled_count=0, status='published'` один
    `UPDATE` с `filled_count + 1` и `status='full'` в одном `SET` проходит
    `ck_event_cards_full_capacity` из `0011`.

    Это и есть доказательство, что схема НЕ требует правки: чинится
    репозиторий (`build_mark_full_statement` делает CAS отдельным
    `UPDATE`), это — 5а.5. Краснеет, если в `0011` CHECK ослабят или
    заменят на односторонний: тогда тест перестал бы ловить регрессию
    согласованности.
    """
    author_id = await _create_user(session)
    card_id = await _create_card(session, author_id=author_id, capacity=1)

    async with session.begin_nested():
        # `CursorResult.rowcount` есть только у DML — приведение к нему
        # точнее, чем `Result` из общего `execute`.
        from sqlalchemy import CursorResult

        dml = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE event_cards SET filled_count = filled_count + 1, status = 'full' "
                    "WHERE id = :id AND status = 'published' AND filled_count < capacity"
                ),
                {"id": card_id},
            ),
        )

        assert dml.rowcount == 1

    row = (
        await session.execute(
            text("SELECT filled_count, capacity, status FROM event_cards WHERE id = :id"),
            {"id": card_id},
        )
    ).one()
    assert row == (1, 1, "full")


async def test_two_step_increment_is_rejected_by_check(session: AsyncSession) -> None:
    """AC-09, X-39: раздельный `UPDATE` только `filled_count` падает на CHECK.

    Тест документирует ПРИЧИНУ, по которой `build_mark_full_statement`
    обязан быть объединён в 5а.5: промежуточное состояние
    `published` + `filled_count = capacity` запрещено
    `ck_event_cards_full_capacity`, и отдельный CAS дал бы `IntegrityError`
    (в сервисе — `500` на легальном действии).

    §18.1: доказано откатом — если снять `status='full'` из 7.1, тест 7.1
    краснеет; здесь краснеет при ослаблении CHECK в `0011`.
    """
    author_id = await _create_user(session)
    card_id = await _create_card(session, author_id=author_id, capacity=1)

    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text("UPDATE event_cards SET filled_count = filled_count + 1 WHERE id = :id"),
                {"id": card_id},
            )

    assert "ck_event_cards_full_capacity" in str(excinfo.value)


# --- AC-10: границы ----------------------------------------------------------------


async def test_attendance_is_created_by_later_migration(session: AsyncSession) -> None:
    """AC-10: граница `0012` проверяется отсутствием ЕЁ объектов, не чужих.

    Исходная проверка «таблицы `attendance` нет» держалась на том, что стори
    5а.1 её не создаёт. Стори 7.1 создала `attendance` миграцией `0020` — и
    прежний ассерт стал бы ложным запретом на законную схему (тот же путь,
    которым 6.1 закрыла проверку для `chats`).

    Поэтому граница проверяется положительно: таблица ЕСТЬ — значит её
    создала более поздняя миграция, а не `0012`; и `0012` остаётся
    самодостаточной (откат `0012` не трогает `attendance`).
    """
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'attendance'"
        )
    )

    assert [row[0] for row in result] == ["attendance"]
