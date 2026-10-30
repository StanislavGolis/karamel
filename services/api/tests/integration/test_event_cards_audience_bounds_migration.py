"""Схема рамок аудитории `event_cards` (стори 12.6; AC-1, AC-1a, AC-1b).

Проверки идут через `information_schema`, `pg_constraint` и ФАКТИЧЕСКУЮ
вставку/обновление, а не через модель: модель описывает намерение, а AC
говорит о фактической схеме. Тест на модели прошёл бы и при расхождении
миграции с моделью (урок 12.2).

**Пара утверждений вместо одного (P-21).** CHECK `age_min <= age_max` можно
заменить на `CHECK (TRUE)` — и тест «40/30 падает» останется зелёным, если
рядом нет теста «30/NULL ПРОХОДИТ». Полуоткрытые пары легальны (AC-1a),
и именно вторая половина пары отличает настоящий CHECK от заглушки.

Краснеет при:
- удалении `op.add_column` (U-1) — колонок не будет в каталоге;
- замене `<=` на `<` в CHECK — пара `30/30` перестанет вставляться;
- добавлении `IS NOT NULL` на границы — полуоткрытая пара перестанет
  вставляться (мутация U2);
- удалении `gender_filter IS NULL` из CHECK — существующие карточки
  с `NULL` перестанут обновляться (мутация U3);
- удалении `op.drop_column` из `downgrade` — откат оставит колонки.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid as uuid_lib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import HEAD_REVISION

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]

#: Уникальный хвост на прогон: тест может упасть до уборки, и следующий запуск
#: не должен упираться в уникальность телефона/названия места.
_SUFFIX = uuid_lib.uuid4().hex[:10]


async def _fetch(sql: str, **params: Any) -> list[tuple]:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


def _run_alembic(*args: str) -> None:
    """`python -m alembic <args>` отдельным процессом.

    Сабпроцесс, а не `alembic.command`: тот вызывает `asyncio.run()` внутри
    `env.py`, а тест уже работает в запущенном event loop.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n{result.stderr}"
    )


def _apply_migration() -> None:
    """Ставит схему на голову **с отката** (урок 12.1).

    `upgrade head` на голове — no-op: тест зеленел бы на схеме, которую
    миграция не трогала. Откат на `0037` гарантирует, что `upgrade`
    действительно исполнится.
    """
    _run_alembic("downgrade", "0037")
    _run_alembic("upgrade", "head")


async def _make_card() -> tuple[int, int, int]:
    """Создаёт пользователя, место и карточку отдельными коммитами.

    Возвращает `(user_id, place_id, card_id)`. Карточка нужна как объект
    для `UPDATE` колонок рамок: CHECK срабатывает и на `UPDATE`, поэтому
    вставлять карточку заново под каждую пару значений не нужно.
    """
    from app.core.config import settings

    suffix = uuid_lib.uuid4().hex[:10]
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            user_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO users (provider, provider_user_id, phone, role, "
                            "gender, status, city_slug) VALUES ('phone', :pid, :phone, 'user', "
                            "'female', 'active', :slug) RETURNING id"
                        ),
                        {"pid": phone, "phone": phone, "slug": settings.default_city_slug},
                    )
                ).scalar_one()
            )
            place_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO places (name, category, address_text, city_slug, "
                            "is_active) VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) "
                            "RETURNING id"
                        ),
                        {"name": f"Кафе {suffix}", "slug": settings.default_city_slug},
                    )
                ).scalar_one()
            )
            card_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO event_cards (author_id, place_id, city_slug, title, "
                            "description, category, starts_at, capacity, "
                            "decision_window_minutes, expires_at, status, filled_count, "
                            "verification_status, kind, activity) VALUES (:author, :place, "
                            ":slug, 'Встреча', NULL, 'cafe', now() + interval '3 days', 1, 60, "
                            "now() + interval '1 hour', 'draft', 0, 'pending', 'company', "
                            "'coffee') RETURNING id"
                        ),
                        {
                            "author": user_id,
                            "place": place_id,
                            "slug": settings.default_city_slug,
                        },
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()
    return user_id, place_id, card_id


async def _drop_card(user_id: int, place_id: int) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM event_cards WHERE author_id = :author"),
                {"author": user_id},
            )
            await connection.execute(
                text("DELETE FROM places WHERE id = :id"), {"id": place_id}
            )
            await connection.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": user_id}
            )
    finally:
        await engine.dispose()


async def _set_bounds(card_id: int, **values: Any) -> None:
    """`UPDATE` трёх колонок рамок — здесь и срабатывает CHECK."""
    sets = ", ".join(f"{column} = :{column}" for column in values)
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE event_cards SET {sets} WHERE id = :card"),
                {**values, "card": card_id},
            )
    finally:
        await engine.dispose()


# ——— AC-1: колонки ———————————————————————————————————————————————————————


async def test_three_nullable_columns_exist() -> None:
    """AC-1: три колонки, все NULL-able и без `server_default`."""
    _apply_migration()
    rows = await _fetch(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'event_cards' "
        "AND column_name IN ('gender_filter', 'age_min', 'age_max') "
        "ORDER BY column_name"
    )
    assert rows == [
        ("age_max", "smallint", "YES", None),
        ("age_min", "smallint", "YES", None),
        ("gender_filter", "character varying", "YES", None),
    ], f"ожидались три NULL-able колонки без дефолта, получено {rows}"


async def test_checks_exist_in_catalog() -> None:
    """AC-1a/AC-1b: оба CHECK объявлены с полными именами по конвенции."""
    _apply_migration()
    rows = await _fetch(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'event_cards'::regclass "
        "AND contype = 'c' AND conname IN "
        "('ck_event_cards_age_range', 'ck_event_cards_gender_filter') ORDER BY conname"
    )
    assert [row[0] for row in rows] == [
        "ck_event_cards_age_range",
        "ck_event_cards_gender_filter",
    ], f"ограничения не найдены: {rows}"


# ——— AC-1a: пара утверждений о CHECK возраста ————————————————————————————


async def test_inverted_age_range_is_rejected() -> None:
    """AC-1a, первая половина пары: `age_min > age_max` отвергается БД.

    Без этой половины CHECK можно заменить на `CHECK (TRUE)` — и вторая
    половина (полуоткрытая пара) осталась бы зелёной.
    """
    _apply_migration()
    user_id, place_id, card_id = await _make_card()
    try:
        with pytest.raises(IntegrityError):
            await _set_bounds(card_id, age_min=40, age_max=30)
    finally:
        await _drop_card(user_id, place_id)


async def test_half_open_age_range_is_allowed() -> None:
    """AC-1a, вторая половина пары: `30/NULL` ПРОХОДИТ.

    Без этой половины проверка «40/30 отвергается» не отличает настоящий
    CHECK от `CHECK (TRUE)` (P-21). Полуоткрытая пара — легальный запрос
    «ищу от 30, без верхней границы».
    """
    _apply_migration()
    user_id, place_id, card_id = await _make_card()
    try:
        await _set_bounds(card_id, age_min=30, age_max=None)
        rows = await _fetch(
            "SELECT age_min, age_max FROM event_cards WHERE id = :id", id=card_id
        )
        assert rows == [(30, None)], f"полуоткрытая пара должна сохраниться, получено {rows}"
        # Симметричный случай: верхняя граница без нижней.
        await _set_bounds(card_id, age_min=None, age_max=35)
        rows = await _fetch(
            "SELECT age_min, age_max FROM event_cards WHERE id = :id", id=card_id
        )
        assert rows == [(None, 35)], f"пара NULL/35 должна сохраниться, получено {rows}"
    finally:
        await _drop_card(user_id, place_id)


async def test_equal_age_bounds_are_allowed() -> None:
    """AC-1a: граница `<=` включительна — `30/30` легально (ровно 30 лет)."""
    _apply_migration()
    user_id, place_id, card_id = await _make_card()
    try:
        await _set_bounds(card_id, age_min=30, age_max=30)
        rows = await _fetch(
            "SELECT age_min, age_max FROM event_cards WHERE id = :id", id=card_id
        )
        assert rows == [(30, 30)], f"равные границы должны сохраниться, получено {rows}"
    finally:
        await _drop_card(user_id, place_id)


# ——— AC-1b: CHECK пола ———————————————————————————————————————————————————


async def test_gender_filter_accepts_null_and_known_values() -> None:
    """AC-1b: `NULL`, `female`, `male` — проходят.

    `NULL` здесь принципиален: карточки, созданные до 12.6, имеют `NULL`,
    и CHECK без `IS NULL` отверг бы их (мутация U3).
    """
    _apply_migration()
    user_id, place_id, card_id = await _make_card()
    try:
        for value in (None, "female", "male"):
            await _set_bounds(card_id, gender_filter=value)
            rows = await _fetch(
                "SELECT gender_filter FROM event_cards WHERE id = :id", id=card_id
            )
            assert rows == [(value,)], f"значение {value!r} должно сохраниться, получено {rows}"
    finally:
        await _drop_card(user_id, place_id)


async def test_gender_filter_rejects_unknown_value() -> None:
    """AC-1b: значение вне `Gender` отвергается БД, а не только схемой."""
    _apply_migration()
    user_id, place_id, card_id = await _make_card()
    try:
        with pytest.raises(IntegrityError):
            await _set_bounds(card_id, gender_filter="other")
    finally:
        await _drop_card(user_id, place_id)


# ——— обратимость —————————————————————————————————————————————————————————


async def test_downgrade_drops_columns_and_checks() -> None:
    """AC-1: откат убирает три колонки и оба CHECK."""
    _apply_migration()
    _run_alembic("downgrade", "0037")
    try:
        columns = await _fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'event_cards' "
            "AND column_name IN ('gender_filter', 'age_min', 'age_max')"
        )
        assert columns == [], f"после отката колонок быть не должно, получено {columns}"
        checks = await _fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'event_cards'::regclass "
            "AND conname IN ('ck_event_cards_age_range', 'ck_event_cards_gender_filter')"
        )
        assert checks == [], f"после отката ограничений быть не должно, получено {checks}"
    finally:
        _run_alembic("upgrade", "head")


async def test_double_cycle_survives_0037_0038() -> None:
    """Цикл `0038 → 0037 → 0038`: откат не ломает предыдущую ревизию."""
    _apply_migration()
    _run_alembic("downgrade", "0037")
    _run_alembic("upgrade", "head")
    rows = await _fetch("SELECT version_num FROM alembic_version")
    assert rows == [(HEAD_REVISION,)], f"голова должна быть {HEAD_REVISION}, получено {rows}"
