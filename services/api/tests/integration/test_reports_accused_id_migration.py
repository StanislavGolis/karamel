"""Цикл миграции `0028` на живой БД (стори 8.5; AC-12, P-13).

Откат нельзя проверять внутри сессионной фикстуры `session`: `alembic
downgrade` выполняет DDL на своём соединении и увидел бы чужой
незакоммиченный слой. Поэтому файл работает через `alembic.command` и сам
возвращает схему на `head` в `finally` (тот же приём, что в
`test_attendance_migration_cycle`).

Тесты СИНХРОННЫЕ: `alembic/env.py` в async-режиме вызывает `asyncio.run`, а
вложенный `asyncio.run` внутри работающего loop падает.

**Что доказывается.** (1) колонка появляется и исчезает; (2) второй индекс
существует и `indisvalid`; (3) backfill заполнил ВСЕ жалобы — guard миграции
не сработал на реальных данных.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

pytestmark = [
    pytest.mark.integration,
    # Стори 12.9 (AC-4, W-11.4-d): DDL на общей схеме — несовместим с xdist.
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> AlembicConfig:
    """Конфиг как у `conftest.migrated_database`: URL берёт `env.py` из настроек."""
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


async def _scalar(statement: str, params: dict[str, Any] | None = None) -> Any:
    """Один скаляр из живой БД (короткое соединение)."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), params or {})
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


def _accused_regclass() -> str | None:
    return asyncio.run(_scalar("SELECT to_regclass('public.reports')::text"))


def _column_exists(name: str) -> bool:
    return (
        asyncio.run(
            _scalar(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'reports' AND column_name = :c",
                {"c": name},
            )
        )
        is not None
    )


def _index_valid(name: str) -> bool:
    return (
        asyncio.run(
            _scalar(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)",
                {"n": name},
            )
        )
        is True
    )


def _cleanup_seed() -> None:
    """Удаляет строки, созданные `_seed_legacy_report`, по их метке.

    **Уборка обязательна.** Этот файл работает через `engine.begin()` —
    РЕАЛЬНЫЕ коммиты, в отличие от SAVEPOINT-изоляции §18.8. Без уборки
    созданная `message`-жалоба переживает тест и валит legacy-тесты отката
    на `ck_reports_target_type` (`target_type IN ('no_show')` у старых
    ревизий) и счётчики «ровно две строки». Тот же дефект изоляции, что
    `Y-8.4-1` в CR 8.4 — здесь он мой и закрывается сразу.
    """

    async def _run() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as conn:
                # Порядок — от зависимых к независимым: санкций нет, но
                # `reports` ссылается на `chats`/`event_cards`/`users`.
                await conn.execute(
                    text(
                        "DELETE FROM reports WHERE reporter_id IN "
                        "(SELECT id FROM users WHERE phone LIKE '+7921%' OR phone LIKE '+7922%')"
                    )
                )
                await conn.execute(
                    text(
                        "DELETE FROM chats WHERE event_card_id IN (SELECT id FROM event_cards "
                        "WHERE author_id IN (SELECT id FROM users WHERE phone LIKE '+7921%'))"
                    )
                )
                await conn.execute(
                    text(
                        "DELETE FROM join_requests WHERE applicant_id IN "
                        "(SELECT id FROM users WHERE phone LIKE '+7922%')"
                    )
                )
                await conn.execute(
                    text(
                        "DELETE FROM event_cards WHERE author_id IN "
                        "(SELECT id FROM users WHERE phone LIKE '+7921%')"
                    )
                )
                await conn.execute(
                    text(
                        "DELETE FROM places WHERE name LIKE 'Кафе %' "
                        "AND id NOT IN (SELECT DISTINCT place_id FROM event_cards)"
                    )
                )
                await conn.execute(
                    text("DELETE FROM users WHERE phone LIKE '+7921%' OR phone LIKE '+7922%'")
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _seed_legacy_report() -> tuple[str, str]:
    """Жалоба БЕЗ `accused_id` — так выглядит строка до миграции `0028`.

    Создаётся ПОСЛЕ отката до `0027`, когда колонки ещё нет. Возвращает
    `(report_uuid, accused_uuid)` — ожидаемого обвиняемого для сверки
    backfill-а: автор карточки жалобщика не он, значит обвиняемый = участник.
    """

    async def _run() -> tuple[str, str, str]:
        import uuid as uuid_lib

        from app.core.config import settings

        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=None)
        try:
            async with engine.begin() as conn:
                phone_reporter = f"+7921{uuid_lib.uuid4().int % 10**7:07d}"
                phone_accused = f"+7922{uuid_lib.uuid4().int % 10**7:07d}"
                ids: list[int] = []
                for phone in (phone_reporter, phone_accused):
                    row = await conn.execute(
                        text(
                            "INSERT INTO users (uuid, provider, provider_user_id, phone, "
                            "role, gender, status, age_confirmed, consent_given_at) "
                            "VALUES (:u, 'phone', :p, :p, 'user', 'female', 'active', "
                            "        true, now()) RETURNING id"
                        ),
                        {"u": uuid_lib.uuid4(), "p": phone},
                    )
                    ids.append(int(row.scalar_one()))
                reporter_id, accused_id = ids
                accused_uuid = (
                    await conn.execute(
                        text("SELECT uuid FROM users WHERE id = :i"), {"i": accused_id}
                    )
                ).scalar_one()
                place = await conn.execute(
                    text(
                        "INSERT INTO places (name, category, address_text, community_slug, "
                        "is_active) "
                        "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
                    ),
                    {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_city_slug},
                )
                place_id = int(place.scalar_one())
                chat_uuid = uuid_lib.uuid4()
                card = await conn.execute(
                    text(
                        "INSERT INTO event_cards (author_id, place_id, community_slug, title, "
                        "category, starts_at, capacity, decision_window_minutes, expires_at, "
                        "status, filled_count, verification_status, published_at, chat_uuid) "
                        "VALUES (:a, :p, :s, 'Встреча', 'cafe', now(), 2, 60, now(), 'full', 2, "
                        "        'approved', now(), :chat) RETURNING id"
                    ),
                    {
                        "a": reporter_id,
                        "p": place_id,
                        "s": settings.default_city_slug,
                        "chat": chat_uuid,
                    },
                )
                card_id = int(card.scalar_one())
                chat = await conn.execute(
                    text("INSERT INTO chats (uuid, event_card_id) VALUES (:u, :c) RETURNING id"),
                    {"u": chat_uuid, "c": card_id},
                )
                chat_id = int(chat.scalar_one())
                # **`selected`-заявка ОБЯЗАТЕЛЬНА.** Чат создаётся только после
                # выбора участника (5б.2), и backfill находит вторую сторону
                # именно через `join_requests.status='selected'`. Без этой
                # строки guard миграции падает `RuntimeError` — проверено
                # прогоном: это не дефект миграции, а нереалистичный сид
                # (чат без выбранного участника в домене невыразим).
                await conn.execute(
                    text(
                        "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                        "submitted_at, expires_at, decided_at) "
                        "VALUES (:c, :a, '', 'selected', now(), now() + interval '1 day', now())"
                    ),
                    {"c": card_id, "a": accused_id},
                )
                report = await conn.execute(
                    text(
                        "INSERT INTO reports (reporter_id, chat_id, target_type, status, "
                        "reported_at) VALUES (:r, :chat, 'message', 'open', now()) "
                        "RETURNING uuid"
                    ),
                    {"r": reporter_id, "chat": chat_id},
                )
                return str(report.scalar_one()), str(accused_uuid), str(reporter_id)
        finally:
            await engine.dispose()

    report_uuid, accused_uuid, _ = asyncio.run(_run())
    return report_uuid, accused_uuid


def test_upgrade_adds_column_and_backfills() -> None:
    """AC-12: `0028` добавляет колонку, оба индекса валидны, backfill полон.

    **Backfill проверяется на РЕАЛЬНОЙ строке.** Тест откатывает схему до
    `0027` (где колонки нет), создаёт жалобу прямым `INSERT` — ровно так
    выглядят данные до миграции — и только потом поднимает `0028`. Без этого
    тест гонял бы backfill на ПУСТОЙ таблице: guard при нуле строк проходит, и
    мутация «убрать backfill» остаётся зелёной (проверено прогоном в DS 8.5).

    P-13: проверка идёт по ЖИВОЙ схеме — модель и миграция это две копии
    одной схемы, и расхождение иначе не поймать.
    """
    config = _alembic_config()
    try:
        command.downgrade(config, "0027")
        report_uuid, accused_uuid = _seed_legacy_report()
        command.upgrade(config, "0028")

        assert _column_exists("accused_id")
        assert _index_valid("ix_reports_accused_id_reported_at")
        assert _index_valid("ix_reports_reporter_id_reported_at")

        # Backfill заполнил ИМЕННО эту строку — и именно второй стороной диалога.
        filled = asyncio.run(
            _scalar("SELECT accused_id FROM reports WHERE uuid = :u", {"u": report_uuid})
        )
        assert filled is not None, "backfill не заполнил accused_id у существующей жалобы"
        accused_actual = asyncio.run(
            _scalar("SELECT uuid FROM users WHERE id = :i", {"i": int(filled)})
        )
        assert str(accused_actual) == accused_uuid, (accused_actual, accused_uuid)
    finally:
        # Порядок важен: сначала данные (иначе `head`-схема не даст удалить
        # строки старого формата), затем возврат схемы на вершину.
        _cleanup_seed()
        command.upgrade(config, "head")


def test_downgrade_removes_column_and_indexes() -> None:
    """AC-12: откат `0028 → 0027` снимает колонку и оба индекса.

    `downgrade` обязан быть атомарным (урок H-1 CR 8.2): без
    `autocommit_block`, поэтому остановка guard'ом ниже не оставит индексы
    снесёнными при формально верном `alembic_version`.
    """
    config = _alembic_config()
    try:
        command.upgrade(config, "0028")
        command.downgrade(config, "0027")
        assert not _column_exists("accused_id")
        assert _index_valid("ix_reports_accused_id_reported_at") is False
        assert _index_valid("ix_reports_reporter_id_reported_at") is False
        # Таблица на месте: колонка удалена, данные не потеряны.
        assert _accused_regclass() == "reports"
    finally:
        # Порядок важен: сначала данные (иначе `head`-схема не даст удалить
        # строки старого формата), затем возврат схемы на вершину.
        _cleanup_seed()
        command.upgrade(config, "head")
