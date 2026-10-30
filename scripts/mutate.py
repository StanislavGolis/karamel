#!/usr/bin/env python3
"""Мутационное тестирование одной правкой (долг D-97, правило D-98).

**Зачем.** Шесть эпиков подряд (6.7, 7.4, 7.5, 8.3–8.6) CR/DS находили
СЛЕПЫЕ тесты: зелёные при снятой мутации. Причина не в лени — мутацию
применяли руками, и в 8.6 четыре из двенадцати «не применились» (образец
не найден) или «покраснели не туда». Руками этот класс ошибки не ловится.

**Что делает.** Четыре шага, и каждый — проверка:

1. **BASELINE: прогон теста ДО мутации на чистом файле.** Если тест уже
   красный (нет `DATABASE_URL`, сломан импорт, опечатка в имени теста),
   результат мутации НЕИЗВЕСТЕН: «покраснел» ничего не доказывает.
   Это дефект H-3 из CR 9.1: без baseline скрипт сообщал `KILLED` и код 0
   на несуществующем тесте — то есть активно врал в безопасную сторону.
   Прогон, где забыт `DATABASE_URL`, давал бы 24 из 24 `KILLED`.
2. `cp`-бэкап файла и **проверка, что после правки файл реально изменился**.
   Мутация, которая «не применилась», даёт зелёный тест и ЛОЖНЫЙ вывод
   «тест слепой» — это хуже отсутствия проверки.
3. Прогон того же теста на мутированном файле.
4. Восстановление из бэкапа в `finally` — даже при падении прогона, Ctrl-C,
   исключении в самом скрипте. Без `finally` рабочее дерево осталось бы
   с мутацией, и следующий прогон тестов был бы ложным.

**Чего НЕ делает.** Не пишет отчёт в файл, не гоняет весь набор, не
параллелит, не «умно» подбирает тесты. Это инструмент одной проверки;
таблица мутаций живёт в стори, отчёт — в Dev Agent Record.

Использование:

    python scripts/mutate.py \\
        --file karamel/services/api/app/models/notification.py \\
        --old 'name="type_format"' --new 'name="type_formatX"' \\
        --test 'tests/integration/test_notifications_support_schema.py::test_x' \\
        --cwd karamel/services/api

Коды выхода:

| Код | Значение | Что делать |
|---|---|---|
| 0 | `KILLED` — baseline зелёный, после мутации красный | тест не слепой, мутация годная |
| 1 | `SURVIVED` — после мутации зелёный | слепой тест ИЛИ не та мутация (D-98) |
| 2 | `MUTATION-NOT-APPLIED` — образец не найден / файл не изменён | о тесте вывод невозможен |
| 3 | `BASELINE-RED` — тест красный УЖЕ без мутации | починить окружение/имя теста |
| 4 | `BASELINE-SKIPPED` — в baseline есть `skipped` | включить окружение (`DATABASE_URL`) |

**Почему код 4 отдельный, а не `BASELINE-RED`** (CR 9.2 r2, долг M-9.2-2).
`pytest` возвращает `0`, когда ВСЕ собранные тесты скипнуты: для него это
успех. Без этой проверки прогон без `DATABASE_URL` рапортовал ложный
`SURVIVED` (код 1) — «тест слепой» вместо «окружения нет». Код 4 отличает
причину от красного baseline: решение о тесте НЕ принято, и в таблицу
мутаций такой прогон не годится.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: Коды выхода — часть контракта инструмента: таблица мутаций в стори
#: фиксирует ФАКТИЧЕСКИЙ код, и смешение значений сделало бы отчёт ложным.
EXIT_KILLED = 0
EXIT_SURVIVED = 1
EXIT_NOT_APPLIED = 2
EXIT_BASELINE_RED = 3
#: CR 9.2 r2 (долг M-9.2-2): в baseline есть `skipped` — тест НЕ выполнялся.
#: Отдельный код, а не `BASELINE-RED`: причина другая (нет окружения против
#: красного теста), и диагностика в отчёте должна быть однозначной.
EXIT_BASELINE_SKIPPED = 4


def _run_pytest(python: str, test: str, cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Один прогон целевого теста. `returncode != 0` = красный."""
    # Аргументы формирует вызывающий, не пользователь ввода — shell-инъекция
    # невозможна: список передаётся без `shell=True`.
    return subprocess.run(
        [python, "-m", "pytest", "-q", "--no-header", test],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _has_skipped(completed: subprocess.CompletedProcess[str]) -> bool:
    """`True` — в выводе pytest есть хотя бы один `skipped` (M-9.2-2).

    **Почему парсится текст, а не код выхода.** `pytest` возвращает `0`,
    когда ВСЕ собранные тесты скипнуты: для него это успех. Для
    мутационного инструмента это ОТСУТСТВИЕ результата — «тест не
    выполнялся», и вывод о мутации делать нельзя.

    Ищется слово `skipped` в summary-строке (`--no-header -q` даёт
    `1 skipped in 0.83s`). При `returncode == 0` трейсбека нет, поэтому
    ложное срабатывание на тексте ошибки невозможно.
    """
    output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    return "skipped" in output


def _tail(completed: subprocess.CompletedProcess[str], lines: int = 12) -> str:
    return "\n".join((completed.stdout or "").strip().splitlines()[-lines:])


def main() -> int:
    parser = argparse.ArgumentParser(description="Применить одну мутацию и проверить, краснеет ли тест")
    parser.add_argument("--file", required=True, help="файл, который мутируем")
    parser.add_argument("--old", required=True, help="точный текст, который заменяем")
    parser.add_argument("--new", required=True, help="на что заменяем")
    parser.add_argument("--test", required=True, help="целевой тест (путь или путь::имя)")
    parser.add_argument("--cwd", default=".", help="рабочая папка прогона (где pytest)")
    parser.add_argument("--python", default=".venv/bin/python", help="интерпретатор для pytest")
    parser.add_argument("--timeout", type=int, default=600, help="таймаут прогона, сек")
    parser.add_argument("--quiet", action="store_true", help="не печатать хвост прогонов")
    args = parser.parse_args()

    target = Path(args.file)
    if not target.exists():
        print(f"MUTATION-NOT-APPLIED: файла нет: {target}", file=sys.stderr)
        return EXIT_NOT_APPLIED

    # H-3 (CR 9.1) + X-9.1-1: интерпретатор проверяется ДО прогона и НЕЗАВИСИМО
    # от формы пути. `--python` с опечаткой иначе дал бы `FileNotFoundError` от
    # `subprocess` → код 1 (`SURVIVED`), то есть инструмент ложно обвинил бы тест
    # в слепоте вместо контрактного кода 2 (`MUTATION-NOT-APPLIED`).
    # Абсолютный путь берётся как есть, относительный — от `--cwd` (там, где
    # реально исполняется pytest, а не от текущей папки вызывающего).
    python = Path(args.python) if Path(args.python).is_absolute() else Path(args.cwd, args.python)
    if not python.exists() or not python.is_file():
        print(f"MUTATION-NOT-APPLIED: интерпретатор не найден: {args.python}", file=sys.stderr)
        return EXIT_NOT_APPLIED

    original = target.read_text(encoding="utf-8")
    if args.old not in original:
        print(f"MUTATION-NOT-APPLIED: образец не найден в {target}", file=sys.stderr)
        return EXIT_NOT_APPLIED
    if original.count(args.old) > 1:
        print(
            f"MUTATION-NOT-APPLIED: образец встречается {original.count(args.old)} раз — "
            "замена неоднозначна, уточните контекст",
            file=sys.stderr,
        )
        return EXIT_NOT_APPLIED

    mutated = original.replace(args.old, args.new, 1)
    # Защита от «мутации-пустышки»: `old == new` или замена, не меняющая смысл,
    # даёт зелёный тест и ложный вывод «тест слепой».
    if mutated == original:
        print("MUTATION-NOT-APPLIED: файл не изменился (old == new?)", file=sys.stderr)
        return EXIT_NOT_APPLIED

    # ---- H-3: BASELINE. Тест обязан быть ЗЕЛЁНЫМ до мутации ----
    baseline = _run_pytest(args.python, args.test, args.cwd, args.timeout)
    if baseline.returncode != 0:
        print(
            "BASELINE-RED: тест красный УЖЕ без мутации — результат мутации "
            "неизвестен. Проверьте DATABASE_URL, имя теста и импорты.",
            file=sys.stderr,
        )
        if not args.quiet:
            tail = _tail(baseline)
            if tail:
                print(tail, file=sys.stderr)
        return EXIT_BASELINE_RED

    # ---- M-9.2-2 (CR 9.2 r2): SKIP — это НЕ PASS ----
    # `pytest` даёт код 0, когда все собранные тесты скипнуты. Без этой
    # проверки прогон без `DATABASE_URL` рапортовал ложный SURVIVED (код 1):
    # «тест слепой» вместо «окружения нет». Вывод о мутации НЕ делается.
    if _has_skipped(baseline):
        print(
            "BASELINE-SKIPPED: в baseline есть `skipped` — тест НЕ выполнялся "
            "(нет DATABASE_URL/REDIS_URL?). Результат мутации неизвестен.",
            file=sys.stderr,
        )
        if not args.quiet:
            tail = _tail(baseline)
            if tail:
                print(tail, file=sys.stderr)
        return EXIT_BASELINE_SKIPPED

    backup = Path(tempfile.mkdtemp(prefix="mutate-")) / target.name
    shutil.copy2(target, backup)

    try:
        target.write_text(mutated, encoding="utf-8")
        if target.read_text(encoding="utf-8") == original:
            print("MUTATION-NOT-APPLIED: запись не изменила файл", file=sys.stderr)
            return EXIT_NOT_APPLIED

        completed = _run_pytest(args.python, args.test, args.cwd, args.timeout)
    finally:
        # Восстановление ВСЕГДА: и при зелёном, и при красном, и при исключении.
        shutil.copy2(backup, target)
        shutil.rmtree(backup.parent, ignore_errors=True)

    assert target.read_text(encoding="utf-8") == original, "файл не восстановлен!"

    if not args.quiet:
        tail = _tail(completed)
        if tail:
            print(tail)

    if completed.returncode != 0:
        print(f"KILLED: тест покраснел при мутации в {target} (мутация поймана)")
        return EXIT_KILLED

    print(
        f"SURVIVED: тест ЗЕЛЁНЫЙ при мутации в {target} — тест слепой "
        "или мутация не про то (D-98: такой тест не принимается в CR)",
        file=sys.stderr,
    )
    return EXIT_SURVIVED


if __name__ == "__main__":
    sys.exit(main())
