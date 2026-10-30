"""Реестр обратимости миграций, построенный ИЗ КАТАЛОГА (AC-5 стори 12.9).

**Что доказывается.** У КАЖДОЙ ревизии в `alembic/versions/` есть функция
`upgrade` и функция `downgrade`, и хотя бы один тест в `tests/` упоминает
эту ревизию рядом с вызовом `downgrade`. То есть миграцию можно откатить,
и откат кем-то проверен.

**Почему реестр из каталога, а не список из пяти ревизий.** Стори 12.9
требует проверить `0035`–`0039`. Литеральный список устареет на первой же
миграции `0040` и даст ложную зелень «все пять обратимы» при полностью
необратимой новой. Каталог — источник истины: появился файл, появилось
требование (мутация M16 — захардкодить диапазон — краснеет именно здесь).

**Почему отдельный тест на `alembic check`.** `alembic check` сравнивает
модели с БД и находит РАСХОЖДЕНИЕ СХЕМЫ, а не отсутствие `downgrade`.
Это разные оси: миграция без `downgrade` проходит `check` молча.

**Почему тест `alembic check` требует БД.** Без `DATABASE_URL` он
пропускается (`skipif`), а не зеленеет: `check` без БД ничего не сравнивает
(P-30 — «skipped > 0 это не baseline»).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

API_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = API_ROOT / "alembic" / "versions"
TESTS_DIR = API_ROOT / "tests"


def _revision_files() -> dict[str, Path]:
    """Ревизия → файл миграции. Ревизия — префикс имени файла до `_`.

    Каталог фильтруется по `0*.py`: `__pycache__` и посторонние файлы
    в каталоге миграций не ревизии.
    """
    return {
        path.stem.split("_")[0]: path
        for path in sorted(VERSIONS_DIR.glob("0*.py"))
        if re.fullmatch(r"\d{4}", path.stem.split("_")[0])
    }


def _has_function(path: Path, name: str) -> bool:
    """Есть ли в файле миграции функция верхнего уровня `name`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body)


def _missing_functions(revisions: dict[str, Path]) -> list[str]:
    """Ревизии, у которых нет `upgrade` или нет `downgrade` — обе стороны.

    Вынесено в чистую функцию ради проверяемости (M-3 CR 12.9): тест на
    реальном каталоге доказывает «у всех всё есть», но НЕ доказывает, что
    ветка `upgrade` в проверке жива. CR-17 убрал её — и 4 теста остались
    зелёными (`SURVIVED`): каталог-то в порядке, а проверка половины условия
    исчезла молча. Пара к этому тесту подаёт подставной файл без `upgrade`
    и требует записи в списке.
    """
    missing: list[str] = []
    for revision, path in revisions.items():
        if not _has_function(path, "upgrade"):
            missing.append(f"{revision}: нет upgrade ({path.name})")
        if not _has_function(path, "downgrade"):
            missing.append(f"{revision}: нет downgrade ({path.name})")
    return missing


@pytest.fixture(scope="module")
def revisions() -> dict[str, Path]:
    """Каталог ревизий: пустой каталог — ошибка замера, а не «нет миграций»."""
    found = _revision_files()
    assert found, f"в {VERSIONS_DIR} не найдено ни одной ревизии"
    return found


def test_every_revision_defines_upgrade_and_downgrade(revisions: dict[str, Path]) -> None:
    """AC-5: у каждой ревизии есть И `upgrade`, И `downgrade`.

    Краснеет на миграции без `downgrade` — продукт стал неоткатываемым, и
    деплой следующей версии не имеет пути назад. Проверка через `ast`, а не
    поиск подстроки: `def downgrade` в комментарии не считается.
    """
    missing = _missing_functions(revisions)
    assert missing == [], "неоткатываемые миграции: " + "; ".join(missing)


def test_missing_upgrade_is_detected(tmp_path: Path) -> None:
    """AC-5 (M-3 CR 12.9, пара): проверка ловит отсутствие `upgrade`, не только `downgrade`.

    **Дыра, которую закрывает тест.** CR 12.9 (CR-17) убрал ветку
    `_has_function(path, "upgrade")` — и все 4 теста остались зелёными
    (`SURVIVED`). Реальный каталог в порядке, поэтому проверка на реальном
    каталоге не может отличить «обе ветки живы» от «осталась одна». Здесь
    подаётся подставной файл, у которого `upgrade` НЕТ, а `downgrade` есть:
    единственная причина попасть в `missing` — живая ветка `upgrade`.
    """
    without_upgrade = tmp_path / "0099_only_downgrade.py"
    without_upgrade.write_text(
        "from alembic import op\n\ndef downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    missing = _missing_functions({"0099": without_upgrade})
    assert missing == ["0099: нет upgrade (0099_only_downgrade.py)"], (
        f"отсутствие `upgrade` не поймано: {missing}"
    )


def test_missing_downgrade_is_detected(tmp_path: Path) -> None:
    """AC-5 (M-3 CR 12.9, пара к тесту выше): отсутствие `downgrade` ловится тоже.

    Симметрия: миграция без `downgrade` — неоткатываемая, без `upgrade` —
    нерабочая. Одна ветка без второй означает, что проверка покрыта
    наполовину, и это не видно на здоровом каталоге.
    """
    without_downgrade = tmp_path / "0098_only_upgrade.py"
    without_downgrade.write_text(
        "from alembic import op\n\ndef upgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    missing = _missing_functions({"0098": without_downgrade})
    assert missing == ["0098: нет downgrade (0098_only_upgrade.py)"], (
        f"отсутствие `downgrade` не поймано: {missing}"
    )


def test_every_revision_has_a_cycle_test(revisions: dict[str, Path]) -> None:
    """AC-5: каждая ревизия упомянута в тесте, который вызывает `downgrade`.

    Замер CS 12.9: покрытие есть у всех 39 ревизий. Тест держит это
    состояние: новая миграция без цикла-теста упадёт здесь, а не всплывёт
    на деплое.

    Упоминание ревизии и `downgrade` ищутся в ОДНОМ файле — иначе «есть
    тест с downgrade» и «есть тест, где названа ревизия» были бы разными
    фактами, и связь между ними держалась бы на честном слове.
    """
    test_sources = [(path, path.read_text(encoding="utf-8")) for path in TESTS_DIR.rglob("*.py")]
    uncovered = [
        revision
        for revision in revisions
        if not any(revision in source and "downgrade" in source for _path, source in test_sources)
    ]
    assert uncovered == [], (
        f"ревизии без цикла-теста (упоминание + `downgrade` в одном файле): {uncovered}"
    )


def test_head_matches_versions_dir(revisions: dict[str, Path]) -> None:
    """AC-5: голова совпадает с НАИБОЛЬШЕЙ ревизией каталога.

    Независимая от `conftest.HEAD_REVISION` проверка (та живёт в
    `test_head_revision_constant.py`): здесь обе стороны вычисляются из
    разных источников — `ScriptDirectory` и список файлов каталога. Развилка
    голов или пропавший файл краснеют тут, даже если константу обновили.
    """
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    heads = tuple(ScriptDirectory.from_config(config).get_heads())
    greatest = max(revisions)
    assert heads == (greatest,), (
        f"голова {heads} не совпадает с наибольшей ревизией каталога {greatest!r}"
    )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")
def test_alembic_check_is_clean() -> None:
    """AC-5: `alembic check` не находит расхождений схемы с моделями.

    Краснеет, если модель изменена без миграции (или миграция применена не
    до головы). `returncode` сверяется с нулём, а не «нет слова error»:
    alembic печатает диагностику в stdout и возвращает 1.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"`alembic check` вернул {result.returncode}:\n"
        f"stdout:\n{result.stdout[-3000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
    assert "No new upgrade operations detected" in result.stdout, (
        f"`check` не подтвердил чистоту явно:\n{result.stdout[-2000:]}"
    )
