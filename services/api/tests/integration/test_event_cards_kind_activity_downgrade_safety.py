"""Безопасность отката `0037` на РЕАЛЬНЫХ данных (CR 12.4, находка H-1).

**Что нашёл CR.** `downgrade()` удалял `kind`/`activity`, а `upgrade()` создавал
их заново с `server_default` (`company`/`other`). Цикл `0037 → 0036 → 0037`
проходил, строки не терялись — AC-8 формально выполнялся, — но карточка
«свидание, кино» становилась «компания, активность неизвестна». Молча.

Класс CR 12.2 H-1, только опаснее: там откат падал (дефект виден), здесь
проходил и затирал (дефект невидим). Откат релиза — штатная операция
(`runbook-deploy.md` §3), поэтому «откатываем и теряем смысл встреч» =
продукт, неоткатываемый в терминах данных.

**Решение.** `downgrade()` считает строки, чей смысл бэкфилл НЕ восстановит
(`kind <> 'company' OR activity <> 'other'`), и отказывается работать, пока
оператор не подтвердит потерю явно (`ALEMBIC_ALLOW_MEANING_LOSS=1`). Отказ
происходит ДО изменения схемы: оператор узнаёт о затирании до операции.

**Почему не отказ по любым строкам.** Замер: три чужих теста
(`test_attendance_history_index`, `test_attendance_migration_cycle`,
`test_reports_accused_id_migration`) откатываются ниже `0037` при непустых
`event_cards`, но засеяны парой бэкфилла — смысла в них откат не несёт.
Гард различает «строки есть» и «строки со смыслом есть».

Краснеет при:
- удалении гарда из `downgrade` (M-H1-1) — откат проходит молча;
- счёте всех строк вместо значимых (M-H1-2) — ломается разрешённый откат;
- игнорировании `ALEMBIC_ALLOW_MEANING_LOSS` (M-H1-3) — осознанный откат
  становится невозможным, то есть продукт снова неоткатываем;
- снятии offline-проверки (M-H1-4) — `--sql` отдаёт разрушительный скрипт;
- бэкфилле не тем значением (M-H2-2) — `company`/`other` в AC-2.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid as uuid_lib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import HEAD_REVISION

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


API_ROOT = Path(__file__).resolve().parents[2]

#: Переменная осознанного подтверждения потери смысла (см. `0037.downgrade`).
ALLOW_ENV = "ALEMBIC_ALLOW_MEANING_LOSS"

#: Уникальный хвост на прогон: тест может падать до `finally`, и следующий
#: прогон не должен упираться в уникальность телефона/названия места.
_SUFFIX = uuid_lib.uuid4().hex[:10]


def _run_alembic(
    *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **(extra_env or {})},
        check=False,
    )


def _must_run(*args: str, extra_env: dict[str, str] | None = None) -> None:
    result = _run_alembic(*args, extra_env=extra_env)
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n{result.stderr}"
    )


async def _seed_card(*, kind: str, activity: str) -> str:
    """Карточка с ЯВНО заданным смыслом; возвращает `title` для уборки."""
    title = f"CR-12.4-H1-{_SUFFIX}-{uuid_lib.uuid4().hex[:6]}"
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            user_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (uuid, provider, provider_user_id, phone, role, "
                        "status, age_confirmed, consent_given_at, city_slug) "
                        "VALUES (gen_random_uuid(), 'phone', :pid, :phone, 'user', 'active', "
                        "true, now(), 'tomsk') RETURNING id"
                    ),
                    {
                        "pid": f"cr124h1-{_SUFFIX}-{uuid_lib.uuid4().hex[:8]}",
                        "phone": f"+7912{uuid_lib.uuid4().int % 10**7:07d}",
                    },
                )
            ).scalar_one()
            place_id = (
                await connection.execute(
                    text(
                        "INSERT INTO places (name, category, address_text, city_slug, "
                        "is_active) VALUES (:name, 'cafe', 'ул. Мира, 1', 'tomsk', true) "
                        "RETURNING id"
                    ),
                    {"name": f"CR-12.4-H1-place-{_SUFFIX}-{uuid_lib.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO event_cards (author_id, place_id, city_slug, title, "
                    "category, starts_at, capacity, decision_window_minutes, expires_at, "
                    "status, filled_count, verification_status, published_at, kind, "
                    "activity) VALUES (:a, :p, 'tomsk', :t, 'cafe', now() + interval '3 days', "
                    "2, 60, now() + interval '2 days', 'published', 0, 'approved', now(), "
                    ":kind, :activity)"
                ),
                {
                    "a": user_id,
                    "p": place_id,
                    "t": title,
                    "kind": kind,
                    "activity": activity,
                },
            )
    finally:
        await engine.dispose()
    return title


async def _cleanup() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM event_cards WHERE title LIKE :t"),
                {"t": f"CR-12.4-H1-{_SUFFIX}%"},
            )
            await connection.execute(
                text("DELETE FROM users WHERE provider_user_id LIKE :p"),
                {"p": f"cr124h1-{_SUFFIX}%"},
            )
            await connection.execute(
                text("DELETE FROM places WHERE name LIKE :n"),
                {"n": f"CR-12.4-H1-place-{_SUFFIX}%"},
            )
    finally:
        await engine.dispose()


async def _card_meaning(title: str) -> tuple[str, str]:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT kind, activity FROM event_cards WHERE title = :t"),
                    {"t": title},
                )
            ).one()
            return str(row[0]), str(row[1])
    finally:
        await engine.dispose()


async def _kind_column_exists() -> bool:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return bool(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'event_cards' "
                            "AND column_name = 'kind'"
                        )
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def _seed_card_without_kind() -> str:
    """Карточка в схеме `0036`: колонок `kind`/`activity` ещё нет.

    Именно так выглядела таблица до `0037`, поэтому `upgrade` заполняет
    обе колонки своим `server_default` — это и есть предмет проверки AC-2.
    """
    title = f"CR-12.4-H1-{_SUFFIX}-{uuid_lib.uuid4().hex[:6]}"
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            user_id = (
                await connection.execute(
                    text(
                        "INSERT INTO users (uuid, provider, provider_user_id, phone, role, "
                        "status, age_confirmed, consent_given_at, city_slug) "
                        "VALUES (gen_random_uuid(), 'phone', :pid, :phone, 'user', 'active', "
                        "true, now(), 'tomsk') RETURNING id"
                    ),
                    {
                        "pid": f"cr124h1-{_SUFFIX}-{uuid_lib.uuid4().hex[:8]}",
                        "phone": f"+7912{uuid_lib.uuid4().int % 10**7:07d}",
                    },
                )
            ).scalar_one()
            place_id = (
                await connection.execute(
                    text(
                        "INSERT INTO places (name, category, address_text, city_slug, "
                        "is_active) VALUES (:name, 'cafe', 'ул. Мира, 1', 'tomsk', true) "
                        "RETURNING id"
                    ),
                    {"name": f"CR-12.4-H1-place-{_SUFFIX}-{uuid_lib.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO event_cards (author_id, place_id, city_slug, title, "
                    "category, starts_at, capacity, decision_window_minutes, expires_at, "
                    "status, filled_count, verification_status, published_at) "
                    "VALUES (:a, :p, 'tomsk', :t, 'cafe', now() + interval '3 days', 2, 60, "
                    "now() + interval '2 days', 'published', 0, 'approved', now())"
                ),
                {"a": user_id, "p": place_id, "t": title},
            )
    finally:
        await engine.dispose()
    return title


async def _column_facts() -> dict[str, tuple[str | None, str]]:
    """`column_default` и `is_nullable` обеих колонок из каталога PostgreSQL."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT column_name, column_default, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'event_cards' "
                    "AND column_name IN ('kind', 'activity')"
                )
            )
            return {str(row[0]): (row[1], str(row[2])) for row in rows}
    finally:
        await engine.dispose()


async def test_upgrade_cycle_leaves_no_column_default() -> None:
    """AC-2 (CR 12.4 H-2, мутация U-2): дефолт снимается КАЖДЫМ накатом.

    Проверяется на цикле `downgrade 0036 → upgrade 0037`, а не на текущей
    схеме: `upgrade head` на уже накатанной базе — no-op, и удаление
    `alter_column(server_default=None)` осталось бы незамеченным (урок 12.1,
    ложные KILLED).

    Краснеет, если `DROP DEFAULT` убрать: новая карточка унаследует
    `company`/`other`, и AC-3 (тип выбирает автор) станет необязательным.
    """
    _must_run("upgrade", "head")
    _must_run("downgrade", "0036", extra_env={ALLOW_ENV: "1"})
    try:
        _must_run("upgrade", HEAD_REVISION)

        facts = await _column_facts()
        assert set(facts) == {"kind", "activity"}, facts
        for column in ("kind", "activity"):
            default, nullable = facts[column]
            assert default is None, f"у {column} остался дефолт {default!r}"
            # Пара к дефолту: «дефолт снят вместе с NOT NULL» открыл бы
            # карточкам без смысла дорогу в БД, а тест на один `DEFAULT`
            # этого не отличил бы.
            assert nullable == "NO", f"{column} стала nullable"
    finally:
        _must_run("upgrade", "head")


async def test_backfill_gives_company_and_other() -> None:
    """AC-2 (CR 12.4 H-2, мутация U-1): строка до `0037` получает `company`/`other`.

    Замер идёт НА ОТКАТАННОЙ БД (`0036` → вставка → `0037`), а не на голове:
    `upgrade head` на уже накатанной схеме — no-op, и подмена значения
    бэкфилла осталась бы незамеченной (урок 12.1, ложные KILLED).

    Краснеет при `server_default="date"`/`"coffee"`: бэкфилл выдумал бы
    смысл, которого у карточек не было, — тот же класс, что отклонённый
    бэкфилл `consent_version='1.0'` в 11.4.
    """
    _must_run("upgrade", "head")
    # Гард отката смотрит на значимые строки; засеянных ранее быть не должно.
    _must_run("downgrade", "0036", extra_env={ALLOW_ENV: "1"})
    title = await _seed_card_without_kind()
    try:
        _must_run("upgrade", HEAD_REVISION)

        assert await _card_meaning(title) == ("company", "other")
    finally:
        await _cleanup()
        _must_run("upgrade", "head")


async def test_downgrade_refuses_when_cards_lose_meaning() -> None:
    """H-1: карточка «свидание/кино» останавливает откат ДО изменения схемы.

    До правки: `downgrade 0036` проходил, `upgrade 0037` возвращал
    `company`/`other` — смысл стирался без единого сообщения.

    P-11: проверяются ОБЕ половины — отказ и целостность. Отказ без второй
    половины прошёл бы и при «гард сработал, но схему уже успел сломать».
    """
    _must_run("upgrade", "head")
    # ДВЕ карточки, а не одна: на одной сообщение «1 карточек» совпало бы
    # с литералом в коде, и мутация «печатать 1 вместо счётчика» выжила бы
    # (класс H-1 из CR 11.8: алерт «число 3» при любом реальном).
    title_one = await _seed_card(kind="date", activity="cinema")
    title_two = await _seed_card(kind="company", activity="sport")
    try:
        result = _run_alembic("downgrade", "0036")

        assert result.returncode != 0, "гард не сработал: откат прошёл молча"
        assert "Откат 0037 остановлен" in result.stderr, result.stderr
        assert "у 2 карточек" in result.stderr, (
            "в сообщении нет реального числа пострадавших строк:\n" + result.stderr
        )
        assert ALLOW_ENV in result.stderr, "оператор не узнал, как подтвердить выбор"

        # Вторая половина (P-11): схема и данные не пострадали.
        assert await _kind_column_exists(), "гард отказал, но колонку уже снёс"
        assert await _card_meaning(title_one) == ("date", "cinema")
        assert await _card_meaning(title_two) == ("company", "sport")
    finally:
        await _cleanup()
        _must_run("upgrade", "head")


async def test_downgrade_of_backfill_pair_is_allowed() -> None:
    """H-1: строки, равные паре бэкфилла, откат не блокируют.

    Пара к тесту выше: без неё гард «всегда отказывает» не отличался бы от
    гарда, который считает ЗНАЧИМЫЕ строки (класс одностороннего теста,
    урок 11.10). Именно этот путь используют чужие тесты, уходящие ниже
    `0037` (`test_attendance_*`, `test_reports_accused_id_migration`).
    """
    _must_run("upgrade", "head")
    title = await _seed_card(kind="company", activity="other")
    try:
        _must_run("downgrade", "0036")
        assert not await _kind_column_exists(), "откат не удалил колонки"

        _must_run("upgrade", HEAD_REVISION)
        assert await _card_meaning(title) == ("company", "other")
    finally:
        await _cleanup()
        _must_run("upgrade", "head")


async def test_meaning_loss_requires_explicit_opt_in() -> None:
    """H-1: осознанный откат возможен и ровно так теряет смысл, как обещано.

    Тест фиксирует КОНТРАКТ обхода: с `ALEMBIC_ALLOW_MEANING_LOSS=1` откат
    проходит, а повторный накат даёт `company`/`other` — то есть сообщение
    гарда правдиво, и оператор получает именно то, о чём его предупредили.
    """
    _must_run("upgrade", "head")
    title = await _seed_card(kind="date", activity="cinema")
    try:
        _must_run("downgrade", "0036", extra_env={ALLOW_ENV: "1"})
        _must_run("upgrade", HEAD_REVISION)

        assert await _card_meaning(title) == ("company", "other"), (
            "осознанный откат должен приводить ровно к паре бэкфилла"
        )
    finally:
        await _cleanup()
        _must_run("upgrade", "head")


def test_downgrade_refuses_offline_mode() -> None:
    """H-1: в offline-режиме (`--sql`) откат запрещён.

    Без соединения посчитать значимые строки невозможно, а печатать в скрипт
    разрушительный `DROP COLUMN` без проверки — значит отдать оператору
    защиту ровно там, где её не видно (прецедент гарда `0024`).
    """
    result = _run_alembic("downgrade", f"{HEAD_REVISION}:0036", "--sql")

    assert result.returncode != 0, "offline-откат напечатал скрипт без проверки"
    assert "offline-режиме" in result.stderr, result.stderr
