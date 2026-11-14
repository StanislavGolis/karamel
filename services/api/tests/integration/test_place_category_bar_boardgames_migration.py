"""Миграция `0040` — категории «бар» и «настольные игры» (стори 13.2; AC-1, AC-2).

**Что доказывается.** Белый список `places.category` и `event_cards.category`
расширен ДВУМЯ значениями (`bar`, `boardgames`), оба CHECK пересозданы,
откат обратим и не теряет данные молча.

**Почему проверки идут через `pg_constraint`, а не через модель.** Модель
описывает намерение, AC говорит о фактической схеме. Тест «`PlaceCategory`
содержит `bar`» прошёл бы и при миграции, забывшей пересоздать CHECK.

**Почему оба CHECK, а не один (V-4 стори).** Ограничения `places` и
`event_cards` независимы. Пересоздание только одного оставляет вторую
таблицу узкой: карточка с `category='bar'` получала бы `IntegrityError` при
валидном справочнике — дефект, который выглядит как ошибка клиента.

Краснеет при:
- непересоздании `ck_places_category` — вставка `bar` падает;
- непересоздании `ck_event_cards_category` — то же для карточки;
- удалении подсчёта строк в `downgrade` — откат с данными не падает;
- замене сравнения `!= "1"` на `!= "0"` — обход перестаёт работать;
- снятии CHECK без создания — `theatre` прошёл бы.

**P-32.** Помечен `pytest.mark.ddl`: откатывает ОБЩУЮ схему; хук в
`tests/integration/conftest.py` превращает маркер в `skip` при xdist.

**P-33 не применяется:** колонок не добавляется, `pg_attribute` не расходуется.
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ddl,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

API_ROOT = Path(__file__).resolve().parents[2]

#: Проверяемая ревизия и предыдущая. Литералы осознанно: тест про ЭТУ миграцию,
#: `HEAD_REVISION` подставил бы следующую голову и проверял бы не то.
_MIGRATION = "0040"
_PREVIOUS = "0039"

#: AC-1: значения, которых до миграции не было.
_NEW_CATEGORIES = ("bar", "boardgames")

#: Переменная осознанного отката (AC-2). Допустимо ровно `1`: любое непустое
#: (`yes`, `true`) обязано останавливать откат, иначе опечатка оператора стирает
#: данные без предупреждения (класс M07, стори 12.4).
_MEANING_LOSS_ENV = "ALEMBIC_ALLOW_MEANING_LOSS"

#: Ограничения обеих таблиц: имя и таблица.
_CHECKS = (
    ("ck_places_category", "places"),
    ("ck_event_cards_category", "event_cards"),
)


def _run_alembic(
    *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`python -m alembic <args>` отдельным процессом.

    Сабпроцесс, а не `alembic.command`: тот вызывает `asyncio.run()` внутри
    `env.py`, а тест уже работает в запущенном event loop. Переменную
    окружения тоже можно передать только процессу — правка `os.environ`
    откатывалась бы не гарантированно.
    """
    env = dict(os.environ)
    env.pop(_MEANING_LOSS_ENV, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _alembic_ok(*args: str, env_extra: dict[str, str] | None = None) -> None:
    result = _run_alembic(*args, env_extra=env_extra)
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} упал ({result.returncode}):\n"
        f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
    )


async def _fetch(sql: str, **params: Any) -> list[tuple]:
    """Читает в СВОЁМ соединении и закрывает его до возврата.

    `ALTER TABLE DROP CONSTRAINT` требует `ACCESS EXCLUSIVE`; сессия фикстуры
    держала бы `ACCESS SHARE` и ждала бы вечно.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


async def _execute(sql: str, **params: Any) -> None:
    """Пишет отдельной транзакцией: данные обязаны пережить откат схемы."""
    await _insert(sql, **params)


async def _insert(sql: str, **params: Any) -> list[tuple]:
    """Пишет и КОММИТИТ, возвращая строки `RETURNING`.

    **Почему не `_fetch`.** `engine.connect()` в SQLAlchemy 2.0 открывает
    неявную транзакцию, которая откатывается при закрытии соединения. Первая
    версия теста вставляла место через `_fetch`, `INSERT` откатывался, и
    гард `downgrade` видел ноль строк: тест «откат падает на данных» был
    зелёным при полностью неработающем гарде. Здесь коммит явный.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


async def _check_definition(name: str) -> str:
    """Тело ограничения из каталога — проверяется ФАКТ, а не наличие `raise`."""
    rows = await _fetch(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name",
        name=name,
    )
    assert len(rows) == 1, f"ограничение {name} не найдено в каталоге"
    return str(rows[0][0])


def _apply_migration() -> None:
    """Ставит схему на голову **с отката** (урок 12.1).

    `upgrade head` на голове — no-op: тест зеленел бы на схеме, которую
    миграция не трогала. Откат на `_PREVIOUS` гарантирует исполнение `upgrade`.
    """
    _alembic_ok("downgrade", _PREVIOUS)
    _alembic_ok("upgrade", "head")


async def _clean_risky_rows() -> None:
    """Убирает строки с новыми категориями перед откатом.

    Нужно, чтобы `downgrade` в `_apply_migration` проходил БЕЗ переменной
    обхода: тест успешного отката не должен зависеть от данных, оставшихся
    от теста отказа. Иначе порядок тестов влиял бы на результат.
    """
    await _execute(
        "DELETE FROM event_cards WHERE category = ANY(:values)",
        values=list(_NEW_CATEGORIES),
    )
    await _execute("DELETE FROM places WHERE category = ANY(:values)", values=list(_NEW_CATEGORIES))


def _city() -> str:
    """Город по умолчанию из настроек — без литерала `tomsk` в тесте."""
    from app.core.config import settings

    return str(settings.default_city_slug)


async def _make_place(category: str = "bar") -> tuple[int, str]:
    """Место с заданной категорией. Возвращает `(id, uuid)`."""
    suffix = uuid_lib.uuid4().hex[:10]
    rows = await _insert(
        "INSERT INTO places (name, category, address_text, city_slug, is_active) "
        "VALUES (:name, :category, 'ул. Мира, 1', :slug, true) RETURNING id, uuid",
        name=f"Бар {suffix}",
        category=category,
        slug=_city(),
    )
    place_id, place_uuid = rows[0]
    return int(place_id), str(place_uuid)


async def _make_user() -> int:
    """Пользователь-автор: `author_id` карточки — FK на `users.id`."""
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    rows = await _insert(
        "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
        "city_slug) VALUES ('phone', :pid, :phone, 'user', 'female', 'active', :slug) "
        "RETURNING id",
        pid=phone,
        phone=phone,
        slug=_city(),
    )
    return int(rows[0][0])


async def _make_card(*, author_id: int, place_id: int, category: str) -> int:
    """Карточка с заданной категорией — вторая таблица со своим CHECK."""
    rows = await _insert(
        "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
        "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
        "filled_count, verification_status, kind, activity) "
        "VALUES (:author, :place, :slug, 'Встреча', NULL, :category, "
        "now() + interval '3 days', 1, 60, now() + interval '1 hour', 'draft', 0, "
        "'pending', 'company', 'boardgames') RETURNING id",
        author=author_id,
        place=place_id,
        slug=_city(),
        category=category,
    )
    return int(rows[0][0])


# --- AC-1: расширение белого списка -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("constraint", "table"), _CHECKS)
async def test_upgrade_widens_check_on_both_tables(constraint: str, table: str) -> None:
    """AC-1, V-4: оба CHECK содержат `bar` и `boardgames`, легаси целы.

    Параметризация по обеим таблицам — это и есть V-4: пересоздание одного
    ограничения оставляет вторую таблицу узкой, и ошибка невидима, если
    проверять только `places`.

    Проверка легаси-значений — пара к расширению: мутация «оставить только
    два новых значения» иначе была бы зелёной.
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        definition = await _check_definition(constraint)
        for value in _NEW_CATEGORIES:
            assert value in definition, (
                f"{constraint} ({table}) не содержит {value!r}: список не расширен"
            )
        for legacy in ("cafe", "cinema", "other"):
            assert legacy in definition, (
                f"{constraint} ({table}) потерял легаси-значение {legacy!r}"
            )
    finally:
        await _clean_risky_rows()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["places", "event_cards"])
async def test_upgrade_accepts_bar_in_both_tables(table: str) -> None:
    """AC-1, P-25: строка с `category='bar'` реально вставляется.

    Каталог может содержать нужное определение, а проверка не срабатывать
    из-за второго, узкого CHECK на той же колонке. Вставка отличает
    «определение расширено» от «ограничение выполняет расширение».
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        if table == "places":
            await _make_place(category="bar")
        else:
            author_id = await _make_user()
            place_id, _ = await _make_place()
            await _make_card(author_id=author_id, place_id=place_id, category="bar")
    finally:
        await _clean_risky_rows()


@pytest.mark.asyncio
async def test_upgrade_rejects_unknown_category() -> None:
    """AC-1, пара к предыдущему: расширение не открыло список целиком.

    Без этого теста мутация «снять CHECK совсем» (`DROP` без `CREATE`) была бы
    зелёной: `bar` вставлялся бы — и `theatre` тоже.
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        with pytest.raises(IntegrityError) as excinfo:
            await _make_place(category="theatre")
        assert "ck_places_category" in str(excinfo.value)
    finally:
        await _clean_risky_rows()


# --- AC-2: откат -------------------------------------------------------------


@pytest.mark.asyncio
async def test_downgrade_restores_narrow_list() -> None:
    """AC-2: после отката новых значений в обоих CHECK нет, легаси целы."""
    await _clean_risky_rows()
    _apply_migration()

    try:
        _alembic_ok("downgrade", _PREVIOUS)
        for constraint, _table in _CHECKS:
            definition = await _check_definition(constraint)
            for value in _NEW_CATEGORIES:
                assert value not in definition, (
                    f"{constraint} после отката всё ещё содержит {value!r}"
                )
            for legacy in ("cafe", "cinema", "other"):
                assert legacy in definition, f"{constraint} потерял легаси {legacy!r}"
    finally:
        _alembic_ok("upgrade", "head")
        await _clean_risky_rows()


@pytest.mark.asyncio
async def test_downgrade_fails_when_places_reference_new_categories() -> None:
    """AC-2, V-3: откат ПАДАЕТ, если есть место с новой категорией.

    Строка вставляется МЕЖДУ `upgrade` и `downgrade` (прецедент 11.5 M5):
    если вставить её до `upgrade`, ограничение её не пропустит, и тест
    проверял бы не поведение `downgrade`.

    Краснеет, если убрать подсчёт строк: откат дойдёт до `ADD CONSTRAINT` и
    упадёт на `check constraint ... is violated by some row` — сообщение
    говорит о нарушении, а не о том, сколько строк и в какой таблице виноваты.
    Гард обязан останавливать ДО изменения схемы.
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        await _make_place(category="bar")

        result = _run_alembic("downgrade", _PREVIOUS)
        assert result.returncode != 0, (
            "откат прошёл, хотя в places есть строка с category='bar' — "
            "гард подсчёта строк не работает"
        )
        combined = result.stderr + result.stdout
        assert _MEANING_LOSS_ENV in combined, (
            f"сообщение не называет переменную обхода: {combined[-1000:]}"
        )
    finally:
        _alembic_ok("upgrade", "head")
        await _clean_risky_rows()


@pytest.mark.asyncio
async def test_downgrade_fails_when_cards_reference_new_categories() -> None:
    """AC-2: то же для `event_cards` — второй таблицы, не только `places`.

    Пара к предыдущему: гард, считающий только `places`, оставил бы карточку
    с `boardgames` невидимой, и откат падал бы на DDL вместо внятного отказа.
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        author_id = await _make_user()
        place_id, _ = await _make_place(category="cafe")
        await _make_card(author_id=author_id, place_id=place_id, category="boardgames")

        result = _run_alembic("downgrade", _PREVIOUS)
        assert result.returncode != 0, (
            "откат прошёл, хотя в event_cards есть строка с category='boardgames' "
            "— гард считает только places"
        )
    finally:
        _alembic_ok("upgrade", "head")
        await _clean_risky_rows()


@pytest.mark.asyncio
async def test_meaning_loss_override_replaces_rows_and_succeeds() -> None:
    """AC-2, V-3: обход проходит и переводит строки в `other`.

    Фиксируется не «откат не упал», а конкретная семантика: строки остаются
    (удалять нельзя — `event_cards.place_id` ссылается на `places.id`),
    категория заменяется на `other` — единственное значение, проходящее оба
    списка.

    Краснеет при:
    - сравнении с `!= "0"` (M5, класс M07) — обход не срабатывает на `1`;
    - удалении строк вместо замены — карточка потеряла бы место;
    - замене на значение вне обоих списков — `ADD CONSTRAINT` упал бы.
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        place_id, _ = await _make_place(category="bar")
        rows = await _fetch("SELECT category FROM places WHERE id = :id", id=place_id)
        assert str(rows[0][0]) == "bar"

        _alembic_ok("downgrade", _PREVIOUS, env_extra={_MEANING_LOSS_ENV: "1"})

        rows = await _fetch("SELECT category FROM places WHERE id = :id", id=place_id)
        assert rows, "строка места удалена — нельзя: на неё ссылаются карточки"
        assert str(rows[0][0]) == "other", (
            f"категория не заменена на other, получено {rows[0][0]!r}"
        )
    finally:
        _alembic_ok("upgrade", "head")
        await _clean_risky_rows()


@pytest.mark.asyncio
async def test_meaning_loss_override_requires_exactly_one() -> None:
    """AC-2, M07: `=yes` НЕ считается подтверждением.

    Сравнение `!= "1"` не отличается от «любое непустое» до тех пор, пока
    кто-то не напишет `=yes`. Тогда откат стирает категории без явного
    подтверждения — ровно то, от чего гард защищает. Пара к предыдущему:
    там `1` работает, здесь `yes` — нет.
    """
    await _clean_risky_rows()
    _apply_migration()

    try:
        await _make_place(category="bar")

        result = _run_alembic("downgrade", _PREVIOUS, env_extra={_MEANING_LOSS_ENV: "yes"})
        assert result.returncode != 0, (
            "откат прошёл при ALEMBIC_ALLOW_MEANING_LOSS=yes: подтверждением "
            "считается любое непустое значение вместо строго '1'"
        )
    finally:
        _alembic_ok("upgrade", "head")
        await _clean_risky_rows()


@pytest.mark.asyncio
async def test_offline_downgrade_is_refused() -> None:
    """AC-2: `downgrade --sql` запрещён — без соединения строки не посчитать.

    В offline-режиме соединения нет, и гард молча пропустил бы откат, отдав
    скрипт без защиты (класс H-1 стори 12.4).
    """
    # `--sql` требует явной пары ревизий: `downgrade 0039 --sql` неоднозначен.
    result = _run_alembic("downgrade", f"{_MIGRATION}:{_PREVIOUS}", "--sql")
    assert result.returncode != 0, "offline-откат отдал SQL без гарда"
    combined = (result.stderr + result.stdout).lower()
    assert "offline" in combined, f"отказ не объясняет причину: {combined[-1000:]}"


# --- Цикл целиком (AC-2) -----------------------------------------------------


@pytest.mark.asyncio
async def test_full_cycle_is_reversible() -> None:
    """AC-2: `0040 → 0039 → 0040` без данных проходит и возвращает схему.

    Без данных откат обязан проходить БЕЗ переменной обхода: пустая таблица
    смысла не теряет, и требовать подтверждения было бы ложной тревогой.
    Это ловит гард, который останавливает откат ВСЕГДА.
    """
    await _clean_risky_rows()
    _alembic_ok("downgrade", _PREVIOUS)

    try:
        for constraint, _table in _CHECKS:
            definition = await _check_definition(constraint)
            for value in _NEW_CATEGORIES:
                assert value not in definition

        _alembic_ok("upgrade", "head")
        for constraint, _table in _CHECKS:
            definition = await _check_definition(constraint)
            for value in _NEW_CATEGORIES:
                assert value in definition, f"{constraint} не расширен после повторного upgrade"
    finally:
        _alembic_ok("upgrade", "head")
        await _clean_risky_rows()
