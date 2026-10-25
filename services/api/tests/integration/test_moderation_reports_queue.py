"""Очередь жалоб модератора (стори 8.3; AC-01, AC-04, AC-05).

**Проверяется то, что модератор увидит, а не то, что лежит в БД.** Поэтому
часть тестов идёт через HTTP (гард, фильтры, коды ответа), а часть — через
функцию сервиса (порог просрочки, число SQL).

**Почему порог и SQL-счётчик проверяются на сервисе, а не на HTTP.**
`now` — параметр сервиса (инъекция времени, `testing-rules.md`): ждать 20
часов нельзя, а `freezegun` запрещён. Счётчик SQL по той же причине живёт
рядом с функцией, которую считает (урок H-1 из CR 8.2: тест на число
запросов обязан гонять сервис, иначе он слеп к снятию логики).

**Почему `total` проверяется отдельно от `items`.** Усечённый счётчик не
ломает выдачу страницы и не краснит ни один тест состава — он занижает
планирование работы модератора (FR53).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.moderation_reports_service import list_reports
from app.services.reports_queue_cursor import encode_queue_cursor
from tests.integration.moderation_reports_helpers import (
    build_client,
    get_queue,
    make_message_report,
    make_moderator,
    make_no_show_report,
    make_plain_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _counter(session: AsyncSession) -> tuple[list[str], Any]:
    """Счётчик операторов прод-кода (приём `test_report_no_show_sql_parity`)."""
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *a: Any, **k: Any) -> None:
        statements.append(" ".join(statement.split()))

    bind = session.get_bind()
    listen_engine = getattr(bind, "sync_engine", bind)
    event.listen(listen_engine, "before_cursor_execute", _capture)
    return statements, lambda: event.remove(listen_engine, "before_cursor_execute", _capture)


def _app_sql(statements: list[str]) -> list[str]:
    """Без служебных операторов фикстуры (`SAVEPOINT`/`RELEASE`)."""
    noise = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")
    return [s for s in statements if not s.upper().startswith(noise)]


async def test_queue_shows_only_open_reports(session: AsyncSession) -> None:
    """AC-04, D-31: разобранные жалобы — архив, в очередь не попадают.

    Краснеет, если из `_QUEUE_SQL` убрать `r.status = 'open'`: тогда
    `resolved_warning`/`dismissed` вернутся в очередь, и модератор увидит
    работу, которую уже сделал.
    """
    moderator, token = await make_moderator(session)
    earlier = datetime.now(UTC) - timedelta(hours=3)
    later = datetime.now(UTC) - timedelta(hours=1)

    open_no_show = await make_no_show_report(session, reported_at=earlier)
    open_message = await make_message_report(session, reported_at=later)
    await make_no_show_report(session, reported_at=earlier, status="resolved_warning")
    await make_message_report(session, reported_at=later, status="dismissed")

    async with build_client(session) as client:
        response = await get_queue(client, token=token)

    assert response.status_code == 200, response.text
    body = response.json()
    uuids = [item["uuid"] for item in body["items"]]
    assert uuids == [str(open_no_show["report_uuid"]), str(open_message["report_uuid"])]
    assert body["total"] == 2
    assert all(item["status"] == "open" for item in body["items"])


async def test_queue_filter_by_target_type_and_garbage(session: AsyncSession) -> None:
    """AC-01, AC-04: фильтр сужает очередь; мусор отбивает схема.

    `Literal` в роуте (D-4.3-4) — единственный барьер: без него `target_type`
    ушёл бы в `WHERE` как есть и вернул пустую страницу вместо `422`.
    """
    _, token = await make_moderator(session)
    no_show = await make_no_show_report(session)
    message = await make_message_report(session)

    async with build_client(session) as client:
        only_no_show = await get_queue(client, token=token, target_type="no_show")
        only_message = await get_queue(client, token=token, target_type="message")
        garbage = await get_queue(client, token=token, target_type="какая-то хрень")

    assert only_no_show.status_code == 200, only_no_show.text
    assert [i["uuid"] for i in only_no_show.json()["items"]] == [str(no_show["report_uuid"])]
    assert only_message.status_code == 200, only_message.text
    assert [i["uuid"] for i in only_message.json()["items"]] == [str(message["report_uuid"])]
    assert garbage.status_code == 422, garbage.text


async def test_no_show_item_carries_both_sides_facts(session: AsyncSession) -> None:
    """AC-01, FR112: у жалобы на неявку видны факты ОБЕИХ сторон.

    Без этого «неявка» неотличима от «один сказал, второй молчал», и решение
    опиралось бы на пересказ одной стороны — то, ради чего написана стори.
    Краснеет, если `queue_attendance_facts` перестанет подмешиваться в элемент
    или вернёт `attendance=None` для `no_show`.
    """
    _, token = await make_moderator(session)
    seeded = await make_no_show_report(session, pair_status="disputed")

    async with build_client(session) as client:
        response = await get_queue(client, token=token, target_type="no_show")

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["target_type"] == "no_show"
    assert item["attendance"] == {
        "status": "disputed",
        "author_confirmed": True,
        "participant_confirmed": False,
        "card_uuid": seeded["card_uuid"],
    }


async def test_message_report_has_no_attendance_facts(session: AsyncSession) -> None:
    """AC-01: у жалобы на переписку фактов явки нет — и это `null`, не `{}`.

    Пустой объект заставил бы клиент ветвиться по «есть ли поля», а `null`
    прямо говорит «пары не существует».
    """
    _, token = await make_moderator(session)
    await make_message_report(session)

    async with build_client(session) as client:
        response = await get_queue(client, token=token, target_type="message")

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["attendance"] is None


async def test_overdue_boundary_is_exact(session: AsyncSession) -> None:
    """AC-05, NFR52, NFR63: граница ровно 20 часов — не просрочена, +1 с — просрочена.

    Порог читается ОДИН раз в сервисе и сравнивается со строгим `>`; тест
    подставляет `now`, а не ждёт. Краснеет при замене порога на литерал
    (мутация M6): при `REPORTS_OVERDUE_HOURS` из настроек граница сдвигается.
    """
    threshold_hours = int(settings.reports_overdue_hours)
    moment = datetime.now(UTC)

    exactly = await make_no_show_report(
        session, reported_at=moment - timedelta(hours=threshold_hours)
    )
    just_over = await make_no_show_report(
        session, reported_at=moment - timedelta(hours=threshold_hours, seconds=1)
    )
    fresh = await make_no_show_report(session, reported_at=moment - timedelta(hours=1))

    page = await list_reports(session, target_type="no_show", limit=20, cursor=None, now=moment)

    by_uuid = {str(item.uuid): item for item in page.items}
    assert by_uuid[str(exactly["report_uuid"])].overdue is False
    assert by_uuid[str(just_over["report_uuid"])].overdue is True
    assert by_uuid[str(fresh["report_uuid"])].overdue is False


async def test_keyset_pagination_survives_tie_group(session: AsyncSession) -> None:
    """AC-04, D-9, мутации M1/M2: tie-группа не теряет и не дублирует строки.

    **Восемь жалоб с ОДИНАКОВЫМ `reported_at`** (так бывает: `now()` одной
    транзакции совпадает) плюс одна позже. Постранично по одной.

    Почему восемь, а не две. Внутри tie-группы порядок задаёт `uuid ASC`, и
    `uuid` случаен. На двух строках вероятность случайного совпадения
    физического порядка с порядком по `uuid` — 1/2, и мутация M1 (порядок без
    второго ключа) проходит зелёной в половине прогонов. На восьми — 1/40320,
    то есть тест детерминирован настолько, насколько это возможно без
    управления `uuid` извне.

    Мутация M1 (порядок без `uuid`) ломает тест ПОТЕРЕЙ: страница 1 отдаёт
    строку с не крайним `uuid`, keyset отсекает остальных по `(same, uuid) >`,
    и они не появляются никогда. Мутация M2 (две `OR`-ветви) — тем же
    механизмом при ошибке приоритетов.
    """
    _, token = await make_moderator(session)
    same = datetime.now(UTC) - timedelta(hours=2)
    tie = [await make_no_show_report(session, reported_at=same) for _ in range(8)]
    later = await make_no_show_report(session, reported_at=same + timedelta(minutes=1))

    seen: list[str] = []
    cursor: str | None = None
    async with build_client(session) as client:
        for _ in range(9):
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = await get_queue(client, token=token, **params)
            assert response.status_code == 200, response.text
            body = response.json()
            if not body["items"]:
                break
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["uuid"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

    expected = sorted(str(item["report_uuid"]) for item in tie) + [str(later["report_uuid"])]
    # Порядок задаётся контрактом `reported_at ASC, uuid ASC`, поэтому
    # ожидание сортируется тем же ключом, а не порядком вставки.
    assert seen == expected, (
        f"tie-группа прочитана неверно: получено {len(seen)} из {len(expected)}\n"
        f"seen: {seen}\nexpected: {expected}"
    )


async def test_broken_cursor_is_invalid_cursor(session: AsyncSession) -> None:
    """AC-04: битый курсор — `422 invalid_cursor`, не `500` и не пустая страница."""
    _, token = await make_moderator(session)
    await make_no_show_report(session)

    async with build_client(session) as client:
        response = await get_queue(client, token=token, cursor="не-курсор")

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_cursor"


async def test_cursor_with_foreign_keys_is_a_cursor_error() -> None:
    """AC-04, D-9: курсор с ЛИШНИМ ключом — ошибка ввода, а не тихий разбор.

    Проверка набора ключей (`set(payload) == _CURSOR_KEYS`) ловит попытку
    протащить внутреннее поле (`report_id`) в публичный конверт. Тест
    собирает такой курсор сам: сервер его никогда не формирует.
    """
    import base64
    import json

    payload = json.dumps(
        {"reported_at": "2026-10-25T00:00:00Z", "uuid": str(uuid_lib.uuid4()), "report_id": 1}
    ).encode("utf-8")
    forged = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    from app.services.place_cursor import InvalidCursorError
    from app.services.reports_queue_cursor import decode_queue_cursor

    with pytest.raises(InvalidCursorError):
        decode_queue_cursor(forged)


async def test_non_moderator_is_rejected(session: AsyncSession) -> None:
    """AC-01: обычный пользователь — `403` до чтения данных.

    Единственное отличие пользователя от модератора — `role`, поэтому тест
    краснеет ровно при снятии `require_moderator`.
    """
    _, token = await make_plain_user(session)
    await make_no_show_report(session)

    async with build_client(session) as client:
        response = await get_queue(client, token=token)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


async def test_sql_count_does_not_depend_on_limit(session: AsyncSession) -> None:
    """AC-01, NFR39, мутация M7: число SQL одинаково при `limit=1` и `limit=10`.

    Очередь из десяти `no_show`-жалоб. Если факты явки читать по одной жалобе
    (N+1), содержимое страницы не изменится — а число запросов вырастет
    вместе с `limit`, и объём выданных данных начнёт утекать через тайминг.

    Тест гоняет ФУНКЦИЮ сервиса (урок H-1 из CR 8.2): счётчик на сыром SQL
    был бы слеп к снятию вызова `queue_attendance_facts`.
    """
    for _ in range(10):
        await make_no_show_report(session)

    one_sql, stop = _counter(session)
    try:
        page_one = await list_reports(
            session, target_type="no_show", limit=1, cursor=None, now=datetime.now(UTC)
        )
    finally:
        stop()

    ten_sql, stop2 = _counter(session)
    try:
        page_ten = await list_reports(
            session, target_type="no_show", limit=10, cursor=None, now=datetime.now(UTC)
        )
    finally:
        stop2()

    assert len(page_one.items) == 1
    assert len(page_ten.items) == 10
    assert len(_app_sql(one_sql)) == len(_app_sql(ten_sql)), (
        f"число SQL зависит от limit: limit=1 → {len(_app_sql(one_sql))}, "
        f"limit=10 → {len(_app_sql(ten_sql))}\n"
        f"limit=1: {_app_sql(one_sql)}\nlimit=10: {_app_sql(ten_sql)}"
    )


async def test_cursor_from_last_page_is_not_issued(session: AsyncSession) -> None:
    """AC-04: неполная страница — курсора нет.

    `cursor` выдаётся только когда страница заполнена целиком (`len(rows) ==
    limit`): иначе клиент сделал бы лишний запрос за пустой страницей.
    """
    _, token = await make_moderator(session)
    await make_no_show_report(session)

    async with build_client(session) as client:
        response = await get_queue(client, token=token, limit=10)

    assert response.status_code == 200, response.text
    assert response.json()["next_cursor"] is None


async def test_token_of_foreign_page_starts_after_position(session: AsyncSession) -> None:
    """AC-04: курсор — ПОЗИЦИЯ, а не «страница 2».

    Курсор, собранный вручную из середины очереди, отдаёт строки строго после
    указанной пары `(reported_at, uuid)`. Так проверяется, что предикат —
    лексикографическое сравнение кортежа, а не смещение.
    """
    _, token = await make_moderator(session)
    base = datetime.now(UTC) - timedelta(hours=5)
    first = await make_no_show_report(session, reported_at=base)
    second = await make_no_show_report(session, reported_at=base + timedelta(minutes=1))

    cursor = encode_queue_cursor(base, first["report_uuid"])

    async with build_client(session) as client:
        response = await get_queue(client, token=token, cursor=cursor)

    assert response.status_code == 200, response.text
    assert [i["uuid"] for i in response.json()["items"]] == [str(second["report_uuid"])]
