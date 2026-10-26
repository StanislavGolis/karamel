"""Сервис удаления аккаунта с 7-дневным окном (стори 10.2; AC-01…AC-08).

**Главное решение D-10.2-1.** До этой стори `deletion_requested_at` трактовался
как запрет доступа (`403 user_blocked`) в пяти местах. Это делало AC-01
недостижимым: пользователь не мог войти, значит не мог нажать «отменить» —
окно превращалось в мгновенный бан. Предикат заменён на «окно истекло»:
`deletion_requested_at IS NOT NULL AND deletion_requested_at <= now() - 7d`.

**Единственный предикат окна** — `deletion_window_expired(user, *, now)`.
Все пять точек (`api/deps.py`, `api/v1/ws.py`, `services/auth_service.py`,
`services/session_service.py`, `api/v1/phone_auth.py`) зовут её, а не
переписывают условие: расхождение хотя бы в одной точке = чужой вход или
чужая отмена (P-16).

**Окно = константа, не настройка** (D-10.2-4). `DELETION_WINDOW_DAYS = 7`
живёт здесь. `Settings`-переменная позволила бы на стенде поставить 0 дней и
«доказать» удаление, сломав NFR37. Тесты работают с параметром `now`.

**Порядок физического удаления (AC-08, D-10 стори 2.4).** `storage_key` фото
собираются ДО `DELETE` (после удаления строк их негде взять), объекты S3
удаляются ПОСЛЕ `commit`: ошибка хранилища не откатывает удаление аккаунта.
Обратный порядок удалил бы объекты, которые при откате остались бы в БД
с битыми ссылками.

**`access_logs` НЕ удаляется** (D-10.2-5, ADR-14): FK на `users` нет, журнал
аудита обязан переживать удаление.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.account_deletion import AccountDeletionRepository
from app.repositories.users import UserRepository
from app.services import media_service
from app.services.deletion_window import DELETION_WINDOW, DELETION_WINDOW_DAYS

__all__ = [
    "DELETION_WINDOW_DAYS",
    "DeletionAlreadyRequestedError",
    "DeletionStatus",
    "PurgeResult",
    "as_utc",
    "cancel_deletion",
    "deletion_window_expired",
    "purge_due_accounts",
    "purge_after",
    "read_status",
    "request_deletion",
]

logger = structlog.get_logger(__name__)


class DeletionAlreadyRequestedError(Exception):
    """AC-01, D-10.2-7: повторный `POST` в активном окне → `409`.

    Отдельный класс, а не `ValueError`: `409` получает только эта причина,
    иначе клиент не отличит «окно уже идёт» от любого сбоя сервиса.
    Поле `purge_after` в исключении нужно роутеру: ответ `409` обязан нести
    дату окончания, иначе клиент не нарисует кнопку отмены.
    """

    code = "deletion_already_requested"

    def __init__(self, purge_after: datetime) -> None:
        super().__init__(self.code)
        self.purge_after = purge_after


@dataclass(frozen=True, slots=True)
class DeletionStatus:
    """AC-05: единственный источник даты окна для клиента (D-10.2-6)."""

    deletion_requested_at: datetime | None
    purge_after: datetime | None


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """AC-02, AC-07: результат одного прогона воркера.

    `purged` — число ФАКТИЧЕСКИ удалённых учёток (не «сколько найдено»).
    `orphaned_objects` — сколько объектов S3 не удалось убрать: ошибка
    хранилища логируется (D-10 стори 2.4), но прогон не откатывается.
    """

    purged: int = 0
    orphaned_objects: int = 0


def as_utc(moment: datetime) -> datetime:
    """P-12 (CR 10.2): naive `datetime` трактуется как UTC, aware приводится к UTC.

    Без нормализации `now` naive из `run_account_purges(now=…)` падал бы в
    `requested_at <= now - DELETION_WINDOW` с `TypeError` (500 в гарде),
    а если бы и прошёл — сравнение в SQL-предикатах репозитория уехало бы
    на смещение зоны процесса (`tzinfo` там не проверяется).

    `requested_at` нормализовался и раньше; теперь обе стороны сравнения
    проходят одну функцию — асимметрия «`now` не нормализуется,
    `requested_at` — да» снята.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def deletion_window_expired(user: User, *, now: datetime) -> bool:
    """AC-01, AC-06: единственный предикат «доступ закрыт по удалению».

    **P-19: граница проверяется по обе стороны.** `now - 7d + 1s` — окно ещё
    активно, доступ есть. `now - 7d - 1s` — окно истекло, доступ закрыт.
    Значение `now()` без смещения не доказывает ничего: оно внутри окна.

    Зачем параметр `now`, а не `datetime.now(UTC)` внутри: тест не должен
    ждать семь суток и не должен подменять конфиг (D-10.2-4).

    Naive `deletion_requested_at` трактуется как UTC — так же, как
    `schemas/user.py` трактует naive `created_at`; asyncpg с
    `DateTime(timezone=True)` возвращает aware, но защита от naive нужна.
    """
    requested_at = user.deletion_requested_at
    if requested_at is None:
        return False
    # P-12: обе стороны сравнения через `as_utc` — иначе naive `now` даёт TypeError.
    return as_utc(requested_at) <= as_utc(now) - DELETION_WINDOW


def purge_after(requested_at: datetime) -> datetime:
    """AC-01, AC-05: `purge_after = deletion_requested_at + 7d`.

    Считается из даты запроса, а не из `now`: повторный `POST` окно не
    продлевает (AC-01), и клиент обязан видеть неизменный срок.
    """
    # P-12: обе стороны через `as_utc` — same форма отказа, что в предикате.
    return as_utc(requested_at) + DELETION_WINDOW


async def request_deletion(
    session: AsyncSession, user: User, *, now: datetime
) -> tuple[datetime, datetime]:
    """AC-01: пометить учётку на удаление, вернуть `(requested_at, purge_after)`.

    **Окно не продлевается** (D-10.2-7): повторный запрос в активном окне даёт
    `DeletionAlreadyRequestedError` с уже существующим `purge_after`. Отдать
    `200` с новой датой значило бы позволить клиенту отодвигать удаление
    бесконечно — то есть не удалять никогда.

    Если пометка есть, но окно истекло, — это не «активный запрос»: такой
    пользователь до сервиса не доходит (AC-06 закрывает доступ `403` раньше),
    поэтому ветка не обрабатывается отдельно.

    **P-1 (CR 10.2): гонка двойного `POST`.** Проверка `is not None` и запись
    `now` разнесены, а `User` пришёл из `get_by_uuid` без `FOR UPDATE`: два
    параллельных запроса оба видели `None`, оба ставили метку, второй
    перезаписывал дату — `409` не отдавался ни одному, окно продлевалось.
    Теперь строка захватывается `UserRepository.lock_for_update` (готовый
    метод, `users.py:95`) ДО проверки, а `refresh` перечитывает поле из БД:
    второй запрос ждёт коммита первого и видит уже выставленную метку.
    Порядок «блокировка → refresh → проверка → запись» обязателен: блокировка
    без `refresh` оставила бы в объекте устаревшее `None` из identity map.
    """
    # P-1: сериализация двух POST на одной строке — иначе окно продлевается.
    await UserRepository(session).lock_for_update(user.id)
    await session.refresh(user, attribute_names=["deletion_requested_at"])

    if user.deletion_requested_at is not None:
        raise DeletionAlreadyRequestedError(purge_after(user.deletion_requested_at))

    # P-12: в БД пишется нормализованное значение — naive `now` не сдвигает зону.
    moment = as_utc(now)
    user.deletion_requested_at = moment
    await session.flush()
    return moment, purge_after(moment)


async def cancel_deletion(session: AsyncSession, user: User) -> None:
    """AC-03: снять пометку. Идемпотентно — повторный `DELETE` тоже `204`.

    `flush`, а не `commit`: владелец транзакции — роутер (как в
    `profile_service`). Иначе отмена коммитилась бы до проверок роутера.
    """
    if user.deletion_requested_at is None:
        return
    user.deletion_requested_at = None
    await session.flush()


def read_status(user: User) -> DeletionStatus:
    """AC-05: `GET /me/delete` — `deletion_requested_at` и `purge_after`.

    Без пометки оба поля `null`: клиент различает «окна нет» по `null`, а не
    по отсутствию ключей (D-10.2-6). Другого источника даты у клиента нет —
    расширять `GET /profiles/me` отказано, анкета и учётка — разные оси.
    """
    requested_at = user.deletion_requested_at
    if requested_at is None:
        return DeletionStatus(deletion_requested_at=None, purge_after=None)
    return DeletionStatus(deletion_requested_at=requested_at, purge_after=purge_after(requested_at))


async def purge_due_accounts(session: AsyncSession, *, now: datetime, limit: int) -> PurgeResult:
    """AC-02, AC-07, AC-08: физическое удаление учёток с истёкшим окном.

    **Порядок (AC-08, D-10 стори 2.4):**
    1. выбрать кандидатов `FOR UPDATE SKIP LOCKED` (два воркера не берут одну
       строку — D-10.2-3, NFR46–NFR49);
    2. собрать `storage_key` фото — ДО удаления строк, иначе их негде взять;
    3. физическое `DELETE` мимо ORM — каскады отрабатывает БД (Ловушка 4);
    4. `commit` — строка и все каскады зафиксированы;
    5. удалить объекты S3; ошибка хранилища логируется, но НЕ откатывает
       удаление (иначе «аккаунт жив, потому что S3 недоступен»).

    **CAS-предикат (AC-07).** `DELETE ... WHERE deletion_requested_at <= now - 7d`
    в одном операторе: второй прогон видит пустой `RETURNING` и удаляет 0 строк.
    Отдельной таблицы «что обработано» нет и не будет — предикат и есть флаг.

    `commit` внутри — в отличие от `request_deletion`/`cancel_deletion`:
    воркер владеет транзакцией сам, роутера над ним нет.
    """
    repo = AccountDeletionRepository(session)
    candidates = await repo.select_due_candidates(now=now, limit=limit)
    if not candidates:
        return PurgeResult()

    purged = 0
    orphaned = 0
    for user_id, user_uuid in candidates:
        # P-9: `duration_ms` считается на КАЖДУЮ учётку от этой точки.
        started = time.monotonic()
        # AC-08: ключи собираются ДО `DELETE` — после удаления строк фото нет.
        keys = await repo.collect_photo_keys(user_id)
        deleted = await repo.delete_user(user_id, now=now)
        if deleted == 0:
            # AC-07: строка уже удалена другим воркером или окно сдвинулось —
            # CAS не сработал, объекты S3 не трогаем (ключи могли не удалиться).
            continue
        purged += 1
        await session.commit()

        for key in keys:
            try:
                await media_service.delete_object(key)
            except Exception:  # noqa: BLE001 - D-10 стори 2.4, как в DELETE фото
                # Строка в БД уже удалена (`commit` выше): возврат ошибки
                # откатил бы успешное удаление аккаунта из-за сбоя S3.
                # Ключ, а не объект: сам ключ в лог не идёт (D-11).
                orphaned += 1
                logger.warning("deletion_photo_object_orphaned", storage_key_len=len(key))

        # P-9 (CR 10.2): D-10.2-8 требует `duration_ms` в `account_purged`, а не
        # только `user_uuid`. Считается на учётку от `started` (до SELECT
        # кандидатов не включается — это время прогона, а не удаления).
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "account_purged",
            user_uuid=str(user_uuid),
            photos=len(keys),
            duration_ms=duration_ms,
        )

    return PurgeResult(purged=purged, orphaned_objects=orphaned)
