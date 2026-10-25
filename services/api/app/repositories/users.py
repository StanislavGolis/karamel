"""Репозиторий учётных записей (Task 5.1).

D-4: сессия передаётся аргументом — репозиторий не создаёт её сам, иначе
составная операция («создать пользователя + выдать токен») перестаёт быть
одной транзакцией.
AC-02: доступ снаружи — только `get_by_uuid`; `get_by_id` существует
исключительно для внутренних путей, где известен `user_id` из строки БД
(ротация refresh-токена, стори 1.5), и не принимает значение из запроса.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.user import User


class UserRepository:
    """Запросы к `users` в терминах домена."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def build_lock_statement(user_id: int) -> Select[Any]:
        """ADR-09 (стори 5а.2, AC-09): блокировка строки актора ПЕРВОЙ в транзакции.

        `FOR UPDATE` на `users` — единственная точка сериализации, где счётчик
        активных заявок меняется САМИМ запросом: две подачи одного участника
        встают в очередь здесь, и `COUNT` под блокировкой видит уже
        зафиксированный результат первой (ADR-09: порядок `users → cards`).

        Выборка `User.id`, а не `User`: блокировка строки не требует остальных
        колонок (приём `EventCardRepository.build_author_lock_statement` 4.4).

        Билдер статический, чтобы текст `FOR UPDATE` проверялся без БД (§18.2):
        снятие `.with_for_update()` — реальный дефект, дающий три успешные
        подачи вместо двух (AC-08 краснеет).
        """
        return select(User.id).where(User.id == user_id).with_for_update()

    async def lock_for_update(self, user_id: int) -> None:
        """AC-09, ADR-09: захватывает строку актора первой в транзакции.

        Отдельный метод, а не вызов билдера из сервиса: сервис не должен
        собирать SQL вручную (project-context §6).
        """
        await self._session.execute(self.build_lock_statement(user_id))

    async def get_by_uuid(self, user_uuid: uuid_lib.UUID) -> User | None:
        """AC-02: единственный способ адресовать пользователя снаружи."""
        result = await self._session.execute(select(User).where(User.uuid == user_uuid))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        """Task 2.1 (AC-02, AC-05): поиск по внутреннему PK для путей без UUID.

        Ротации refresh-токена известен только `user_id` из строки
        `refresh_tokens` — публичного `uuid` там нет и не будет (у refresh-токенов
        нет адресуемости по API, project-context §7).

        Значение приходит из БД, а не из запроса, поэтому перебор `/1..N`
        через этот метод невозможен: снаружи адресация только по `uuid`.
        """
        # Task 2.2: SELECT по PK без `joinedload` — `lazy="raise"` остаётся
        # страховкой от случайной ленивой загрузки в async-контексте.
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_provider_user_id(self, provider: str, provider_user_id: str) -> User | None:
        """Task 5.1 (AC-03): поиск по паре провайдер+внешний id.

        D3: не по email. Почта из OAuth не подтверждена владельцем в контексте
        продукта и может отсутствовать; поиск по email позволил бы захватить
        чужую учётку, сменив почту у провайдера. Пара закрыта уникальным
        ограничением `uq_users_provider_user_id` (стори 1.2).
        """
        result = await self._session.execute(
            select(User).where(
                User.provider == provider,
                User.provider_user_id == provider_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> User | None:
        """Task 6.1 (AC-05): поиск по номеру в E.164.

        Отдельный метод, а не `get_by_provider_user_id("phone", phone)`: номер
        может быть привязан к учётке **другого** провайдера (OAuth-вход), и
        именно этот случай даёт `409 phone_taken` вместо автосклейки (D-6).
        `get_by_id` по-прежнему отсутствует (AR21/AR22).
        """
        result = await self._session.execute(select(User).where(User.phone == phone))
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        provider: str,
        provider_user_id: str | None = None,
        phone: str | None = None,
        role: str = "user",
        gender: str | None = None,
        status: str = "active",
    ) -> User:
        """Создаёт запись. Коммит — на вызывающей стороне (единица работы)."""
        user = User(
            provider=provider,
            provider_user_id=provider_user_id,
            phone=phone,
            role=role,
            gender=gender,
            status=status,
        )
        self._session.add(user)
        await self._session.flush()
        return user
