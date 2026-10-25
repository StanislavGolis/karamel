"""WS-событие `attendance.done` (стори 7.2; TD 7.2 W-*).

Покрытие TD: `W-DONE-01`, `W-DONE-02`, `W-ORDER-01` (перехват порядка),
`W-ORDER-02` (видимость из второго соединения), `W-DEDUP-04` (двойная
доставка кадра), `W-SUBSCRIBED-01` (регрессия 6.5), `W-NOCHAT-01`
(`chat_uuid IS NULL`), `W-IDEM-02`.

**Ровно один `TestClient` на тест.** `ws_hub` и `broker` — модульные
синглтоны, а задача брокера привязана к event loop того `TestClient`,
который её запустил: второй одновременный клиент запустил бы свой loop и
уронил отмену задачи (`RuntimeError: got Future attached to a different
loop`, доказано в 6.5 — 6 падений). Cookie подставляется per-connection.

**Почему оба способа проверки порядка.** Перехват (W-ORDER-01) доказывает
порядок ВЫЗОВОВ; видимость из второго соединения (W-ORDER-02) доказывает
порядок ЭФФЕКТОВ. Мутация «вызов публикации после `commit`, но внутри
транзакции» прошла бы первый тест — урок 6.5.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import TestClient, WebSocketTestSession

from app.main import app
from app.services import attendance_service
from app.services.ws_hub import ws_hub
from tests.integration.ws_harness import (
    ACCESS_COOKIE_NAME,
    WS_PATH,
    expect_silence,
    receive_envelope,
    subscribe,
)
from tests.integration.ws_seed import WsCompany, seed_company

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")

pytestmark = [
    pytest.mark.usefixtures("domain_cleanup"),
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="нужны реальные PostgreSQL и Redis: DATABASE_URL + REDIS_URL",
    ),
]


def _seed(*, with_chat: bool = True, with_attendance: bool = True) -> WsCompany:
    """Компания реальным коммитом + строка `attendance` для пары.

    `seed_company` (chore P-6) даёт автора, участника, постороннего, карточку
    и открытый чат. 7.2 добавляет к этому строку факта явки: в проде её
    создаёт воркер 7.3, здесь — тест, чтобы не зависеть от ненаписанной стори.
    """
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _go() -> WsCompany:
        async with factory() as session:
            company = await seed_company(session)
            # AC-07: подтверждение возможно только ПОСЛЕ `starts_at`, а
            # `seed_company` (chore P-6) создаёт карточку с `starts_at` +3 дня
            # — нормальное состояние для WS-тестов чата, но не для явки.
            # Сдвигаем встречу в прошлое, сохраняя инвариант схемы
            # `expires_at <= starts_at` (`ck_event_cards_expires_at`).
            await session.execute(
                text(
                    "UPDATE event_cards SET starts_at = now() - interval '1 hour', "
                    "expires_at = now() - interval '2 hours' WHERE id = :c"
                ),
                {"c": company.card_id},
            )
            if with_attendance:
                await session.execute(
                    text("INSERT INTO attendance (card_id, participant_id) VALUES (:c, :p)"),
                    {"c": company.card_id, "p": company.member.id},
                )
            if not with_chat:
                # D-7.2-10: компания не собрана — `chat_uuid IS NULL`, чата нет.
                await session.execute(
                    text("DELETE FROM chats WHERE event_card_id = :c"),
                    {"c": company.card_id},
                )
                await session.execute(
                    text("UPDATE event_cards SET chat_uuid = NULL WHERE id = :c"),
                    {"c": company.card_id},
                )
            await session.commit()
            return company

    async def _main() -> WsCompany:
        try:
            return await _go()
        finally:
            await engine.dispose()

    return asyncio.run(_main())


@pytest.fixture
def company() -> Iterator[WsCompany]:
    """Компания и чистый реестр соединений перед тестом."""
    ws_hub.clear()
    yield _seed()
    ws_hub.clear()


@pytest.fixture
def server() -> Iterator[TestClient]:
    """ЕДИНСТВЕННЫЙ `TestClient` на тест (см. модульный докстринг)."""
    ws_hub.clear()
    with TestClient(app) as client:
        yield client
    ws_hub.clear()


def _cookie_headers(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


@contextmanager
def ws_open(server: TestClient, token: str, *, chat_uuid: str) -> Iterator[WebSocketTestSession]:
    """Открывает WS с cookie участника и подписывает на чат компании."""
    with server.websocket_connect(WS_PATH, headers=_cookie_headers(token)) as ws:
        subscribe(ws, chat_uuid)
        receive_envelope(ws, expected_type="chat.subscribed")
        yield ws


def _post_attendance(server: TestClient, token: str, card_uuid: str, arrived: bool) -> Any:
    """Подтверждение через HTTP-эндпоинт 7.2 от имени стороны."""
    return server.post(
        f"/api/v1/cards/{card_uuid}/attendance",
        json={"arrived": arrived},
        cookies={ACCESS_COOKIE_NAME: token},
    )


# ——— W-DONE-01: событие на переход, с event_uuid (AC-09) ———————————————


def test_terminal_transition_publishes_single_event(company: WsCompany, server: TestClient) -> None:
    """W-DONE-01 (AC-09): обе стороны `true` → ОДИН кадр `attendance.done`.

    Проверяется содержимое: `card_uuid`, `status`, валидный `event_uuid`.
    Кадр приходит подписанному участнику через реальный Redis-канал
    `ws:chat:{uuid}`.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        first = _post_attendance(server, company.author_token, company.card_uuid, True)
        assert first.status_code == 200, first.text
        # ВНИМАНИЕ: здесь нельзя звать `expect_silence`.
        # `ws_harness._receive_with_timeout` оставляет висящий поток
        # `receive_json` после таймаута, и этот поток перехватывает СЛЕДУЮЩИЙ
        # кадр — `receive_envelope` ниже не получил бы ничего (проверено
        # прогоном: `TimeoutError` на корректном коде). Тишина после первого
        # подтверждения проверяется отдельным тестом, где она ПОСЛЕДНЯЯ.

        second = _post_attendance(server, company.member_token, company.card_uuid, True)
        assert second.status_code == 200, second.text
        # Второе — переход: ровно один кадр.
        event = receive_envelope(ws, expected_type="attendance.done")

    payload = event["payload"]
    assert payload["card_uuid"] == company.card_uuid
    assert payload["status"] == "confirmed"
    assert payload["chat_uuid"] == company.chat_uuid
    # D-2: `event_uuid` — у событий-переходов, валидный uuid4.
    assert str(uuid_lib.UUID(event["event_uuid"])) == event["event_uuid"]


def test_no_show_transition_publishes_event(company: WsCompany, server: TestClient) -> None:
    """W-DONE-01 (AC-03): обе стороны `false` → кадр со `status='no_show'`.

    P-11: статус проверяется в ОБОИХ значениях — тест только на `confirmed`
    прошёл бы и при константе `confirmed`, и `no_show` (единственный статус,
    влекущий санкцию) остался бы непроверенным.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        _post_attendance(server, company.author_token, company.card_uuid, False)
        # `expect_silence` здесь НЕ вызывается: см. пояснение в
        # `test_terminal_transition_publishes_single_event` (висящий поток
        # `receive_json` перехватил бы следующий кадр).
        _post_attendance(server, company.member_token, company.card_uuid, False)
        event = receive_envelope(ws, expected_type="attendance.done")

    assert event["payload"]["status"] == "no_show"


def test_first_confirmation_sends_no_event(company: WsCompany, server: TestClient) -> None:
    """W-DONE-01 / W-DONE-02 (AC-09): ПЕРВОЕ подтверждение события не порождает.

    `attendance.done` означает «факт зафиксирован», а не «кто-то нажал кнопку»:
    до второго подтверждения перехода нет. Проверка тишины стоит ПОСЛЕДНЕЙ в
    тесте намеренно — `expect_silence` оставляет висящий поток `receive_json`
    (см. `ws_harness`), и после него ждать кадр в этом соединении нельзя.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        response = _post_attendance(server, company.author_token, company.card_uuid, True)
        assert response.status_code == 200, response.text

        expect_silence(ws)


# ——— W-DONE-02 / W-IDEM-02: событие только на переход —————————————————


def test_repeat_post_sends_no_event(company: WsCompany, server: TestClient) -> None:
    """W-DONE-02 / W-IDEM-02 (AC-05, AC-09): повторный `POST` — НОЛЬ кадров.

    Терминальный статус уже достигнут, `WHERE status='pending'` не совпадает:
    второго события быть не может. Проверяется тишиной, а не числом кадров:
    «событие не пришло» — то же утверждение, но без ожидания на удачу.
    """
    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid) as ws:
        _post_attendance(server, company.author_token, company.card_uuid, True)
        _post_attendance(server, company.member_token, company.card_uuid, True)
        receive_envelope(ws, expected_type="attendance.done")

        repeat = _post_attendance(server, company.author_token, company.card_uuid, True)

        assert repeat.status_code == 200
        expect_silence(ws)


# ——— W-ORDER-01: publish строго после commit (ADR-11) —————————————————


def test_call_order_commit_then_publish(
    company: WsCompany, server: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-ORDER-01 / M7 (ADR-11, R5): последовательность — `commit`, затем `publish`.

    Сравнивается ПОЛНАЯ последовательность, а не вхождение (§18.11): проверка
    `"publish" in calls` прошла бы и при обратном порядке.

    Мутация M7 (вызвать `publish_to_chat` ДО `session.commit()`) делает тест
    красным.
    """
    calls: list[str] = []
    original_commit = AsyncSession.commit
    original_publish = attendance_service.publish_to_chat

    async def spy_commit(self: AsyncSession) -> None:
        result = await original_commit(self)
        calls.append("commit")
        return result

    async def spy_publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        calls.append("publish")
        await original_publish(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr(AsyncSession, "commit", spy_commit)
    monkeypatch.setattr(attendance_service, "publish_to_chat", spy_publish)

    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid):
        # Первое подтверждение — запись без перехода: `commit` без `publish`.
        _post_attendance(server, company.author_token, company.card_uuid, True)
        calls.clear()
        response = _post_attendance(server, company.member_token, company.card_uuid, True)

    assert response.status_code == 200, response.text
    assert calls == ["commit", "publish"], calls


# ——— W-ORDER-02: видимость строки в момент публикации ———————————————


def test_publish_sees_committed_row(
    company: WsCompany, server: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-ORDER-02 / M7 (ADR-11, R5): в момент `publish` строка УЖЕ видна снаружи.

    Второй способ доказать порядок: перехват видит ВЫЗОВЫ, а этот тест —
    ЭФФЕКТ. Мутация «публикация внутри транзакции, но вызов после
    `commit`-функции» прошла бы W-ORDER-01 и упала бы здесь.

    Чтение идёт ИЗ ДРУГОГО соединения (`second_engine`): соединение самой
    транзакции видело бы свои незакоммиченные строки по построению (§18.8).
    """
    observed: list[str] = []
    original_publish = attendance_service.publish_to_chat

    async def spy_publish(*, chat_uuid: uuid_lib.UUID, envelope: Any) -> None:
        engine = create_async_engine(DATABASE_URL, poolclass=None)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as probe:
                row = await probe.execute(
                    text("SELECT status FROM attendance WHERE card_id = :c"),
                    {"c": company.card_id},
                )
                observed.append(str(row.scalar_one()))
        finally:
            await engine.dispose()
        await original_publish(chat_uuid=chat_uuid, envelope=envelope)

    monkeypatch.setattr(attendance_service, "publish_to_chat", spy_publish)

    with ws_open(server, company.member_token, chat_uuid=company.chat_uuid):
        _post_attendance(server, company.author_token, company.card_uuid, True)
        _post_attendance(server, company.member_token, company.card_uuid, True)

    assert observed == ["confirmed"], (
        f"в момент publish из другого соединения видно {observed}: commit ещё не прошёл"
    )


# ——— W-NOCHAT-01: chat_uuid IS NULL (D-7.2-10) ———————————————————————


def test_no_chat_writes_fact_without_event(server: TestClient) -> None:
    """W-NOCHAT-01 (D-7.2-10, R13): компания не собрана → факт есть, кадров нет.

    Подтверждение имеет смысл и без чата (история 7.5): публикация —
    ускоритель, а не условие записи. Ошибки быть не должно.
    """
    ws_hub.clear()
    company = _seed(with_chat=False)

    first = _post_attendance(server, company.author_token, company.card_uuid, True)
    second = _post_attendance(server, company.member_token, company.card_uuid, True)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "confirmed"

    async def _read() -> str:
        engine = create_async_engine(DATABASE_URL, poolclass=None)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as probe:
                row = await probe.execute(
                    text("SELECT status FROM attendance WHERE card_id = :c"),
                    {"c": company.card_id},
                )
                return str(row.scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(_read()) == "confirmed"


# ——— W-SUBSCRIBED-01: регрессия 6.5 (D-2, M12) ———————————————————————


def test_chat_subscribed_twice_both_delivered(company: WsCompany, server: TestClient) -> None:
    """W-SUBSCRIBED-01 / M12 (D-2, R6): легитимный повтор `chat.subscribed` доходит.

    `chat.subscribed` описывает СОСТОЯНИЕ СОЕДИНЕНИЯ, а не переход: повторная
    подписка на тот же чат — нормальное поведение клиента после реконнекта.

    Мутация M12 (ставить `event_uuid` в `build()` ВСЕМ событиям) делает тест
    красным: брокер начал бы дедупить повторы, и второе подтверждение
    подписки потерялось бы — регрессия 6.5, вносимая 7.2.
    """
    with server.websocket_connect(WS_PATH, headers=_cookie_headers(company.member_token)) as ws:
        subscribe(ws, company.chat_uuid)
        first = receive_envelope(ws, expected_type="chat.subscribed")
        subscribe(ws, company.chat_uuid)
        second = receive_envelope(ws, expected_type="chat.subscribed")

    assert first["payload"]["chat_uuid"] == company.chat_uuid
    assert second["payload"]["chat_uuid"] == company.chat_uuid
    # D-2: у события состояния соединения `event_uuid` НЕТ вовсе.
    assert "event_uuid" not in first
    assert "event_uuid" not in second
