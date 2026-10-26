"""Миграция `0021` на живой БД (стори 7.3; AC-01, AC-02, D-TD-1…D-TD-3).

Почему файл СИНХРОННЫЙ: `alembic/env.py` в async-режиме вызывает `asyncio.run`,
а вложенный `asyncio.run` внутри работающего event-loop падает. Все обращения
к БД идут короткими соединениями в `asyncio.run`, миграции — через
`alembic.command`. Схема возвращается на `head` в `finally` теста откатa.

**Каждая вставка — в СВОЕЙ транзакции.** `IntegrityError` абортит транзакцию,
и любой `DELETE` в её `finally` упал бы с «current transaction is aborted»
(проверено прогоном на первой версии этого файла).

Покрытие: S01–S08 (схема и цикл миграции), S45 (backfill, D-TD-1),
S46 (`CHECK (id > 0)` рядом с `Identity()`, D-TD-2), S47 (явная чистка
`_DOMAIN_TABLES`, D-TD-3).
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_head() -> Any:
    """Возвращает схему на `head` после КАЖДОГО теста модуля.

    Тесты этого файла намеренно снимают `0021` (guard откатa, backfill).
    Падение в середине оставило бы БД на `0020`, и следующий тест сессии упал
    бы на `TRUNCATE notifications` с «relation does not exist» — то есть один
    дефект теста превратился бы в каскад ложных падений по всей сессии.
    """
    yield
    command.upgrade(_config(), "head")


_INSERT_USER = (
    "INSERT INTO users (provider, provider_user_id, phone, role, gender, status) "
    "VALUES ('phone', :p, :p, 'user', 'female', 'active') RETURNING id"
)
_INSERT_USER_HEAD = (
    "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
    "community_slug) "
    "VALUES ('phone', :p, :p, 'user', 'female', 'active', 'tomsk') RETURNING id"
)
_INSERT_PLACE = (
    "INSERT INTO places (name, category, address_text, community_slug, is_active) "
    "VALUES ('B', 'cafe', 'A', 'tomsk', true) RETURNING id"
)


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


async def _scalar(statement: str, params: dict[str, Any] | None = None) -> Any:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement), params or {})).scalar_one()
    finally:
        await engine.dispose()


async def _execute(statement: str, params: dict[str, Any] | None = None) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), params or {})
    finally:
        await engine.dispose()


async def _new_user() -> int:
    """Пользователь в своей закоммиченной транзакции — адресат уведомления."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(_INSERT_USER_HEAD), {"p": f"+7909{uuid_lib.uuid4().int % 10**7:07d}"}
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _try_insert(statement: str, params: dict[str, Any]) -> None:
    """Вставка в своей транзакции: исключение откатывает ТОЛЬКО её."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), params)
    finally:
        await engine.dispose()


def _delete_user(user_id: int) -> None:
    """Уборка адресата: параметры передаются в `_execute`, не в `asyncio.run`."""
    asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": user_id}))


def _heads() -> tuple[str, ...]:
    return tuple(ScriptDirectory.from_config(_config()).get_heads())


def _down_revision_of(revision_id: str) -> str | None:
    script = ScriptDirectory.from_config(_config()).get_revision(revision_id)
    assert script is not None
    revision = script.down_revision
    return None if revision is None else str(revision)


def _down_revision_of_0021() -> str | None:
    return _down_revision_of("0021")


def _regclass(name: str) -> str | None:
    return asyncio.run(_scalar(f"SELECT to_regclass('public.{name}')::text"))


def _indexdef(index: str) -> str | None:
    """`indexdef` или `None`, если индекса нет.

    `scalar_one()` здесь не годится: отсутствие строки — ОЖИДАЕМЫЙ исход
    после откатa миграции, а не ошибка теста.
    """

    async def _go() -> str | None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                    {"n": index},
                )
                row = result.first()
                return None if row is None else str(row[0])
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _columns() -> dict[str, str]:
    async def _go() -> dict[str, str]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = 'notifications' ORDER BY column_name"
                    )
                )
                return {str(row[0]): str(row[1]) for row in rows.all()}
        finally:
            await engine.dispose()

    return asyncio.run(_go())


# ——— AC-01: схема ————————————————————————————————————————————————


def test_columns_and_types_match_model() -> None:
    """AC-01 (S01): 8 колонок с ожидаемыми типами.

    Краснеет от «добавили колонку» и от `json` вместо `jsonb`.

    Стори 9.1 добавила `uuid` (D-9.1-8): уведомление стало адресуемым по API.
    Набор проверяется РАВЕНСТВОМ, поэтому и лишняя, и недостающая колонка
    роняют тест — именно так эта проверка и поймала необходимость правки.
    """
    columns = _columns()
    assert set(columns) == {
        "id",
        "uuid",
        "user_id",
        "type",
        "payload",
        "created_at",
        "delivered_at",
        "read_at",
    }
    assert columns["payload"] == "jsonb"
    assert columns["type"] == "character varying"
    assert columns["created_at"] == "timestamp with time zone"


def test_single_head_and_down_revision() -> None:
    """AC-01 (S02): ровно один head (с 9.1 — `0030`), `down_revision` `0021` = `0020`.

    Проверка на уровне объекта `Script`, а не grep: grep нашёл бы строку в
    комментарии и «подтвердил» бы неверное.
    """
    # CR 7.3 H-2: head стал `0022` (бикондиционал `read_order`), цепочка
    # 0021 -> 0020 сохранена.
    # Стори 7.4: head стал `0023` (`reports`). Звену 0022 это НЕ мешает:
    # проверяется его собственный `down_revision`, а не то, что он последний.
    # Head сравнивается с текущей вершиной цепочки — иначе каждая следующая
    # миграция ломала бы этот тест, и он превратился бы в «тест номера».
    # Стори 8.1: вершина цепочки — `0025` (`sanctions` + расширение `reports`).
    # Стори 9.1: вершина стала `0030` (уведомления и обращения, D-9.1-5).
    # Стори 9.3: вершина стала `0031` (`notification_mutes`, D-9.3-4).
    # Связка `0021 -> 0020`
    # ниже от номера вершины не зависит, поэтому тест остаётся осмысленным.
    # Сравнение с ТЕКУЩЕЙ вершиной, а не «тестом номера»: собственная связка
    # `0021 -> 0020` проверяется ниже и от номера вершины не зависит.
    # Стори 10.2: вершина — `0033` (индекс удаления).
    assert _heads() == ("0033",)
    assert _down_revision_of_0021() == "0020"
    assert _down_revision_of("0022") == "0021"
    assert _down_revision_of("0023") == "0022"


def test_index_exists() -> None:
    """AC-01: `ix_notifications_user_delivery (user_id, delivered_at)`."""
    definition = _indexdef("ix_notifications_user_delivery")
    assert definition is not None
    assert "user_id" in definition
    assert "delivered_at" in definition


def test_constraint_names_present_in_database() -> None:
    """AC-01: пять `CHECK` + FK с именами конвенции присутствуют в БД.

    Тот же набор, что проверяет unit-тест по модели: расхождение модели и
    миграции (класс дефекта 3.1/4.1) видно только сравнением обеих сторон.
    """

    async def _names() -> set[str]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'notifications'::regclass"
                    )
                )
                return {str(row[0]) for row in rows.all()}
        finally:
            await engine.dispose()

    names = asyncio.run(_names())
    assert {
        "ck_notifications_id_positive",
        "ck_notifications_type_format",
        "ck_notifications_payload_object",
        "ck_notifications_delivery_order",
        "ck_notifications_read_order",
        "fk_notifications_user_id_users",
        "pk_notifications",
    } <= names


# ——— AC-01: CHECK-ограничения работают на живой БД ————————————————


def test_type_regex_rejects_bad_forms() -> None:
    """AC-01 (S03): `AttendanceRequest`, пустая строка, `a.b.c` — отвергаются."""
    user_id = asyncio.run(_new_user())
    try:
        for bad in ("AttendanceRequest", "", "a.b.c"):
            with pytest.raises(IntegrityError):
                asyncio.run(
                    _try_insert(
                        "INSERT INTO notifications (user_id, type, payload) "
                        "VALUES (:u, :t, '{}'::jsonb)",
                        {"u": user_id, "t": bad},
                    )
                )
    finally:
        _delete_user(user_id)


def test_type_regex_accepts_attendance_request() -> None:
    """AC-01 (P-11): `attendance.request` проходит — второе значение признака."""
    user_id = asyncio.run(_new_user())
    try:
        asyncio.run(
            _try_insert(
                "INSERT INTO notifications (user_id, type, payload) "
                "VALUES (:u, 'attendance.request', '{}'::jsonb)",
                {"u": user_id},
            )
        )
    finally:
        _delete_user(user_id)


def test_payload_must_be_object() -> None:
    """AC-01 (S04): массив и скаляры отвергаются `jsonb_typeof`."""
    user_id = asyncio.run(_new_user())
    try:
        for payload in ("[1,2,3]", '"scalar"', "42"):
            with pytest.raises(IntegrityError):
                asyncio.run(
                    _try_insert(
                        "INSERT INTO notifications (user_id, type, payload) "
                        "VALUES (:u, 'attendance.request', CAST(:p AS jsonb))",
                        {"u": user_id, "p": payload},
                    )
                )
    finally:
        _delete_user(user_id)


def test_delivery_and_read_order_enforced() -> None:
    """AC-01 (S05): порядки доставки и прочтения — в обоих значениях (P-11)."""
    user_id = asyncio.run(_new_user())
    try:
        # Прочтение раньше доставки — `ck_notifications_read_order`.
        with pytest.raises(IntegrityError):
            asyncio.run(
                _try_insert(
                    "INSERT INTO notifications "
                    "(user_id, type, payload, delivered_at, read_at) "
                    "VALUES (:u, 'attendance.request', '{}'::jsonb, "
                    "now() + interval '1 hour', now() + interval '30 minutes')",
                    {"u": user_id},
                )
            )
        # CR 7.3 H-2 (M18): прочтение БЕЗ доставки запрещено бикондиционалом.
        # Прежняя форма `read_at >= delivered_at` пропускала это: при
        # `delivered_at IS NULL` выражение даёт `UNKNOWN`, а CHECK отвергает
        # только `FALSE`. Тест краснеет от отката на старое выражение.
        with pytest.raises(IntegrityError):
            asyncio.run(
                _try_insert(
                    "INSERT INTO notifications (user_id, type, payload, read_at) "
                    "VALUES (:u, 'attendance.request', '{}'::jsonb, now())",
                    {"u": user_id},
                )
            )
        # Доставка раньше создания — `ck_notifications_delivery_order`.
        with pytest.raises(IntegrityError):
            asyncio.run(
                _try_insert(
                    "INSERT INTO notifications "
                    "(user_id, type, payload, delivered_at) "
                    "VALUES (:u, 'attendance.request', '{}'::jsonb, "
                    "now() - interval '1 hour')",
                    {"u": user_id},
                )
            )
        # Корректный порядок проходит — второе значение признака.
        asyncio.run(
            _try_insert(
                "INSERT INTO notifications (user_id, type, payload, delivered_at) "
                "VALUES (:u, 'attendance.request', '{}'::jsonb, "
                "now() + interval '1 hour')",
                {"u": user_id},
            )
        )
    finally:
        _delete_user(user_id)


# ——— AC-02: откат с guard ——————————————————————————————————————


def test_downgrade_guard_keeps_rows() -> None:
    """AC-02 (S06, S07, S08): guard держит данные, пустая таблица удаляется чисто.

    Три шага цикла: непустая таблица → откат бросает и НЕ удаляет данные;
    после очистки → откат удаляет таблицу и индекс без остатков;
    `upgrade` возвращает схему и единственный head `0021`.
    """
    user_id = asyncio.run(_new_user())
    try:
        asyncio.run(
            _try_insert(
                "INSERT INTO notifications (user_id, type, payload) "
                "VALUES (:u, 'attendance.request', '{}'::jsonb)",
                {"u": user_id},
            )
        )
        before = int(asyncio.run(_scalar("SELECT count(*) FROM notifications")))
        assert before >= 1

        with pytest.raises(RuntimeError, match="Откат 0021 невозможен"):
            command.downgrade(_config(), "0020")

        after = int(asyncio.run(_scalar("SELECT count(*) FROM notifications")))
        assert after == before
    finally:
        _delete_user(user_id)

    # Пустая таблица: чистое удаление (S07) и возврат схемы (S08).
    assert int(asyncio.run(_scalar("SELECT count(*) FROM notifications"))) == 0
    command.downgrade(_config(), "0020")
    assert _regclass("notifications") is None
    assert _indexdef("ix_notifications_user_delivery") is None

    command.upgrade(_config(), "head")
    assert _regclass("notifications") == "notifications"
    # Стори 7.4: вершина цепочки — `0023` (`reports`). Возврат на `head`
    # обязан вернуть и ВСЕ ревизии выше снятой, а не только `0022`.
    # Стори 8.1: вершина цепочки — `0025` (`sanctions` + расширение `reports`).
    # Стори 9.1: вершина стала `0030` (уведомления и обращения, D-9.1-5).
    # Стори 9.3: вершина стала `0031` (`notification_mutes`, D-9.3-4).
    # Связка `0021 -> 0020`
    # ниже от номера вершины не зависит, поэтому тест остаётся осмысленным.
    # Сравнение с ТЕКУЩЕЙ вершиной, а не «тестом номера»: собственная связка
    # `0021 -> 0020` проверяется ниже и от номера вершины не зависит.
    # Стори 10.2: вершина — `0033` (индекс удаления).
    assert _heads() == ("0033",)


# ——— D-TD-1: backfill ——————————————————————————————————————————


def test_backfill_marks_old_cards_only() -> None:
    """D-TD-1 (S45, score 9): backfill помечает ТОЛЬКО исторические карточки.

    Без него первый прогон после деплоя разослал бы запросы по всей базе:
    предикат воркера без нижней границы (D-7.3-4), а маркер у всех карточек
    `NULL` (колонка с 4.1 никем не заполнялась).

    Форма: снять `0021`, засеять три карточки, поднять `head`. Старая (3 суток)
    получает маркер; свежая (1 час) — нет; `draft` с прошлым `starts_at` — нет
    (backfill фильтрует статусы).
    """
    command.downgrade(_config(), "0020")
    assert _regclass("notifications") is None

    async def _seed() -> dict[str, int]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as session:
                user_id = int(
                    (
                        await session.execute(
                            text(_INSERT_USER),
                            {"p": f"+7909{uuid_lib.uuid4().int % 10**7:07d}"},
                        )
                    ).scalar_one()
                )
                place_id = int((await session.execute(text(_INSERT_PLACE))).scalar_one())

                async def _card(starts_at: datetime, status: str) -> int:
                    statement = (
                        "INSERT INTO event_cards (author_id, place_id, community_slug, "
                        "title, description, category, starts_at, capacity, "
                        "decision_window_minutes, expires_at, status, filled_count, "
                        "verification_status, published_at) "
                        "VALUES (:a, :p, 'tomsk', 'T', NULL, 'cafe', :s, 4, 60, :e, "
                        ":st, 0, 'approved', :pa) RETURNING id"
                    )
                    result = await session.execute(
                        text(statement),
                        {
                            "a": user_id,
                            "p": place_id,
                            "s": starts_at,
                            "e": starts_at - timedelta(hours=1),
                            "st": status,
                            "pa": starts_at - timedelta(days=1),
                        },
                    )
                    return int(result.scalar_one())

                moment = datetime.now(UTC)
                old_id = await _card(moment - timedelta(days=3), "finished")
                fresh_id = await _card(moment - timedelta(hours=1), "finished")
                draft_id = await _card(moment - timedelta(days=3), "draft")
                await session.commit()
                return {
                    "user": user_id,
                    "place": place_id,
                    "old": old_id,
                    "fresh": fresh_id,
                    "draft": draft_id,
                }
        finally:
            await engine.dispose()

    seeded = asyncio.run(_seed())
    try:
        command.upgrade(_config(), "head")

        def _marker(card_id: int) -> Any:
            return asyncio.run(
                _scalar(
                    "SELECT attendance_prompted_at FROM event_cards WHERE id = :id",
                    {"id": card_id},
                )
            )

        assert _marker(seeded["old"]) is not None, "историческая карточка не помечена"
        assert _marker(seeded["fresh"]) is None, "свежая карточка помечена зря"
        assert _marker(seeded["draft"]) is None, "draft не должен помечаться"
    finally:
        asyncio.run(_execute("DELETE FROM event_cards WHERE author_id = :u", {"u": seeded["user"]}))
        asyncio.run(_execute("DELETE FROM places WHERE id = :p", {"p": seeded["place"]}))
        asyncio.run(_execute("DELETE FROM users WHERE id = :u", {"u": seeded["user"]}))


# ——— D-TD-2: CHECK (id > 0) рядом с Identity() ————————————————————


def test_explicit_zero_id_rejected_despite_identity() -> None:
    """D-TD-2 (S46, P-10): явная вставка `id = 0` падает на `CHECK`.

    `Identity()` — это `GENERATED BY DEFAULT`: без `CHECK` строка с «невозможным»
    нулём физически прошла бы (прецедент 6.7). Проверка обязана краснеть,
    если `CHECK (id > 0)` убрать ИЗ МИГРАЦИИ — модель тут не участвует.
    """
    user_id = asyncio.run(_new_user())
    try:
        with pytest.raises(IntegrityError):
            asyncio.run(
                _try_insert(
                    "INSERT INTO notifications (id, user_id, type, payload) "
                    "VALUES (0, :u, 'attendance.request', '{}'::jsonb)",
                    {"u": user_id},
                )
            )
    finally:
        _delete_user(user_id)


def test_auto_id_starts_from_one() -> None:
    """D-TD-2 (P-11): без явного `id` строка получает положительный идентификатор."""
    user_id = asyncio.run(_new_user())
    try:
        asyncio.run(
            _try_insert(
                "INSERT INTO notifications (user_id, type, payload) "
                "VALUES (:u, 'attendance.request', '{}'::jsonb)",
                {"u": user_id},
            )
        )
        new_id = int(
            asyncio.run(_scalar("SELECT id FROM notifications WHERE user_id = :u", {"u": user_id}))
        )
        assert new_id > 0
    finally:
        _delete_user(user_id)


# ——— D-TD-3: явная чистка ————————————————————————————————————


def test_domain_tables_include_notifications_and_attendance() -> None:
    """D-TD-3 (S47): обе таблицы перечислены ЯВНО в `_DOMAIN_TABLES`.

    До решения изоляция держалась транзитивно на `TRUNCATE users CASCADE`:
    смена FK на `SET NULL` молча сломала бы её, и второй прогон подряд дал бы
    другой результат (нарушение P-12 и X-6.7-2).
    """
    from tests.integration import conftest as integration_conftest

    assert "notifications" in integration_conftest._DOMAIN_TABLES
    assert "attendance" in integration_conftest._DOMAIN_TABLES
