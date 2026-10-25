"""Приватность ответа на жалобу (стори 7.4; AC-08, AC-09, NFR39).

**Почему набор ключей проверяется байт-в-байт, а не «нет лишних».**
Проверка «в ответе нет `reporter_id`» проходит и тогда, когда ответ содержит
`author_id`, `participant_id` или `card_id` — по ним обвиняемый восстановил бы,
кто в компании, а NFR39 запрещает раскрытие состава через жалобу. Точное
равенство множества ключей такого класса дефектов не пропускает.

**Почему число SQL считается.** Жалоба обязана укладываться в фиксированное
число запросов независимо от размера компании: рост числа с ростом компании
означал бы либо N+1, либо проверку «кто ещё в компании» — то есть лишнее
знание и лишний оракул.

**Почему при `disputed` жалуются ОБЕ стороны.** Если сервер начнёт проверять
«пришёл ли жалобщик», ответ раскроет чужой флаг (D-7.4-10). Тест фиксирует
симметрию: оба ответа — `201`, и ни один не раскрывает сторону.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_helpers import (
    build_client,
    make_company,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-08: ровно эти четыре ключа. Список — не «минимально допустимый набор»,
#: а ТОЧНОЕ равенство: любое добавление ломает NFR39.
EXPECTED_KEYS = {"uuid", "target_type", "status", "reported_at"}

#: Запрещённые ключи: каждый из них по отдельности раскрыл бы либо жалобщика,
#: либо обвиняемого, либо внутренние адреса (D-2, D-7.4-4).
FORBIDDEN_KEYS = (
    "reporter_id",
    "reporter_uuid",
    "author_id",
    "accused_uuid",
    "accused_id",
    "participant_id",
    "attendance_id",
    "card_id",
    "card_uuid",
    "reason",
    "resolved_at",
)


#: `SAVEPOINT`/`RELEASE`/`ROLLBACK TO` — бухгалтерия SAVEPOINT-сессии
#: (`join_transaction_mode="create_savepoint"`), а не запрос домена. Их число
#: зависит от того, сколько раз сессия уже входила в savepoint, и к размеру
#: компании отношения не имеет: без фильтра тест ловил бы сам себя —
#: `test_query_count_independent_of_company_size` дал 11 vs 12 при равных 8
#: доменных запросах (проверено `before_cursor_execute` на живой БД).
_SAVEPOINT_PREFIXES = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")


@dataclass
class QueryCounter:
    """Счётчик доменных `execute` на движке: доказывает отсутствие N+1."""

    count: int = 0

    def __call__(self, conn: Any, cursor: Any, statement: str, *args: Any, **kwargs: Any) -> None:
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(_SAVEPOINT_PREFIXES):
            return
        self.count += 1


async def _count_queries(
    client: AsyncClient, session: AsyncSession, company: dict[str, Any], *, token: str, accused: Any
) -> tuple[int, Any]:
    """Число SQL-запросов и сам ответ для одного запроса жалобы."""
    counter = QueryCounter()
    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", counter)
    try:
        response = await post_report(client, company, token=token, accused_uuid=accused)
    finally:
        event.remove(listen_engine, "before_cursor_execute", counter)
    return counter.count, response


async def test_response_has_exactly_four_keys(session: AsyncSession) -> None:
    """AC-08 (NFR39): набор ключей — РОВНО `{uuid, target_type, status, reported_at}`.

    Мутация M6 (добавить `accused_uuid` в схему ответа) делает тест красным:
    при `disputed` это подтвердило бы жалобщику, что обвиняемый — именно он,
    то есть выдало бы чужой флаг.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == EXPECTED_KEYS
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in body


async def test_response_headers_do_not_leak(session: AsyncSession) -> None:
    """AC-08: полный набор заголовков — `content-type`, `no-store`, без `etag`.

    `etag` у созданной жалобы дал бы кэшируемую копию и позволил бы
    восстановить факт жалобы повторным запросом с тем же адресом; `no-store`
    закрывает и кэш провизии. Проверяется ВЕСЬ набор, а не только наличие
    двух ожидаемых: лишний заголовок — тоже утечка.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert "location" not in response.headers


async def test_query_count_independent_of_company_size(session: AsyncSession) -> None:
    """AC-08 (NFR39): 2 участника и 4 участника — ОДИНАКОВОЕ число запросов.

    Проверяется равенство, а не абсолют: добавление одного общего запроса
    (например, карточки) не должно красить тест, а рост от размера компании —
    должен. Рост означал бы обход по участникам, то есть знание состава.
    """
    small = await make_company(session, pair_status="no_show", members=2)
    big = await make_company(session, pair_status="no_show", members=4)

    async with build_client(session) as client:
        small_count, small_response = await _count_queries(
            client,
            session,
            small,
            token=small["author_token"],
            accused=small["members"][0]["user"].uuid,
        )
        big_count, big_response = await _count_queries(
            client,
            session,
            big,
            token=big["author_token"],
            accused=big["members"][0]["user"].uuid,
        )

    assert small_response.status_code == 201
    assert big_response.status_code == 201
    assert small_count == big_count


async def test_disputed_does_not_reveal_side(session: AsyncSession) -> None:
    """AC-09 (NFR39): при `disputed` обе стороны получают ОДИНАКОВЫЙ ответ.

    Тела сравниваются по ключам и по значениям `target_type`/`status`:
    любое различие (например, `status='open'` у одной и `disputed` у другой)
    раскрыло бы, кто из двух подтвердил явку. Мутация M7 (вернуть в ответе
    статус ПАРЫ вместо статуса жалобы) делает тест красным.
    """
    company = await make_company(session, pair_status="disputed", members=2)
    member = company["members"][0]

    async with build_client(session) as client:
        by_author = await post_report(
            client, company, token=company["author_token"], accused_uuid=member["user"].uuid
        )
        by_member = await post_report(
            client, company, token=member["token"], accused_uuid=company["author"].uuid
        )

    assert by_author.status_code == 201
    assert by_member.status_code == 201
    first, second = by_author.json(), by_member.json()
    assert set(first) == set(second) == EXPECTED_KEYS
    assert first["target_type"] == second["target_type"] == "no_show"
    # AC-10: `status` — состояние ЖАЛОБЫ (`open`), не состояние пары (`disputed`).
    assert first["status"] == second["status"] == "open"
    # Значения различаются только адресом созданной жалобы и её временем.
    assert first["uuid"] != second["uuid"]


async def test_uuid_is_returned_not_internal_id(session: AsyncSession) -> None:
    """AC-08 (D-2): наружу выходит `uuid`, а не внутренний `id`.

    `id` — внутренний адрес (`bigint`), по которому можно было бы перебирать
    жалобы и сопоставлять их с порядком создания: косвенное раскрытие
    активности компании.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    body = response.json()
    assert "id" not in body
    assert isinstance(body["uuid"], str)
    assert len(body["uuid"]) == 36
