"""Аудит FR60 переживает закрытие сессии (стори 12.10; CR H-1, M-2, AC-3).

**Зачем отдельный файл, если строки аудита уже проверяются.** Все прежние
проверки шли через `build_client`, который подменяет `commit` на `flush` и
отдаёт ту же SAVEPOINT-сессию. Такая проверка видит незакоммиченную строку и
**не отличает «зафиксировано» от «откатится»** — ровно дефект CR H-1:
в проде на ветви `404` `commit()` не вызывался, строка терялась, а тест
`test_missing_user_still_writes_audit_row` оставался зелёным.

**Как проверяется эффект, а не форма (P-25).** Запрос идёт через
`get_session`-оверрайд, отдающий НОВУЮ сессию на каждый запрос поверх
настоящего движка (`second_engine`), поэтому `commit()` роутера — настоящий.
Чтение — третьей, заведомо свежей сессией того же движка: она видит только
зафиксированные строки. Без коммита строки нет ни для кого, кроме сессии
самого запроса, которая уже закрыта.

**Почему своя уборка, а не `domain_cleanup`.** `access_logs` НЕ имеет FK на
`users` (0029: UUID-снимки автора и адресата переживают удаление аккаунта —
это замысел аудита), поэтому `TRUNCATE users CASCADE` строки аудита не
трогает. Своя уборка удаляет ровно созданное этим тестом: иначе следующий
тест увидел бы чужой `card_read` и проверка «ровно одна строка» стала бы
зависеть от порядка запуска (P-12).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import get_session
from app.main import app
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@asynccontextmanager
async def real_commit_client(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """Клиент, у которого КАЖДЫЙ запрос получает собственную настоящую сессию.

    Отличие от `build_client` принципиальное: там `commit` подменён на `flush`.
    Здесь коммит роутера закрывает транзакцию по-настоящему, и следующий
    читатель обязан увидеть строку или не увидеть её вовсе.
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        app.dependency_overrides.pop(get_session, None)


def cookie_headers(token: str) -> dict[str, str]:
    """Заголовок аутентификации по access-cookie."""
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


@dataclass
class CommittedWorld:
    """Настоящие коммиты + список созданного для уборки после теста."""

    factory: async_sessionmaker[AsyncSession]
    user_ids: list[int] = field(default_factory=list)
    actor_uuids: list[uuid_lib.UUID] = field(default_factory=list)

    async def make_user(self, *, role: str) -> tuple[User, str]:
        """Пользователь + access-токен, зафиксированные настоящим коммитом."""
        phone = f"+7911{uuid_lib.uuid4().int % 10**7:07d}"
        async with self.factory() as session:
            user = await UserRepository(session).create(
                provider="phone",
                provider_user_id=phone,
                phone=phone,
                role=role,
                gender="female",
                status="active",
            )
            user.age_confirmed = True
            user.consent_given_at = datetime.now(UTC)
            await session.flush()
            issued = await issue_session(session, user)
            user_id = user.id
            user_uuid = user.uuid
            await session.commit()
        self.user_ids.append(user_id)
        self.actor_uuids.append(user_uuid)
        return user, issued.access_token

    def note_subject(self, subject_uuid: uuid_lib.UUID) -> uuid_lib.UUID:
        """Регистрирует адресата аудита — строка уйдёт в уборке."""
        self.actor_uuids.append(subject_uuid)
        return subject_uuid

    async def audit_rows(self, *, subject_uuid: uuid_lib.UUID) -> list[tuple[str, str]]:
        """Строки аудита по адресату, прочитанные СВЕЖЕЙ сессией.

        Сессия своя и новая намеренно: читатель не должен быть той же сессией,
        что писала, иначе проверка выродится в «я вижу свой незакоммиченный
        `flush`» — исходный дефект H-1.
        """
        async with self.factory() as reader:
            result = await reader.execute(
                text(
                    "SELECT action, moderator_uuid::text FROM access_logs  WHERE subject_uuid = :s"
                ),
                {"s": subject_uuid},
            )
            return [(row[0], row[1]) for row in result.all()]

    async def cleanup(self) -> None:
        """Удаляет созданное этим тестом: аудит, затем пользователей.

        Порядок обязателен: `access_logs` не связана с `users` FK, но
        `refresh_tokens` связана каскадом — пользователи удаляются последними,
        чтобы не оставить висящих токенов.
        """
        async with self.factory() as session:
            if self.actor_uuids:
                await session.execute(
                    text(
                        "DELETE FROM access_logs "
                        " WHERE subject_uuid = ANY(:u) OR moderator_uuid = ANY(:u)"
                    ),
                    {"u": self.actor_uuids},
                )
            if self.user_ids:
                await session.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"),
                    {"ids": self.user_ids},
                )
            await session.commit()


@pytest.fixture
async def world(second_engine: AsyncEngine) -> AsyncIterator[CommittedWorld]:
    """Настоящие сессии поверх второго движка + уборка после теста.

    `second_engine` даёт соединение ВНЕ SAVEPOINT-транзакции теста: иначе
    `commit()` роутера остался бы SAVEPOINT-коммитом и проверка снова
    выродилась бы в «вижу то же соединение».
    """
    world = CommittedWorld(async_sessionmaker(bind=second_engine, expire_on_commit=False))
    try:
        yield world
    finally:
        await world.cleanup()


async def test_missing_user_audit_is_visible_from_new_session(world: CommittedWorld) -> None:
    """AC-3, D-96, CR H-1: `404` оставляет строку, видимую ДРУГОЙ сессией.

    Без коммита в ветви `except UserNotFoundError` тест падает: строка
    остаётся в закрытой сессии запроса и откатывается вместе с ней.
    """
    moderator, moderator_token = await world.make_user(role=str(UserRole.MODERATOR))
    missing = world.note_subject(uuid_lib.uuid4())

    async with real_commit_client(world.factory) as client:
        response: Response = await client.get(
            f"/api/v1/moderation/users/{missing}", headers=cookie_headers(moderator_token)
        )
    assert response.status_code == 404, response.text

    rows = await world.audit_rows(subject_uuid=missing)
    # «Ровно одна» и «автор верный» — два разных утверждения: дубль строки
    # превратил бы журнал в счётчик просмотров.
    assert rows == [("card_read", str(moderator.uuid))], rows


async def test_successful_read_audit_is_visible_from_new_session(
    world: CommittedWorld,
) -> None:
    """AC-3, CR M-2: `200` тоже ФИКСИРУЕТСЯ, а не только пишется.

    Убивает мутацию «`commit()` → `pass`» на успешной ветви: строка
    остаётся в сессии запроса и откатывается вместе с ней.
    """
    moderator, moderator_token = await world.make_user(role=str(UserRole.MODERATOR))
    target, _ = await world.make_user(role=str(UserRole.USER))

    async with real_commit_client(world.factory) as client:
        response = await client.get(
            f"/api/v1/moderation/users/{target.uuid}", headers=cookie_headers(moderator_token)
        )
    assert response.status_code == 200, response.text

    rows = await world.audit_rows(subject_uuid=target.uuid)
    assert rows == [("card_read", str(moderator.uuid))], rows
