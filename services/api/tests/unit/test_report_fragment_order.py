"""Билдер фрагмента переписки: порядок, лимит, переворот (стори 8.3; AC-02).

**Что здесь проверяется и почему этого не видно в интеграционном тесте.**
Интеграционный тест ловит `ASC LIMIT 10` по СОСТАВУ ответа, но не отличит
`ORDER BY created_at DESC, uuid DESC` от `ORDER BY created_at DESC`: при
совпавших метках времени (batch-импорт, два сообщения в одну микросекунду)
второй вариант даёт недетерминированный хвост. Второй ключ — контракт,
и проверяется он на скомпилированном SQL, а не на данных.

**Почему проверяется и переворот.** Билдер возвращает ХВОСТ диалога в
обратном порядке; «от старых к новым» получается переворотом в сервисе.
Если переворот пропадёт, модератор прочитает разговор задом наперёд —
дефект, который не краснит ни один тест состава.
"""

from __future__ import annotations

import asyncio

from app.repositories.messages import MessageRepository
from app.services.moderation_reports_service import FRAGMENT_LIMIT, _read_last_messages


def test_builder_orders_by_created_at_and_uuid_desc() -> None:
    """AC-02, D-33: `created_at DESC, uuid DESC` — оба ключа.

    Краснеет от мутации «убрать второй ключ»: при совпавших `created_at`
    порядок строк становится произвольным, и постранично/повторно
    фрагмент отдаёт разные сообщения.
    """
    statement = MessageRepository.build_fragment_statement(chat_id=1, limit=FRAGMENT_LIMIT)
    sql = " ".join(str(statement.compile(compile_kwargs={"literal_binds": False})).split())
    assert "ORDER BY messages.created_at DESC, messages.uuid DESC" in sql


def test_builder_limits_to_fragment_limit() -> None:
    """AC-02, FR40, NFR27: `LIMIT` в SQL — тот же `FRAGMENT_LIMIT`.

    Лимит живёт константой сервиса и передаётся в билдер параметром; тест
    фиксирует, что билдер его ПРИМЕНЯЕТ, а не только принимает.
    """
    statement = MessageRepository.build_fragment_statement(chat_id=1, limit=FRAGMENT_LIMIT)
    sql = " ".join(str(statement.compile(compile_kwargs={"literal_binds": True})).split())
    assert f"LIMIT {FRAGMENT_LIMIT}" in sql


def test_builder_filters_by_chat_id() -> None:
    """AC-02: фрагмент — ТОЛЬКО этот диалог.

    Без предиката модератор жалобы на один диалог получил бы хвост всех
    переписок проекта — утечка в масштабе базы (NFR27).
    """
    statement = MessageRepository.build_fragment_statement(chat_id=4242, limit=3)
    sql = " ".join(str(statement.compile(compile_kwargs={"literal_binds": True})).split())
    assert "messages.chat_id = 4242" in sql


def test_reversal_restores_old_to_new_order() -> None:
    """AC-02: `_read_last_messages` переворачивает хвост в «старые → новые».

    **CR 8.3, H-2: переписан.** Первая версия делала `rows = [3,2,1];
    rows.reverse()` — это тест стандартной библиотеки, а не сервиса
    (§18.2). Покраснеть от правки прод-кода он не мог.

    Теперь проверяется сама функция на поддельной сессии: билдер вернул
    `DESC` (последние сверху), функция обязана отдать их в обратном порядке.
    """
    import uuid as _uuid
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from typing import Any as _Any

    class _Row:
        def __init__(self, n: int) -> None:
            self.uuid = _uuid.uuid4()
            self.sender_id = 1
            self.sender_uuid = _uuid.uuid4()
            self.body = f"сообщение {n}".encode()
            self.created_at = _dt(2026, 10, 25, tzinfo=_UTC)
            self.n = n

    class _Result:
        def __init__(self, rows: list[_Row]) -> None:
            self._rows = rows

        def all(self) -> list[_Row]:
            return self._rows

    class _Session:
        async def execute(self, *_a: _Any, **_k: _Any) -> _Result:
            # Билдер отдаёт `DESC`: 3, 2, 1.
            return _Result([_Row(3), _Row(2), _Row(1)])

    rows = asyncio.run(_read_last_messages(_Session(), chat_id=1))

    assert [row[3].decode("utf-8") for row in rows] == ["сообщение 1", "сообщение 2", "сообщение 3"]


def test_sender_uuid_is_outer_join() -> None:
    """AC-02: системное сообщение (`sender_id IS NULL`) не выпадает.

    `INNER JOIN` выбросил бы системные сообщения («участник отменил
    участие»), и модератор не увидел бы причину конфликта.
    """
    statement = MessageRepository.build_fragment_statement(chat_id=1, limit=10)
    sql = " ".join(str(statement.compile(compile_kwargs={"literal_binds": True})).split())
    assert "LEFT OUTER JOIN users" in sql
