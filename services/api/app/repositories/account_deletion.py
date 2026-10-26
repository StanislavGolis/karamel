"""Репозиторий физического удаления аккаунта (стори 10.2; AC-02, AC-04, AC-07, AC-08).

**Почему `DELETE` мимо ORM** (Ловушка 4). `session.delete(user)` падает на
связанных объектах: `lazy="raise"` стоит на `User.refresh_tokens`, а
`passive_deletes=True` объявлен только там. ORM попытался бы обнулить
`join_requests.user_id`, `event_cards.author_id` и прочие NOT NULL колонки,
что дало бы `NotNullViolation` вместо каскада БД. `delete(User).where(...)`
идёт мимо unit-of-work: каскады отрабатывает PostgreSQL, как и задумано
миграциями (`ON DELETE CASCADE` в `0001`, `0004`, `0008`, `0012`, `0014`,
`0020`, `0021`, `0025`, `0028`, `0030`, `0031`).

**`access_logs` НЕ удаляется** (D-10.2-5, ADR-14): FK на `users` нет, журнал
аудита обязан переживать удаление аккаунта.

**`SKIP LOCKED` обязателен** (D-10.2-3, NFR46–NFR49): два контейнера воркера
не должны блокировать друг друга на одной пачке кандидатов.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile
from app.models.profile_photo import ProfilePhoto
from app.models.user import User
from app.services.deletion_window import DELETION_WINDOW

__all__ = ["AccountDeletionRepository"]


class AccountDeletionRepository:
    """Запросы воркера ретенции. `commit` не делает — владелец транзакции сервис."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _validate_limit(limit: int) -> int:
        """P-15 (CR 10.2): `limit` проверяется ДО `LIMIT`.

        Без проверки `limit=-1` доходил до PostgreSQL и падал
        `LIMIT must not be negative`; воркер ловил это `except Exception`
        и логировал `account_purge_failed` КАЖДЫЙ цикл, то есть настоящая
        причина тонула в шуме, а прогон не удалял никого.

        `ValueError`, а не `return 0`: отрицательный лимит — ошибка
        вызывающего кода, а не «нечего делать». Молчаливый `0` скрыл бы
        дефект навсегда.
        """
        if limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")
        return limit

    @staticmethod
    def build_due_candidates_statement(*, now: datetime, limit: int) -> Any:
        """AC-02, AC-07: оператор кандидатов — отдельно от `execute`.

        Вынесен в статический метод НАМЕРЕННО (18.3): `FOR UPDATE SKIP LOCKED`
        в одной сессии ведёт себя как без него — блокировку никто не держит.
        Мутация «убрать `SKIP LOCKED`» на интеграционном тесте выживает
        (M2 SURVIVED), поэтому текст запроса проверяется unit-тестом
        `tests/unit/test_account_deletion_sql.py`, который зовёт ЭТОТ метод,
        а не копию запроса. Копия в тесте не увидела бы правку репозитория.
        """
        checked = AccountDeletionRepository._validate_limit(limit)
        return (
            select(User.id, User.uuid)
            .where(
                User.deletion_requested_at.is_not(None),
                User.deletion_requested_at <= now - DELETION_WINDOW,
            )
            .order_by(User.deletion_requested_at)
            .limit(checked)
            .with_for_update(skip_locked=True, of=User)
        )

    async def select_due_candidates(
        self, *, now: datetime, limit: int
    ) -> list[tuple[int, uuid_lib.UUID]]:
        """AC-02, AC-07: учётки с истёкшим окном — пары `(id, uuid)`.

        `FOR UPDATE SKIP LOCKED` (D-10.2-3): второй воркер пропускает уже
        заблокированные строки вместо ожидания. `uuid` нужен логу
        (`account_purged`): внутренний `id` наружу не выходит (§7).

        Пара, а не ORM-объект: после `DELETE` мимо ORM объект в identity map
        держал бы удалённую строку. Идентификаторы внутренние, наружу не идут.
        """
        statement = self.build_due_candidates_statement(now=now, limit=limit)
        result = await self._session.execute(statement)
        return [(int(row[0]), row[1]) for row in result.all()]

    async def collect_photo_keys(self, user_id: int) -> list[str]:
        """AC-08: `storage_key` фото пользователя — ДО физического удаления.

        После `DELETE FROM users` каскад уносит `profiles` и `profile_photos`,
        и ключи взять будет негде: объекты остались бы в S3 навсегда.
        """
        statement = (
            select(ProfilePhoto.storage_key)
            .join(Profile, Profile.id == ProfilePhoto.profile_id)
            .where(Profile.user_id == user_id)
        )
        result = await self._session.execute(statement)
        return [str(row[0]) for row in result.all()]

    async def delete_user(self, user_id: int, *, now: datetime) -> int:
        """AC-02, AC-04, AC-07: физическое удаление с CAS-предикатом окна.

        Предикат `deletion_requested_at <= now - 7d` повторён в `DELETE`
        намеренно: между `SELECT` и `DELETE` второй воркер мог уже удалить
        строку, а пользователь — отменить запрос (тогда предикат не совпадёт
        и удаления не будет). Возвращает число удалённых строк: `0` значит
        «CAS не сработал», и сервис не трогает объекты S3.

        Каскады отрабатывает БД (см. докстринг модуля).
        """
        statement = (
            delete(User)
            .where(
                User.id == user_id,
                User.deletion_requested_at.is_not(None),
                User.deletion_requested_at <= now - DELETION_WINDOW,
            )
            .returning(User.id)
        )
        result = await self._session.execute(statement)
        return len(result.all())
