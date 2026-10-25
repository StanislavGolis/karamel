"""Блокировка строки чата (стори 8.2; AC-07, D-25).

**Почему unit, а не только гонка.** `FOR SHARE` тоже блокирует запись, и
интеграционный тест на два соединения НЕ отличает его от `FOR UPDATE`
(проверено мутацией M6: `3 passed`). Отличие видно лишь в тексте запроса,
поэтому режим фиксируется здесь, а сериализация — в интеграционном тесте.

Без блокировки возможен рассинхрон: `_resolve_chat` увидел `active`, а
`INSERT` прошёл уже после `UPDATE ... status='hidden'`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.chats import ChatRepository


@pytest.mark.asyncio
async def test_lock_for_update_uses_for_update() -> None:
    """AC-07: запрос содержит `FOR UPDATE`, а не `FOR SHARE` и не пустоту.

    Мутация M6 (убрать или заменить режим) делает тест красным.
    """
    session = AsyncMock()
    session.execute.return_value = MagicMock()
    repo = ChatRepository(session)

    await repo.lock_for_update(chat_id=42)

    assert session.execute.await_count == 1
    statement, params = session.execute.await_args.args
    sql = str(statement)
    assert "FOR UPDATE" in sql.upper(), f"нет FOR UPDATE: {sql}"
    assert "FOR SHARE" not in sql.upper()
    assert "FOR UPDATE" not in sql.upper().replace("FOR UPDATE", "", 1)
    assert params == {"id": 42}


@pytest.mark.asyncio
async def test_lock_for_update_returns_status_under_lock() -> None:
    """CR 8.2 H-1: блокировка отдаёт статус, прочитанный ПОД `FOR UPDATE`.

    Без возврата статуса `send_message` не может проверить его после
    `commit` жалобы: `_resolve_chat` прочитал `active` ДО блокировки, и
    сообщение уходило в скрытый диалог (воспроизведено на двух соединениях).

    Мутация: убрать `status` из `SELECT` (заменить на `id`) — тест красный.
    """
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = ("hidden",)
    session.execute.return_value = result

    status = await ChatRepository(session).lock_for_update(chat_id=42)

    assert status == "hidden"
    statement, _params = session.execute.await_args.args
    assert "status" in str(statement).lower(), str(statement)


@pytest.mark.asyncio
async def test_lock_for_update_returns_none_when_row_missing() -> None:
    """CR 8.2 H-1: строку удалили между чтением и блокировкой → `None`.

    Вызывающий обязан трактовать это как отказ: `send_message` бросает тот
    же `404`, `report_message` — `ReportSubjectNotFoundError`.
    """
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = None
    session.execute.return_value = result

    assert await ChatRepository(session).lock_for_update(chat_id=42) is None
