#!/usr/bin/env bash
# Бэкап PostgreSQL с ретенцией 7 дней (стори 10.7, AC-02; NFR45, D-10.7-7).
#
# ЧТО ЭТОТ СКРИПТ ДЕЛАЕТ. Создаёт дамп `pg_dump --format=custom` и удаляет
# файлы старше RETENTION_DAYS (по умолчанию 7 дней — NFR45).
#
# ЧЕГО ОН НЕ ДЕЛАЕТ. Он НЕ проверяет восстановление. «Восстановление выполнено
# однократно до пилота» (AC-02) — это ДОКУМЕНТ и РАЗОВАЯ ПРОЦЕДУРА
# (`docs/runbook-backup-restore.md`, W-10.7-a), а не код: тест не докажет, что
# человек восстановил базу. Выдавать документ за код запрещено (D-10.7-7).
#
# ПОЧЕМУ `--format=custom`. Формат `plain` (SQL-текст) не поддерживает
# выборочное восстановление таблицы и сжимается хуже; `custom` — формат
# `pg_restore`, с которым работает рунбук, и единственный, где `--no-owner`
# имеет смысл.
#
# ПОЧЕМУ `--no-owner --no-privileges`. Дамп снимается под технической ролью
# стенда (`karamel`), а восстанавливаться может под другой ролью или в другую
# базу. Владельцы и гранты в дампе сделали бы восстановление в копию падающим
# на `role "karamel" does not exist` — то есть проверка восстановления
# упиралась бы в права, а не в данные.
#
# Запуск: `bash scripts/backup/pg-backup.sh` на хосте стенда (или из cron).
# `pg_dump` обязан быть в PATH: в образе api его НЕТ (`Dockerfile:1` —
# python:3.13-slim без postgresql-client), поэтому скрипт ставится на хост.
# `postgresql-client-16` из PGDG, а не 17 из trixie: формат дампа обязан
# совпадать с `postgres:16-alpine` (D-10.7-7).

set -euo pipefail

#: Куда складывать дампы. По умолчанию — `./backups` рядом с корнем репозитория.
BACKUP_DIR="${BACKUP_DIR:-./backups}"
#: Сколько дней хранить. 7 — требование NFR45; больше не «надёжнее», а дороже
#: по диску стенда, который не резиновый.
RETENTION_DAYS="${RETENTION_DAYS:-7}"

mkdir -p "$BACKUP_DIR"

# Метка времени ISO-подобная и без пробелов: `20261027T011500`. Сортируется
# лексикографически как хронологически — по имени файла видно, какой дамп
# новее, без `ls -lt`.
STAMP="$(date +%Y%m%dT%H%M%S)"
OUTPUT_FILE="$BACKUP_DIR/karamel-$STAMP.dump"

echo "backup.start file=$OUTPUT_FILE"

# `--format=custom` — см. выше. `--dbname` из `DATABASE_URL`, из которого
# снимается SQLAlchemy-префикс `+asyncpg`: `pg_dump` его не понимает.
DB_URL="${DATABASE_URL:-postgresql://karamel:karamel_pass@127.0.0.1:5432/karamel_db}"
DB_URL="${DB_URL/postgresql+asyncpg:\/\//postgresql://}"

pg_dump --format=custom --no-owner --no-privileges --dbname="$DB_URL" --file="$OUTPUT_FILE"

# Ретенция: `-mtime +N` — строго старше N суток. `+7` при дампе раз в сутки
# оставляет ровно 7 файлов, а не 8: восьмой имеет mtime ровно 7*24 ч и под
# `+7` не попадает.
find "$BACKUP_DIR" -name 'karamel-*.dump' -type f -mtime +"$RETENTION_DAYS" -delete

echo "backup.ok file=$OUTPUT_FILE retention_days=$RETENTION_DAYS"
