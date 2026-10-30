"""Тесты инструмента `scripts/mutate.py` (CR 9.1 H-3; долг D-97).

**Почему это тесты, а не «проверено руками».** Инструмент — часть
доказательной базы процесса: таблица мутаций в стори ссылается на его коды
выхода. Если инструмент врёт, врёт весь отчёт о покрытии. CR 9.1 нашёл именно
такой дефект: без baseline несуществующий тест давал `KILLED` (код 0),
то есть «мутация поймана» — при том, что тест не запускался вообще.

**Почему `subprocess`, а не импорт модуля.** Контракт инструмента — КОД
ВЫХОДА. Импорт и вызов `main()` проверил бы возвращаемое значение, но не то,
что видит вызывающий в shell: `sys.exit(main())` мог бы потерять код, а
`argparse` — завершить процесс раньше. Тест гоняет ровно прод-сценарий.

**Почему временная папка.** Мутация пишет в файл и восстанавливает его в
`finally`. Тестировать на реальном коде значило бы рисковать рабочим деревом
при баге в восстановлении. Здесь файл и тест создаются в `tmp_path`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

#: Инструмент лежит ВНЕ `services/api` (репозиторный `scripts/`): он общий
#: для всех сервисов, а не принадлежит API.
#:
#: `parents[4]`, а не `parents[5]` (CI run 41137571125). До правки путь вёл
#: на уровень ВЫШЕ корня репозитория — в каталог, которого в раннере нет.
#: Локально `mutate.py` там случайно лежал, и 7 тестов исполнялись; в CI
#: условие `skipif(not MUTATE.exists())` срабатывало, и весь файл молча
#: скипался: `3358 passed, 7 skipped` вместо полного прогона. Это долг
#: `W-12.1-b` («раннер мутаций вне репо») в его худшей форме — тест,
#: который в CI не проверяет ничего и при этом выглядит зелёным.
MUTATE = Path(__file__).resolve().parents[4] / "scripts" / "mutate.py"

pytestmark = pytest.mark.skipif(not MUTATE.exists(), reason=f"нет инструмента: {MUTATE}")


def _run(tmp_path: Path, *, old: str, new: str, test: str) -> subprocess.CompletedProcess[str]:
    """Один прогон инструмента на мини-проекте в `tmp_path`."""
    return subprocess.run(
        [
            sys.executable,
            str(MUTATE),
            "--file",
            str(tmp_path / "target.py"),
            "--old",
            old,
            "--new",
            new,
            "--test",
            test,
            "--cwd",
            str(tmp_path),
            "--python",
            sys.executable,
            "--quiet",
            "--timeout",
            "120",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _scaffold(tmp_path: Path, test_body: str) -> None:
    """Мини-проект: файл с константой и тест, который его проверяет."""
    (tmp_path / "target.py").write_text('VALUE = "ok"\n', encoding="utf-8")
    (tmp_path / "test_min.py").write_text(test_body, encoding="utf-8")


#: Тест, который падает ровно тогда, когда мутация сломала `VALUE`.
#: Это и есть образец «годного» теста: он читает мутируемый объект.
_MUTATION_SENSITIVE = """
from target import VALUE


def test_value() -> None:
    assert VALUE == "ok"
"""

#: Тест, который НЕ читает мутируемый объект: мутация его не касается.
#: Образец СЛЕПОГО теста — он должен дать SURVIVED (код 1).
_MUTATION_BLIND = """
def test_always_green() -> None:
    assert 1 + 1 == 2
"""


#: Тест, который красный САМ ПО СЕБЕ (baseline-RED).
_BASELINE_RED = """
def test_always_red() -> None:
    assert False, "сломан намеренно"
"""

#: Тест, который СКИПНУТ (baseline-SKIPPED, M-9.2-2).
#:
#: `pytest` возвращает 0, когда тест скипнут: для него это успех. Для
#: мутационного инструмента — отсутствие результата, и до фикса прогон
#: рапортовал ложный `SURVIVED` (код 1) вместо кода 4.
_BASELINE_SKIPPED = """
import pytest


@pytest.mark.skip(reason="нужно окружение")
def test_skipped() -> None:
    assert 1 + 1 == 2
"""


# ---------------------------------------------------------------------------
# 4 сценария контракта: 0 / 1 / 2 / 3
# ---------------------------------------------------------------------------


def test_baseline_green_and_mutation_breaks_test_gives_killed(tmp_path: Path) -> None:
    """Сценарий 1: baseline зелёный, мутация ломает тест → код 0 (`KILLED`).

    Краснеет, если инструмент перестанет возвращать `EXIT_KILLED` на
    состоявшейся мутации (например, если baseline начнёт затирать результат).
    """
    _scaffold(tmp_path, _MUTATION_SENSITIVE)

    completed = _run(tmp_path, old='VALUE = "ok"', new='VALUE = "broken"', test="test_min.py")

    assert completed.returncode == 0, completed.stderr
    assert "KILLED" in completed.stdout


def test_baseline_green_and_mutation_survives_gives_survived(tmp_path: Path) -> None:
    """Сценарий 2: baseline зелёный, мутация НЕ ломает тест → код 1 (`SURVIVED`).

    Это главный сценарий D-98: слепой тест обязан быть виден как код 1, а не
    как «поймано». Краснеет, если инструмент начнёт считать зелёный прогон
    успехом мутации.
    """
    _scaffold(tmp_path, _MUTATION_BLIND)

    completed = _run(tmp_path, old='VALUE = "ok"', new='VALUE = "broken"', test="test_min.py")

    assert completed.returncode == 1, completed.stderr
    assert "SURVIVED" in completed.stderr


def test_mutation_not_applied_gives_code_2(tmp_path: Path) -> None:
    """Сценарий 3: образец не найден → код 2 (`MUTATION-NOT-APPLIED`).

    Проверяется ДО baseline: о тесте вывод невозможен, но и прогон не нужен.
    Краснеет, если проверка образца исчезнет (тогда мутации «не применились»
    молча считались бы выжившими или пойманными).
    """
    _scaffold(tmp_path, _MUTATION_SENSITIVE)

    completed = _run(tmp_path, old="НЕТ_ТАКОЙ_СТРОКИ", new="x", test="test_min.py")

    assert completed.returncode == 2, completed.stderr
    assert "MUTATION-NOT-APPLIED" in completed.stderr


def test_baseline_red_gives_code_3(tmp_path: Path) -> None:
    """Сценарий 4 (H-3): тест красный УЖЕ без мутации → код 3 (`BASELINE-RED`).

    **Дефект, который закрывает этот тест.** До фикса инструмент сообщал
    `KILLED` и код 0 — то есть подтверждал «мутация поймана» на тесте,
    который не проходит вообще (нет `DATABASE_URL`, опечатка в имени,
    сломанный импорт). В отчёте это выглядело бы как 24 из 24 пойманных.

    Краснеет, если baseline-прогон убрать: тогда код станет 0 (KILLED).
    """
    _scaffold(tmp_path, _BASELINE_RED)

    completed = _run(tmp_path, old='VALUE = "ok"', new='VALUE = "broken"', test="test_min.py")

    assert completed.returncode == 3, completed.stderr
    assert "BASELINE-RED" in completed.stderr
    assert "KILLED" not in completed.stdout, "инструмент выдал ложный KILLED"


def test_baseline_skipped_gives_code_4(tmp_path: Path) -> None:
    """M-9.2-2 (CR 9.2 r2): скипнутый тест в baseline → код 4, а НЕ 1.

    **Дефект, который закрывает этот тест.** `pytest` возвращает `0`, когда
    все собранные тесты скипнуты: для него это успех. До фикса инструмент
    считал такой baseline зелёным, применял мутацию и рапортовал
    `SURVIVED` (код 1) — то есть объявлял тест СЛЕПЫМ, хотя тот не
    выполнялся (нет `DATABASE_URL`). Это тот же класс, что H-3, но через
    `skip`.

    Краснеет, если убрать проверку `_has_skipped`: код станет 1 (SURVIVED)
    или 0 (KILLED) вместо 4.
    """
    _scaffold(tmp_path, _BASELINE_SKIPPED)

    completed = _run(tmp_path, old='VALUE = "ok"', new='VALUE = "broken"', test="test_min.py")

    assert completed.returncode == 4, (
        f"ожидался код 4 (BASELINE-SKIPPED), получен {completed.returncode}; "
        f"stderr={completed.stderr!r}"
    )
    assert "BASELINE-SKIPPED" in completed.stderr
    assert "SURVIVED" not in completed.stderr, "инструмент объявил тест слепым без прогона"
    assert "KILLED" not in completed.stdout


def test_missing_interpreter_gives_code_2(tmp_path: Path) -> None:
    """X-9.1-1: несуществующий интерпретатор → код 2, а не 1 (`SURVIVED`).

    **Дефект, который закрывает этот тест.** Проверка была написана как
    `if not (python.is_absolute() or Path(args.cwd, args.python).exists())`:
    при абсолютном пути первое условие всегда `True`, вся ветка пропускалась,
    и `subprocess` падал с `FileNotFoundError`. Инструмент сообщал `SURVIVED`
    (код 1) — то есть обвинял тест в слепоте из-за опечатки в `--python`,
    хотя тест не запускался вообще. Это ложный вывод в отчёте о покрытии.

    Проверяется абсолютный несуществующий путь: именно он обходил проверку.
    Краснеет, если вернуть инверсию условия или убрать проверку существования.
    """
    _scaffold(tmp_path, _MUTATION_SENSITIVE)

    completed = subprocess.run(
        [
            sys.executable,
            str(MUTATE),
            "--file",
            str(tmp_path / "target.py"),
            "--old",
            'VALUE = "ok"',
            "--new",
            'VALUE = "broken"',
            "--test",
            "test_min.py",
            "--cwd",
            str(tmp_path),
            "--python",
            str(tmp_path / "no-such-python"),
            "--quiet",
            "--timeout",
            "120",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 2, (
        f"ожидался код 2 (MUTATION-NOT-APPLIED), получен {completed.returncode}; "
        f"stderr={completed.stderr!r}"
    )
    assert "MUTATION-NOT-APPLIED" in completed.stderr
    assert "SURVIVED" not in completed.stderr, "инструмент ложно обвинил тест в слепоте"
    assert "KILLED" not in completed.stdout


def test_file_restored_after_every_scenario(tmp_path: Path) -> None:
    """`finally`-восстановление: файл не остаётся мутированным ни в одном исходе.

    Без этого следующий прогон тестов работал бы с испорченным деревом, и
    ложная зелень распространилась бы на весь набор. Проверяются ОБА исхода,
    меняющие файл: KILLED и SURVIVED.
    """
    _scaffold(tmp_path, _MUTATION_SENSITIVE)
    _run(tmp_path, old='VALUE = "ok"', new='VALUE = "broken"', test="test_min.py")
    assert (tmp_path / "target.py").read_text(encoding="utf-8") == 'VALUE = "ok"\n'

    _scaffold(tmp_path, _MUTATION_BLIND)
    _run(tmp_path, old='VALUE = "ok"', new='VALUE = "broken"', test="test_min.py")
    assert (tmp_path / "target.py").read_text(encoding="utf-8") == 'VALUE = "ok"\n'
