"""Репозиторий учётных записей (Task 5.1).

D-4: сессия передаётся аргументом — репозиторий не создаёт её сам, иначе
составная операция («создать пользователя + выдать токен») перестаёт быть
одной транзакцией.
AC-02: доступ снаружи — только `get_by_uuid`; `get_by_id` существует
исключительно для внутренних путей, где известен `user_id` из строки БД
(ротация refresh-токена, стори 1.5), и не принимает значение из запроса.
"""

from __future__ import annotations

import re
import uuid as uuid_lib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.user import User

COMMUNITY_SLUG_PATTERN = re.compile(r"[a-z0-9_-]{3,64}")

# DN-3 (CR 10.1): коды отказа — верхний регистр через подчёркивания, как
# `PLACE_UNKNOWN_COMMUNITY`. Две причины разделены намеренно: сообщества нет
# (данные устарели) и форма не та (битый ввод) — действия клиента разные.
UNKNOWN_COMMUNITY_CODE = "UNKNOWN_COMMUNITY"
INVALID_COMMUNITY_SLUG_CODE = "INVALID_COMMUNITY_SLUG"


class UserCommunitySlugError(Exception):
    """DN-3 (CR 10.1): слаг сообщества отвергнут ДО `INSERT`.

    Без этой проверки обе причины доходили до БД: неверная форма ломала
    `ck_users_community_slug` (`IntegrityError`, который `auth_service`
    разбирал как гонку и отдавал `500`), переполнение — `DataError`, не
    перехватываемый вовсе. ADR-01 требует сверки значений с
    `Settings.community_slugs` на записи; здесь она и выполняется.
    """

    def __init__(self, code: str, community_slug: str) -> None:
        super().__init__(code)
        self.code = code
        self.community_slug = community_slug


def _validate_community_slug(community_slug: str, community_slugs: object) -> None:
    """DN-3: форма и принадлежность конфигу — до обращения к БД.

    Рантайм-гард к типовому (тот же приём, что `place_service._resolve_...`):
    `in` по строке ищет ПОДСТРОКУ, и `community_slugs = "tomsk"` пропустил бы
    слаг `toms`.
    """
    is_valid_form = isinstance(community_slug, str) and (
        COMMUNITY_SLUG_PATTERN.fullmatch(community_slug) is not None
    )
    if not is_valid_form:
        raise UserCommunitySlugError(INVALID_COMMUNITY_SLUG_CODE, str(community_slug))
    if not isinstance(community_slugs, (tuple, list)):
        raise TypeError(
            "settings.community_slugs должен быть последовательностью слагов, "
            f"получено {type(community_slugs).__name__}"
        )
    if community_slug not in community_slugs:
        raise UserCommunitySlugError(UNKNOWN_COMMUNITY_CODE, community_slug)


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
        community_slug: str | None = None,
    ) -> User:
        """Создаёт запись. Коммит — на вызывающей стороне (единица работы).

        AC-01 стори 10.1, D-10.1-5: `community_slug` обязателен в схеме, но
        необязателен в API. `None` — берём `settings.default_community_slug`:
        ADR-01 предписывает при **записи** брать слаг из конфига, а не от
        клиента, поэтому fallback здесь не обходит правило, а реализует его.
        Импорт локальный: модуль репозитория не должен тянуть конфиг при
        импорте (unit-тесты без окружения).

        DN-3 (CR 10.1): явно заданный `community_slug` сверяется с
        `Settings.community_slugs` и с формой до `INSERT`. Отказ —
        `UserCommunitySlugError` (`422` в роуте), а не `IntegrityError`/`500`.
        """
        # Импорт локальный: модуль репозитория не должен тянуть конфиг при
        # импорте (unit-тесты без окружения) — форма из D-10.1-5 сохранена.
        from app.core.config import settings

        if community_slug is None:
            # Fallback реализует ADR-01 (запись — из конфига), а не обходит его.
            community_slug = settings.default_community_slug
        else:
            # DN-3: явно заданный слаг проверяется до INSERT. Fallback не
            # проверяется: `Settings` валидирует `default_community_slug`
            # на старте (`config.py:617`), второе чтение того же инварианта
            # дало бы ложное чувство защиты без нового покрытия.
            _validate_community_slug(community_slug, settings.community_slugs)
        user = User(
            provider=provider,
            provider_user_id=provider_user_id,
            phone=phone,
            role=role,
            gender=gender,
            status=status,
            community_slug=community_slug,
        )
        self._session.add(user)
        await self._session.flush()
        return user
