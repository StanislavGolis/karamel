"""Воркер ретенции: физическое удаление аккаунтов с истёкшим окном (стори 10.2).

**Что здесь есть** (AC-02, AC-07). Цикл с интервалом 60 с проходит кандидатов
`deletion_requested_at <= now() - 7d` и удаляет их: учётку, переписку (NFR40),
фото (NFR41) и связанные домены по перечню D-10.2-5.

**Почему отдельный процесс, а не HTTP** (AR33/AR34). Прогон каждые 60 секунд
в API-процессе держал бы блокировки `FOR UPDATE SKIP LOCKED` и конкурировал
с запросами пользователей. `docker-compose.yml` запускает этот модуль как
сервис `worker` (Task 6.4): до 10.2 там стоял `app.workers.likes_reset` —
заглушка, после 10.1 не делавшая ничего, то есть воркера удаления в проде
не существовало и AC-02 проверялся бы только локально (§18.5).

**Идемпотентность (AC-07).** CAS-предикат живёт в `account_deletion_repository`:
второй прогон видит пустой `RETURNING` и удаляет 0 строк. Отдельной таблицы
«что обработано» нет и не будет — предикат и есть флаг.

**Наблюдаемость (ADR-14).** Structlog-событие `account_purged` на КАЖДУЮ
учётку с `user_uuid` (не `id`, project-context §7), `photos` и `duration_ms`
(P-9, CR 10.2). Сводка прогона `account_purge_run` несёт `purged`,
`orphaned_objects` и `duration_ms` (P-10): до правки `orphaned_objects`
считался в сервисе, возвращался в `PurgeResult` и молча терялся —
`s3`-сироты не попадали ни в лог, ни в метрику.

**Граница с 10.6.** Ретенция `attendance` (6 месяцев) — отдельная задача того
же воркера с другим предикатом; обе дают свой `run_*` в `main()`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import structlog

from app.core.db import get_session_factory
from app.services.account_deletion_service import PurgeResult, purge_due_accounts
from app.services.attendance_retention_service import RetentionResult, purge_expired_attendance

#: Те же значения, что в `card_windows.py` (D-10.2-8): эксплуатация не должна
#: держать в голове вторую цифру.
WORKER_INTERVAL_SECONDS = 60
#: Воркер обязан вернуть управление, даже если кандидатов тысячи.
WORKER_BATCH_SIZE = 200

logger = structlog.get_logger(__name__)


async def run_account_purges(
    *, now: datetime | None = None, limit: int = WORKER_BATCH_SIZE
) -> PurgeResult:
    """AC-02, AC-07: один прогон удаления. Возвращает `PurgeResult`.

    Тонкая обёртка над ядром `account_deletion_service.purge_due_accounts`:
    второй точки входа НЕТ (приём D-5b.3-3). Своя сессия на прогон — воркер
    владеет транзакцией, `commit` делает сервис после `DELETE` и до S3.

    `now` параметром, а не внутри: тест не ждёт семь суток и не подменяет
    конфиг (D-10.2-4, P-19).

    **P-10 (CR 10.2): возвращается `PurgeResult`, а не `int`.** Раньше функция
    отдавала `result.purged`, и `orphaned_objects` уничтожался между сервисом
    и логом: объекты S3, которые не удалось убрать, не попадали никуда.
    Счётчик, который никто не читает, — это заявление без доказательства
    (тот же класс, что 6 из 25 находок CR 10.1).
    """
    moment = now if now is not None else datetime.now(UTC)
    factory = get_session_factory()
    async with factory() as session:
        return await purge_due_accounts(session, now=moment, limit=limit)


async def run_attendance_retention(
    *, moment: datetime | None = None, limit: int = WORKER_BATCH_SIZE
) -> RetentionResult:
    """AC-01, AC-03: один прогон ретенции явки. Возвращает `RetentionResult`.

    Тонкая обёртка над `attendance_retention_service.purge_expired_attendance`:
    второй точки входа НЕТ (приём D-5b.3-3). Своя сессия на прогон — воркер
    владеет транзакцией, `commit` делает сервис.

    `moment` параметром, а не внутри: тест не ждёт полгода и не подменяет
    конфиг (D-10.2-4, P-19).

    Второй прогон в `main()` идёт ПОСЛЕ удаления аккаунтов: каскад
    `participant_id` (`0020:162-166`) уже унёс часть строк, и ретенция
    работает по урезанному набору (D-10.6-8).
    """
    moment_value = moment if moment is not None else datetime.now(UTC)
    factory = get_session_factory()
    async with factory() as session:
        return await purge_expired_attendance(session, moment=moment_value, limit=limit)


async def main() -> None:
    """Цикл воркера. Один прогон не роняет процесс: ошибка логируется.

    Падение контейнера на первой же ошибке БД оставило бы удаление
    невыполненным до ручного рестарта, а `restart: unless-stopped` в compose
    всё равно поднял бы его через секунды — то есть цикл повтора уже есть,
    и терять накопленный прогресс незачем.
    """
    while True:
        started = time.monotonic()
        try:
            result = await run_account_purges()
        except Exception as exc:  # noqa: BLE001 - воркер не должен умирать
            logger.error("account_purge_failed", error_type=type(exc).__name__)
        else:
            duration_ms = int((time.monotonic() - started) * 1000)
            # P-10: `orphaned_objects` доходит до лога — иначе счётчик мёртв.
            if result.purged or result.orphaned_objects:
                logger.info(
                    "account_purge_run",
                    purged=result.purged,
                    orphaned_objects=result.orphaned_objects,
                    duration_ms=duration_ms,
                )
        # D-10.6-8: ретенция явки — ВТОРОЙ прогон, ПОСЛЕ удаления аккаунтов
        # (каскад `participant_id` уже унёс часть строк). Отдельный `try`:
        # падение ретенции не должно останавливать удаление аккаунтов.
        # Лог — ЗДЕСЬ, в `main()` (сервис не логирует, только возвращает числа),
        # и только при `deleted > 0` — иначе безусловная запись уронила бы
        # `test_worker_logs_orphaned_objects` стори 10.2.
        try:
            attendance = await run_attendance_retention()
        except Exception as exc:  # noqa: BLE001 - воркер не должен умирать
            logger.error("attendance_retention_failed", error_type=type(exc).__name__)
        else:
            # D-10.6-8: событие эмитится ТОЛЬКО при `deleted > 0`. Безусловный
            # лог каждые 60 с уронил бы `test_worker_logs_orphaned_objects`
            # стори 10.2 (`len(captured) == 1`) шумом, а не дефектом.
            # ADR-14: числа и `duration_ms`, без `card_id`/`participant_id` (§7).
            if attendance.deleted > 0:
                logger.info(
                    "attendance_retention_run",
                    deleted=attendance.deleted,
                    cards=attendance.cards,
                    duration_ms=attendance.duration_ms,
                )
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
