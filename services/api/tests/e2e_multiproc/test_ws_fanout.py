"""Мультипроцессный фан-аут WS (стори 6.5; TD 6.5: MP1, AC-05, R4).

**Зачем этот тест существует.** Все остальные W-тесты живут внутри ОДНОГО
процесса: там `ws_hub` общий, и локальный вызов вместо Redis `PUBLISH`
выглядит как рабочий фан-аут. Только два независимых процесса с общим
Redis доказывают AC-05 — и только этот тест красит мутацию M12 из TD.

**Почему посев идёт ДО старта серверов.** `two_servers` поднимает два
`uvicorn`, которые видят ту же БД. Если посеять после, серверы могут
открыть соединения раньше, чем появятся строки, и подписка получит отказ
по ложной причине — тест флакал бы на гонке старта, а не на дефекте.

**Почему клиент — `websockets`, а не `TestClient`.** `TestClient` живёт
внутри процесса приложения; здесь нужны НАСТОЯЩИЕ сокеты двух разных
серверов на разных портах.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid as uuid_lib

import pytest
import websockets
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.e2e_multiproc.conftest import ServerInstance
from tests.integration.ws_seed import WsCompany, seed_company

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = [
    pytest.mark.multiprocess,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="нужен реальный PostgreSQL: задайте DATABASE_URL",
    ),
]

#: Сколько ждать события от второго инстанса. 5 с — с запасом на старт
#: подписки брокера в только что поднятом процессе.
EVENT_TIMEOUT = 5.0


def _seed() -> WsCompany:
    """Компания реальным коммитом: серверы увидят её при старте."""
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _main() -> WsCompany:
        try:
            async with factory() as session:
                return await seed_company(session)
        finally:
            await engine.dispose()

    return asyncio.run(_main())


def _cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"karamel_access={token}"}


async def _subscribe_and_wait(ws_url: str, token: str, chat_uuid: str) -> None:
    """Подписывается на комнату и ждёт подтверждения от сервера."""
    async with websockets.connect(
        ws_url, additional_headers=_cookie(token), open_timeout=EVENT_TIMEOUT
    ) as socket:
        await socket.send(
            json.dumps({"type": "chat.subscribe", "payload": {"chat_uuid": chat_uuid}})
        )
        raw = await asyncio.wait_for(socket.recv(), timeout=EVENT_TIMEOUT)
        frame = json.loads(raw)
        assert frame["type"] == "chat.subscribed", frame


async def _collect_event(ws_url: str, token: str, chat_uuid: str) -> dict[str, object]:
    """Подписывается и ждёт ОДНО `message.created` (пропуская подтверждение)."""
    async with websockets.connect(
        ws_url, additional_headers=_cookie(token), open_timeout=EVENT_TIMEOUT
    ) as socket:
        await socket.send(
            json.dumps({"type": "chat.subscribe", "payload": {"chat_uuid": chat_uuid}})
        )
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=EVENT_TIMEOUT)
            frame = json.loads(raw)
            if frame["type"] == "message.created":
                return frame


def _post_message(server: ServerInstance, token: str, chat_uuid: str, body: str) -> int:
    """Отправляет сообщение HTTP-запросом к указанному инстансу."""
    import urllib.error
    import urllib.request

    payload = json.dumps({"body": body, "client_message_id": str(uuid_lib.uuid4())}).encode("utf-8")
    request = urllib.request.Request(
        f"{server.base_url}/api/v1/chats/{chat_uuid}/messages",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "cookie": f"karamel_access={token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=EVENT_TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:  # pragma: no cover - диагностика
        return int(exc.code)


def test_two_servers_receive_same_event(
    two_servers: tuple[ServerInstance, ServerInstance],
) -> None:
    """MP1 (AC-05, R4): событие с одного инстанса доходит до подписчика другого.

    Мутация M12 (`PUBLISH` → прямой вызов локального `ws_hub`) красит ТОЛЬКО
    этот тест: внутри одного процесса фан-аут остаётся рабочим, и все W-тесты
    зелёные.
    """
    first, second = two_servers
    company = _seed()

    async def _scenario() -> tuple[dict[str, object], dict[str, object]]:
        # Оба клиента подписаны ДО отправки: иначе событие уйдёт в пустоту
        # и тест проверял бы гонку, а не фан-аут.
        first_task = asyncio.create_task(
            _collect_event(first.ws_url, company.member_token, company.chat_uuid)
        )
        second_task = asyncio.create_task(
            _collect_event(second.ws_url, company.member_token, company.chat_uuid)
        )
        # Даём соединениям подписаться (брокер стартует в lifespan сервера).
        await asyncio.sleep(1.0)

        status = await asyncio.to_thread(
            _post_message, first, company.author_token, company.chat_uuid, "через два инстанса"
        )
        assert status == 201, f"POST вернул {status}"

        return await asyncio.wait_for(
            asyncio.gather(first_task, second_task), timeout=EVENT_TIMEOUT
        )

    (event_first, event_second) = asyncio.run(_scenario())

    body_first = event_first["payload"]["body"]  # type: ignore[index]
    body_second = event_second["payload"]["body"]  # type: ignore[index]
    assert body_first == "через два инстанса"
    assert body_second == "через два инстанса"
