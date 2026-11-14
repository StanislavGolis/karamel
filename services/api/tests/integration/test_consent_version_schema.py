"""Схема `users.consent_version` (стори 11.4; AC-1, AC-2, AC-4).

Проверки идут через `information_schema` и `pg_indexes`, а не через модель:
модель описывает намерение, а AC говорит о фактической схеме. Тест на модели
прошёл бы и при расхождении миграции `0034` с моделью (P-13).

Краснеет при:
- удалении `op.add_column` из `0034` (M2) — колонки не будет в каталоге;
- добавлении `UPDATE users SET consent_version = '1.0'` в `upgrade` (M5) —
  тест «строка до миграции осталась `NULL`» покраснеет.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid as uuid_lib
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [
    pytest.mark.integration,
    # Стори 13.2 (P-32): файл исполняет `alembic upgrade/downgrade`
    # сабпроцессом и откатывает ОБЩУЮ схему. Маркер обязателен: под xdist
    # хук `tests/integration/conftest.py` превращает его в `skip`, иначе
    # чужой воркер прочитает таблицу, которую мы откатили.
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]

_COLUMN_SQL = text(
    "SELECT data_type, character_maximum_length, is_nullable "
    "FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'users' "
    "AND column_name = 'consent_version'"
)


def _run_alembic(*args: str) -> None:
    """Запускает `python -m alembic <args>` отдельным процессом.

    Сабпроцесс, а не `alembic.command`: тот вызывает `asyncio.run()` внутри
    `env.py`, а тест уже работает в запущенном event loop (проверено прогоном:
    `RuntimeError: asyncio.run() cannot be called from a running event loop`).
    Сабпроцесс — тот же путь, каким пользуется человек и CI.

    `assert returncode == 0` обязателен: молча проглоченный ненулевой код
    вернул бы «откат прошёл», схема осталась бы нетронутой, и тест проверял
    бы пустоту.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n"
        f"stdout: {result.stdout.decode()[-2000:]}\n"
        f"stderr: {result.stderr.decode()[-2000:]}"
    )


async def _column_detached() -> tuple[str, int | None, str] | None:
    """Читает колонку в СВОЁМ соединении и закрывает его до возврата.

    Нужно тестам, которые запускают миграцию: фикстура `session` держала бы
    `ACCESS SHARE` на `users`, а `DROP COLUMN` требует `ACCESS EXCLUSIVE` и ждёт
    её вечно (проверено: первый прогон повис в `alembic downgrade 0033`).
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(_COLUMN_SQL)).first()
            if row is None:
                return None
            return str(row[0]), row[1], str(row[2])
    finally:
        await engine.dispose()


async def _insert_user_detached(phone: str) -> int:
    """Вставляет учётку в СХЕМУ БЕЗ `consent_version` (после `downgrade 0033`).

    Так воспроизводится состояние «строка существовала до миграции» —
    единственное, о котором говорит AC-2. Вставка в уже мигрированную схему
    ничего не доказывает: бэкфилл (мутация M5) её не затронет.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            row = await connection.execute(
                text(
                    "INSERT INTO users (uuid, provider, provider_user_id, phone, "
                    # Стори 12.1: вставка идёт ПОСЛЕ `downgrade 0033`, то есть на
                    # схеме БЕЗ переименования `0035` — там колонка называется
                    # `community_slug`. Имя взято по ревизии, а не по голове.
                    "role, status, age_confirmed, community_slug) "
                    "VALUES (gen_random_uuid(), 'phone', :p, :p, 'user', 'active', false, 'tomsk') "
                    "RETURNING id"
                ),
                {"p": phone},
            )
            return int(row.scalar_one())
    finally:
        await engine.dispose()


async def _delete_user_detached(user_id: int) -> None:
    """Удаляет строку, вставленную отдельным коммитом (CR 11.4).

    Без этого строка переживает откат транзакции фикстуры и копится прогонами:
    baseline `users=40008` из `W-11.2-d` перестал бы совпадать, а метрики пилота
    считали бы активных по сообществу.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
    finally:
        await engine.dispose()


async def _version_detached(user_id: int) -> str | None:
    """Читает `consent_version` строки в своём соединении."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = await connection.execute(
                text("SELECT consent_version FROM users WHERE id = :i"), {"i": user_id}
            )
            return row.scalar_one()
    finally:
        await engine.dispose()


async def _count_users() -> int:
    """Число строк `users` в своём соединении."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return int((await connection.execute(text("SELECT count(*) FROM users"))).scalar_one())
    finally:
        await engine.dispose()


async def test_consent_version_column_exists_with_expected_type(
    session: AsyncSession,
) -> None:
    """AC-1: `VARCHAR(16) NULL` в каталоге, а не в модели.

    Краснеет при удалении `op.add_column` (`None` вместо кортежа) и при смене
    типа/длины/nullability в миграции без правки AC.
    """
    row = (await session.execute(_COLUMN_SQL)).first()

    assert row is not None, "колонки users.consent_version нет: миграция 0034 не применилась"
    data_type, max_length, is_nullable = row[0], row[1], row[2]
    assert data_type == "character varying"
    assert max_length == 16
    assert is_nullable == "YES"


async def test_consent_version_has_no_index(session: AsyncSession) -> None:
    """AC-1: индекса на `consent_version` нет — выборок по ней в продукте нет.

    Краснеет, если кто-то добавит индекс «на всякий случай»: он стоил бы
    записи при каждом согласии, не ускоряя ни одного запроса.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'users'")
    )
    definitions = [str(row[0]) for row in result.all()]

    assert not [d for d in definitions if "consent_version" in d]


async def test_row_created_before_migration_keeps_null_version(
    session: AsyncSession,
) -> None:
    """AC-2: у строки, существовавшей ДО миграции, версия `NULL`, не `1.0`.

    **Почему тест делает откат и накат** (доказано прогоном): первая версия
    просто вставляла строку в уже мигрированную схему. Мутация M5 (бэкфилл
    `UPDATE users SET consent_version = '1.0'` в `upgrade`) осталась ЗЕЛЁНОЙ —
    миграция к тому моменту была применена (session-scoped фикстура), и `UPDATE`
    не выполнялся ни разу. AC-2 говорит именно о строках ДО миграции, поэтому
    строка вставляется между `downgrade` и `upgrade`.

    P-19: схема возвращается в исходное состояние (`upgrade head` в `finally`).
    """
    phone = f"+7902{uuid_lib.uuid4().int % 10**7:07d}"

    _run_alembic("downgrade", "0033")
    try:
        user_id = await _insert_user_detached(phone)
    finally:
        _run_alembic("upgrade", "head")

    try:
        # Чтение НАПРЯМУЮ из БД: `NULL` в сохранённой строке — единственное
        # доказательство, что бэкфилла не было.
        stored = await _version_detached(user_id)
        assert stored is None
    finally:
        # P-19, CR 11.4: убирается не только схема, но и ДАННЫЕ. Вставка идёт
        # отдельным коммитом (иначе DDL ждал бы блокировку), поэтому строка
        # пережила бы откат транзакции фикстуры и попала в baseline-счётчики
        # `W-11.2-d` (`users=40008`) — «+1 активный пользователь в Томске»
        # на каждый прогон.
        await _delete_user_detached(user_id)


async def test_downgrade_drops_column_and_keeps_rows(session: AsyncSession) -> None:
    """AC-4: `downgrade` удаляет ровно колонку, строки остаются.

    Проверяется настоящий откат через Alembic, а не чтение кода: `drop_column`
    в тексте ещё не значит, что миграция применяется.

    **Почему НЕ используется фикстура для чтения схемы** (доказано прогоном):
    открытая транзакция держит `ACCESS SHARE` на `users`, а `DROP COLUMN`
    требует `ACCESS EXCLUSIVE` — первый прогон повис на 4+ минуты в
    `alembic downgrade 0033`, пока процесс не был убит.

    **`commit()` блокировку НЕ снимает** (CR 11.4, P15): фикстура создана с
    `join_transaction_mode="create_savepoint"`, поэтому `commit()` освобождает
    только SAVEPOINT внутри внешней транзакции, а внешняя остаётся открытой.
    Здесь это безопасно ровно потому, что до `downgrade` через фикстуру НЕ
    выполняется ни одного запроса — блокировка не берётся. Добавление любого
    чтения через `session` до отката превратит тест в вечное ожидание.

    P-19: тест возвращает схему в исходное состояние (`upgrade head` в `finally`).
    """
    await session.commit()
    before = await _count_users()

    try:
        _run_alembic("downgrade", "0033")
        assert await _column_detached() is None
    finally:
        _run_alembic("upgrade", "head")

    # AC-4: данные не потеряны — число строк то же.
    after = await _count_users()
    assert after == before
    column = await _column_detached()
    assert column is not None
    assert column[1] == 16


async def test_validator_bound_matches_column_width(session: AsyncSession) -> None:
    """AC-1, AC-3 (CR 11.4, P12): ширина колонки == граница валидатора.

    Ширина `VARCHAR(16)` и граница `len(...) > 16` заданы ДВУМЯ литералами в
    двух файлах (`0034` и `config.py`), и гарда на их расхождение не было.

    Краснеет, если расширить формат версии (`1.10.3`) и поправить только
    валидатор: тогда значение пройдёт проверку и упадёт на `INSERT` с
    `StringDataRightTruncation` — то есть `500` в момент принятия согласия,
    когда пользователь на онбординге не может продолжить.
    """
    from app.core.config import Settings

    row = (await session.execute(_COLUMN_SQL)).first()
    assert row is not None
    column_width = int(row[1])

    # Максимальное допустимое значение формата `X.Y`, ровно в ширину колонки.
    accepted = "9" * (column_width - 2) + ".9"
    assert len(accepted) == column_width
    assert (
        Settings(_env_file=None, moderation_rules_version=accepted).moderation_rules_version
        == accepted
    )

    # Один символ сверх ширины обязан быть отвергнут валидатором, а не БД.
    too_long = accepted + "9"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, moderation_rules_version=too_long)


async def test_model_mirrors_migration(session: AsyncSession) -> None:
    """AC-1.3, P-13: модель объявляет то же поле, что и миграция.

    Краснеет, если поле убрать из `User` (мутация M1): `alembic check`
    предложит удалить колонку из БД. Здесь проверяется тип и nullable
    в самой модели — быстрый замок, а полноценный `alembic check` живёт
    в `test_alembic_revisions.py`.
    """
    from app.models.user import User

    column = User.__table__.columns["consent_version"]

    assert column.nullable is True
    assert column.type.length == 16
