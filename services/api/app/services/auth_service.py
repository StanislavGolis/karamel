"""Разрешение учётной записи по профилю провайдера (Task 5.2, 5.3; AC-03).

D4: гонка двух одновременных callback с одним внешним id реальна (двойной клик,
повтор браузера). Уникальное ограничение `uq_users_provider_user_id` уже есть,
поэтому `INSERT` → `IntegrityError` → `rollback` → повторный `SELECT`
дешевле advisory-локов. `rollback` обязателен: без него сессия остаётся
в состоянии ошибки и следующий запрос падает с `PendingRollbackError`.

D-4 (стори 1.2): `commit()` здесь не вызывается — единица работы принадлежит роуту.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.users import UserRepository
from app.services.oauth import ExternalProfile


class UserBlockedError(Exception):
    """Учётка забанена или ждёт удаления: вход запрещён.

    Без этой проверки бан обходился повторным OAuth-входом: `resolve_user`
    возвращал существующую запись, и роут выдавал свежий refresh на 30 дней.
    """


class PhoneTakenError(Exception):
    """Номер уже привязан к учётке другого провайдера (AC-05, D-6).

    Автосклейки нет намеренно: у OAuth-учётки нет подтверждения владения
    телефоном, поэтому склейка = захват чужой учётки тем, кто первым ввёл номер.
    Владелец номера получил бы доступ к переписке и анкете предыдущего владельца.
    """


async def resolve_phone_user(session: AsyncSession, phone: str) -> tuple[User, bool]:
    """Task 6.2–6.5 (AC-02, AC-05): находит или создаёт учётку по телефону.

    Возвращает `(user, is_new)`: роут отдаёт `is_new` клиенту, чтобы фронт
    отличал первый вход от повторного (разные экраны онбординга).

    D-5: `provider_user_id` = нормализованный E.164, то есть тот же `phone`.
    Поиск идёт по паре `(provider, provider_user_id)` — через существующий метод,
    без второго пути уникальности.
    """
    repository = UserRepository(session)

    existing = await repository.get_by_provider_user_id("phone", phone)
    if existing is not None:
        _ensure_allowed(existing)
        return existing, False

    # AC-05/D-6: номер занят учёткой другого провайдера → 409, без склейки.
    by_phone = await repository.get_by_phone(phone)
    if by_phone is not None:
        # Именно PhoneTakenError, а не UserBlockedError: клиенту нужно
        # предложить войти тем способом, которым учётка создана.
        raise PhoneTakenError

    try:
        created = await repository.create(
            provider="phone",
            provider_user_id=phone,
            phone=phone,
        )
    except IntegrityError:
        # Task 6.5: параллельный verify с тем же номером успел вставить запись.
        await session.rollback()
        raced = await repository.get_by_provider_user_id("phone", phone)
        if raced is not None:
            _ensure_allowed(raced)
            return raced, False
        # Гонка была не на нашей паре (например, нарушен uq_users_phone
        # из-за параллельной вставки другим путём) — проверяем номер ещё раз.
        taken = await repository.get_by_phone(phone)
        if taken is not None:
            raise PhoneTakenError from None
        raise
    # AC-02: 18+ и согласие — стори 1.6; здесь создаются false/NULL (дефолты модели).
    _ensure_allowed(created)
    return created, True


async def resolve_user(session: AsyncSession, profile: ExternalProfile) -> User:
    """AC-03: возвращает существующую учётку или создаёт новую — ровно одну.

    Возвращает `User`, а не DTO: роуту нужны и `uuid` (в токен), и `id`
    (в `refresh_tokens.user_id`), а `id` за пределы сервиса не выходит.
    """
    repository = UserRepository(session)

    existing = await repository.get_by_provider_user_id(profile.provider, profile.provider_user_id)
    if existing is not None:
        _ensure_allowed(existing)
        return existing

    try:
        created = await repository.create(
            provider=profile.provider,
            provider_user_id=profile.provider_user_id,
        )
    except IntegrityError:
        # AC-03: параллельный callback успел вставить запись первым.
        await session.rollback()
        raced = await repository.get_by_provider_user_id(profile.provider, profile.provider_user_id)
        if raced is None:
            # Ошибка не была гонкой (например, нарушен другой CHECK) — не глотаем.
            raise
        _ensure_allowed(raced)
        return raced
    return created


def _ensure_allowed(user: User) -> None:
    """AC-03: бан и запрошенное удаление блокируют вход.

    Проверка на входе, а не только в `get_active`: иначе санкция снимается
    первым же входом через провайдера.
    """
    if user.status == "banned" or user.deletion_requested_at is not None:
        raise UserBlockedError
