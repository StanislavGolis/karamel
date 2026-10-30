#!/usr/bin/env bash
# Бэкап PostgreSQL в S3 с ретенцией (стори 11.10, AC-1…AC-3; NFR43, NFR45).
#
# ЧТО ЭТОТ СКРИПТ ДЕЛАЕТ. Снимает `pg_dump --format=custom` с контейнера
# `postgres`, кладёт дамп в `$BACKUP_DIR`, загружает его в `s3://<bucket>/db/`
# и сверяет ВЕРНУВШИЙСЯ `ContentLength` с размером локального файла числом.
# Затем чистит локальные дампы старше RETENTION_DAYS и объекты S3 старше
# S3_RETENTION_DAYS, перечисляя каждый удалённый ключ в логе.
#
# ГДЕ ОН ЗАПУСКАЕТСЯ. На ХОСТЕ стенда, из cron (`/etc/cron.d/karamel-backup`),
# cwd = каталог compose-проекта. Ни `pg_dump`, ни `boto3` на хосте нет — оба
# берутся из контейнеров (`postgres` и `api` соответственно), поэтому скрипт
# НЕ требует установки клиентов на хост (замер З-2: `pg_dump`, `aws`, `s3cmd`,
# `rclone`, `mc`, `pip3` на хосте отсутствуют; `python3` есть без `boto3`).
#
# ПОЧЕМУ `set -o pipefail` ОБЯЗАТЕЛЕН. Канал `pg_dump | tee | python` без него
# вернул бы код 0 от последней команды, даже если `pg_dump` упал на первой
# секунде. Тогда `backup.ok` записался бы в лог при пустом дампе — ровно тот
# молчаливый отказ, ради обнаружения которого существует AC-4 (замер З-8:
# `false | tee f | wc -c` даёт 1 только с `pipefail`).
#
# ПОЧЕМУ ЗАГРУЗКА ИДЁТ ЧЕРЕЗ `api`. `boto3 1.43.117` уже в образе api
# (замер З-3), а на хосте его нет и поставить нечем (З-2). Отдельный клиент
# на хосте был бы новой зависимостью ради одной операции.
#
# ПОЧЕМУ `--no-owner --no-privileges`. Дамп снимается под технической ролью
# стенда, а восстанавливаться может под другой ролью или в другую базу;
# владельцы и гранты сделали бы восстановление падающим на `role ... does not
# exist` — то есть проверка восстановления (AC-5) упиралась бы в права.
#
# ЧЕГО ОН НЕ ДЕЛАЕТ. Не восстанавливает базу: AC-5 — разовая проверка по
# рунбуку (`docs/runbook-backup-restore.md` §4), а не авто-рестарт. Секреты не
# логируются: в лог попадают только имена файлов, числа и ключи объектов.

set -euo pipefail

#: Куда складывать дампы. На стенде задаётся `BACKUP_DIR=/opt/backups` в cron.
BACKUP_DIR="${BACKUP_DIR:-./backups}"
#: Сколько дней хранить локально. 7 — требование NFR45; больше не «надёжнее»,
#: а дороже по диску стенда (7 файлов × ~5.6 МБ, замер З-7).
RETENTION_DAYS="${RETENTION_DAYS:-7}"
#: Сколько дней хранить в S3 (решение владельца Б-2): месяц в холодном бакете.
S3_RETENTION_DAYS="${S3_RETENTION_DAYS:-30}"
#: Каталог compose-проекта: там лежит `docker-compose.yml` и `.env`.
COMPOSE_DIR="${COMPOSE_DIR:-/opt/karamel}"
#: Реквизиты БД внутри контейнера `postgres` (`POSTGRES_*` из `.env`).
POSTGRES_USER="${POSTGRES_USER:-karamel}"
POSTGRES_DB="${POSTGRES_DB:-karamel_db}"
#: Префикс объектов в бакете. `db/` — дампы БД; фото профилей живут в ДРУГОМ
#: бакете (`S3_BUCKET`), и это разделение не косметика: смешать = положить
#: дампы в бакет с другой политикой доступа.
S3_PREFIX="${S3_PREFIX:-db/}"
#: Минимальный размер дампа. Замер З-7: реальный дамп — 5 880 235 байт.
#: Порог 1 МБ из AC-1 отсекает «успешную» загрузку пустоты.
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1048576}"

# Лог только в stdout: cron перенаправляет его в `/var/log/karamel-backup.log`.
# Своя запись в файл дала бы вторую точку правды о пути лога.
log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# `cd` до создания файла: относительный `BACKUP_DIR` обязан считаться от
# compose-каталога (cwd cron — не тот каталог, что у человека в ssh-сессии).
cd "$COMPOSE_DIR"
mkdir -p "$BACKUP_DIR"

# Метка без пробелов и двоеточий: `20261027T031500`. Сортируется
# лексикографически как хронологически — по имени видно, какой дамп новее.
STAMP="$(date +%Y%m%dT%H%M%S)"
NAME="karamel-$STAMP.dump"
OUTPUT_FILE="$BACKUP_DIR/$NAME"

log "backup.start file=$NAME"

# Скрипт загрузки для контейнера `api`: читает дамп из stdin, кладёт объект,
# сверяет `ContentLength` с числом прочитанных байт и чистит S3.
#
# `Config(signature_version='s3v4', addressing_style='path')` — как в
# `media_service.py:53`: `path`-адресация совместима с провайдерами ДЦ РФ,
# которые не всегда умеют virtual-hosted стиль для бакетов с точками в имени.
read -r -d '' UPLOAD_PY <<'PY' || true
import datetime
import io
import os
import sys

import boto3
from botocore.config import Config

name, prefix, s3_retention_days = sys.argv[1], sys.argv[2], int(sys.argv[3])

config = Config(signature_version="s3v4", s3={"addressing_style": "path"})
client = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_BACKUP_ENDPOINT"],
    region_name=os.environ["S3_BACKUP_REGION"],
    aws_access_key_id=os.environ["S3_BACKUP_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_BACKUP_SECRET_KEY"],
    config=config,
)
bucket = os.environ["S3_BACKUP_BUCKET"]
key = prefix + name

payload = sys.stdin.buffer.read()
client.upload_fileobj(io.BytesIO(payload), bucket, key)

# AC-2: число против числа. `head_object` спрашивается у S3, а не берётся
# из локального `stat` — иначе проверялось бы, что мы посчитали у себя.
remote_size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
print(f"s3_content_length={remote_size} bytes_uploaded={len(payload)}")
if remote_size != len(payload):
    print(f"backup.fail reason=size_mismatch local={len(payload)} remote={remote_size}")
    sys.exit(1)

# AC-3: ретенция S3 по `LastModified`, а не по имени объекта. Имя может быть
# любым (его задаёт скрипт), а `LastModified` — единственный факт о возрасте,
# который знает хранилище. Каждый удалённый ключ — строкой в лог.
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
    days=s3_retention_days
)
now = datetime.datetime.now(datetime.timezone.utc)
paginator = client.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
    for item in page.get("Contents", []):
        if item["LastModified"] >= cutoff:
            continue
        client.delete_object(Bucket=bucket, Key=item["Key"])
        age_days = (now - item["LastModified"]).days
        print(f"backup.s3_pruned key={item['Key']} age_days={age_days}")
PY

# Канал: дамп снимается в контейнере `postgres`, локальная копия пишется
# `tee`, загрузка и сверка — в `api`. Один конвейер, одна точка отказа.
UPLOAD_OUTPUT=""
if ! UPLOAD_OUTPUT="$(
  docker compose exec -T postgres pg_dump \
    -U "$POSTGRES_USER" -Fc --no-owner --no-privileges "$POSTGRES_DB" \
    | tee "$OUTPUT_FILE" \
    | docker compose exec -T api python -c "$UPLOAD_PY" "$NAME" "$S3_PREFIX" "$S3_RETENTION_DAYS"
)"; then
  log "backup.fail reason=dump_or_upload"
  exit 1
fi

# Права: в дампе вся база. `644` (umask по умолчанию) читается любым
# пользователем хоста — это утечка данных, а не косметика.
chmod 600 "$OUTPUT_FILE"

LOCAL_SIZE="$(stat -c %s "$OUTPUT_FILE")"
if [ "$LOCAL_SIZE" -lt "$MIN_DUMP_BYTES" ]; then
  log "backup.fail reason=too_small size=$LOCAL_SIZE min=$MIN_DUMP_BYTES"
  exit 1
fi

# `s3_content_length` из вывода загрузчика — вторая половина сверки AC-2:
# число от S3 против числа с диска, а не «загрузка прошла без исключения».
S3_SIZE="$(printf '%s\n' "$UPLOAD_OUTPUT" | awk -F'[= ]' '/^s3_content_length=/{print $2; exit}')"
if [ -z "$S3_SIZE" ]; then
  log "backup.fail reason=no_s3_content_length"
  exit 1
fi
if [ "$S3_SIZE" -ne "$LOCAL_SIZE" ]; then
  log "backup.fail reason=size_mismatch local=$LOCAL_SIZE remote=$S3_SIZE"
  exit 1
fi

# Строки ретенции S3 — как есть: их формат (`backup.s3_pruned key=...`)
# задан AC-3, и добавление префикса ломало бы разбор.
printf '%s\n' "$UPLOAD_OUTPUT" | grep '^backup\.s3_pruned ' || true

# AC-1/AC-2 одним следом: имя файла, размер с диска и размер из S3 рядом.
log "backup.ok file=$NAME size=$LOCAL_SIZE s3_content_length=$S3_SIZE"

# AC-3 локально: ровно RETENTION_DAYS файлов.
#
# ПОЧЕМУ `RETENTION_DAYS - 1`, А НЕ `RETENTION_DAYS`. `-mtime +N` — «строго
# больше N полных суток». Дамп семидневной давности имеет возраст 7 суток
# ПЛЮС секунды снятия, поэтому под `+7` попадает и `+7` оставляет 8 файлов —
# то есть AC-3 («остаётся 7») НЕ выполняется при формально верном
# `RETENTION_DAYS=7`. Замер З-9: из 8 файлов (0…7 дней) `-mtime +7` оставил 8,
# `-mtime +6` — 7. Константа остаётся 7 (NFR45), в `find` уходит `6`.
find "$BACKUP_DIR" -name 'karamel-*.dump' -type f -mtime +$((RETENTION_DAYS - 1)) -delete
