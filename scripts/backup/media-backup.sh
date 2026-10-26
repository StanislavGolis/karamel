#!/usr/bin/env bash
# Бэкап медиа с ретенцией 7 дней (стори 10.7, AC-02; NFR45, W-10.7-c).
#
# СТАТУС: ЗАГОТОВКА. Этот скрипт — архив каталога и ретенция; канала выгрузки
# из S3 у него НЕТ. Источник медиа в пилоте — приватный бакет S3 (Timeweb,
# ДЦ РФ), и выгрузку из него выполняет владелец вручную (W-10.7-c): ключи
# доступа S3 живут в `.env` стенда, а не в CI, и скрипт, притворяющийся
# рабочим каналом, опаснее отсутствующего — на него рассчитывают.
#
# ЧТО ЭТОТ СКРИПТ ДЕЛАЕТ. `tar -czf` по каталогу из MEDIA_DIR и удаление
# архивов старше RETENTION_DAYS (7 дней — NFR45). Без новых зависимостей:
# `tar` есть в любом дистрибутиве стенда, `aws-cli`/`s3cmd` — нет.
#
# ЧЕГО ОН НЕ ДЕЛАЕТ. Не проверяет восстановление. «Восстановление выполнено
# однократно до пилота» (AC-02) — документ и разовая процедура
# (`docs/runbook-backup-restore.md`), а не код (D-10.7-7).
#
# Запуск: `bash scripts/backup/media-backup.sh` на хосте стенда (или из cron).

set -euo pipefail

#: Источник медиа. На стенде — каталог, куда смонтирован/выгружен бакет;
#: значения по умолчанию НЕ существует намеренно (пустая строка = падение на
#: `tar`), иначе скрипт «успешно» архивировал бы пустоту и создавал ложную
#: уверенность в наличии бэкапа.
MEDIA_DIR="${MEDIA_DIR:-}"
#: Куда складывать архивы.
BACKUP_DIR="${BACKUP_DIR:-./backups/media}"
#: Сколько дней хранить. 7 — требование NFR45.
RETENTION_DAYS="${RETENTION_DAYS:-7}"

if [ -z "$MEDIA_DIR" ]; then
  echo "media-backup.error reason=MEDIA_DIR_not_set" >&2
  echo "Укажите MEDIA_DIR — каталог выгруженного бакета медиа (W-10.7-c)." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%dT%H%M%S)"
OUTPUT_FILE="$BACKUP_DIR/media-$STAMP.tar.gz"

echo "media.backup.start file=$OUTPUT_FILE source=$MEDIA_DIR"

tar -czf "$OUTPUT_FILE" -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")"

# Ретенция: `-mtime +N` — строго старше N суток (см. `pg-backup.sh`).
find "$BACKUP_DIR" -name 'media-*.tar.gz' -type f -mtime +"$RETENTION_DAYS" -delete

echo "media.backup.ok file=$OUTPUT_FILE retention_days=$RETENTION_DAYS"
