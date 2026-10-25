"""Chore X-7.4-4 (D-2): число SQL на негативных `404` НЕ различает причину.

**Ось NFR39 (анти-оракул O-5/O-6).** Тело, код и заголовки `404` совпадали и
до правки, но число обращений к БД — нет: «карточки нет» стоило 2 запроса,
«карточка есть, но я не участник» — 7. По разнице можно было узнать
существование карточки, не видя её.

**Что чинилось.** `_load_card` падал на `CardNotFoundError` СРАЗУ, минуя
запрос участия, — а на успешном пути `get_card` его выполняет. Приём
выравнивания тот же, что в `_resolve_chat` (D-6.4-6): на not-found пути
выполняется тот же запрос участия с заведомо несуществующим `card_id`.

**Что НЕ утверждается.** Абсолютное число запросов не фиксируется: оно
меняется при правках `get_card`, и жёсткое `== 3` сделало бы тест хрупким
без пользы. Утверждается РАВЕНСТВО двух негативных ветвей — именно оно
и есть ось оракула.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_helpers import build_client, make_company, make_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _counter(session: AsyncSession) -> tuple[list[str], Any]:
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *a: Any, **k: Any) -> None:
        statements.append(" ".join(statement.split()))

    bind = session.get_bind()
    listen_engine = getattr(bind, "sync_engine", bind)
    event.listen(listen_engine, "before_cursor_execute", _capture)
    return statements, lambda: event.remove(listen_engine, "before_cursor_execute", _capture)


def _cookie() -> str:
    from app.services.session_service import ACCESS_COOKIE_NAME

    return ACCESS_COOKIE_NAME


def _app_sql(statements: list[str]) -> list[str]:
    """Только запросы прод-кода: служебные операторы фикстуры отброшены.

    `SAVEPOINT`/`RELEASE` — артефакт внешней транзакции теста (`conftest`),
    в проде их нет. Считать их означало бы мерить фикстуру, а не сервис.
    """
    noise = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")
    return [s for s in statements if not s.upper().startswith(noise)]


async def test_attendance_404_branches_cost_same_sql(session: AsyncSession) -> None:
    """AC-12, NFR39: `GET /cards/{uuid}/attendance` — «нет карточки» == «не участник».

    До правки: **2 против 7**. После: **3 против 3**.

    Мутация, которая краснит: вернуть в `_load_card` немедленный `raise` вместо
    выровненного запроса участия — «нет карточки» станет дешевле на один.
    """
    # `card_status='finished'`: окно уже закрыто, ленивого закрытия нет.
    # Иначе число запросов плавает от данных (UPDATE + перечитывание), и
    # тест мерил бы состояние карточки, а не выравнивание ветвей.
    company = await make_company(session, pair_status="no_show", members=2, card_status="finished")
    _stranger, stranger_token = await make_user(session)

    async with build_client(session) as client:
        author_headers = {"cookie": f"{_cookie()}={company['author_token']}"}
        stranger_headers = {"cookie": f"{_cookie()}={stranger_token}"}

        missing_sql, stop = _counter(session)
        try:
            missing = await client.get(
                f"/api/v1/cards/{uuid_lib.uuid4()}/attendance", headers=author_headers
            )
        finally:
            stop()

        outsider_sql, stop2 = _counter(session)
        try:
            outsider = await client.get(
                f"/api/v1/cards/{company['card_uuid']}/attendance", headers=stranger_headers
            )
        finally:
            stop2()

    assert missing.status_code == 404
    assert outsider.status_code == 404
    # Один и тот же ответ по контракту (AC-08)…
    assert missing.json() == outsider.json()
    # …и по числу обращений к БД (NFR39).
    assert len(_app_sql(missing_sql)) == len(_app_sql(outsider_sql)), (
        f"ветви 404 различимы по числу SQL: нет карточки {len(_app_sql(missing_sql))}, "
        f"не участник {len(_app_sql(outsider_sql))}\n"
        f"нет: {_app_sql(missing_sql)}\nне участник: {_app_sql(outsider_sql)}"
    )


async def test_report_no_show_404_branches_cost_same_sql(session: AsyncSession) -> None:
    """AC-12, NFR39: `POST /cards/{uuid}/report-no-show` — «нет карточки» == «не участник».

    До правки `_load_card`: **2 против 7**. После: **3 против 3**.

    Последовательность вставки жалобы отличается от чтения явки: на
    успешном пути добавляются резолв `accused_uuid`, чтение пары и `INSERT`.
    Здесь обе ветви — негативные, поэтому отличие только в выравнивании
    запроса участия.
    """
    company = await make_company(session, pair_status="no_show", members=2, card_status="finished")
    _stranger, stranger_token = await make_user(session)
    accused = str(company["members"][0]["user"].uuid)

    async with build_client(session) as client:
        author_headers = {"cookie": f"{_cookie()}={company['author_token']}"}
        stranger_headers = {"cookie": f"{_cookie()}={stranger_token}"}

        missing_sql, stop = _counter(session)
        try:
            missing = await client.post(
                f"/api/v1/cards/{uuid_lib.uuid4()}/report-no-show",
                json={"accused_uuid": accused},
                headers=author_headers,
            )
        finally:
            stop()

        outsider_sql, stop2 = _counter(session)
        try:
            outsider = await client.post(
                f"/api/v1/cards/{company['card_uuid']}/report-no-show",
                json={"accused_uuid": accused},
                headers=stranger_headers,
            )
        finally:
            stop2()

    assert missing.status_code == 404
    assert outsider.status_code == 404
    assert missing.json() == outsider.json()
    assert len(_app_sql(missing_sql)) == len(_app_sql(outsider_sql)), (
        f"ветви 404 различимы по числу SQL: нет карточки {len(_app_sql(missing_sql))}, "
        f"не участник {len(_app_sql(outsider_sql))}\n"
        f"нет: {_app_sql(missing_sql)}\nне участник: {_app_sql(outsider_sql)}"
    )
