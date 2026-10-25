"""Схема `profile_photos` (Task 9.5; AC-01).

Проверки идут через `information_schema` и `pg_indexes`, а не через модель:
модель описывает намерение, а AC говорит о фактической схеме. Тест на модели
прошёл бы и при расхождении миграции `0006` с моделью.

Реальный PostgreSQL, схема через Alembic (conftest).

§18.1: каждое ограничение проверяется НАРУШЕНИЕМ, а не чтением каталога:
наличие `CHECK` в `pg_constraint` не доказывает, что он что-то запрещает.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.users import UserRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01: точный состав колонок с типами и nullability. Равенство, а не
# подмножество: тест падает и при лишней колонке, и при недостающей.
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "profile_id": ("bigint", "NO"),
    "storage_key": ("character varying", "NO"),
    "position": ("smallint", "NO"),
    "status": ("character varying", "NO"),
    "rejection_reason": ("text", "YES"),
    "moderated_at": ("timestamp with time zone", "YES"),
    "content_type": ("character varying", "NO"),
    "size_bytes": ("integer", "NO"),
    "consent_given_at": ("timestamp with time zone", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}


async def _columns(session: AsyncSession, table: str) -> dict[str, tuple[str, str | None]]:
    """Фактические колонки таблицы из каталога, не из модели."""
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    )
    return {row[0]: (row[1], row[2]) for row in result}


async def _user(session: AsyncSession) -> Any:
    """Учётка с уникальным телефоном: тесты живут в одной транзакции."""
    phone = f"+7903{uuid_lib.uuid4().int % 10**7:07d}"
    return await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, gender="female"
    )


async def _profile(session: AsyncSession, user_id: int) -> int:
    """Вставляет анкету-черновик сырым SQL и возвращает её внутренний `id`."""
    result = await session.execute(
        text(
            "INSERT INTO profiles (user_id, gender, status) "
            "VALUES (:user_id, 'female', 'draft') RETURNING id"
        ),
        {"user_id": user_id},
    )
    profile_id: int = result.scalar_one()
    return profile_id


async def _insert_photo(session: AsyncSession, profile_id: int, **values: Any) -> uuid_lib.UUID:
    """Вставляет фото сырым SQL: тест схемы не должен зависеть от ORM-модели.

    Дефолты подобраны так, чтобы строка проходила все CHECK-и; тест меняет
    ровно то поле, ограничение которого проверяет.
    """
    params: dict[str, Any] = {
        "profile_id": profile_id,
        "storage_key": values.pop("storage_key", f"photos/x/{uuid_lib.uuid4()}.jpg"),
        "position": values.pop("position", 1),
        "content_type": values.pop("content_type", "image/jpeg"),
        "size_bytes": values.pop("size_bytes", 1024),
        "consent_given_at": values.pop("consent_given_at", datetime.now(UTC)),
    }
    columns = list(params)
    columns.extend(values)
    params.update(values)
    placeholders = ", ".join(f":{column}" for column in columns)
    result = await session.execute(
        text(
            f"INSERT INTO profile_photos ({', '.join(columns)}) "
            f"VALUES ({placeholders}) RETURNING uuid"
        ),
        params,
    )
    inserted: uuid_lib.UUID = result.scalar_one()
    return inserted


# --- AC-01: колонки --------------------------------------------------------------


async def test_columns_match_expected(session: AsyncSession) -> None:
    """AC-01: состав колонок, типы и nullability — ровно как в стори.

    Служебные `content_type`, `size_bytes`, `consent_given_at` входят в набор
    осознанно (D-1, D-4): без них не выполнить AC-05 и AC-07.
    """
    columns = await _columns(session, "profile_photos")

    assert {name: (kind, nullable) for name, (kind, nullable) in columns.items()} == (
        EXPECTED_COLUMNS
    )


# --- AC-01: уникальность позиции -------------------------------------------------


async def test_position_unique_per_profile(session: AsyncSession) -> None:
    """AC-01, D-12: `uq(profile_id, position)` нарушается второй вставкой.

    Ограничение — часть лимита «не больше трёх фото» и защита от гонки:
    без него две загрузки встали бы на одну позицию и порядок показа
    стал бы неопределённым.
    """
    user = await _user(session)
    profile_id = await _profile(session, user.id)
    await _insert_photo(session, profile_id, position=1)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, position=1)

    await session.rollback()


async def test_same_position_allowed_for_different_profiles(session: AsyncSession) -> None:
    """AC-01: уникальность именно парная — разные анкеты имеют позицию 1.

    Без этой проверки глобальный UNIQUE по `position`
    (`uq(position)` вместо `uq(profile_id, position)`) остался бы незамеченным:
    первое фото смогла бы загрузить только одна анкета в системе.
    """
    first = await _profile(session, (await _user(session)).id)
    second = await _profile(session, (await _user(session)).id)

    await _insert_photo(session, first, position=1)
    await _insert_photo(session, second, position=1)  # не должно падать


# --- AC-01: частичный индекс очереди модерации -----------------------------------


async def test_pending_partial_index_exists(session: AsyncSession) -> None:
    """AC-01: частичный индекс `WHERE status = 'pending'` существует.

    project-context §7 называет его обязательным для p95 очереди модерации:
    без него выборка «все pending» идёт seq scan по всей таблице.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = 'profile_photos'")
    )
    definitions = [row[0] for row in result]

    matching = [d for d in definitions if "idx_profile_photos_status_pending" in d]
    assert len(matching) == 1, definitions
    assert "WHERE" in matching[0]
    assert "'pending'" in matching[0]


# --- AC-06: каскад удаления ------------------------------------------------------


async def test_delete_user_cascades_to_photos(session: AsyncSession) -> None:
    """AC-01: удаление учётки удаляет фото каскадом в БД.

    Доказательство откатом: строка вставляется, затем удаляется `users`,
    и фото не находится. Без `ON DELETE CASCADE` `DELETE` упал бы с
    `ForeignKeyViolation`, а сиротские строки остались бы в таблице.
    """
    user = await _user(session)
    profile_id = await _profile(session, user.id)
    await _insert_photo(session, profile_id)

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    left = await session.execute(
        text("SELECT count(*) FROM profile_photos WHERE profile_id = :pid"),
        {"pid": profile_id},
    )

    assert left.scalar_one() == 0
    await session.rollback()


# --- AC-01, AC-05: CHECK-и нарушением --------------------------------------------


@pytest.mark.parametrize("position", [0, 4, -1])
async def test_position_out_of_range_rejected(session: AsyncSession, position: int) -> None:
    """AC-01, D-12: позиция вне 1..3 нарушает `ck_profile_photos_position`.

    Проверяются обе границы и отрицательное значение: `BETWEEN 1 AND 3`
    отбивает все три случая, а `position > 0` пропустил бы `4`.
    """
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, position=position)

    await session.rollback()


async def test_unknown_status_rejected(session: AsyncSession) -> None:
    """AC-01: статус вне {pending, approved, rejected} не принимается."""
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, status="moderated")

    await session.rollback()


async def test_approved_without_moderated_at_rejected(session: AsyncSession) -> None:
    """AC-01: `approved` без `moderated_at` неотличим от дефолта — запрещён.

    Без этого CHECK решение модератора нельзя было бы отличить от строки,
    которую просто забыли обработать.
    """
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, status="approved")

    await session.rollback()


async def test_rejected_requires_reason(session: AsyncSession) -> None:
    """AC-01: `rejected` без `rejection_reason` запрещён.

    Отказ без причины пользователь не сможет исправить, а модератор —
    объяснить.
    """
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises(IntegrityError):
        await _insert_photo(
            session,
            profile_id,
            status="rejected",
            moderated_at=datetime.now(UTC),
        )

    await session.rollback()


@pytest.mark.parametrize("content_type", ["image/gif", "image/heic", "application/pdf"])
async def test_unsupported_content_type_rejected(session: AsyncSession, content_type: str) -> None:
    """AC-05, D-5: MIME вне allowlist запрещён на уровне БД.

    Сервис уже отбивает HEIC/GIF/PDF, но CHECK — вторая линия: строка из
    воркера модерации или миграции не должна обойти allowlist.
    """
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, content_type=content_type)

    await session.rollback()


@pytest.mark.parametrize("size_bytes", [0, -1])
async def test_non_positive_size_rejected(session: AsyncSession, size_bytes: int) -> None:
    """AC-05: нулевой и отрицательный размер запрещены."""
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, size_bytes=size_bytes)

    await session.rollback()


async def test_blank_storage_key_rejected(session: AsyncSession) -> None:
    """D-3: пустой ключ и ключ с пробелами по краям запрещены.

    Пробельный символ по краю сделал бы объект недостижимым по ключу из БД:
    подпись считалась бы по одному значению, а объект лежал бы под другим.
    """
    profile_id = await _profile(session, (await _user(session)).id)

    for key in ("", "  ", "photos/x/y.jpg\t"):
        with pytest.raises(IntegrityError):
            await _insert_photo(session, profile_id, storage_key=key)
        await session.rollback()


async def test_storage_key_unique(session: AsyncSession) -> None:
    """D-3: один ключ — одно фото, повторное использование запрещено."""
    profile_id = await _profile(session, (await _user(session)).id)
    key = f"photos/x/{uuid_lib.uuid4()}.jpg"
    await _insert_photo(session, profile_id, storage_key=key, position=1)

    with pytest.raises(IntegrityError):
        await _insert_photo(session, profile_id, storage_key=key, position=2)

    await session.rollback()


async def test_default_status_is_pending(session: AsyncSession) -> None:
    """AC-01, AC-02: без явного статуса строка создаётся `pending`.

    AC-02 требует, чтобы загруженное фото попадало в очередь модерации;
    дефолт в БД — последняя линия, если сервис забудет поле.
    """
    profile_id = await _profile(session, (await _user(session)).id)
    await _insert_photo(session, profile_id)

    result = await session.execute(
        text("SELECT status FROM profile_photos WHERE profile_id = :pid"),
        {"pid": profile_id},
    )

    assert result.scalar_one() == "pending"


async def test_consent_given_at_is_not_null(session: AsyncSession) -> None:
    """AC-07, D-4: фото без времени согласия физически не создаётся.

    `NULL` в `consent_given_at` означал бы «согласие неизвестно», и фото
    нельзя было бы показывать ни при каком статусе модерации.
    """
    profile_id = await _profile(session, (await _user(session)).id)

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text(
                "INSERT INTO profile_photos "
                "(profile_id, storage_key, position, content_type, size_bytes) "
                "VALUES (:pid, :key, 1, 'image/jpeg', 1024)"
            ),
            {"pid": profile_id, "key": f"photos/x/{uuid_lib.uuid4()}.jpg"},
        )

    await session.rollback()
