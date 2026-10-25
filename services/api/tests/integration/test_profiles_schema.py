"""Схема `profiles` (Task 7.1–7.9; AC-01, AC-02, AC-03, AC-06, AC-07).

Проверки идут через `information_schema` и `pg_indexes`, а не через модель:
модель описывает намерение, а AC говорит о фактической схеме. Тест на модели
прошёл бы и при расхождении миграции с моделью (D-8).

Реальный PostgreSQL, схема через Alembic (conftest).
"""

import os
import uuid as uuid_lib
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.profiles import ProfileRepository
from app.repositories.users import UserRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# AC-01: точный состав колонок. Равенство, а не подмножество: тест падает
# и при лишней колонке (`reliability_score`, D-9), и при недостающей.
# CR 2.1, патч 7: проверяется не только имя, но и тип с nullability.
# Раньше тест прошёл бы, если `gender` стал nullable, `age` — integer
# вместо smallint, а `is_hidden` потерял NOT NULL.
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "user_id": ("bigint", "NO"),
    "name": ("character varying", "YES"),
    "city": ("character varying", "YES"),
    "age": ("smallint", "YES"),
    "gender": ("character varying", "NO"),
    "bio": ("text", "YES"),
    "status": ("character varying", "NO"),
    "is_hidden": ("boolean", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}


async def _columns(session: AsyncSession, table: str) -> dict[str, tuple[str, str | None]]:
    """Фактические колонки таблицы из каталога, не из модели: `{имя: (тип, nullable)}`."""
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    )
    return {row[0]: (row[1], row[2]) for row in result}


async def _user(session: AsyncSession, **overrides: Any) -> Any:
    """Учётка с уникальным телефоном: тесты в одной транзакции.

    CR 2.1, Решение 1: пол обязателен. Составной FK `(user_id, gender)`
    ссылается на `users(id, gender)`, и учётка с `gender = NULL` не примет
    ни одну анкету — пара (id, NULL) не совпадает по `=`.
    """
    phone = overrides.pop("phone", f"+7901{uuid_lib.uuid4().int % 10**7:07d}")
    overrides.setdefault("gender", "female")
    return await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, **overrides
    )


async def _insert_profile(session: AsyncSession, user_id: int, **values: Any) -> uuid_lib.UUID:
    """Вставляет анкету сырым SQL: тест схемы не должен зависеть от ORM-модели."""
    params: dict[str, Any] = {"user_id": user_id, "gender": values.pop("gender", "female")}
    columns = ["user_id", "gender"]
    for column, value in values.items():
        columns.append(column)
        params[column] = value
    placeholders = ", ".join(f":{column}" for column in columns)
    result = await session.execute(
        text(f"INSERT INTO profiles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING uuid"),
        params,
    )
    return result.scalar_one()


# --- AC-01: состав таблиц ---


async def test_profiles_columns_exactly_as_specified(session: AsyncSession) -> None:
    """AC-01: набор колонок `profiles` равен ожидаемому.

    Падает и при лишней колонке (например `reliability_score`, D-9),
    и при недостающей.
    """
    assert await _columns(session, "profiles") == EXPECTED_COLUMNS


async def test_users_has_no_profile_columns(session: AsyncSession) -> None:
    """AC-01: анкетных полей в `users` не появилось.

    Проверяется `users`, а не модель: колонка, добавленная миграцией мимо
    модели, всё равно нарушает AC-01.
    """
    users_columns = set(await _columns(session, "users"))

    assert users_columns.isdisjoint({"city", "bio", "age", "is_hidden", "name"})


async def test_reliability_score_absent_everywhere(session: AsyncSession) -> None:
    """AC-01, D-9: `reliability_score` не создаётся ни в одной таблице.

    Запрос по каталогу, а не по модели: если колонка появится миграцией,
    но не попадёт в модель, тест на модели этого не увидит.
    """
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'reliability_score'"
        )
    )

    assert result.fetchall() == []


# --- AC-02: индекс и связи ---


async def test_city_gender_index_columns_in_order(session: AsyncSession) -> None:
    """AC-02, D-6: индекс `(city, gender)` существует, порядок колонок именно такой.

    Порядок важен: `(gender, city)` не покрывает запрос `WHERE city = ...`
    и дал бы полное сканирование.
    """
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'profiles' "
            "AND indexname = 'idx_profiles_city_gender'"
        )
    )
    row = result.first()

    assert row is not None, "индекс idx_profiles_city_gender отсутствует"
    assert "(city, gender)" in row[0]


async def test_foreign_key_is_composite_on_user_and_gender(session: AsyncSession) -> None:
    """CR 2.1, Решение 1: FK составной `(user_id, gender) -> users(id, gender)`.

    Раньше проверялось только имя старого простого FK. Здесь читаются и
    колонки источника, и колонки цели: простой FK на `users(id)` прошёл бы
    проверку имени, но не защитил бы от расхождения пола — на этом тест
    и должен краснеть.

    `confdeltype = b'c'` — ON DELETE CASCADE (AC-06) сохраняется в новом FK.
    """
    result = await session.execute(
        text(
            "SELECT conname, confdeltype, "
            "       (SELECT array_agg(a.attname ORDER BY a.attname) "
            "          FROM unnest(con.conkey) AS k(attnum) "
            "          JOIN pg_attribute a "
            "            ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS src, "
            "       (SELECT array_agg(a.attname ORDER BY a.attname) "
            "          FROM unnest(con.confkey) AS k(attnum) "
            "          JOIN pg_attribute a "
            "            ON a.attrelid = con.confrelid AND a.attnum = k.attnum) AS dst "
            "  FROM pg_constraint con "
            " WHERE con.conrelid = 'profiles'::regclass AND con.contype = 'f'"
        )
    )
    row = result.first()

    assert row is not None
    assert row[0] == "fk_profiles_user_gender_users"
    assert row[1] == b"c"
    # array_agg приходит списком; порядок — по имени колонки.
    assert row[2] == ["gender", "user_id"]
    assert row[3] == ["gender", "id"]


async def test_gender_mismatch_is_rejected(session: AsyncSession) -> None:
    """CR 2.1, Решение 1: `gender='male'` у женской учётки — IntegrityError.

    Это дефект, ради которого вводился составной FK: прежний простой FK
    проверял только существование учётки, и такая строка вставлялась —
    выборка эпика 4 показала бы однополые анкеты.
    """
    user = await _user(session, gender="female")

    with pytest.raises(IntegrityError):
        await _insert_profile(session, user.id, gender="male")
    await session.rollback()


async def test_gender_match_is_accepted(session: AsyncSession) -> None:
    """Позитивный контроль: совпадающий пол принимается.

    Без него предыдущий тест прошёл бы и при FK, отклоняющем вообще всё.
    """
    user = await _user(session, gender="male")

    profile_uuid = await _insert_profile(session, user.id, gender="male")

    result = await session.execute(
        text("SELECT gender FROM profiles WHERE uuid = :uuid"), {"uuid": profile_uuid}
    )
    assert result.scalar_one() == "male"


async def test_users_id_gender_unique_constraint_exists(session: AsyncSession) -> None:
    """CR 2.1, Решение 1: `uq_users_id_gender` создан — цель составного FK.

    Без этого ограничения PostgreSQL не даёт создать FK (42830). Имя задано
    через `op.f` (§18.7).
    """
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'users'::regclass AND contype = 'u' "
            "AND conname = 'uq_users_id_gender'"
        )
    )

    assert result.first() is not None


async def test_unique_constraints_present(session: AsyncSession) -> None:
    """AC-02: `uq_profiles_uuid` и `uq_profiles_user_id` существуют в БД."""
    result = await session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'profiles'::regclass AND contype = 'u'"
        )
    )
    names = {row[0] for row in result}

    assert names == {"uq_profiles_uuid", "uq_profiles_user_id"}


# --- AC-03: одна анкета на учётную запись ---


async def test_second_profile_for_same_user_is_rejected(session: AsyncSession) -> None:
    """AC-03: вторая анкета на того же пользователя отклоняется БД.

    Последовательная вставка, не гонка: конкурентных записей в 2.1 нет
    (создание анкеты — 2.3), проверяется само ограничение `uq_profiles_user_id`.
    """
    user = await _user(session)
    await _insert_profile(session, user.id, city="Томск")

    with pytest.raises(IntegrityError):
        await _insert_profile(session, user.id, city="Новосибирск")
    await session.rollback()


# --- AC-06: каскад в одну сторону ---


async def test_deleting_user_removes_profile(session: AsyncSession) -> None:
    """AC-06: удаление строки `users` удаляет анкету каскадом в БД.

    Удаление сырым SQL, а не `session.delete(user)`: ORM-cascade в модели
    не задан (D-6), и тест должен проверять правило БД, а не поведение ORM.
    """
    user = await _user(session)
    await _insert_profile(session, user.id, city="Томск")

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})

    remaining = await session.execute(
        text("SELECT count(*) FROM profiles WHERE user_id = :id"), {"id": user.id}
    )
    assert remaining.scalar_one() == 0


async def test_deleting_profile_keeps_user_and_tokens(session: AsyncSession) -> None:
    """AC-06: `DELETE FROM profiles` не трогает учётку и её refresh-токены.

    Обратное направление каскада — суть D-1: удаление анкеты не ломает вход.
    Токен создаётся прямым INSERT: `UserRepository.create` не выдаёт сессию.
    """
    user = await _user(session)
    await _insert_profile(session, user.id, city="Томск")
    await session.execute(
        text(
            "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) "
            "VALUES (:id, decode(repeat('ab', 32), 'hex'), now() + interval '30 days')"
        ),
        {"id": user.id},
    )

    await session.execute(text("DELETE FROM profiles WHERE user_id = :id"), {"id": user.id})

    users_left = await session.execute(
        text("SELECT count(*) FROM users WHERE id = :id"), {"id": user.id}
    )
    tokens_left = await session.execute(
        text("SELECT count(*) FROM refresh_tokens WHERE user_id = :id"), {"id": user.id}
    )
    assert users_left.scalar_one() == 1
    assert tokens_left.scalar_one() == 1


# --- AC-02, AC-07: CHECK-ограничения ---


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("gender", "other"),  # AC-02: пол только female/male
        ("age", 17),  # AC-02: нижняя граница
        ("age", 100),  # AC-02: верхняя граница
        ("city", ""),  # AC-07: пустой город
        ("city", "  Томск"),  # AC-07: пробелы по краям
        ("bio", ""),  # AC-02: пустое описание
        ("name", ""),  # AC-02: пустое имя
    ],
)
async def test_check_constraints_reject_invalid(
    session: AsyncSession, column: str, value: object
) -> None:
    """AC-02, AC-07: каждое недопустимое значение отклоняется БД.

    Вставка сырым SQL обязательна: через ORM значение могло бы не дойти
    до БД, и тест проверял бы валидацию Python, а не ограничение схемы.
    """
    user = await _user(session)

    with pytest.raises(IntegrityError):
        await _insert_profile(session, user.id, **{column: value})
    await session.rollback()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("name", "я" * 51),  # D-7: varchar(50)
        ("city", "я" * 101),  # AC-07: varchar(100)
        ("gender", "я" * 17),  # AC-02: varchar(16)
    ],
)
async def test_string_column_limits(session: AsyncSession, column: str, value: str) -> None:
    """AC-02, AC-07: превышение длины строки БД не принимает — проверка вставкой.

    CR 2.1, патч 6: прежняя версия сравнивала `character_maximum_length`
    с литералом из миграции, то есть переписывала объявление и не проверяла
    поведение. Теперь проверяется наблюдаемый результат.

    Патч 5: проверяется конкретный sqlstate `22001` (string_data_right_truncation).
    Класса `DataError` здесь недостаточно: SQLAlchemy 2.1 поверх asyncpg
    отдаёт этот сбой как голый `DBAPIError`, и `pytest.raises(DataError)`
    просто не срабатывает. Проверка sqlstate отличает усечение от `23514`
    (CHECK) и `23503` (FK) — иначе тест доказывал бы не тот механизм.
    """
    user = await _user(session)

    with pytest.raises(DBAPIError) as exc_info:
        await _insert_profile(session, user.id, **{column: value})

    assert exc_info.value.orig.sqlstate == "22001"
    await session.rollback()


@pytest.mark.parametrize("value", ["Томск\t", "Томск\n", "\tТомск", "Томск\x0b"])
async def test_whitespace_around_city_is_rejected(session: AsyncSession, value: str) -> None:
    """CR 2.1, патч 1: `btrim` режет таб, перевод строки и \v, а не только пробел.

    Прежний `btrim(city)` пропускал `city = E'Томск\t'`: строка проходила
    CHECK и навсегда выпадала из выборки `WHERE city = 'Томск'`.
    Проверяются все символы набора, каждая сторона строки.
    """
    user = await _user(session)

    with pytest.raises(IntegrityError):
        await _insert_profile(session, user.id, city=value)
    await session.rollback()


async def test_space_inside_city_is_accepted(session: AsyncSession) -> None:
    """Позитивный контроль: `Нижний Новгород` — валидный город.

    Без него ужесточение btrim прошло бы и при CHECK, запрещающем любой
    пробел внутри строки.
    """
    user = await _user(session)

    profile_uuid = await _insert_profile(
        session, user.id, name="Анна", city="Нижний Новгород", age=25, bio="Привет"
    )

    result = await session.execute(
        text("SELECT city FROM profiles WHERE uuid = :uuid"), {"uuid": profile_uuid}
    )
    assert result.scalar_one() == "Нижний Новгород"


async def test_valid_profile_is_accepted(session: AsyncSession) -> None:
    """AC-02, AC-07: допустимые значения вставляются без ошибок.

    Позитивный контроль к предыдущему тесту: он доказывает, что CHECK-и
    отклоняют именно недопустимое, а не любую вставку.
    """
    user = await _user(session)

    profile_uuid = await _insert_profile(
        session,
        user.id,
        name="Анна",
        city="Томск",
        age=25,
        gender="female",
        bio="Люблю горы и кофе",
    )

    result = await session.execute(
        text("SELECT name, city, age, gender FROM profiles WHERE uuid = :uuid"),
        {"uuid": profile_uuid},
    )
    assert result.one() == ("Анна", "Томск", 25, "female")


async def test_uuid_server_default_generates_value(session: AsyncSession) -> None:
    """AC-02: `uuid` генерируется БД, если не передан (вставки вне ORM).

    Проверяется server_default, а не Python default модели: INSERT сырым SQL
    без `uuid` должен получить значение, иначе внешняя вставка упадёт.
    """
    user = await _user(session)

    first = await _insert_profile(session, user.id)
    second_user = await _user(session)
    second = await _insert_profile(session, second_user.id)

    assert isinstance(first, uuid_lib.UUID)
    assert first != second


# --- CR 2.1, патч 14: признак полноты ---


async def test_complete_status_requires_all_fields(session: AsyncSession) -> None:
    """CR 2.1, патч 14: `status='complete'` с пустым городом — IntegrityError.

    Дефект, ради которого вводился признак: строка `city='Томск'` с пустыми
    `name`/`age`/`bio` объявлялась бы полной и рендерилась пустой карточкой
    в выборке эпика 4.
    """
    user = await _user(session)

    with pytest.raises(IntegrityError):
        await _insert_profile(session, user.id, city="Томск", status="complete")
    await session.rollback()


async def test_draft_status_allows_partial_fields(session: AsyncSession) -> None:
    """CR 2.1, патч 14: `draft` разрешает частичную анкету — онбординг пошаговый.

    Позитивный контроль к предыдущему тесту: связка «все поля NULL или все
    заполнены» запретила бы сохранить анкету с одним городом, а фото (2.2)
    приезжают раньше текста.
    """
    user = await _user(session)

    profile_uuid = await _insert_profile(session, user.id, city="Томск")

    result = await session.execute(
        text("SELECT status, city, name FROM profiles WHERE uuid = :uuid"),
        {"uuid": profile_uuid},
    )
    assert result.one() == ("draft", "Томск", None)


async def test_complete_status_with_all_fields_is_accepted(session: AsyncSession) -> None:
    """CR 2.1, патч 14: полностью заполненная анкета может стать `complete`.

    Без этого теста CHECK мог бы отклонять `complete` всегда, и предыдущий
    тест остался бы зелёным.
    """
    user = await _user(session)

    profile_uuid = await _insert_profile(
        session,
        user.id,
        name="Анна",
        city="Томск",
        age=25,
        bio="Горы",
        status="complete",
    )

    result = await session.execute(
        text("SELECT status FROM profiles WHERE uuid = :uuid"), {"uuid": profile_uuid}
    )
    assert result.scalar_one() == "complete"


async def test_unknown_status_is_rejected(session: AsyncSession) -> None:
    """CR 2.1, патч 14: `status` — закрытый набор, значение вне его отклоняется.

    Иначе опечатка `status='completed'` проходила бы, и фильтр выборки
    `status = 'complete'` молча терял бы анкету.
    """
    user = await _user(session)

    with pytest.raises(IntegrityError):
        await _insert_profile(session, user.id, status="completed")
    await session.rollback()


# --- CR 2.1, патч 10: ProfileRepository.create ---


async def test_repository_create_returns_flushed_profile(session: AsyncSession) -> None:
    """CR 2.1, патч 10 (D-10): `create` возвращает объект с заполненными дефолтами.

    Метод существует «для тестов и для 2.3», но не был написан вовсе.
    Проверяется, что после возврата доступны `id`, `uuid` и server-дефолты:
    без `flush` внутри метода они остались бы `None`, и сервис 2.3 получил бы
    объект, который нельзя сериализовать.
    """
    user = await _user(session)
    repository = ProfileRepository(session)

    profile = await repository.create(
        user_id=user.id, gender="female", name="Анна", city="Томск", age=25, bio="Горы"
    )

    assert profile.id is not None
    assert profile.uuid is not None
    assert profile.status == "draft"
    assert profile.is_hidden is False
    assert profile.city == "Томск"


async def test_repository_create_rejects_gender_mismatch(session: AsyncSession) -> None:
    """CR 2.1, патч 10: `create` не обходит составной FK.

    Метод — единственный путь записи для 2.3; если он молча принимал чужой
    пол, Решение 1 защищало бы только сырой SQL.
    """
    user = await _user(session, gender="female")
    repository = ProfileRepository(session)

    with pytest.raises(IntegrityError):
        await repository.create(user_id=user.id, gender="male")
    await session.rollback()


# --- CR 2.1, патч 11: populate_existing ---


async def test_get_by_user_id_reflects_update_in_same_session(session: AsyncSession) -> None:
    """CR 2.1, патч 11: чтение репозиторием отдаёт новое состояние, не снимок.

    Без `populate_existing=True` объект, уже лежащий в identity map, вернулся
    бы со старым городом: UPDATE в той же сессии не виден чтению через
    `select`. Тот же класс дефекта, что чинили в 1.6.
    """
    user = await _user(session)
    profile_uuid = await _insert_profile(
        session, user.id, name="Анна", city="Томск", age=25, bio="Горы"
    )
    repository = ProfileRepository(session)
    first = await repository.get_by_user_id(user.id)
    assert first is not None and first.city == "Томск"

    await session.execute(
        text("UPDATE profiles SET city = 'Новосибирск' WHERE uuid = :uuid"),
        {"uuid": profile_uuid},
    )
    second = await repository.get_by_user_id(user.id)

    assert second is not None
    assert second.city == "Новосибирск"


# --- Стори 2.3, Task 9.4: конкретный sqlstate, а не суперкласс -------------


async def test_completeness_violation_has_sqlstate_23514(session: AsyncSession) -> None:
    """Task 9.4 стори 2.3: нарушение CHECK даёт `23514`, а не любой `DBAPIError`.

    §18.2, находка 2.1: `pytest.raises(IntegrityError)` проходит и на нарушении
    уникальности (`23505`), и на нарушении внешнего ключа (`23503`). Тогда тест
    «CHECK полноты работает» остаётся зелёным, даже если сам CHECK удалён,
    а падает что-то другое. `sqlstate='23514'` (check_violation) —
    единственное доказательство, что сработало именно ограничение полноты.
    """
    user = await _user(session)

    with pytest.raises(IntegrityError) as exc_info:
        await _insert_profile(session, user.id, city="Томск", status="complete")

    assert exc_info.value.orig.sqlstate == "23514"
    await session.rollback()


async def test_city_whitespace_violation_has_sqlstate_23514(session: AsyncSession) -> None:
    """Task 9.3 стори 2.3 (AC-04): `city = E'Томск\\t'` — именно нарушение CHECK.

    Проверяется `23514`, а не суперкласс: `'Томск\\t'` укладывается в
    `varchar(100)`, поэтому усечения (`22001`) здесь быть не может, и любой
    другой sqlstate означал бы, что отбило не то ограничение, которое
    защищает AC-04.
    """
    user = await _user(session)

    with pytest.raises(IntegrityError) as exc_info:
        await _insert_profile(session, user.id, city="Томск\t")

    assert exc_info.value.orig.sqlstate == "23514"
    await session.rollback()
