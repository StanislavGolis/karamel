"""`pg-backup.sh` целиком, а не только его heredoc (CR 11.10, P-25).

**Зачем этот файл.** CR 11.10 прогнал 15 мутаций, 9 выжили. Все выжившие
били по SHELL-обвязке скрипта: направление `-mtime`, glob удаления, `-F=` в
разборе, `chmod`, направление сравнения размера, замена `tee` на `>`.
Причина системная: `test_backup_scripts.py` проверяет обвязку ТЕКСТОМ
(`'-mtime' in source`), а поведенческие тесты исполняют только heredoc
`UPLOAD_PY`. Значит 178 строк канала, разбора и ретенции не исполнял никто.

**Как ловится.** Скрипт запускается ЦЕЛИКОМ в `subprocess` против поддельного
`docker` в `PATH`: он отдаёт дамп на стороне `postgres` и считает stdin на
стороне `api`. Проверяется ЭФФЕКТ — код выхода, права файла, число
оставшихся файлов, отсутствие ошибок разбора — а не подстроки в исходнике
(P-25: форма зелёная не значит, что работает).

**Почему это возможно без postgres и docker.** Настоящие `pg_dump` и `boto3`
скрипту не нужны: он вызывает их в контейнерах, а контейнеры здесь —
подставной bash. `MIN_DUMP_BYTES` понижается через env: проверяется логика
порога, а не размер реальной базы.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PG_BACKUP = REPO_ROOT / "scripts" / "backup" / "pg-backup.sh"

#: Поддельный `docker`. Сторона `postgres` отдаёт 2000 байт «дампа»,
#: сторона `api` считает прочитанное и печатает обе величины так же, как
#: настоящий блок загрузки. `FAKE_DELTA` имитирует расхождение `ContentLength`.
_FAKE_DOCKER = '''#!/bin/bash
if [ "$4" = "postgres" ]; then
  printf '%*s' 2000 '' | tr ' ' 'D'
elif [ "$4" = "api" ]; then
  payload=$(cat)
  n=${#payload}
  echo "s3_content_length=$((n+${FAKE_DELTA:-0})) bytes_uploaded=$n"
fi
'''


def _run_script(
    tmp: Path,
    *,
    delta: int = 0,
    retention_days: int = 7,
    min_bytes: int = 100,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "COMPOSE_DIR": str(tmp),
        "BACKUP_DIR": str(tmp / "backups"),
        "RETENTION_DAYS": str(retention_days),
        "MIN_DUMP_BYTES": str(min_bytes),
        "FAKE_DELTA": str(delta),
    }
    return subprocess.run(
        ["bash", str(PG_BACKUP)],
        capture_output=True,
        env=env,
        text=True,
        check=False,
        timeout=120,
    )


def _seed(backups: Path, ages: range) -> None:
    backups.mkdir(parents=True, exist_ok=True)
    for age in ages:
        handle = backups / f"karamel-d{age}.dump"
        handle.write_bytes(b"x")
        stamp = subprocess.run(
            ["touch", "-d", f"{age} days ago", str(handle)], check=False
        )
        assert stamp.returncode == 0


def test_script_succeeds_end_to_end(tmp_path: Path) -> None:
    """AC-1/AC-2: скрипт проходит канал и пишет `backup.ok`.

    Ловит M9 (`tee` → `>`): без `tee` вывод загрузчика не попадает в
    `UPLOAD_OUTPUT`, и скрипт выходит с `no_s3_content_length` вместо `ok`.
    """
    result = _run_script(tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "backup.ok file=" in result.stdout
    assert "s3_content_length=2000" in result.stdout


def test_script_reports_size_mismatch(tmp_path: Path) -> None:
    """AC-2: расхождение размеров валит скрипт, а не только блок загрузки.

    Ловит M10 (`[ $S3_SIZE -ne $LOCAL_SIZE ]` → `-ge 0`): сверка на стороне
    bash исчезает, и скрипт пишет `backup.ok` при разных числах.
    """
    result = _run_script(tmp_path, delta=1)

    assert result.returncode == 1, f"stdout={result.stdout!r}"
    assert "size_mismatch" in result.stdout
    assert "backup.ok" not in result.stdout


def test_script_fails_when_dump_below_minimum(tmp_path: Path) -> None:
    """AC-1: дамп меньше порога — отказ, а не успех.

    Ловит M5 (`-lt` → `-gt`): сравнение переворачивается, и порог начинает
    отбивать нормальные дампы, пропуская пустоту.
    """
    result = _run_script(tmp_path, min_bytes=10_000)

    assert result.returncode == 1, f"stdout={result.stdout!r}"
    assert "too_small" in result.stdout


def test_script_keeps_exactly_retention_days_and_spares_foreign_files(
    tmp_path: Path,
) -> None:
    """AC-3: остаётся ровно 7 дампов, посторонние файлы не тронуты.

    Ловит разом три класса:
    - M1 (`+` → `-` в `-mtime`): удаляются СВЕЖИЕ дампы вместо старых;
    - M2 (glob `karamel-*.dump` → `*`): удаляется `notes.txt`;
    - M14/M15 (`-mtime ±999`): удаляется всё либо ничего.
    """
    backups = tmp_path / "backups"
    _seed(backups, range(1, 9))
    foreign = backups / "notes.txt"
    foreign.write_text("не дамп", encoding="utf-8")
    # Файл СОЗНАТЕЛЬНО старый: свежий `-mtime +6` не тронет при любом glob,
    # и мутация M2 (glob `karamel-*.dump` → `*`) выжила бы на нём. Проверка
    # «посторонний файл не удалён» имеет смысл только для файла, который
    # ретенция действительно рассматривала бы.
    subprocess.run(["touch", "-d", "30 days ago", str(foreign)], check=True)

    result = _run_script(tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    dumps = sorted(p.name for p in backups.glob("karamel-*.dump"))
    assert len(dumps) == 7, f"AC-3 требует 7 файлов, получено {len(dumps)}: {dumps}"
    assert foreign.is_file(), "ретенция удалила посторонний файл: glob слишком широк"


def test_script_makes_dump_unreadable_for_others(tmp_path: Path) -> None:
    """AC-1 (граница 2): дамп не читается никем, кроме владельца.

    Ловит M4 (`chmod 600` → `644`): в дампе вся база, и `644` — утечка.
    Проверяются ПРАВА ФАЙЛА, а не подстрока `chmod` в исходнике.
    """
    result = _run_script(tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    dump = next((tmp_path / "backups").glob("karamel-*.dump"))
    mode = stat.S_IMODE(dump.stat().st_mode)
    assert mode == 0o600, f"права дампа {oct(mode)}: файл читается группой/другими"


def test_script_log_has_no_parse_errors(tmp_path: Path) -> None:
    """AC-2: разбор `s3_content_length` не спотыкается о хвост строки.

    Ловит M3 (`awk -F'[= ]'` → `-F=`): захватывается ` bytes_uploaded=…`,
    `[ -ne ]` падает с `integer expected`, и сверка молча не выполняется.
    Это ровно тот дефект, который DS нашёл живым прогоном и не закрыл тестом.
    """
    result = _run_script(tmp_path)

    assert "integer expected" not in result.stderr, f"stderr={result.stderr!r}"
    assert "integer expected" not in result.stdout
    assert result.returncode == 0, f"stdout={result.stdout!r}"
