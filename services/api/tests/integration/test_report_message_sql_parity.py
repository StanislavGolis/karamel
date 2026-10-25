"""Task 5.2 стори 8.3 (D-36): число SQL на `404` жалобы на диалог не различает причину.

**Ось NFR39 (анти-оракул O-5/O-6).** Роут `POST /api/v1/reports` отвечает
одинаково (`404 card_not_found`) и на «чата нет», и на «чат есть, но я не его
участник». До правки D-36 первая ветвь стоила 2 запроса, вторая — 4: разница
читалась снаружи и сообщала существование диалога.

**Что чинилось.** На ветви «чата нет» сервис падал сразу после первого
чтения, минуя запрос участия, — а успешный путь его выполняет. Приём
выравнивания тот же, что в `_load_card` (chore X-7.4-4) и `_resolve_chat`:
на not-found пути выполняется тот же запрос участия с заведомо несуществующим
`card_id` (`_MISSING_CARD_ID`).

**Что НЕ утверждается.** Абсолютное число запросов не фиксируется: оно
меняется при правках `get_by_uuid_including_hidden`/`lock_for_update`, и
жёсткое `== 4` сделало бы тест хрупким без пользы. Утверждается РАВЕНСТВО
двух негативных ветвей — именно оно и есть ось оракула.

**Почему тела сравниваются.** Равенство числа запросов при разном теле
смысла не имеет: оракул переехал бы в конверт. Проверяются оба признака.

**Почему счётчик — `before_cursor_execute` на `sync_engine`.** Другого
способа увидеть реальные обращения к БД из сервиса нет; `SAVEPOINT`/`RELEASE`
фикстуры (`join_transaction_mode="create_savepoint"`) отбрасываются: в проде
их нет, и считать их значило бы мерить фикстуру, а не сервис.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_message_helpers import (
    make_client,
    make_message_chat,
    make_user,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _counter(session: AsyncSession) -> tuple[list[str], Any]:
    """Счётчик операторов прод-кода на сессии теста."""
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *a: Any, **k: Any) -> None:
        statements.append(" ".join(statement.split()))

    bind = session.get_bind()
    listen_engine = getattr(bind, "sync_engine", bind)
    event.listen(listen_engine, "before_cursor_execute", _capture)
    return statements, lambda: event.remove(listen_engine, "before_cursor_execute", _capture)


def _app_sql(statements: list[str]) -> list[str]:
    """Только запросы прод-кода: служебные операторы фикстуры отброшены.

    `SAVEPOINT`/`RELEASE` — артефакт внешней транзакции теста (`conftest`),
    в проде их нет. Считать их означало бы мерить фикстуру, а не сервис.
    """
    noise = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")
    return [s for s in statements if not s.upper().startswith(noise)]


async def test_message_report_404_branches_cost_same_sql(session: AsyncSession) -> None:
    """D-36, NFR39: «чата нет» == «не участник» по числу SQL и по телу.

    Мутация, которая краснит: вернуть в `report_message` немедленный `raise`
    вместо выровненного запроса участия — «чата нет» станет дешевле на один.

    Второй краснящий сценарий: переставить `lock_for_update` ПЕРЕД первым
    чтением — тогда ветвь «не участник» подешевеет, а «чата нет» останется
    на месте, потому что блокировать нечего.
    """
    company = await make_message_chat(session)
    _stranger, stranger_token = await make_user(session)

    async with make_client(session) as client:
        missing_sql, stop = _counter(session)
        try:
            missing = await post_report(
                client,
                token=company.author_token,
                chat_uuid=str(uuid_lib.uuid4()),
                accused_uuid=company.member.uuid,
            )
        finally:
            stop()

        outsider_sql, stop2 = _counter(session)
        try:
            outsider = await post_report(
                client,
                token=stranger_token,
                chat_uuid=company.chat_uuid,
                accused_uuid=company.member.uuid,
            )
        finally:
            stop2()

    assert missing.status_code == 404, missing.text
    assert outsider.status_code == 404, outsider.text
    # Один и тот же ответ по контракту (ADR-13)…
    assert missing.json() == outsider.json()
    # …и по числу обращений к БД (NFR39, D-36).
    assert len(_app_sql(missing_sql)) == len(_app_sql(outsider_sql)), (
        f"ветви 404 различимы по числу SQL: нет чата {len(_app_sql(missing_sql))}, "
        f"не участник {len(_app_sql(outsider_sql))}\n"
        f"нет: {_app_sql(missing_sql)}\nне участник: {_app_sql(outsider_sql)}"
    )
