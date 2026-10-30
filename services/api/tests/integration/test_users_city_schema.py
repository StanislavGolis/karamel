"""Схема `users.city_slug` и удаление лайков (стори 10.1; AC-01, AC-02).

Проверки идут через `information_schema` и `pg_constraint`, а не через модель:
модель описывает намерение, а AC говорит о фактической схеме. Тест на модели
прошёл бы и при расхождении миграции `0032` с моделью.

§18.1: CHECK проверяется НАРУШЕНИЕМ, а не чтением каталога. Наличие
`ck_users_city_slug` в `pg_constraint` не доказывает, что он что-то
запрещает — каталог используется только для проверки ИМЁН (они часть
контракта, `op.f`, §18.7).
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _user(**overrides: object) -> dict[str, object]:
    """Строка пользователя для прямой вставки SQL: тест идёт мимо Pydantic.

    Проверяется, что запрет живёт в БД: воркер и будущий внутренний код
    пишут сырым SQL, минуя схемы.
    """
    phone = f"+7912{uuid_lib.uuid4().int % 10**7:07d}"
    base: dict[str, object] = {
        "provider": "phone",
        "provider_user_id": phone,
        "phone": phone,
        "city_slug": "tomsk",
    }
    base.update(overrides)
    return base


async def _insert_user(session: AsyncSession, **overrides: object) -> None:
    values = _user(**overrides)
    await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, city_slug) "
            "VALUES (:provider, :provider_user_id, :phone, :city_slug)"
        ),
        values,
    )


# --- AC-01: колонка и её свойства ------------------------------------------------


async def test_city_slug_is_not_null(session: AsyncSession) -> None:
    """AC-01: `NOT NULL` БЕЗ дефолта — схема не хранит «магическое» значение.

    Краснеет двумя способами: если миграция не сняла `DEFAULT` (тогда
    `column_default` не пуст) и если `NOT NULL` пропал (тогда `is_nullable='YES'`).
    Второе означало бы, что пользователь без сообщества легален, а лента
    фильтрует по `city_slug` — такой пользователь не увидел бы ничего.
    """
    row = (
        await session.execute(
            text(
                "SELECT is_nullable, column_default "
                "  FROM information_schema.columns "
                " WHERE table_name = 'users' AND column_name = 'city_slug'"
            )
        )
    ).one_or_none()
    assert row is not None, "колонка users.city_slug отсутствует"
    assert row.is_nullable == "NO", row
    assert row.column_default is None, f"дефолт не снят: {row.column_default!r}"


async def test_city_slug_check_rejects_bad_forms(session: AsyncSession) -> None:
    """AC-01: CHECK `~ '^[a-z0-9_-]{3,64}$'` реально отбивает плохие формы.

    §18.1: проверка НАРУШЕНИЕМ. Три класса, каждый ломает свой участок
    шаблона: заглавные (регистр), пробел (разделитель), `ab` (минимум 3).
    Пройди CHECK на `^.+$` — все три вставки пройдут, тест красный.
    """
    for bad in ("Tomsk", "tom sk", "ab"):
        with pytest.raises(IntegrityError):
            await _insert_user(session, city_slug=bad)
        await session.rollback()


async def test_city_slug_check_rejects_65_chars(session: AsyncSession) -> None:
    """P-15 (CR 10.1): верхняя граница 64 — 65 символов отвергаются.

    Без этого шаблон `{3,}` без верхней границы остался бы зелёным, хотя
    `sa.String(64)` в модели и `VARCHAR(64)` в БД обещают 64.
    """
    # Верхняя граница нарушается дважды: PostgreSQL отвергает VARCHAR(64)
    # переполнением, а regex-CHECK — несовпадением. Драйвер отдаёт разные
    # классы (`DBAPIError` на переполнение, `IntegrityError` на CHECK), и какой
    # сработает первым, зависит от плана. Перехватывается общий предок:
    # предмет теста — отказ, а не конкретный класс исключения драйвера.
    with pytest.raises((IntegrityError, DataError, DBAPIError)):
        await _insert_user(session, city_slug="a" * 65)
    await session.rollback()


async def test_city_slug_check_accepts_exactly_64(session: AsyncSession) -> None:
    """P-15, P-19: ровно 64 символа — ПРОХОДЯТ (граница с обеих сторон).

    Пара к предыдущему тесту: шаблон `{3,63}` прошёл бы проверку на 65,
    но отверг бы легальное значение.
    """
    await _insert_user(session, city_slug="b" * 64)
    count = (
        await session.execute(
            text("SELECT count(*) FROM users WHERE city_slug = :s"),
            {"s": "b" * 64},
        )
    ).scalar_one()
    assert count == 1


async def test_city_slug_check_accepts_valid_form(session: AsyncSession) -> None:
    """AC-01: положительная базовая линия (P-19) — валидный слаг ПРОХОДИТ.

    Без этого теста `test_city_slug_check_rejects_bad_forms` был бы
    зелёным и при CHECK, отвергающем всё подряд.
    """
    await _insert_user(session, city_slug="tomsk-2_a")
    count = (
        await session.execute(text("SELECT count(*) FROM users WHERE city_slug = 'tomsk-2_a'"))
    ).scalar_one()
    assert count == 1


async def test_city_slug_constraint_name_matches_convention(session: AsyncSession) -> None:
    """AC-01, §18.7: имя ограничения по конвенции `ck_users_<name>`.

    Расхождение ломает `alembic check` и `downgrade`: миграция снимает
    ограничение по имени, и с чужим именем откат упал бы.
    """
    names = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    " WHERE conrelid = 'users'::regclass AND contype = 'c'"
                )
            )
        ).all()
    }
    assert "ck_users_city_slug" in names, sorted(names)


# --- AC-02: механика лайков удалена ----------------------------------------------


async def test_likes_columns_are_gone(session: AsyncSession) -> None:
    """AC-02: колонок `likes_used_today` и `likes_counted_on` в схеме НЕТ.

    Краснеет, если `downgrade` случайно прогнан поверх или колонки вернули:
    мёртвая механика лайков снята разворотом Action-first.
    """
    rows = (
        await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                " WHERE table_name = 'users' AND column_name LIKE 'likes%'"
            )
        )
    ).all()
    assert rows == [], f"колонки лайков остались: {rows}"


async def test_likes_check_constraint_is_gone(session: AsyncSession) -> None:
    """AC-02: CHECK `ck_users_likes_non_negative` удалён вместе с колонкой.

    Отдельно от колонок: ограничение могло остаться «висящим» на другой
    колонке и ломать вставки неочевидным образом.
    """
    names = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    " WHERE conrelid = 'users'::regclass AND contype = 'c'"
                )
            )
        ).all()
    }
    assert "ck_users_likes_non_negative" not in names, sorted(names)


async def test_model_matches_migration(session: AsyncSession) -> None:
    """AC-01, AC-02: модель и схема не разошлись (`alembic check` в миниатюре).

    CR 10.1, P-06: прежняя редакция сравнивала модель САМА С СЕБОЙ (`session`
    не использовался), и тест оставался зелёным при колонке, удалённой из
    миграции. Теперь множества колонок сверяются с `information_schema`:
    расхождение модели и схемы даёт красный.
    """
    from app.models.user import User

    model_columns = {c.name for c in User.__table__.columns}
    db_columns = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    " WHERE table_name = 'users'"
                )
            )
        ).all()
    }

    assert "city_slug" in db_columns
    assert "city_slug" in model_columns
    # Модель описывает ровно те колонки, что есть в схеме: иначе `alembic check`
    # покажет расхождение, а приложение упадёт на первом же запросе.
    assert model_columns == db_columns, {
        "only_model": sorted(model_columns - db_columns),
        "only_db": sorted(db_columns - model_columns),
    }
    assert "likes_used_today" not in model_columns
    assert "likes_counted_on" not in model_columns


# --- P-03 (CR 10.1): литерал миграции и конфиг не разошлись -------------------


def test_migration_literal_matches_config() -> None:
    """D-10.1-1, P-03: литерал `0032` совпадает с `Settings.default_city_slug`.

    Миграция обязана хранить ЛИТЕРАЛ (историческая ревизия воспроизводима при
    смене конфига), но расхождение литерала с конфигом невидимо иначе: старые
    строки получат одно сообщество, новые пользователи — другое, и старые
    увидят пустую ленту. Тест читает файл миграции, а не импортирует её.
    """
    import re
    from pathlib import Path as _Path

    from app.core.config import settings

    migration = (
        _Path(__file__).resolve().parents[2]
        / "alembic/versions/0032_users_community_and_drop_likes.py"
    )
    source = migration.read_text(encoding="utf-8")

    # Стори 12.1: имя литерала в `0032` осталось историческим
    # (`DEFAULT_COMMUNITY_SLUG`) — ревизия неизменяема, колонку переименовывает
    # `0035`. Сверяется ЗНАЧЕНИЕ: старые строки и новые пользователи должны
    # попасть в один город, и это не зависит от того, как назывался литерал.
    match = re.search(r'^DEFAULT_COMMUNITY_SLUG = "([^"]+)"$', source, re.M)
    assert match is not None, "литерал DEFAULT_COMMUNITY_SLUG в 0032 не найден"

    assert match.group(1) == settings.default_city_slug, (
        f"литерал миграции {match.group(1)!r} != default_city_slug "
        f"{settings.default_city_slug!r}: старые строки и новые пользователи "
        f"попадут в разные сообщества"
    )


def test_migration_does_not_import_settings() -> None:
    """D-10.1-1: в миграции НЕТ импорта конфига — только литерал.

    Импорт сделал бы историческую ревизию зависимой от текущего `.env`:
    прогон `upgrade` на другой машине дал бы другую схему.
    """
    import ast
    from pathlib import Path as _Path

    migration = (
        _Path(__file__).resolve().parents[2]
        / "alembic/versions/0032_users_community_and_drop_likes.py"
    )
    source = migration.read_text(encoding="utf-8")

    # CR 10.1: проверяются ИМПОРТЫ, а не подстрока в тексте — `app.core.config`
    # законно упоминается в docstring как объяснение запрета. Подстрочная
    # проверка краснела бы на собственном комментарии миграции.
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    assert not [m for m in imported if m.startswith("app")], imported
