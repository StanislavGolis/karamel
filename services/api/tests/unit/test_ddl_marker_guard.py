"""Гард защиты DDL-тестов от xdist (AC-4 стори 12.9, `W-11.4-d`).

**Что защищается.** DDL-тесты (`pytest.mark.ddl`) исполняют
`alembic.command` на ОБЩЕЙ схеме: изоляции по схеме в проекте нет
(`search_path`/`schema_translate_map`/`CREATE SCHEMA` — 0 совпадений,
замер CS 12.9). Под `-n auto` воркер, читающий таблицу во время
`downgrade`, получает «relation does not exist». Хук
`pytest_collection_modifyitems` в `tests/integration/conftest.py`
пропускает такие тесты при `PYTEST_XDIST_WORKER`.

**Почему подпроцесс, а не проверка текста conftest (P-25).** Проверка
«в файле есть `os.environ.get("PYTEST_XDIST_WORKER")`» зеленела бы и от
закомментированной строки, и от `if False and ...`. Единственное
доказательство эффекта — запустить pytest и посмотреть, что тесты
действительно пропущены с причиной.

**Почему пара.** Односторонний тест («под env пропускаются») проходит и у
защиты, которая пропускает ВСЁ всегда (мутация M14): тогда без xdist DDL
не проверяется вообще, и никто не замечает. Второй тест требует, чтобы
без env тест ИСПОЛНЯЛСЯ.

**Стоимость.** Под env все 56 DDL-тестов скипаются за ~3 с. Без env
гоняется ОДИН тест (по `-k`), а не 56: пара доказывается одним
исполнением, а полный прогон DDL делает основной baseline (P-30).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[1]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Причина из хука — по ней отличаем наш skip от чужого (`skipif DATABASE_URL`).
DDL_SKIP_MARKER = "DDL меняет общую схему"

#: Один конкретный DDL-тест для проверки «без env исполняется».
#: Выбран синхронный тест цепочки: он короткий и не требует Redis.
SAMPLE_DDL_TEST = (
    "tests/integration/test_attendance_migration_cycle.py::"
    "test_downgrade_and_upgrade_leave_no_residue"
)


def _run_pytest(*args: str, worker: str | None) -> subprocess.CompletedProcess[str]:
    """Запускает pytest подпроцессом с env основного прогона или без воркера."""
    env = dict(os.environ)
    env.pop("PYTEST_XDIST_WORKER", None)
    if worker is not None:
        env["PYTEST_XDIST_WORKER"] = worker
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "--no-header", "-p", "no:cacheprovider"],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")
def test_all_ddl_tests_are_skipped_under_xdist() -> None:
    """AC-4: под `PYTEST_XDIST_WORKER` ВСЕ `ddl`-тесты в `skipped`, причина наша.

    Краснеет от удаления хука (M13): тогда 56 тестов пошли бы исполняться на
    общей схеме. Краснеет и от смены условия — см. пару ниже (M14).
    """
    result = _run_pytest("-q", "-m", "ddl", "-rs", worker="gw0")
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"прогон DDL под xdist не зелёный:\n{output[-3000:]}"

    match = re.search(r"(\d+) skipped", output)
    assert match is not None, f"в выводе нет строки `N skipped`:\n{output[-3000:]}"
    skipped = int(match.group(1))
    assert skipped > 0, "под xdist ни один DDL-тест не пропущен — защиты нет"

    # Ни один не исполнился и ни один не упал.
    assert " passed" not in output, f"под xdist DDL-тесты ИСПОЛНЯЛИСЬ:\n{output[-3000:]}"
    assert " failed" not in output, f"под xdist DDL-тесты падали:\n{output[-3000:]}"
    # Причина — наша, а не `skipif DATABASE_URL`.
    assert DDL_SKIP_MARKER in output, (
        f"skip без нашей причины (сработал чужой skipif?):\n{output[-3000:]}"
    )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")
def test_ddl_test_runs_without_xdist() -> None:
    """AC-4 (пара к M13/M14): без `PYTEST_XDIST_WORKER` DDL-тест ИСПОЛНЯЕТСЯ.

    Краснеет от безусловного `pytest.skip(...)` в хуке (M14): тогда DDL не
    проверялся бы никогда, а первый тест оставался бы зелёным.
    """
    result = _run_pytest("-q", SAMPLE_DDL_TEST, worker=None)
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"DDL-тест без xdist упал:\n{output[-3000:]}"
    assert "1 passed" in output, f"DDL-тест без xdist не исполнился:\n{output[-3000:]}"
    assert "skipped" not in output.split("warnings summary")[0], (
        f"без xdist DDL-тест пропущен — хук skip безусловен:\n{output[-3000:]}"
    )


def _uses_alembic_command(tree: ast.Module) -> bool:
    """Меняет ли файл схему: `command.upgrade/downgrade` ИЛИ сабпроцесс alembic.

    Читается AST, а не текст (H-3): строка `command.downgrade` в комментарии
    или в докстринге не делает файл DDL-тестом, и наоборот — обёртка над
    вызовом в хелпере обязана считаться.

    **Вторая форма вызова (стори 13.2).** DDL-тесты гоняют alembic и
    САБПРОЦЕССОМ: `subprocess.run([sys.executable, "-m", "alembic",
    "downgrade", ...])`. Это не стилистическая прихоть: `alembic.command`
    вызывает `asyncio.run()` внутри `env.py`, а тест уже работает в
    запущенном event loop. До 13.2 гард такую форму не видел и объявлял
    файл не-DDL. Замер перед правкой: **8 файлов** меняли схему сабпроцессом
    без маркера — `unmarked` был пуст, и P-32 не исполнялся именно там, где
    нужен.

    Проверяются только мутирующие подкоманды (`upgrade`, `downgrade`);
    `alembic check`, `heads`, `current` схему не меняют и маркера не требуют.
    """
    calls_command_api = False
    runs_alembic_subprocess = False
    calls_upgrade_or_downgrade = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Форма 1: `alembic.command.downgrade(config, "0039")`.
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"upgrade", "downgrade"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "command"
        ):
            calls_command_api = True
        first = node.args[0] if node.args else None
        # Форма 2а: `subprocess.run([sys.executable, "-m", "alembic", ...])`.
        if isinstance(first, (ast.List, ast.Tuple)):
            constants = {
                element.value
                for element in first.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            if "alembic" in constants:
                runs_alembic_subprocess = True
        # Форма 2б: `_run_alembic("downgrade", "0039")` — литерал первым
        # позиционным аргументом. Именно так устроены существующие
        # migration-cycle тесты: сабпроцесс спрятан в локальном хелпере.
        if isinstance(first, ast.Constant) and first.value in {"upgrade", "downgrade"}:
            calls_upgrade_or_downgrade = True
    # Форма 1 самодостаточна: `command.downgrade` уже называет мутирующую
    # подкоманду. Для сабпроцесса нужны ОБЕ половины: файл, читающий голову
    # (`alembic heads`) или сравнивающий модели (`alembic check`), схему не
    # меняет и маркера не требует.
    return calls_command_api or (runs_alembic_subprocess and calls_upgrade_or_downgrade)


def _has_ddl_marker(tree: ast.Module) -> bool:
    """Есть ли в файле `pytest.mark.ddl` (в `pytestmark` или на тесте).

    Проверяется именно выражение `pytest.mark.ddl`, а не строка `"ddl"`:
    `pytest.mark.skipif(...)` маркером DDL не является, а `-m ddl` в
    аргументах подпроцесса — тем более.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "ddl":
            continue
        mark = node.value
        if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
            continue
        if isinstance(mark.value, ast.Name) and mark.value.id == "pytest":
            return True
    return False


def test_ddl_marker_and_downgrade_are_bidirectional() -> None:
    """H-3: маркер `ddl` и `command.upgrade|downgrade` обязывают друг друга.

    **Дыра, которую закрывает тест.** CR 12.9 доказал дважды: снятие маркера
    с `test_access_log_migration.py` (**файл исполняет `downgrade`!**) дало
    `3 passed`, и лишний маркер на не-DDL файле — тоже `3 passed`. Прежний
    гард проверял «хук работает», но не «все DDL-файлы под хуком». Новый
    DDL-тест, забывший маркер, уронил бы сессию под xdist — ровно тот
    дефект, который чинит `W-11.4-d`.

    **Две стороны одной ошибки.** (1) Файл, меняющий схему, БЕЗ маркера —
    защита не сработает. (2) Маркер на файле, схему не меняющем, — тест
    молча пропускается под xdist, то есть теряется покрытие без причины.

    **Почему `test_*.py`, а не весь `tests/`.** `tests/integration/conftest.py`
    исполняет `command.upgrade(head)` в сессионной фикстуре — это подготовка
    схемы, а не DDL-тест: пропускать его нельзя, иначе не поднимется ни одна
    интеграционная проверка.
    """
    unmarked: list[str] = []
    mislabelled: list[str] = []
    checked = 0
    for path in sorted((API_ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        changes_schema = _uses_alembic_command(tree)
        marked = _has_ddl_marker(tree)
        relative = str(path.relative_to(API_ROOT))
        if changes_schema and not marked:
            unmarked.append(relative)
        if marked and not changes_schema:
            mislabelled.append(relative)
        if changes_schema:
            checked += 1

    assert checked > 0, "не найдено ни одного DDL-теста — гард ничего не проверяет"
    assert unmarked == [], (
        "DDL-тесты без маркера `ddl` — под xdist уронят общую схему: " + "; ".join(unmarked)
    )
    assert mislabelled == [], (
        "маркер `ddl` на файлах, не меняющих схему, — теряется покрытие под xdist: "
        + "; ".join(mislabelled)
    )


def test_ci_has_separate_ddl_step() -> None:
    """AC-4: в CI есть ОТДЕЛЬНЫЙ шаг `pytest -m ddl` без `-n`.

    Читается **YAML** через `safe_load`, а не подстрока файла (W-12.7-j):
    комментарий «нельзя `-n`» дал бы ложный зелёный, а шаг, положенный в
    закомментированный блок, — тоже.
    """
    assert CI_PATH.is_file(), f"нет файла CI: {CI_PATH}"
    workflow = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))

    api_job = workflow["jobs"]["api"]
    run_steps = [
        step["run"] for step in api_job["steps"] if isinstance(step, dict) and "run" in step
    ]

    ddl_steps = [cmd for cmd in run_steps if "-m ddl" in cmd]
    assert ddl_steps, f"в job `api` нет шага с `-m ddl`; шаги: {run_steps}"

    # `-n` ищем КАК ТОКЕН, а не подстроку: `--no-header` содержит `-n`, и
    # наивная проверка `"-n" in cmd` краснела на собственном корректном шаге
    # (поймано первым же прогоном гарда). Проверяются обе формы флага:
    # короткая `-n` / `-n4` и длинная `--numprocesses`.
    xdist_pattern = re.compile(r"(^|\s)(-n\s*\d*|--numprocesses)(\s|$)")
    for command in ddl_steps:
        assert not xdist_pattern.search(command), (
            f"DDL-шаг запускается с xdist — это ровно тот дефект, что чинится: {command}"
        )

    # Шаг стоит ПОСЛЕ основного прогона: иначе падение DDL скрыло бы общий результат.
    main_index = next(
        i for i, cmd in enumerate(run_steps) if "pytest" in cmd and "-m ddl" not in cmd
    )
    ddl_index = next(i for i, cmd in enumerate(run_steps) if "-m ddl" in cmd)
    assert ddl_index > main_index, "DDL-шаг стоит до основного прогона (P-32 п.1)"
