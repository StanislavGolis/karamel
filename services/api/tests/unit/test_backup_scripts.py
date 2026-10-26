"""Скрипты бэкапа: обязательные флаги и ретенция (стори 10.7, Task 8.3; AC-02).

**Почему проверяется ТЕКСТ, а не поведение** (§18.3 разрешает для таких
случаев, прецедент — `test_0028_migration_text.py`). `pg_dump` в CI-раннере
отсутствует (образ `python:3.13-slim` без `postgresql-client`), а запуск
скрипта против живой базы проверял бы PostgreSQL, а не скрипт. Поведение
самого `pg_dump`/`tar` — библиотечный код, и его проверка не наша задача.

**Чего эти тесты НЕ доказывают.** Что бэкап создаётся, что ретенция реально
удаляет файлы и что восстановление работает. «Восстановление выполнено
однократно до пилота» (AC-02) — документ и разовая процедура (W-10.7-a),
автотестом не проверяется (D-10.7-7). Тест текста — это проверка того, что
скрипт НАПИСАН с нужными флагами, а не того, что он отработал.

**Почему `-mtime +"$RETENTION_DAYS"` проверяется как подстрока.** Переменная
раскрывается в кавычках при запуске; зафиксировать её значение в тексте
теста значило бы проверять `bash`, а не скрипт. Проверяется, что дефолт — 7
(NFR45) и что `-mtime`/`-delete` присутствуют: без `-delete` ретенция
превращается в «копить вечно» — ровно тот дефолт, из-за которого диск
стенда кончается молча.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PG_BACKUP = REPO_ROOT / "scripts" / "backup" / "pg-backup.sh"
MEDIA_BACKUP = REPO_ROOT / "scripts" / "backup" / "media-backup.sh"


def test_pg_backup_script_exists() -> None:
    """AC-02: скрипт бэкапа БД существует и не пуст."""
    assert PG_BACKUP.is_file(), f"нет файла {PG_BACKUP}"
    assert len(PG_BACKUP.read_text(encoding="utf-8")) > 500


def test_pg_backup_uses_strict_mode() -> None:
    """`set -euo pipefail`: без него ошибка `pg_dump` тонет в `| tee`.

    `-e` (упасть на ошибке), `-u` (падать на неопределённой переменной — иначе
    пустой `BACKUP_DIR` дал бы `-delete` в текущем каталоге), `-o pipefail`
    (ошибка в первой команде конвейера не маскируется).
    """
    source = PG_BACKUP.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source


def test_pg_backup_uses_custom_format() -> None:
    """AC-02: `--format=custom` — формат `pg_restore`, с которым работает рунбук.

    `plain` (SQL-текст) не поддерживает выборочное восстановление и не даёт
    `--no-owner` смысла; проверка восстановления на нём упиралась бы в права.
    """
    source = PG_BACKUP.read_text(encoding="utf-8")

    assert "--format=custom" in source


def test_pg_backup_strips_asyncpg_prefix() -> None:
    """`DATABASE_URL` приложения содержит `+asyncpg`, который `pg_dump` не понимает.

    Без снятия префикса скрипт падал бы на разборе DSN — то есть на первом же
    запуске, в момент, когда бэкап нужен.
    """
    source = PG_BACKUP.read_text(encoding="utf-8")

    assert "postgresql+asyncpg" in source


def test_pg_backup_retention_default_is_seven_days() -> None:
    """NFR45: ретенция по умолчанию — 7 дней, и она настраивается."""
    source = PG_BACKUP.read_text(encoding="utf-8")

    assert 'RETENTION_DAYS="${RETENTION_DAYS:-7}"' in source


def test_pg_backup_deletes_old_dumps() -> None:
    """NFR45: старые дампы УДАЛЯЮТСЯ, а не только считаются.

    Без `-delete` строка `-mtime` ничего не делает, и диск стенда заполняется
    молча — дефект, который виден только когда бэкап перестал создаваться
    (места нет), то есть ровно тогда, когда он нужен.
    """
    source = PG_BACKUP.read_text(encoding="utf-8")

    assert "-mtime" in source
    assert "-delete" in source


def test_pg_backup_retention_uses_the_configured_value() -> None:
    """P-7 (CR 10.7): `-mtime` обязан ссылаться на ПЕРЕМЕННУЮ, а не на число.

    **Зачем отдельная проверка.** Первые версии тестов искали подстроки
    `-mtime` и `-delete` — этого достаточно, чтобы поймать УДАЛЕНИЕ строки
    (мутации M7/M9 ревьюера), но не СМЕНУ ЗНАЧЕНИЯ внутри неё. Замена
    `+"$RETENTION_DAYS"` на `+1` оставляла все тесты зелёными, а ретенция
    становилась суточной вместо семидневной — то есть NFR45 нарушался молча.

    Проверяется склейка флага с переменной: `-"$RETENTION_DAYS" -delete`.
    Литерал `+1` (и любой другой) такую строку не содержит.
    """
    source = PG_BACKUP.read_text(encoding="utf-8")

    assert '+"$RETENTION_DAYS" -delete' in source, (
        "ретенция обязана брать значение из RETENTION_DAYS: литерал вместо "
        "переменной делает NFR45 (7 дней) непроверяемым и непредсказуемым"
    )


def test_pg_backup_retention_is_not_a_hardcoded_number() -> None:
    """NFR45: в строке ретенции НЕТ литерала вида `-mtime +<число>`.

    Комплементарная проверка к предыдущей: она ловит попытку «прибить»
    значение гвоздями рядом с переменной (`+1` вместо `+"$RETENTION_DAYS"`
    в той же строке `find`).
    """
    source = PG_BACKUP.read_text(encoding="utf-8")
    # Только исполняемые строки: комментарий «Ретенция: `-mtime +N` — …» тоже
    # содержит подстроку `-mtime`, и без этого фильтра проверка падала бы на
    # документации, а не на коде (поймано при первом прогоне теста).
    find_lines = [
        line
        for line in source.splitlines()
        if "-mtime" in line and not line.lstrip().startswith("#")
    ]

    assert find_lines, "в скрипте нет строки с -mtime: ретенция отсутствует"
    for line in find_lines:
        assert "$RETENTION_DAYS" in line, f"литерал вместо переменной в ретенции: {line.strip()!r}"


def test_media_backup_script_exists() -> None:
    """AC-02/W-10.7-c: заготовка медиа-бэкапа существует.

    Это ЗАГОТОВКА: канала выгрузки из S3 нет, выгрузку делает владелец
    вручную (W-10.7-c). Тест подтверждает наличие файла, а не рабочий канал.
    """
    assert MEDIA_BACKUP.is_file(), f"нет файла {MEDIA_BACKUP}"


def test_media_backup_uses_strict_mode_and_retention() -> None:
    """Заготовка обязана вести себя так же строго, как рабочий скрипт.

    Иначе «необязательный» скрипт с `rm` без кавычек станет инцидентом,
    когда его всё-таки запустят.
    """
    source = MEDIA_BACKUP.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert 'RETENTION_DAYS="${RETENTION_DAYS:-7}"' in source
    assert "-mtime" in source
    assert "-delete" in source


def test_media_backup_requires_source_directory() -> None:
    """Пустой `MEDIA_DIR` — падение, а не «успешный» бэкап пустоты.

    Скрипт, архивирующий пустой каталог, создаёт ложную уверенность в наличии
    бэкапа — хуже отсутствующего скрипта, на который не рассчитывают.
    """
    source = MEDIA_BACKUP.read_text(encoding="utf-8")

    assert "MEDIA_DIR" in source
    assert "exit 1" in source
