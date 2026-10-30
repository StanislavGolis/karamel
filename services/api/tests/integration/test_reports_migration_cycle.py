"""Миграция `0023` на живой БД (стори 7.4; AC-01, AC-02).

Почему файл СИНХРОННЫЙ: `alembic/env.py` в async-режиме вызывает `asyncio.run`,
а вложенный `asyncio.run` внутри работающего event-loop падает. Все обращения
к БД идут короткими соединениями в `asyncio.run`, миграции — через
`alembic.command` (тот же приём, что в `test_notifications_migration_cycle.py`).

**Каждая вставка — в СВОЕЙ транзакции.** `IntegrityError` абортит транзакцию,
и любой `DELETE` в её `finally` упал бы с «current transaction is aborted».

**Схема возвращается на `head` после КАЖДОГО теста.** Тесты намеренно снимают
`0023` (guard отката). Падение в середине оставило бы БД на `0022`, и
следующий тест сессии упал бы на `TRUNCATE reports` с «relation does not
exist» — один дефект теста превратился бы в каскад ложных падений.

Покрытие: `S01` (колонки и типы), `S02` (head и цепочка), `S03` (индекс),
`S04` (имена ограничений), `S05` (`CHECK`-и проверяются НАРУШЕНИЕМ),
`S06`–`S08` (цикл `upgrade → downgrade -1 → upgrade` и guard).
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
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.conftest import HEAD_REVISION

pytestmark = [
    pytest.mark.integration,
    # Стори 12.9 (AC-4, W-11.4-d): DDL на общей схеме — несовместим с xdist.
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


API_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_head() -> Any:
    """Возвращает схему на `head` после каждого теста модуля."""
    yield
    command.upgrade(_config(), "head")


def _config() -> AlembicConfig:
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


def _heads() -> tuple[str, ...]:
    script = ScriptDirectory.from_config(_config())
    return tuple(script.get_heads())


def _down_revision_of(revision_id: str) -> str | None:
    script = ScriptDirectory.from_config(_config())
    revision = script.get_revision(revision_id)
    return revision.down_revision  # type: ignore[return-value]


def _columns() -> dict[str, tuple[str, str]]:
    async def _go() -> dict[str, tuple[str, str]]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'reports' ORDER BY column_name"
                    )
                )
                return {str(r[0]): (str(r[1]), str(r[2])) for r in rows.all()}
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _indexdef(index: str) -> str | None:
    async def _go() -> str | None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": index}
                )
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _constraint_names() -> set[str]:
    async def _go() -> set[str]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = 'reports'::regclass")
                )
                return {str(r[0]) for r in rows.all()}
        finally:
            await engine.dispose()

    return asyncio.run(_go())


async def _seed_pair() -> tuple[int, int]:
    """Пользователь + карточка + пара `attendance`; возвращает `(user_id, attendance_id)`.

    Пара обязательна: `reports.attendance_id NOT NULL` с `CASCADE`, и без неё
    проверять `CHECK`-и было бы нечем (нарушение по FK маскировало бы
    нарушение по доменному ограничению).
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
            user_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO users (provider, provider_user_id, phone, role, "
                            "gender, status, city_slug) "
                            "VALUES ('phone', :p, :p, 'user', 'female', 'active', "
                            "'tomsk') RETURNING id"
                        ),
                        {"p": phone},
                    )
                ).scalar_one()
            )
            place_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO places (name, category, address_text, "
                            "city_slug, is_active) VALUES (:n, 'cafe', 'ул. Мира, 1', "
                            "'tomsk', true) RETURNING id"
                        ),
                        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
                    )
                ).scalar_one()
            )
            card_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO event_cards (author_id, place_id, city_slug, "
                            "title, description, category, starts_at, capacity, "
                            "decision_window_minutes, expires_at, status, filled_count, "
                            "verification_status,"
                            "kind, activity) VALUES (:a, :p, 'tomsk', 'Встреча', NULL, "
                            "'cafe', :st, 2, 60, :e, 'full', 2, 'approved',"
                            "'company', 'other') RETURNING id"
                        ),
                        {
                            "a": user_id,
                            "p": place_id,
                            "st": datetime.now(UTC) - timedelta(hours=1),
                            "e": datetime.now(UTC) - timedelta(hours=2),
                        },
                    )
                ).scalar_one()
            )
            attendance_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO attendance (card_id, participant_id, "
                            "author_confirmed, participant_confirmed, status, confirmed_at) "
                            "VALUES (:c, :p, false, false, 'no_show', now()) RETURNING id"
                        ),
                        {"c": card_id, "p": user_id},
                    )
                ).scalar_one()
            )
        return user_id, attendance_id
    finally:
        await engine.dispose()


async def _seed_chat() -> tuple[int, int]:
    """Пользователь + карточка + диалог; возвращает `(reporter_id, chat_id)`.

    Нужен для ПОЛОЖИТЕЛЬНОЙ проверки домена `target_type`: `message`-жалоба
    обязана пройти, а для этого нужен валидный `chat_id` (AC-03, D-15).
    Без такого теста мутация «вернуть `IN ('no_show')`» осталась бы зелёной:
    ожидание `IntegrityError` падает и при узком домене, и при узкой форме.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
            user_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO users (provider, provider_user_id, phone, role, "
                            "gender, status, city_slug) "
                            "VALUES ('phone', :p, :p, 'user', 'female', 'active', "
                            "'tomsk') RETURNING id"
                        ),
                        {"p": phone},
                    )
                ).scalar_one()
            )
            place_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO places (name, category, address_text, "
                            "city_slug, is_active) VALUES (:n, 'cafe', 'ул. Мира, 1', "
                            "'tomsk', true) RETURNING id"
                        ),
                        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
                    )
                ).scalar_one()
            )
            card_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO event_cards (author_id, place_id, city_slug, "
                            "title, description, category, starts_at, capacity, "
                            "decision_window_minutes, expires_at, status, filled_count, "
                            "verification_status,"
                            "kind, activity) VALUES (:a, :p, 'tomsk', 'Встреча', NULL, "
                            "'cafe', :st, 2, 60, :e, 'full', 2, 'approved',"
                            "'company', 'other') RETURNING id"
                        ),
                        {
                            "a": user_id,
                            "p": place_id,
                            # `ck_event_cards_expires_at`: окно решения
                            # закрывается ДО начала встречи, не после.
                            "st": datetime.now(UTC) + timedelta(hours=2),
                            "e": datetime.now(UTC) + timedelta(hours=1),
                        },
                    )
                ).scalar_one()
            )
            chat_id = int(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO chats (uuid, event_card_id) "
                            "VALUES (gen_random_uuid(), :c) RETURNING id"
                        ),
                        {"c": card_id},
                    )
                ).scalar_one()
            )
        return user_id, chat_id
    finally:
        await engine.dispose()


async def _try_insert_reports(params: dict[str, Any], extra_columns: str = "") -> None:
    """Пробует вставить жалобу; нарушение уходит `IntegrityError`.

    Вставка — в СВОЕЙ транзакции: после `IntegrityError` она аборчена, и
    откат делает контекстный менеджер `engine.begin()`.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.begin() as connection:
            # CR 7.4: запятая перед `extra_columns` обязательна. Без неё
            # конкатенация давала `statusreported_at`, и тест падал бы на
            # `UndefinedColumn`, то есть проверял бы опечатку в хелпере,
            # а не ограничение схемы (поймано первым прогоном).
            extra_sql = f", {extra_columns}" if extra_columns else ""
            await connection.execute(
                text(
                    # Стори 8.1: `ck_reports_target_scope` требует от `no_show`
                    # непустой `event_card_id`. Он берётся из той же пары
                    # подзапросом: тест проверяет ОГРАНИЧЕНИЯ схемы, а не
                    # обязанность каждого вызова знать карточку. Составной FK
                    # подтвердит, что карточка — из этой пары.
                    "INSERT INTO reports (reporter_id, attendance_id, event_card_id, "
                    "target_type, status"
                    f"{extra_sql}) VALUES (:r, :a, "
                    "(SELECT card_id FROM attendance WHERE id = :a), :t, :s"
                    f"{_extra_values(extra_columns)})"
                ),
                params,
            )
    finally:
        await engine.dispose()


def _extra_values(extra_columns: str) -> str:
    """Дополнительные именованные параметры для `extra_columns`."""
    names = [c.strip() for c in extra_columns.split(",") if c.strip()]
    return "".join(f", :{name}" for name in names)


def _insert_reports_sync(params: dict[str, Any], extra_columns: str = "") -> None:
    asyncio.run(_try_insert_reports(params, extra_columns))


def _delete_user_sync(user_id: int) -> None:
    """Удаляет пользователя и ВСЮ созданную им цепочку.

    Раньше удалялась только строка `users`. `places`/`event_cards` оставались
    и роняли cleanup ЧУЖОГО файла: `test_attendance_history_index` чистит
    широким `DELETE FROM places WHERE name LIKE 'Кафе %'` и упирался в
    `RESTRICT` из `fk_event_cards_place_id_places`. Один незакрытый сид
    превращался в FAILED другого теста (каскад, поймано на P-14).

    Порядок обратен вставке: `sanctions` → `reports` → `chats` →
    `attendance` → `event_cards` → `places` → `users`.
    """

    async def _go() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        cards = "(SELECT id FROM event_cards WHERE author_id = :i)"
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM sanctions WHERE report_id IN "
                        "(SELECT id FROM reports WHERE reporter_id = :i "
                        f"OR event_card_id IN {cards})"
                    ),
                    {"i": user_id},
                )
                await connection.execute(
                    text(f"DELETE FROM reports WHERE reporter_id = :i OR event_card_id IN {cards}"),
                    {"i": user_id},
                )
                await connection.execute(
                    text(f"DELETE FROM chats WHERE event_card_id IN {cards}"),
                    {"i": user_id},
                )
                await connection.execute(
                    text(f"DELETE FROM attendance WHERE participant_id = :i OR card_id IN {cards}"),
                    {"i": user_id},
                )
                # Места запоминаются ДО удаления карточек: после него связь
                # `place_id` уже не восстановить.
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE _tmp_places ON COMMIT DROP AS "
                        "SELECT DISTINCT place_id AS id FROM event_cards "
                        "WHERE author_id = :i"
                    ),
                    {"i": user_id},
                )
                await connection.execute(
                    text("DELETE FROM event_cards WHERE author_id = :i"), {"i": user_id}
                )
                await connection.execute(
                    text(
                        "DELETE FROM places WHERE id IN (SELECT id FROM _tmp_places) "
                        "AND id NOT IN (SELECT DISTINCT place_id FROM event_cards "
                        "WHERE place_id IS NOT NULL)"
                    )
                )
                await connection.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        finally:
            await engine.dispose()

    asyncio.run(_go())


# ——— AC-01: схема (S01–S04) ————————————————————————————————————————


def test_columns_and_types_match_model() -> None:
    """AC-01 (S01): 9 колонок с ожидаемыми типами и `nullable`.

    Равенство, а не подмножество: падает и при лишней колонке (например,
    `chat_id` из 8.1), и при недостающей. `uuid`/`reported_at` NOT NULL —
    адрес и время жалобы не могут отсутствовать.
    """
    columns = _columns()
    assert set(columns) == {
        "id",
        "uuid",
        "reporter_id",
        "attendance_id",
        "target_type",
        "status",
        "reason",
        "reported_at",
        "resolved_at",
        # Стори 8.1: второй объект жалобы и исход разбора.
        "chat_id",
        "event_card_id",
        "resolution",
        # Стори 8.5: материализованный обвиняемый для карточки (D-64).
        # Состав остаётся ТОЧНЫМ: лишняя колонка краснит тест.
        "accused_id",
    }
    assert columns["id"] == ("bigint", "NO")
    assert columns["uuid"] == ("uuid", "NO")
    assert columns["reporter_id"] == ("bigint", "NO")
    # Стори 8.1 (AC-01, D-15): `attendance_id` стал nullable — у жалобы на
    # переписку пары явки нет. `ck_reports_target_scope` держит форму:
    # `no_show` без пары не пройдёт, `message` без неё — обязан.
    assert columns["attendance_id"] == ("bigint", "YES")
    assert columns["target_type"] == ("character varying", "NO")
    assert columns["status"] == ("character varying", "NO")
    assert columns["reason"] == ("text", "YES")
    assert columns["reported_at"] == ("timestamp with time zone", "NO")
    assert columns["resolved_at"] == ("timestamp with time zone", "YES")


def test_single_head_and_down_revision() -> None:
    """AC-12: ровно один head `0025`, цепочка `0025 -> 0024 -> 0023 -> 0022`.

    Проверка на уровне объекта `Script`, а не grep: grep нашёл бы строку
    в комментарии и «подтвердил» бы неверное.
    """
    # Стори 8.2: вершина — `0026` (`chats.status`). Сравнение с ТЕКУЩЕЙ
    # вершиной: связки `0025 -> 0024 -> ...` от номера не зависят.
    # Стори 9.1: вершина — `0030` (уведомления и обращения, D-9.1-5).
    # Стори 9.3: вершина — `0031` (`notification_mutes`, D-9.3-4).
    # Сравнение с ТЕКУЩЕЙ вершиной, а не «тест номера»: собственные связки
    # `reports` проверяются ниже и от номера вершины не зависят.
    # Стори 10.2: вершина — `0033` (частичный индекс `users.deletion_requested_at`).
    assert _heads() == (HEAD_REVISION,)
    assert _down_revision_of("0025") == "0024"
    # Стори 7.5: звено `0024` (индекс истории явок) не меняет `0023`.
    assert _down_revision_of("0024") == "0023"
    assert _down_revision_of("0023") == "0022"


def test_index_is_status_then_reported_at() -> None:
    """AC-01: `ix_reports_status_reported_at (status, reported_at)`.

    Порядок важен: предикат очереди 8.3 — `WHERE status = :s ORDER BY
    reported_at`; обратный порядок не покрыл бы фильтр по `status`.
    """
    definition = _indexdef("ix_reports_status_reported_at")
    assert definition is not None
    assert "(status, reported_at)" in definition


def test_constraint_names_present_in_database() -> None:
    """AC-01 (S04): имена ограничений совпадают с конвенцией `models/base.py`.

    Двойной префикс (`ck_reports_ck_reports_...`) — класс дефекта 3.1/4.1:
    миграция задаёт имя БЕЗ префикса, конвенция добавляет его один раз.
    """
    names = _constraint_names()
    assert {
        "pk_reports",
        "ck_reports_id_positive",
        "ck_reports_target_type",
        "ck_reports_status",
        "ck_reports_status_resolved_consistency",
        "ck_reports_resolved_after_reported",
        "uq_reports_uuid",
        "uq_reports_attendance_id_reporter_id",
        "fk_reports_reporter_id_users",
        # Стори 8.1 (AC-04, D-14): одиночный FK заменён СОСТАВНЫМ на пару.
        "fk_reports_attendance_pair_attendance",
        # Стори 8.1 (AC-01, D-13): жалоба на переписку.
        "fk_reports_chat_id_chats",
        # Стори 8.1 (AC-03, D-15): одна формула цели.
        "ck_reports_target_scope",
        # Стори 8.1 (AC-06, D-16): исход разбора и его согласованность.
        "ck_reports_resolution",
        "ck_reports_status_resolution_consistency",
        "ck_reports_status_resolution_mapping",
    } <= names


# ——— AC-01, AC-11: ограничения проверяются НАРУШЕНИЕМ (S05) ——————————


def test_zero_id_rejected_despite_identity() -> None:
    """AC-01 (D-TD-2, P-10): явная вставка `id = 0` падает на `CHECK`.

    `Identity()` — это `GENERATED BY DEFAULT`: без `CHECK` строка `id = 0`
    физически прошла бы, и появился бы «невозможный id» (класс дефекта 6.7).
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        with pytest.raises(IntegrityError):
            _insert_reports_sync(
                {
                    "id": 0,
                    "r": user_id,
                    "a": attendance_id,
                    "t": "no_show",
                    "s": "open",
                },
                extra_columns="id",
            )
    finally:
        _delete_user_sync(user_id)


def test_target_type_message_is_now_allowed_domain() -> None:
    """AC-02 (8.1), P-8: домен `message` РАЗРЕШЁН — проверка в положительном значении.

    **Почему вставка полная и валидная.** Тест доказывает именно РАСШИРЕНИЕ
    домена: `message`-жалоба с корректным `chat_id` обязана пройти. Мутация
    «вернуть `target_type IN ('no_show')`» делает тест красным — и это
    единственный способ отличить расширенный домен от узкого.

    Раньше здесь стояло ожидание `IntegrityError` без указания ограничения:
    при мутации домена падали ДВА `CHECK` (`target_type` и `target_scope`), и
    PostgreSQL сообщал тот, что проверил первым — тест был недетерминирован.
    Поймано на CR откатом.
    """
    reporter_id, chat_id = asyncio.run(_seed_chat())
    try:
        _insert_reports_sync(
            # Имя ключа обязано совпадать с именем колонки: `_extra_values`
            # строит `:chat_id` из строки `extra_columns`.
            {"r": reporter_id, "a": None, "chat_id": chat_id, "t": "message", "s": "open"},
            extra_columns="chat_id",
        )
    finally:
        _delete_user_sync(reporter_id)


def test_no_show_with_chat_rejected_by_target_scope() -> None:
    """AC-03, D-15 (8.1): `no_show` С `chat_id` падает именно на `target_scope`.

    **Почему именно эта вставка.** Мутация M1 («убрать `chat_id IS NULL` из
    `no_show`-ветки») делает строку ЛЕГАЛЬНОЙ, поэтому тест обязан краснеть.
    Проверка `message`-ветки этого не ловит: она не тронута мутацией и
    отвергает строку по другой причине — тест оставался бы зелёным (поймано
    на CR откатом, первая редакция была именно такой).

    `chat_id` берётся из ЧУЖОГО диалога: FK допускает такую строку, значит
    отказ возможен только по `target_scope`, а не по ссылочной целостности.
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        reporter_id, chat_id = asyncio.run(_seed_chat())
        try:
            with pytest.raises(IntegrityError, match="ck_reports_target_scope"):
                _insert_reports_sync(
                    {
                        "r": reporter_id,
                        "a": attendance_id,
                        "chat_id": chat_id,
                        "t": "no_show",
                        "s": "open",
                    },
                    extra_columns="chat_id",
                )
        finally:
            # `_seed_chat` создаёт reporter'а, место, карточку и диалог. Без этой
            # уборки они остаются и включили бы `RESTRICT` в чужом cleanup'е
            # (`DELETE FROM places WHERE name LIKE 'Кафе %'`) — каскадный FAILED
            # в `test_attendance_history_index`, поймано на P-14.
            _delete_user_sync(reporter_id)
    finally:
        _delete_user_sync(user_id)


def test_resolved_status_without_timestamp_rejected() -> None:
    """AC-11 (S05): `status='dismissed'` без `resolved_at` падает.

    Бикондиционал `(status = 'open') = (resolved_at IS NULL)` запрещает
    состояние «разобрана, но время разбора неизвестно» — из него очередь 8.3
    не выбралась бы. Мутация M10 (два раздельных `CHECK` вместо
    бикондиционала) делает тест красным.
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        with pytest.raises(IntegrityError):
            _insert_reports_sync(
                {"r": user_id, "a": attendance_id, "t": "no_show", "s": "dismissed"}
            )
    finally:
        _delete_user_sync(user_id)


def test_open_status_with_timestamp_rejected() -> None:
    """AC-11 (P-11): обратное направление — `open` С `resolved_at` падает.

    Без этого теста бикондиционал мог бы быть реализован как «разобранная
    требует метки», и разобранной можно было бы назвать `open` с меткой —
    то есть открытая жалоба выглядела бы разобранной.
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        with pytest.raises(IntegrityError):
            _insert_reports_sync(
                {
                    "r": user_id,
                    "a": attendance_id,
                    "t": "no_show",
                    "s": "open",
                    "resolved_at": datetime.now(UTC),
                },
                extra_columns="resolved_at",
            )
    finally:
        _delete_user_sync(user_id)


def test_resolved_before_reported_rejected() -> None:
    """AC-11: `resolved_at < reported_at` падает на `CHECK`.

    Разбор не может произойти до жалобы: такая строка утверждала бы решение
    по ещё не поданной жалобе.
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        past = datetime.now(UTC) - timedelta(hours=5)
        with pytest.raises(IntegrityError):
            _insert_reports_sync(
                {
                    "r": user_id,
                    "a": attendance_id,
                    "t": "no_show",
                    "s": "dismissed",
                    "reported_at": datetime.now(UTC),
                    "resolved_at": past,
                },
                extra_columns="reported_at, resolved_at",
            )
    finally:
        _delete_user_sync(user_id)


def test_duplicate_pair_and_reporter_rejected() -> None:
    """AC-07: вторая жалоба той же пары тем же человеком падает на `UNIQUE`.

    Это ТА ЖЕ защита, на которую опирается `ON CONFLICT` в репозитории.
    Мутация M11 (убрать `UNIQUE`) делает тест красным, а одновременно с ним
    красным станет `test_repeat_report_gives_409_and_one_row` — то есть
    дефект виден с двух сторон (схема и API).
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        _insert_reports_sync({"r": user_id, "a": attendance_id, "t": "no_show", "s": "open"})
        with pytest.raises(IntegrityError):
            _insert_reports_sync({"r": user_id, "a": attendance_id, "t": "no_show", "s": "open"})
    finally:
        _delete_user_sync(user_id)


# ——— AC-02: цикл миграции и guard (S06–S08) ————————————————————————


def test_downgrade_guard_keeps_rows() -> None:
    """AC-02 (S06): непустая `reports` останавливает откат, данные сохранены.

    Guard обязателен: жалобы — это данные, и `DROP TABLE` уничтожил бы их
    молча (§12.3). Проверяется ИСКЛЮЧЕНИЕМ, а не фактом вызова: без guard
    `downgrade` прошёл бы и таблица исчезла.
    """
    user_id, attendance_id = asyncio.run(_seed_pair())
    try:
        _insert_reports_sync({"r": user_id, "a": attendance_id, "t": "no_show", "s": "open"})
        with pytest.raises(RuntimeError, match="Откат 0023 остановлен"):
            command.downgrade(_config(), "0022")
        # Данные на месте: `reports` существует и строка в ней осталась.
        assert _columns() != {}
    finally:
        _delete_user_sync(user_id)


def test_downgrade_and_upgrade_cycle() -> None:
    """AC-02 (S07, S08): пустая таблица удаляется и создаётся заново без остатков.

    Проверяется ЦИКЛ, а не один откат: остаток (индекс без таблицы, тип без
    таблицы) виден только на повторном `upgrade`. Мутация M12 (`drop_table`
    без `drop_index`) делает второй `upgrade` падающим.
    """
    command.downgrade(_config(), "0022")
    try:
        assert _columns() == {}
        assert _indexdef("ix_reports_status_reported_at") is None
    finally:
        command.upgrade(_config(), "head")
    # После повторного наката схема полноценна: колонки и индекс на месте.
    assert set(_columns()) == {
        "id",
        "uuid",
        "reporter_id",
        "attendance_id",
        "target_type",
        "status",
        "reason",
        "reported_at",
        "resolved_at",
        # Стори 8.1: второй объект жалобы и исход разбора.
        "chat_id",
        "event_card_id",
        "resolution",
        # Стори 8.5: `reports.accused_id` (D-64).
        "accused_id",
    }
    assert _indexdef("ix_reports_status_reported_at") is not None


def test_domain_tables_include_reports() -> None:
    """D-TD-3 (AC-02): `reports` перечислен ЯВНО в `_DOMAIN_TABLES`.

    Без явного перечисления изоляция держалась бы на `ON DELETE CASCADE`
    и молча ломалась бы при смене правила ретенции (второй прогон подряд дал
    бы другой результат — нарушение P-12 и X-6.7-2).
    """
    from tests.integration import conftest

    assert "reports" in conftest._DOMAIN_TABLES
