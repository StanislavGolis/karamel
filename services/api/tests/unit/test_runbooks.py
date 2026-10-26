"""Рунбуки: наличие обязательных полей и честных оговорок (Task 9.3; AC-02, AC-04).

**Что это за тест.** Проверка АРТЕФАКТА, а не процедуры. Он подтверждает,
что рунбук содержит чек-лист из пяти полей и команду `pg_restore`, а не что
кто-то восстановил базу. «Восстановление выполнено однократно до пилота»
(AC-02) и «uptime ≥ 99%» (AC-04) — операционные пункты, автотестом не
проверяемые (D-10.7-7, D-10.7-8). Выдавать этот тест за доказательство
процедуры запрещено.

**Зачем вообще проверять текст документа.** Рунбук без пяти полей чек-листа
нельзя предъявить как «однократное восстановление»: нечем зафиксировать
факт. Рунбук uptime без оговорки про неполноту источника превращается в
ложное обещание мониторинга. Эти два свойства — контракт документа, и они
проверяются текстом, как `test_0028_migration_text.py` проверяет SQL.

**Почему без `pytest.mark.integration`.** Файлы читаются с диска; база и сеть
не нужны. CI прогоняет его в общем `pytest -q`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKUP_RUNBOOK = REPO_ROOT / "docs" / "runbook-backup-restore.md"
UPTIME_RUNBOOK = REPO_ROOT / "docs" / "runbook-uptime.md"


#: Пять полей чек-листа восстановления (AC-02, W-10.7-a).
#: Проверяются по отдельности: рунбук с четырьмя полями выглядит полным
#: и не даёт зафиксировать расхождение — то есть допускает «восстановили,
#: наверное».
_CHECKLIST_FIELDS = ("Дата", "Кто", "Команда восстановления", "Результат", "Расхождение")


def test_backup_runbook_exists() -> None:
    """AC-02: рунбук бэкапа и восстановления существует."""
    assert BACKUP_RUNBOOK.is_file(), f"нет файла {BACKUP_RUNBOOK}"


def test_backup_runbook_has_all_checklist_fields() -> None:
    """AC-02/W-10.7-a: чек-лист содержит все пять полей.

    Без «Расхождения» восстановление «прошло успешно» даже когда счётчики
    разошлись, и расхождение узнают на бою, а не при проверке.
    """
    source = BACKUP_RUNBOOK.read_text(encoding="utf-8")

    missing = [field for field in _CHECKLIST_FIELDS if field not in source]
    assert not missing, f"в чек-листе нет полей: {missing}"


def test_backup_runbook_documents_restore_command() -> None:
    """AC-02: в рунбуке есть `pg_restore` — процедура восстановления, не только идея."""
    source = BACKUP_RUNBOOK.read_text(encoding="utf-8")

    assert "pg_restore" in source


def test_backup_runbook_mentions_retention_week() -> None:
    """NFR45: ретенция 7 дней названа в рунбуке, а не только в скрипте.

    Дежурный читает рунбук, а не скрипт; расхождение («в скрипте 7, в
    документе 30») даёт ложное ожидание, какой дамп доступен.
    """
    source = BACKUP_RUNBOOK.read_text(encoding="utf-8")

    assert "7" in source
    assert "NFR45" in source


def test_backup_runbook_admits_restore_not_automated() -> None:
    """D-10.7-7: рунбук прямо говорит, что факт восстановления не автотест.

    Обратная сторона — рунбук, который выглядит как пройденная проверка:
    тогда AC-02 считался бы закрытым кодом, а не процедурой.
    """
    source = BACKUP_RUNBOOK.read_text(encoding="utf-8")

    assert "W-10.7-a" in source
    assert "не проверяется" in source


def test_uptime_runbook_exists() -> None:
    """AC-04: рунбук uptime существует."""
    assert UPTIME_RUNBOOK.is_file(), f"нет файла {UPTIME_RUNBOOK}"


def test_uptime_runbook_states_threshold() -> None:
    """AC-04: порог 99% назван и связан с NFR15."""
    source = UPTIME_RUNBOOK.read_text(encoding="utf-8")

    assert "99" in source
    assert "NFR15" in source


def test_uptime_runbook_admits_probe_does_not_measure_uptime() -> None:
    """D-10.7-8: оговорка про неполноту источника ОБЯЗАТЕЛЬНА.

    Без неё рунбук читается как «uptime мониторится», и лежащий хост,
    которого probe не увидит, останется необнаруженным — ровно тот класс
    дефекта, из-за которого AC-04 закрывают документом, а не кодом.
    """
    source = UPTIME_RUNBOOK.read_text(encoding="utf-8")

    assert "W-10.7-b" in source
    assert "не измеряет uptime" in source


def test_uptime_runbook_has_monthly_report_form() -> None:
    """AC-04: форма месячного отчёта на месте — иначе «отчёт» нечем заполнить."""
    source = UPTIME_RUNBOOK.read_text(encoding="utf-8")

    assert "Uptime, %" in source
    assert "Минут простоя" in source
