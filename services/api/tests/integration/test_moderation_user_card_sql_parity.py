"""Равенство числа SQL на двух ветвях карточки (стори 8.5; AC-11, D-65).

**Что доказывается.** «Пользователя нет» (`404`) и «пользователь есть,
данных пусто» (`200`) обязаны стоить ОДИНАКОВО обращений к БД. Если
негативная ветвь выходит после резолва адреса, разница в числе запросов
сообщает постороннему, существует ли пользователь — ось NFR39, тот же класс,
что `_MISSING_CARD_ID` в 7.4 (D-36) и `_MISSING_CHAT_ID` в 8.2 (D-38).

**Почему равенство, а не абсолютное число.** Абсолютное значение хрупко:
добавление блока (например, истории заявок) сломает тест, не сломав защиты.
Равенство ломается ровно тогда, когда ломается свойство.

**Почему не `403` vs `404`.** Просили выравнивать «нет прав / нет
пользователя», но эта пара закрыта иначе: `ModeratorUser` отдаёт `403` ДО
любого SQL (7.5, AC-09 O-3), и не-модератор не узнаёт о существовании
пользователя ни при каком числе запросов. Выравнивать его значило бы читать
БД ради того, чтобы отказать (D-65).
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_user_card_helpers import (
    build_client,
    get_card,
    make_moderator,
    make_target_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _count_sql(session: AsyncSession, *, token: str, user_uuid: object) -> int:
    """Число SQL за один вызов карточки.

    Счётчик ставится на sync-движок: `before_cursor_execute` — синхронное
    событие, и оно видит КАЖДЫЙ запрос, включая те, что уходят через
    `AsyncSession.execute`.
    """
    statements: list[str] = []

    def _count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    bind = session.get_bind()
    sync_engine = getattr(bind, "sync_engine", bind)
    async with build_client(session) as client:
        event.listen(sync_engine, "before_cursor_execute", _count)
        try:
            await get_card(client, token=token, user_uuid=user_uuid)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)
    return len(statements)


async def test_missing_user_and_empty_user_cost_the_same(session: AsyncSession) -> None:
    """AC-11, D-65: «нет пользователя» и «есть без данных» — равное число SQL.

    Краснеет, если негативная ветвь выйдет после пустого резолва: тогда
    разница 1 против 9 и есть оракул. Sentinel `_MISSING_USER_ID = 0`
    заставляет её выполнить те же запросы.
    """
    existing, _ = await make_target_user(session)
    _, token = await make_moderator(session)

    missing_cost = await _count_sql(session, token=token, user_uuid=uuid_lib.uuid4())
    empty_cost = await _count_sql(session, token=token, user_uuid=existing.uuid)

    assert missing_cost == empty_cost, (
        f"число SQL различается: нет пользователя={missing_cost}, "
        f"есть без данных={empty_cost} — существование читается по числу запросов"
    )
