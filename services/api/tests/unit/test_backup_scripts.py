"""Скрипты бэкапа: контракт канала S3 и ретенция (стори 11.10, Task 8; AC-1…AC-3).

**Почему проверяется ТЕКСТ, а не поведение** (§18.3 разрешает для таких
случаев, прецедент — `test_0028_migration_text.py`). `pg_dump` в CI-раннере
отсутствует (образ `python:3.13-slim` без `postgresql-client`), а запуск
скрипта против живой базы проверял бы PostgreSQL, а не скрипт.

**Что изменилось в 11.10.** Скрипт больше не снимает дамп на хосте: `pg_dump`
живёт в контейнере `postgres`, `boto3` — в контейнере `api` (замеры З-2, З-3).
Поэтому `DATABASE_URL` скриптом НЕ читается вообще — проверка «строка
`postgresql+asyncpg` присутствует» стала ложной и заменена на обратную:
«скрипт не читает `DATABASE_URL`». Арифметика ретенции исправлена замером З-9
(`-mtime +7` оставлял 8 файлов вместо 7).

**Чего эти тесты НЕ доказывают.** Что бэкап создаётся и что объект реально
уехал в S3. Это доказывается живым прогоном на стенде (Debug Log стори), а не
pytest: тест текста — проверка того, что скрипт НАПИСАН правильно.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PG_BACKUP = REPO_ROOT / "scripts" / "backup" / "pg-backup.sh"
MEDIA_BACKUP = REPO_ROOT / "scripts" / "backup" / "media-backup.sh"


def _source() -> str:
    return PG_BACKUP.read_text(encoding="utf-8")


def _executable_lines(needle: str) -> list[str]:
    """Строки с подстрокой, исключая комментарии.

    Комментарии в этом скрипте цитируют и `-mtime +7`, и `DATABASE_URL`
    намеренно (объясняют, почему так НЕ надо). Без фильтра проверка падала бы
    на документации, а не на коде.
    """
    return [
        line
        for line in _source().splitlines()
        if needle in line and not line.lstrip().startswith("#")
    ]


def test_pg_backup_script_exists() -> None:
    """AC-1: скрипт бэкапа существует и не пуст."""
    assert PG_BACKUP.is_file(), f"нет файла {PG_BACKUP}"
    assert len(_source()) > 500


def test_pg_backup_uses_strict_mode() -> None:
    """A-3 (AC-1): `set -euo pipefail` — падение `pg_dump` валит скрипт.

    Без `pipefail` код возврата берётся с последней команды конвейера
    (`python`/`tee`), и упавший `pg_dump` даёт `backup.ok` при пустом дампе.
    Замер З-8: `false | tee f | wc -c` возвращает 1 только с `pipefail`.
    """
    assert "set -euo pipefail" in _source()


def test_pg_backup_uses_custom_format() -> None:
    """AC-1: `-Fc` — формат `pg_restore`, с которым работает рунбук §4."""
    assert "-Fc" in _source()


def test_pg_backup_does_not_read_database_url() -> None:
    """A-11 (AC-1): скрипт НЕ читает `DATABASE_URL`.

    Дамп снимается внутри контейнера `postgres`, где DSN не нужен: соединение
    локальное, а `pg_dump` получает роль и базу аргументами. Чтение
    `DATABASE_URL` означало бы зависимость от переменной, которой на хосте
    может не быть (она живёт в `.env`, который cron не экспортирует), — и
    скрипт падал бы на первом запуске в момент, когда бэкап нужен.

    Проверка ОБРАТНАЯ прежней (`postgresql+asyncpg` присутствовала): тест
    переписан, а не удалён — предмет проверки сменился вместе с каналом.
    """
    assert not _executable_lines("DATABASE_URL")


def test_pg_backup_retention_default_is_seven_days() -> None:
    """NFR45: ретенция по умолчанию — 7 дней, и она настраивается."""
    assert 'RETENTION_DAYS="${RETENTION_DAYS:-7}"' in _source()


def test_pg_backup_deletes_old_dumps() -> None:
    """AC-3: старые дампы УДАЛЯЮТСЯ, а не только считаются.

    Без `-delete` строка `-mtime` ничего не делает, и диск стенда заполняется
    молча — дефект, видимый только когда бэкап перестал создаваться.
    """
    assert "-mtime" in _source()
    assert "-delete" in _source()


def test_pg_backup_retention_uses_variable_minus_one() -> None:
    """A-4 (AC-3): в `find` уходит `$((RETENTION_DAYS - 1))`, а не сама константа.

    **Замер З-9.** `-mtime +N` — «строго больше N полных суток». Дамп
    семидневной давности имеет возраст 7 суток ПЛЮС секунды снятия, поэтому
    под `+7` попадает; из 8 файлов `-mtime +7` оставляет 8. Мутация `-1` → без
    `-1` (то есть `+7`) возвращает дефект: AC-3 («остаётся 7») перестаёт
    выполняться при формально верном `RETENTION_DAYS=7`.

    Проверяется ЭФФЕКТ — наличие вычитания в строке ретенции, а не форма
    записи: константа 7 остаётся (NFR45), меняется только передаваемое число.
    """
    lines = _executable_lines("-mtime")
    assert lines, "в скрипте нет строки с -mtime: ретенция отсутствует"
    assert any("RETENTION_DAYS - 1" in line for line in lines), (
        "ретенция обязана передавать RETENTION_DAYS - 1: при `+7` остаётся 8 "
        "файлов вместо 7 (замер З-9), то есть AC-3 нарушен"
    )


def test_pg_backup_retention_is_not_a_hardcoded_number() -> None:
    """NFR45: в строке ретенции НЕТ литерала вида `-mtime +<число>`.

    Комплементарная проверка к предыдущей: ловит попытку «прибить» значение
    гвоздями (`-mtime +6` вместо вычисления от `RETENTION_DAYS`).
    """
    lines = _executable_lines("-mtime")
    assert lines
    for line in lines:
        assert "RETENTION_DAYS" in line, f"литерал вместо переменной в ретенции: {line.strip()!r}"


def test_pg_backup_uploads_to_s3_prefix_db() -> None:
    """A-5 (AC-2, AC-3): загрузка идёт в префикс `db/` через контейнер `api`.

    **Почему через `api`.** На хосте нет `boto3` и нечем его поставить
    (замер З-2), а в образе api `boto3 1.43.117` уже есть (З-3). Загрузка с
    хоста потребовала бы новой зависимости ради одной операции.

    **Почему `db/`.** `S3_BACKUP_BUCKET` (`karamel-backup`) — бакет дампов;
    `S3_BUCKET` (`karamel-media`) — бакет фото профилей. Смешать = положить
    дампы в бакет с другой политикой доступа. Префикс — граница между ними.
    """
    source = _source()
    assert "S3_PREFIX" in source
    assert "db/" in source
    assert "docker compose exec -T api" in source


def test_pg_backup_compares_content_length_with_file_size() -> None:
    """A-1/A-2 (AC-2): `ContentLength` сверяется с размером файла числом.

    AC-2 требует «число против числа», а не «загрузка прошла»: без сверки
    успешный `upload_fileobj` (нулевого объекта, обрезанного потока) дал бы
    `backup.ok`. `head_object` спрашивается у S3 — иначе проверялось бы, что
    мы посчитали у себя.
    """
    source = _source()
    assert "head_object" in source
    assert "ContentLength" in source
    assert "size_mismatch" in source
    assert "stat -c %s" in source


def test_pg_backup_logs_ok_with_name_and_size() -> None:
    """AC-1: в лог пишется `backup.ok` с именем файла и размером.

    Строка — единственный способ доказать постфактум, что бэкап был: файл к
    моменту разбора может быть уже вычищен ретенцией.
    """
    source = _source()
    assert "backup.ok file=" in source
    assert "backup.start" in source
    assert "backup.fail" in source


def test_pg_backup_never_logs_secrets() -> None:
    """A-12 (AC-2): значения `S3_BACKUP_*` не печатаются.

    Секрет в логе = секрет в `/var/log` и в ротациях на два месяца. Скрипт
    читает реквизиты только как имена переменных окружения внутри Python-
    вставки, и в лог попадают лишь имена файлов, числа и ключи объектов.
    """
    source = _source()
    for line in source.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "printenv" not in line, f"печать окружения в лог: {line.strip()!r}"
        assert "echo $S3_BACKUP" not in line and "echo \"$S3_BACKUP" not in line


def test_media_backup_script_exists() -> None:
    """AC-02/W-10.7-c: заготовка медиа-бэкапа существует (не трогается здесь)."""
    assert MEDIA_BACKUP.is_file(), f"нет файла {MEDIA_BACKUP}"


def test_media_backup_uses_strict_mode_and_retention() -> None:
    """Заготовка обязана вести себя так же строго, как рабочий скрипт."""
    source = MEDIA_BACKUP.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert 'RETENTION_DAYS="${RETENTION_DAYS:-7}"' in source
    assert "-mtime" in source
    assert "-delete" in source


def test_media_backup_requires_source_directory() -> None:
    """Пустой `MEDIA_DIR` — падение, а не «успешный» бэкап пустоты."""
    source = MEDIA_BACKUP.read_text(encoding="utf-8")

    assert "MEDIA_DIR" in source
    assert "exit 1" in source


# --- Поведенческие проверки блока загрузки (стори 11.10, AC-2, AC-3) ----------
#
# Текстовые проверки выше ловят УДАЛЕНИЕ строки, но не ПОДМЕНУ её смысла:
# `if remote_size != len(payload)` → `if remote_size >= 0` оставляет все
# нужные подстроки на месте, а сверка `ContentLength` исчезает. Поэтому блок
# `UPLOAD_PY` извлекается из скрипта и ИСПОЛНЯЕТСЯ с поддельным `boto3`:
# проверяется эффект (код выхода, stdout, что именно удалено), а не форма
# (P-25, урок CR 11.8 — «мутации били по свойствам, которые DS сам заложил»).

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402

_FAKE_BOTO3 = '''"""Поддельный boto3: запоминает загруженное, отдаёт заданный ContentLength."""
import datetime
import json
import os


class _Paginator:
    def paginate(self, *, Bucket: str, Prefix: str):
        items = json.loads(os.environ.get("FAKE_OBJECTS", "[]"))
        now = datetime.datetime.now(datetime.UTC)
        yield {
            "Contents": [
                {
                    "Key": item["key"],
                    "LastModified": now - datetime.timedelta(days=item["age_days"]),
                }
                for item in items
            ]
        }


class _Client:
    def __init__(self) -> None:
        self.uploaded = b""
        self.key = ""

    def upload_fileobj(self, fileobj, bucket: str, key: str) -> None:
        self.uploaded = fileobj.read()
        self.key = key

    def head_object(self, *, Bucket: str, Key: str):
        delta = int(os.environ.get("FAKE_SIZE_DELTA", "0"))
        return {"ContentLength": len(self.uploaded) + delta}

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator()

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        print("DELETED " + Key)


def client(*args, **kwargs) -> _Client:
    return _Client()
'''


def _upload_python_block() -> str:
    """Извлекает heredoc `UPLOAD_PY` из скрипта — исполняемый, а не переписанный."""
    source = _source()
    marker_start = "read -r -d '' UPLOAD_PY <<'PY' || true\n"
    start = source.index(marker_start) + len(marker_start)
    end = source.index("\nPY\n", start)
    return source[start:end]


def _run_upload_block(
    payload: bytes, *, size_delta: int = 0, objects: list[dict[str, object]] | None = None
) -> subprocess.CompletedProcess[str]:
    """Запускает блок загрузки отдельным процессом с поддельным `boto3`."""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "boto3.py"), "w", encoding="utf-8") as handle:
            handle.write(_FAKE_BOTO3)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": tmp,
            "S3_BACKUP_ENDPOINT": "https://s3.example",
            "S3_BACKUP_REGION": "ru-1",
            "S3_BACKUP_BUCKET": "karamel-backup",
            "S3_BACKUP_ACCESS_KEY": "key",
            "S3_BACKUP_SECRET_KEY": "secret",
            "FAKE_SIZE_DELTA": str(size_delta),
            "FAKE_OBJECTS": json.dumps(objects or []),
        }
        completed = subprocess.run(
            [sys.executable, "-c", _upload_python_block(), "karamel-test.dump", "db/", "30"],
            input=payload,
            capture_output=True,
            env=env,
            check=False,
            timeout=60,
        )
        # Декодирование ПОСЛЕ прогона: `text=True` вместе с байтовым `input`
        # падает на кодировании ввода, а дамп — двоичный файл, который через
        # текстовый stdin не проходит без потерь.
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )


def test_upload_block_fails_when_content_length_differs() -> None:
    """A-1/A-2 (AC-2): расхождение `ContentLength` и размера → код 1.

    **Что именно ловится.** Мутация `if remote_size != len(payload)` →
    `if remote_size >= 0` проходит любую текстовую проверку: подстрока
    `size_mismatch` остаётся в файле. Здесь поддельный `head_object` отдаёт
    размер на байт больше — правильный код обязан упасть, мутированный
    промолчит.

    **Почему размер приходит от `head_object`, а не из `len(payload)`.**
    Мутация `head_object` → `len(payload)` при `size_delta=+1` тоже даёт
    успех, то есть этот тест ловит и A-2: сверка обязана спрашивать S3.
    """
    result = _run_upload_block(b"x" * 100, size_delta=1)

    assert result.returncode == 1, (
        f"расхождение размеров обязано валить загрузку: stdout={result.stdout!r}"
    )
    assert "size_mismatch" in result.stdout


def test_upload_block_succeeds_when_content_length_matches() -> None:
    """AC-2: совпадение размеров → код 0 и `s3_content_length` в stdout.

    Комплементарная сторона: без неё «всегда падать» проходило бы проверку
    выше и валило исправный канал.
    """
    result = _run_upload_block(b"x" * 100)

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "s3_content_length=100" in result.stdout


def test_upload_block_prunes_only_objects_older_than_threshold() -> None:
    """A-5 (AC-3): удаляется объект старше 30 суток, свежий остаётся.

    **Что ловится.** Мутация сравнения `LastModified` на любое другое условие
    (например `len(key) > 10`) удалила бы оба объекта или ни одного. Возраст
    берётся из `LastModified` — единственного факта о дате, который знает
    хранилище; имя объекта формирует скрипт, и опираться на него нельзя.
    """
    result = _run_upload_block(
        b"x" * 100,
        objects=[
            {"key": "db/old.dump", "age_days": 40},
            {"key": "db/fresh.dump", "age_days": 1},
        ],
    )

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "DELETED db/old.dump" in result.stdout
    assert "DELETED db/fresh.dump" not in result.stdout


def test_upload_block_logs_every_pruned_key() -> None:
    """A-6 (AC-3): каждый удалённый ключ перечислен строкой `backup.s3_pruned`.

    AC-3 требует «удалённые перечислены»: удаление без строки в логе делает
    ретенцию невидимой, и «почему в бакете нет объекта за прошлый месяц»
    разбирается археологией вместо чтения лога.
    """
    result = _run_upload_block(
        b"x" * 100,
        objects=[{"key": "db/old.dump", "age_days": 40}],
    )

    assert result.returncode == 0
    # Возраст в логе — `timedelta.days`, а он УСЕКАЕТ: между `now` поддельного
    # `boto3` и `now` блока загрузки проходят микросекунды, поэтому «40 дней»
    # печатается как 39. Проверяется эффект (объект распознан как старый и
    # перечислен), а не число, зависящее от дрейфа часов.
    assert "backup.s3_pruned key=db/old.dump age_days=" in result.stdout
    logged_age = int(result.stdout.split("backup.s3_pruned key=db/old.dump age_days=")[1].split()[0])
    assert logged_age >= 39
