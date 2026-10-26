"""Схема `notification_mutes` (стори 9.3; AC-01, AC-02; D-9.3-5).

§18.1: каждое ограничение проверяется НАРУШЕНИЕМ, а не чтением каталога.
Наличие `CHECK` в `pg_constraint` не доказывает, что он что-то запрещает:
формулировка, которая всегда даёт `NULL`, тоже была бы строкой в каталоге.
Каталог используется только для ИМЁН (§18.7) и состава PK.

§18.8: каждый ожидаемый отказ — в своём `begin_nested()`: после
`IntegrityError` транзакция аборчена, и следующая проверка была бы про её
состояние, а не про ограничение.

**Три источника перечня типов.** Python (`NOTIFICATION_TYPES`), литерал
`0030` (`notifications.type`) и литерал `0031` (`notification_mutes.type`).
Расхождение между ними не заметно ни клиенту (NFR60 велит игнорировать
неизвестный тип), ни пользователю — поэтому сверка живёт тестом (риск 3 стори).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NOTIFICATION_TYPES, REQUIRED_NOTIFICATION_TYPES

API_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _load_literal(module_path: Path, name: str) -> tuple[str, ...]:
    """Читает кортеж-литерал из миграции без импорта `app`.

    Миграция обязана быть воспроизводимой без приложения, поэтому импортировать
    её модуль нельзя (он тянет `alembic.context`). Разбор исходника — тот же
    приём, что в `test_notifications_support_schema.py` для `0030`.
    """
    source = module_path.read_text(encoding="utf-8")
    start = source.index(f"{name}: tuple[str, ...] = (")
    end = source.index(")\n", start)
    body = source[start:end]
    return tuple(
        part.strip().strip('"') for part in body.split("(", 1)[1].split(",") if part.strip()
    )


async def _make_user(session: AsyncSession) -> int:
    phone = f"+7913{uuid_lib.uuid4().int % 10**7:07d}"
    row = await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
            "community_slug) "
            "VALUES ('phone', :p, :p, 'user', 'female', 'active', 'tomsk') "
            "RETURNING id"
        ),
        {"p": phone},
    )
    return int(row.scalar_one())


def test_python_and_both_migrations_agree_on_types() -> None:
    """Риск 3: три источника перечня совпадают ПОЭЛЕМЕНТНО и по порядку.

    Краснеет, если тип добавлен в Python, но забыт в `0031` (тогда отключить
    его нельзя — `CHECK` отвергнет вставку), или наоборот (тогда в БД можно
    записать тип, которого приложение не знает).
    """
    python_types = tuple(str(t) for t in NOTIFICATION_TYPES)
    mutes_literal = _load_literal(
        API_ROOT / "alembic" / "versions" / "0031_create_notification_mutes.py",
        "NOTIFICATION_TYPES",
    )
    assert mutes_literal == python_types


def test_required_types_agree_between_python_and_migration() -> None:
    """D-9.3-5: перечень неотключаемых совпадает в Python и в миграции `0031`.

    Расхождение сломало бы двойную защиту: API отдавал бы `409`, а схема
    пропускала бы строку (или наоборот — API разрешал бы то, что схема
    отвергает `IntegrityError`-ом вместо `409`).
    """
    python_required = tuple(str(t) for t in REQUIRED_NOTIFICATION_TYPES)
    migration_required = _load_literal(
        API_ROOT / "alembic" / "versions" / "0031_create_notification_mutes.py",
        "REQUIRED_NOTIFICATION_TYPES",
    )
    assert migration_required == python_required


async def test_required_types_cannot_be_muted_by_schema(session: AsyncSession) -> None:
    """AC-01, D-9.3-5, M12: `CHECK` отвергает отключение неотключаемого типа.

    Прямая вставка мимо API — единственный способ обойти гард `409`, и именно
    её закрывает схема. Без `ck_notification_mutes_required` санкция и решение
    по жалобе стали бы отключаемыми через ручной SQL, а FR38/FR39 —
    невыполнимыми по воле пользователя.
    """
    user_id = await _make_user(session)
    for required_type in REQUIRED_NOTIFICATION_TYPES:
        # §18.8: каждый отказ — в своём savepoint. После `IntegrityError`
        # транзакция аборчена, и следующая проверка была бы про её состояние.
        with pytest.raises(IntegrityError) as exc:
            async with session.begin_nested():
                await session.execute(
                    text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
                    {"u": user_id, "t": str(required_type)},
                )
        assert "ck_notification_mutes_required" in str(exc.value)


async def test_unknown_type_rejected_by_schema(session: AsyncSession) -> None:
    """AC-01, NFR60: тип вне перечня 11 отвергается БД, не только API.

    Краснеет, если `ck_notification_mutes_type` снять: строка с опечаткой
    (`foo.bar` валиден по форме) создала бы настройку, которую приложение
    никогда не прочитает — и пользователь не увидел бы причины, почему
    настройка «не работает».
    """
    user_id = await _make_user(session)
    for bad_type in ("foo.bar", "request.recevied", "REQUEST.RECEIVED"):
        with pytest.raises(IntegrityError) as exc:
            async with session.begin_nested():
                await session.execute(
                    text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
                    {"u": user_id, "t": bad_type},
                )
        assert "ck_notification_mutes_type" in str(exc.value)


async def test_duplicate_mute_rejected_by_primary_key(session: AsyncSession) -> None:
    """AC-01, D-9.3-12: PK `(user_id, type)` не даёт отключить тип дважды.

    Idempotency в репозитории держится на `ON CONFLICT DO NOTHING`, и он
    возможен ТОЛЬКО потому, что есть PK. Без него повторный `PATCH` создал бы
    вторую строку, и `DELETE` при включении убрал бы одну из двух.
    """
    user_id = await _make_user(session)
    await session.execute(
        text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, 'report.created')"),
        {"u": user_id},
    )
    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO notification_mutes (user_id, type) VALUES (:u, 'report.created')"
                ),
                {"u": user_id},
            )
    assert "pk_notification_mutes" in str(exc.value)


async def test_user_delete_cascades_mutes(session: AsyncSession) -> None:
    """AC-01, ADR-07: удаление учётки уносит её настройки (`ON DELETE CASCADE`).

    Проверяется ФАКТИЧЕСКИМ удалением в savepoint: `confdeltype` в каталоге
    доказывает только объявление правила, а не то, что PostgreSQL его применил.
    """
    user_id = await _make_user(session)
    await session.execute(
        text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, 'report.created')"),
        {"u": user_id},
    )
    await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    left = await session.execute(
        text("SELECT count(*) FROM notification_mutes WHERE user_id = :u"), {"u": user_id}
    )
    assert int(left.scalar_one()) == 0


async def test_created_at_filled_by_server_default(session: AsyncSession) -> None:
    """AC-01: `created_at` заполняется БД — вставка без него проходит.

    Краснеет, если `server_default` снять: репозиторий пишет строку без
    `created_at` (он не знает времени клиента), и вставка падала бы
    `NotNullViolation` на первом же отключении.
    """
    user_id = await _make_user(session)
    await session.execute(
        text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, 'report.created')"),
        {"u": user_id},
    )
    row = await session.execute(
        text("SELECT created_at FROM notification_mutes WHERE user_id = :u"), {"u": user_id}
    )
    assert row.scalar_one() is not None


async def test_primary_key_columns_and_order(session: AsyncSession) -> None:
    """AC-01, Task 1.3: PK — `(user_id, type)` в этом порядке, без отдельного индекса.

    Порядок колонок входит в условие «PK покрывает gate-предикат»: B-tree
    обслуживает равенство по обоим столбцам в любом порядке, но проверка
    порядка фиксирует само решение, а не случайный результат autogenerate.

    Отдельный индекс `ix_notification_mutes_*` не создаётся: он дублировал бы
    PK (см. докстринг миграции). Тест ловит его появление.
    """
    columns = await session.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            " WHERE i.indrelid = 'public.notification_mutes'::regclass AND i.indisprimary "
            " ORDER BY array_position(i.indkey, a.attnum)"
        )
    )
    assert tuple(r[0] for r in columns.all()) == ("user_id", "type")

    indexes = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            " WHERE tablename = 'notification_mutes' AND indexname LIKE 'ix_%'"
        )
    )
    assert indexes.all() == []
