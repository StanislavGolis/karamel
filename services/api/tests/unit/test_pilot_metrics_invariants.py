"""Инварианты метрик пилота, проверяемые по ИСХОДНИКАМ (стори 10.5; AC-05, TD-10.5-4).

Эти проверки не про поведение, а про ФОРМУ кода — и именно поэтому они нужны:

1. **`NULLIF` в SQL всех четырёх метрик (AC-05.1, P-16).** Поведением это не
   проверить: если убрать `NULLIF`, `count(*)` вернёт `0`, а `_ratio(0, 0)` всё
   равно вернёт `None` — ответ НЕ изменится, и поведенческий тест останется
   зелёным. Мутация M1 («убрать `NULLIF`») выжила бы, а требование AC-05.1
   «`null` рождается в SQL» было бы нарушено незаметно.
2. **Отсутствие `except ZeroDivisionError` (D-TD-3).** Такой обработчик
   маскировал бы отсутствие `NULLIF`: формально «деления на ноль нет», а
   источник `None` переехал бы в Python, и `numerator`/`denominator` перестали
   бы объяснять `null` (AC-05.2).
3. **Пороги только в сервисе (TD-10.5-4).** Своя константа в UI разошлась бы
   с сервером при первой правке, и кабинет показывал бы «провал» там, где
   сервер считает «норму».

Проверки читают файлы, а не импортируют модули: цель — перечень ТОЧЕК
применения (P-16: «4 из 4»), а импорт показал бы только факт существования
функции.
"""

from __future__ import annotations

import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[1]
REPOSITORY = API_ROOT / "app" / "repositories" / "pilot_metrics.py"
SERVICE = API_ROOT / "app" / "services" / "pilot_metrics_service.py"
APP_DIR = API_ROOT / "app"
WEB_SRC = REPO_ROOT / "apps" / "web" / "src"


def test_nullif_present_in_all_four_metrics() -> None:
    """AC-05.1, P-16: `NULLIF(..., 0)` стоит в SQL ВСЕХ четырёх метрик — 4 из 4.

    Мутация M1 (убрать `NULLIF`) обязана покраснеть именно здесь: поведение
    при этом не меняется (`_ratio(0, 0)` тоже даёт `None`), поэтому
    единственное доказательство требования — перечень точек применения.
    """
    source = REPOSITORY.read_text(encoding="utf-8")
    # Считаются ТОЛЬКО строки SQL-кода (`    NULLIF(...)` с отступом и без
    # обратных кавычек), а не упоминания в докстринге: объяснение в прозе не
    # является точкой применения, и засчитывать его значило бы позволить
    # удалить `NULLIF` из запроса, оставив абзац о нём.
    code_lines = [
        line for line in source.splitlines() if re.match(r"\s+NULLIF\(", line) and "`" not in line
    ]
    assert len(code_lines) == 4, (
        f"NULLIF в SQL-коде найден {len(code_lines)} раз вместо 4: AC-05.1 требует, "
        f"чтобы `null` рождался в SQL каждой метрики, а не в Python. "
        f"Строки: {code_lines}"
    )


def test_no_zero_division_error_handler() -> None:
    """D-TD-3: в `app/` НЕТ `except ZeroDivisionError` — 0 вхождений.

    Обработчик замаскировал бы отсутствие `NULLIF`: деления на ноль не было бы
    «видно», а источник `None` переехал бы из SQL в Python (AC-05.1 против
    AC-05.2). Мутация M1 дополнительно проверяется этим перечнем.
    """
    # Ищется КОНСТРУКЦИЯ, а не упоминание: `except ZeroDivisionError` обязана
    # стоять в НАЧАЛЕ строки кода (после отступа). Слово внутри докстринга или
    # комментария — это объяснение, ПОЧЕМУ обработчика нет; запрещать ему
    # упоминать исключение значило бы запретить объяснение.
    pattern = re.compile(r"^\s*except\s+ZeroDivisionError", re.MULTILINE)
    offenders = [
        str(path.relative_to(API_ROOT))
        for path in APP_DIR.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"`ZeroDivisionError` найден в {offenders}: `None` обязан рождаться "
        f"в SQL через `NULLIF`, а не ловиться в Python"
    )


def test_thresholds_live_only_in_service() -> None:
    """TD-10.5-4: пороги объявлены только в сервисе; UI берёт их из API.

    Проверяются литералы порогов как ЧИСЛА с границей слова: `0.30`, `1.5`,
    `0.60`, `0.25`, `0.10`. UI обязан использовать `metric.threshold` из
    ответа, иначе два представления одного порога разойдутся.
    """
    service_source = SERVICE.read_text(encoding="utf-8")
    for literal in ("0.30", "1.5", "0.60", "0.25", "0.10"):
        assert literal in service_source, f"порог {literal} отсутствует в сервисе"

    # UI: ни одного из порогов как числа в исходниках страницы и компонента.
    offenders: list[str] = []
    if WEB_SRC.exists():
        for path in list(WEB_SRC.rglob("mod/pilot-metrics.tsx")) + list(
            WEB_SRC.rglob("mod/metrics/page.tsx")
        ):
            source = path.read_text(encoding="utf-8")
            for literal in ("0.30", "1.5", "0.60", "0.25", "0.10"):
                if re.search(rf"(?<![\d.]){re.escape(literal)}(?![\d])", source):
                    offenders.append(f"{path.name}: {literal}")
    assert offenders == [], (
        f"пороги продублированы в UI: {offenders} — UI обязан брать `threshold` "
        f"из ответа API (TD-10.5-4)"
    )


def test_as_assembled_statuses_exclude_draft_and_cancelled() -> None:
    """AC-01: `draft` и `cancelled` не участвуют в метриках.

    Черновик не видел никто, отмена — решение автора, а не отказ аудитории.
    Проверяется константа репозитория: если в неё попадёт `cancelled`, метрика
    начнёт считать отмены провалом пилота.
    """
    from app.repositories.pilot_metrics import ASSEMBLED_STATUSES

    assert set(ASSEMBLED_STATUSES) == {"published", "full", "closed", "finished"}, (
        f"состав статусов метрик: {ASSEMBLED_STATUSES}"
    )
    assert "draft" not in ASSEMBLED_STATUSES
    assert "cancelled" not in ASSEMBLED_STATUSES
