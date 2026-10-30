"""Свежесть бэкапа БД для `/health/deep` (стори 11.10, AC-4; NFR43).

**Зачем признак вообще нужен.** Cron, который перестал запускаться, не
сообщает об этом: задание просто не выполняется, а лог молчит. Единственный
способ заметить молчаливый отказ — сравнить ВОЗРАСТ последнего объекта в S3
с порогом. Возраст берётся у хранилища (`LastModified`), а не у локального
файла: локальные дампы чистит ретенция, и «нет файла» — штатное состояние,
а не отказ.

**Почему 26 часов, а не 24.** Бэкап идёт раз в сутки в 03:15. Порог 24 ч
давал бы `stale` каждое утро в 03:00–03:15 — ложный алерт на исправной
системе, то есть шум, из-за которого пропустят настоящий. Два часа запаса
покрывают длительность бэкапа и дрейф cron.

**Почему `backup_freshness`, а не `check_backup`.** `check_*` в этом проекте
занято `_check_db`/`_check_redis` в `health.py`, где возврат строго
`ok`/`fail:<тип>`. Здесь третий статус — `stale` — «работает, но
деградировало», и он не должен читаться как отказ.

**Почему `boto3` в потоке.** `boto3` синхронный (D-2): вызов `list_objects_v2`
из event loop заблокировал бы весь api на время сетевого раунда, а
`/health/deep` дёргается раз в минуту воркером. `anyio.to_thread` — тот же
приём, что в `media_service`.

**Почему ошибка не превращается в `stale`.** `stale` означает «бэкапа нет
дольше суток» — инцидент данных. Недоступность S3 — инцидент хранилища, и у
него свой код (`fail:<тип>`). Слить их значило бы послать дежурного
проверять cron, когда легло хранилище.
"""

from __future__ import annotations

import datetime
from typing import Any

import anyio
import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = structlog.get_logger(__name__)

#: Порог «свежести» в часах (AC-4). Константа модуля, а не литерал на месте
#: вызова: порог — часть контракта, и его изменение обязано быть видно в одном
#: месте. Тест порога проверяет ОБЕ стороны границы через неё же.
STALE_AFTER_HOURS = 26

#: Префикс объектов-дампов. Тот же, что `S3_PREFIX` в
#: `scripts/backup/pg-backup.sh`: разойдись они — проверка смотрела бы в
#: пустоту и всегда говорила бы `stale` на исправном канале.
BACKUP_PREFIX = "db/"

OK = "ok"
STALE = "stale"

# D-2: `path`-адресация и `s3v4` — как в `media_service.py:53`: провайдеры ДЦ РФ
# не всегда умеют virtual-hosted стиль для бакетов с точками в имени.
_S3_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "path"})


def _credentials_configured() -> bool:
    """Все пять реквизитов бэкапа заданы.

    Пустые реквизиты — штатное состояние dev и CI, а не дефект. Отличать его
    от «S3 недоступен» нужно, иначе каждый локальный запуск давал бы
    `fail:...` в `checks.backup`.
    """
    return all(
        value.strip()
        for value in (
            settings.s3_backup_endpoint,
            settings.s3_backup_region,
            settings.s3_backup_bucket,
            settings.s3_backup_access_key,
            settings.s3_backup_secret_key,
        )
    )


def _last_backup_age(now: datetime.datetime) -> datetime.timedelta | None:
    """Возраст самого свежего объекта `db/` относительно `now` — синхронно.

    `None` — объектов нет вообще: бэкап не выполнялся ни разу. Это НЕ ошибка
    хранилища (иначе исключение): канал настроен, но не отработал, и признак
    обязан совпасть с «бэкапа нет» — `stale`.

    `now` — аргумент, а не `datetime.now()` внутри: боевой путь передаёт
    текущий момент, тест порога 26 ч — произвольный, и оба идут одной ветвью
    кода. Подмена `datetime.now` через `monkeypatch` в тесте проверяла бы
    отчёт о времени, а не вычисление возраста.
    """
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_backup_endpoint,
        region_name=settings.s3_backup_region,
        aws_access_key_id=settings.s3_backup_access_key,
        aws_secret_access_key=settings.s3_backup_secret_key,
        config=_S3_CONFIG,
    )
    newest: datetime.datetime | None = None
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_backup_bucket, Prefix=BACKUP_PREFIX):
        for item in page.get("Contents", []):
            modified: datetime.datetime = item["LastModified"]
            if newest is None or modified > newest:
                newest = modified
    if newest is None:
        return None
    return now - newest


async def backup_freshness(now: datetime.datetime | None = None) -> str:
    """Признак свежести бэкапа: `ok`, `stale` или `fail:<ТипИсключения>`.

    Возвращает СТРОКУ, как `_check_db`/`_check_redis`, — маршрут складывает её
    в `checks` без разбора типов. `stale` не является отказом сервиса: HTTP-код
    `/health/deep` от него не меняется (AC-4, задача 4.3).
    """
    if not _credentials_configured():
        # Реквизиты не заданы — говорить нечего. `ok` здесь был бы ложью («всё
        # хорошо», хотя бэкап не проверялся), а `stale` — ложным инцидентом в
        # dev. `fail:NotConfigured` — честное «не проверено».
        return "fail:NotConfigured"

    moment = now if now is not None else datetime.datetime.now(datetime.UTC)

    try:
        age: Any = await anyio.to_thread.run_sync(_last_backup_age, moment)
    except (BotoCoreError, ClientError, OSError) as error:
        logger.warning(
            "health.deep.backup_check_failed",
            error_type=type(error).__name__,
        )
        return f"fail:{type(error).__name__}"

    if age is None:
        # Объектов нет — бэкап не выполнялся ни разу. `stale`, а не `fail`:
        # хранилище доступно (иначе была бы ветка выше), данных бэкапа нет.
        return STALE

    return OK if age <= datetime.timedelta(hours=STALE_AFTER_HOURS) else STALE
