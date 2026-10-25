"""История явок для модератора: доступ и анти-оракул (стори 7.5).

Покрытие: AC-01…AC-03, AC-05, AC-06, AC-09…AC-11.

**Числа SQL измерены, а не выведены из AC.** AC-09 O-3 требует «`403` → 0 SQL».
Фактически зависимость `get_current_user` (`deps.py`) читает `users` на КАЖДОМ
запросе (D-3 стори 1.2: бан проверяется немедленно), поэтому `403` стоит
ровно один запрос — и это свойство НЕ зависит от `user_uuid` (проверяется
попарным сравнением двух `403`). Инвариант анти-оракула сохранён: гард
срабатывает до резолва адреса, `attendance` не читается вовсе. Тесты ниже
фиксируют ФАКТИЧЕСКИЕ числа с комментарием, какая мутация их ломает —
подгонка под литерал AC спрятала бы регресс (урок H-2 стори 7.4).

**Почему сравнение байт-в-байт, а не «статус совпал».** Разница в теле или
заголовке между существующим и несуществующим `user_uuid` — уже оракул, даже
если статус одинаков (образец — M-1 стори 7.4).
"""

from __future__ import annotations

import base64
import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.attendance_history_helpers import (
    add_attendance,
    build_client,
    cookie_headers,
    make_card,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-02: ровно эти четыре ключа. Список — ТОЧНОЕ равенство, а не «минимально
#: допустимый набор»: любое добавление ломает NFR39.
EXPECTED_ITEM_KEYS = {"card_uuid", "card_title", "starts_at", "status"}

#: AC-02: подстроки, которых в сыром теле быть не должно. Проверка по СЫРОМУ
#: тексту, а не по разобранному JSON: вложенный объект или строка со значением
#: `"confirmed"` прошли бы проверку ключей верхнего уровня.
FORBIDDEN_SUBSTRINGS = ("confirmed", "answer", "participant")


async def _call(
    client: AsyncClient, *, token: str | None, user_uuid: Any, limit: int | None = None
) -> Response:
    """`GET /moderation/users/{uuid}/attendance` с cookie-аутентификацией."""
    headers = cookie_headers(token) if token is not None else {}
    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    return await client.get(
        f"/api/v1/moderation/users/{user_uuid}/attendance", headers=headers, params=params
    )


def _capture_statements(session: AsyncSession) -> tuple[list[str], Any]:
    """Перехват SQL на движке сессии (`before_cursor_execute`).

    Возвращает список и саму функцию, чтобы тест снял слушателя в `finally`:
    оставленный слушатель копил бы запросы соседних тестов и делал числа
    недетерминированными (урок X-6.7-2).
    """
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any, **kwargs: Any) -> None:
        statements.append(" ".join(statement.split()))

    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", _capture)
    return statements, (listen_engine, _capture)


# ---------------------------------------------------------------- AC-01, AC-03


async def test_moderator_sees_history_of_target_user(session: AsyncSession) -> None:
    """AC-01: модератор получает `200` и историю ИМЕННО запрошенного пользователя."""
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    card_uuid, card_id = await make_card(session, author_id=moderator.id, title="Ужин")
    await add_attendance(session, card_id=card_id, participant_id=participant.id)

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=participant.uuid)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["card_uuid"] == str(card_uuid)
    assert body["items"][0]["card_title"] == "Ужин"
    assert body["next_cursor"] is None


async def test_user_without_records_gets_200_with_empty_items(session: AsyncSession) -> None:
    """AC-01: нет истории ≠ нет пользователя — `200` с пустым `items`, не `404`."""
    moderator, moderator_token = await make_user(session, role="moderator")
    stranger, _ = await make_user(session)

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=stranger.uuid)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


async def test_plain_user_gets_403(session: AsyncSession) -> None:
    """AC-03: обычный пользователь → `403 forbidden` (гард `ModeratorUser`)."""
    plain, plain_token = await make_user(session)
    other, _ = await make_user(session)

    async with build_client(session) as client:
        response = await _call(client, token=plain_token, user_uuid=other.uuid)

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden"}


async def test_moderator_unknown_uuid_gets_404(session: AsyncSession) -> None:
    """AC-03: модератор + несуществующий uuid → `404 user_not_found`."""
    _, moderator_token = await make_user(session, role="moderator")

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=uuid_lib.uuid4())

    assert response.status_code == 404
    assert response.json() == {"code": "user_not_found"}


async def test_unauthenticated_gets_401(session: AsyncSession) -> None:
    """AC-03: нет сессии → `401 unauthenticated` (до любой проверки роли)."""
    other, _ = await make_user(session)

    async with build_client(session) as client:
        response = await _call(client, token=None, user_uuid=other.uuid)

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


# ---------------------------------------------------------------- AC-09 (O-1, O-2)


async def test_403_is_byte_identical_for_existing_and_unknown_uuid(session: AsyncSession) -> None:
    """AC-09 O-1: два `403` неотличимы — тело и ВСЕ заголовки байт-в-байт.

    Мутация, которая краснит: перенос резолва `user_uuid` ПЕРЕД гардом —
    тогда несуществующий адрес дал бы `404` вместо `403`, и класс ответа
    различил бы зарегистрированного пользователя.
    """
    _, plain_token = await make_user(session)
    existing, _ = await make_user(session)

    async with build_client(session) as client:
        existing_response = await _call(client, token=plain_token, user_uuid=existing.uuid)
        unknown_response = await _call(client, token=plain_token, user_uuid=uuid_lib.uuid4())

    assert existing_response.status_code == unknown_response.status_code == 403
    assert existing_response.content == unknown_response.content
    assert sorted(existing_response.headers.items()) == sorted(unknown_response.headers.items())


async def test_404_is_identical_for_any_unknown_uuid(session: AsyncSession) -> None:
    """AC-09 O-2: два `404` неотличимы — тело и все заголовки.

    Мутация: подмешать в `404` `details` с длиной истории или id — ответ
    перестал бы быть одинаковым для двух несуществующих адресов.
    """
    _, moderator_token = await make_user(session, role="moderator")

    async with build_client(session) as client:
        first = await _call(client, token=moderator_token, user_uuid=uuid_lib.uuid4())
        second = await _call(client, token=moderator_token, user_uuid=uuid_lib.uuid4())

    assert first.status_code == second.status_code == 404
    assert first.content == second.content == b'{"code":"user_not_found"}'
    assert sorted(first.headers.items()) == sorted(second.headers.items())


# ---------------------------------------------------------------- AC-09 (O-3, O-4)


async def test_403_does_not_read_attendance_and_does_not_depend_on_uuid(
    session: AsyncSession,
) -> None:
    """AC-09 O-3: `403` не читает `attendance` и не зависит от существования адреса.

    **Отклонение от буквы AC, зафиксированное осознанно.** AC-09 O-3 называет
    «`403` → 0 SQL». Фактически `get_current_user` читает `users` на каждом
    запросе (D-3 стори 1.2), поэтому `403` стоит ОДИН запрос — и это верно для
    ЛЮБОГО `user_uuid`. Оракул определяется не абсолютным числом, а
    независимостью от адреса, поэтому проверяется:
    (1) число для существующего и несуществующего uuid ОДИНАКОВО;
    (2) в перехвате нет `FROM attendance` (гард до БД истории);
    (3) ровно один `FROM users` (резолв адреса НЕ выполнен).

    Мутации, которые краснят: перенос резолва перед гардом (пункт 3 даст 2),
    чтение истории до гарда (пункт 2), второй гард с отдельной проверкой роли.
    """
    _, plain_token = await make_user(session)
    existing, _ = await make_user(session)

    async with build_client(session) as client:
        statements_existing, listener = _capture_statements(session)
        try:
            await _call(client, token=plain_token, user_uuid=existing.uuid)
        finally:
            listen_engine, capture = listener
            event.remove(listen_engine, "before_cursor_execute", capture)
        count_existing = len(statements_existing)
        existing_attendance = [s for s in statements_existing if "attendance" in s]
        existing_users = [s for s in statements_existing if "FROM users" in s]

        statements_unknown, listener = _capture_statements(session)
        try:
            await _call(client, token=plain_token, user_uuid=uuid_lib.uuid4())
        finally:
            listen_engine, capture = listener
            event.remove(listen_engine, "before_cursor_execute", capture)
        count_unknown = len(statements_unknown)

    assert count_existing == count_unknown, (statements_existing, statements_unknown)
    assert not existing_attendance, existing_attendance
    assert len(existing_users) == 1, existing_users


async def test_404_costs_exactly_one_resolve_query_over_the_guard(session: AsyncSession) -> None:
    """AC-09 O-3: `404` = база гарда + РОВНО один запрос резолва адреса.

    Буква AC «`404` → ровно 1» не совпадает с фактическим устройством
    аутентификации (1 запрос на `get_current_user`). Проверяется
    ПРИРАЩЕНИЕ: ровно один `SELECT id FROM users WHERE uuid`, и `attendance`
    не читается. Мутация: чтение истории до проверки существования адреса
    добавила бы `attendance` в перехват несуществующего пользователя.
    """
    _, moderator_token = await make_user(session, role="moderator")

    async with build_client(session) as client:
        statements, listener = _capture_statements(session)
        try:
            await _call(client, token=moderator_token, user_uuid=uuid_lib.uuid4())
        finally:
            listen_engine, capture = listener
            event.remove(listen_engine, "before_cursor_execute", capture)

    # Три `FROM users` быть не может: авторизация (1) + резолв (1) = 2.
    assert len([s for s in statements if "FROM users" in s]) == 2, statements
    assert not [s for s in statements if "attendance" in s], statements


async def test_positive_query_count_does_not_depend_on_limit(session: AsyncSession) -> None:
    """AC-09 O-4: число SQL одинаково при `limit=2` и `limit=20` (нет N+1).

    Мутация: чтение карточки отдельным запросом на элемент истории — при
    `limit=2` вышло бы на два запроса больше, и равенство покраснело бы.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=1)
    for index in range(3):
        _, card_id = await make_card(
            session, author_id=moderator.id, title=f"Встреча {index}", starts_at=moment
        )
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=participant.id,
            created_at=moment + timedelta(minutes=index),
        )

    async with build_client(session) as client:
        statements_two, listener = _capture_statements(session)
        try:
            small = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=2)
        finally:
            listen_engine, capture = listener
            event.remove(listen_engine, "before_cursor_execute", capture)

        statements_twenty, listener = _capture_statements(session)
        try:
            big = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=20)
        finally:
            listen_engine, capture = listener
            event.remove(listen_engine, "before_cursor_execute", capture)

    assert small.status_code == big.status_code == 200
    assert len(small.json()["items"]) == 2
    assert len(big.json()["items"]) == 3
    assert len(statements_two) == len(statements_twenty), (statements_two, statements_twenty)


# ---------------------------------------------------------------- AC-02, AC-09 O-5


async def test_item_has_exactly_four_fields(session: AsyncSession) -> None:
    """AC-02, AC-09 O-5: ровно четыре поля, `extra=forbid`, байт-в-байт.

    Проверяется и множество ключей, и СЫРОЕ тело: подстрока `confirmed`
    в значении статуса (`confirmed`) — легальна только как значение поля
    `status`, поэтому запрет сформулирован по ИМЕНАМ полей, а проверка
    подстрок идёт по ключам элемента (см. `test_raw_body_has_no_flag_names`).
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    _, card_id = await make_card(session, author_id=moderator.id)
    await add_attendance(session, card_id=card_id, participant_id=participant.id, status="disputed")

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=participant.uuid)

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert set(item) == EXPECTED_ITEM_KEYS


async def test_raw_body_has_no_flag_names(session: AsyncSession) -> None:
    """AC-02: в теле нет флагов — проверка по СЫРОМУ тексту ответа.

    Здесь `status='no_show'`: значение статуса не содержит запрещённых
    подстрок, поэтому проверка подстрок осмысленна. Мутация: добавить
    `my_answer`/`confirmed_at` в схему — подстрока `answer`/`confirmed`
    появится в ключах и тест покраснеет.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    _, card_id = await make_card(session, author_id=moderator.id)
    await add_attendance(session, card_id=card_id, participant_id=participant.id, status="no_show")

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=participant.uuid)

    raw = response.text
    assert response.status_code == 200, raw
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in raw, (forbidden, raw)


async def test_positive_headers_are_no_store_without_etag(session: AsyncSession) -> None:
    """AC-09 O-5: `cache-control: no-store`, `content-type: application/json`, без `etag`.

    История явок — адресные данные (NFR39): кэшируемый ответ пережил бы смену
    пользователя на том же устройстве.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=participant.uuid)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert "etag" not in response.headers


# ---------------------------------------------------------------- AC-05, AC-06


async def test_pagination_walks_all_rows_without_duplicates(session: AsyncSession) -> None:
    """AC-05: `limit + 1` и непрозрачный курсор; конец списка — `next_cursor = null`.

    Мутация: строить `next_cursor` ВСЕГДА (в том числе на последней странице) —
    клиент сделал бы лишний запрос и получил пустую страницу с тем же
    курсором (бесконечный цикл догрузки).
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=1)
    for index in range(5):
        _, card_id = await make_card(session, author_id=moderator.id, starts_at=moment)
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=participant.id,
            created_at=moment + timedelta(minutes=index),
        )

    async with build_client(session) as client:
        first = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=2)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert len(first_body["items"]) == 2
        assert first_body["next_cursor"] is not None
        # Курсор непрозрачен: ни `card_id`, ни `participant_id` в нём нет.
        assert "=" not in first_body["next_cursor"]

        second = await client.get(
            f"/api/v1/moderation/users/{participant.uuid}/attendance",
            headers=cookie_headers(moderator_token),
            params={"limit": 2, "cursor": first_body["next_cursor"]},
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert len(second_body["items"]) == 2

        third = await client.get(
            f"/api/v1/moderation/users/{participant.uuid}/attendance",
            headers=cookie_headers(moderator_token),
            params={"limit": 2, "cursor": second_body["next_cursor"]},
        )
        assert third.status_code == 200, third.text
        third_body = third.json()
        assert len(third_body["items"]) == 1
        assert third_body["next_cursor"] is None

    seen = [
        item["card_uuid"]
        for item in first_body["items"] + second_body["items"] + third_body["items"]
    ]
    assert len(seen) == len(set(seen)) == 5


async def test_broken_cursor_gets_422(session: AsyncSession) -> None:
    """AC-06: битый курсор → `422 invalid_cursor`, а не `500`."""
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)

    async with build_client(session) as client:
        response = await client.get(
            f"/api/v1/moderation/users/{participant.uuid}/attendance",
            headers=cookie_headers(moderator_token),
            params={"cursor": "###"},
        )

    assert response.status_code == 422, response.text
    assert response.json() == {"code": "invalid_cursor"}


async def test_limit_out_of_bounds_gets_422(session: AsyncSession) -> None:
    """AC-05: `limit` вне `1..50` → `422` (границы объявлены в Query)."""
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)

    async with build_client(session) as client:
        too_big = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=51)
        zero = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=0)

    assert too_big.status_code == 422
    assert zero.status_code == 422


# ---------------------------------------------------------------- AC-08, AC-10, AC-11


async def test_tie_break_by_card_id_across_pages(session: AsyncSession) -> None:
    """AC-08: 5 записей с ОДИНАКОВЫМ `created_at` проходятся без дублей и пропусков.

    Tie-break по `card_id DESC` — единственное, что делает порядок
    детерминированным: без него страницы пересекались бы и теряли строки.
    Мутация: убрать `a.card_id DESC` из `ORDER BY` — PostgreSQL вернул бы
    произвольный порядок внутри одинаковых меток, и `seen` перестал бы быть
    множеством из пяти.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    same_moment = datetime.now(UTC) - timedelta(hours=2)
    for _ in range(5):
        _, card_id = await make_card(
            session, author_id=moderator.id, starts_at=datetime.now(UTC) - timedelta(hours=1)
        )
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=participant.id,
            created_at=same_moment,
        )

    seen: list[str] = []
    async with build_client(session) as client:
        cursor: str | None = None
        for _ in range(3):
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(
                f"/api/v1/moderation/users/{participant.uuid}/attendance",
                headers=cookie_headers(moderator_token),
                params=params,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            seen.extend(item["card_uuid"] for item in body["items"])
            cursor = body["next_cursor"]

    assert cursor is None
    assert len(seen) == len(set(seen)) == 5


async def test_deleted_card_mid_history_keeps_page_valid(session: AsyncSession) -> None:
    """AC-10: удаление карточки из середины → `200`, элемента нет, курсор валиден.

    Ретенция (ADR-07) реализуется в 10.6 удалением; контракт истории обязан
    быть устойчив к каскаду уже сейчас. Мутация: `JOIN` без проверки — не
    ломает (внутренний `JOIN` строку просто не отдаст), поэтому тест
    дополнительно проверяет, что курсор, выданный ДО удаления, продолжает
    работать: иначе каскад ломал бы догрузку.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=3)
    card_ids: list[int] = []
    for index in range(3):
        _, card_id = await make_card(session, author_id=moderator.id, starts_at=moment)
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=participant.id,
            created_at=moment + timedelta(minutes=index),
        )
        card_ids.append(card_id)

    async with build_client(session) as client:
        first = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=2)
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor is not None

        # Удаляем карточку, которая ЕЩЁ НЕ выдана (последняя в порядке возрастания
        # `created_at`, то есть первая на следующей странице).
        await session.execute(text("DELETE FROM event_cards WHERE id = :id"), {"id": card_ids[0]})
        await session.flush()

        second = await client.get(
            f"/api/v1/moderation/users/{participant.uuid}/attendance",
            headers=cookie_headers(moderator_token),
            params={"limit": 2, "cursor": cursor},
        )
        whole = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=20)

    assert second.status_code == 200, second.text
    assert second.json()["items"] == []
    assert second.json()["next_cursor"] is None
    assert whole.status_code == 200
    assert len(whole.json()["items"]) == 2


async def test_three_person_company_histories_are_separate(session: AsyncSession) -> None:
    """AC-11 (R15): компания из 3 человек — история каждого содержит РОВНО свою запись.

    Мутация: предикат по `card_id` вместо `participant_id` — обе истории
    вернули бы записи друг друга (по одной строке на карточку у каждого
    участника, и обе строки совпали бы по карточке).
    """
    author, _ = await make_user(session)
    first_member, _ = await make_user(session)
    second_member, _ = await make_user(session)
    moderator, moderator_token = await make_user(session, role="moderator")
    moment = datetime.now(UTC) - timedelta(hours=4)
    # У КАЖДОГО участника СВОЯ карточка того же автора: тогда истории
    # различаются наблюдаемо (`card_uuid`/`card_title`), и подмена предиката
    # адресности на `card_id` или `author_id` даёт другой набор, а не тот же.
    # На одной карточке с одинаковыми полями ответа тест был бы тавтологией
    # (§18.2): сравнивались бы два идентичных элемента.
    _, first_card_id = await make_card(
        session, author_id=author.id, title="Первая встреча", starts_at=moment
    )
    _, second_card_id = await make_card(
        session, author_id=author.id, title="Вторая встреча", starts_at=moment
    )
    await add_attendance(
        session, card_id=first_card_id, participant_id=first_member.id, created_at=moment
    )
    await add_attendance(
        session,
        card_id=second_card_id,
        participant_id=second_member.id,
        created_at=moment + timedelta(minutes=1),
    )

    async with build_client(session) as client:
        first = await _call(client, token=moderator_token, user_uuid=first_member.uuid)
        second = await _call(client, token=moderator_token, user_uuid=second_member.uuid)
        author_history = await _call(client, token=moderator_token, user_uuid=author.uuid)

    assert first.status_code == second.status_code == author_history.status_code == 200
    assert len(first.json()["items"]) == 1
    assert len(second.json()["items"]) == 1
    # Автор карточки — НЕ участник: строки `attendance` у него нет (D-7.1-2).
    assert author_history.json()["items"] == []
    # Разные наборы: ключ адресности — `participant_id`, а не карточка.
    assert first.json()["items"] != second.json()["items"]
    assert first.json()["items"][0]["card_title"] == "Первая встреча"
    assert second.json()["items"][0]["card_title"] == "Вторая встреча"


# ---------------------------------------------------------------- CR 7.5 H-1, M-2


#: CR 7.5 H-1: четыре вида битого курсора. Каждый обязан дать `422
#: invalid_cursor` с ОДИНАКОВЫМ телом — иначе клиент ветвился бы по форме
#: ошибки вместо `code`, а `OverflowError` (смещение за границы `datetime`)
#: вылетал бы как `500`, обвиняя сервер в клиентском значении (AC-06).
BROKEN_CURSORS = {
    # `astimezone(UTC)` на таком значении поднимает `OverflowError` — он и был
    # HIGH-находкой: стоя ВНЕ `try`, ошибка давала `500`.
    "overflow": base64.urlsafe_b64encode(
        b'{"created_at":"0001-01-01T00:00:00+14:00",'
        b'"card_uuid":"11111111-1111-1111-1111-111111111111"}'
    )
    .decode()
    .rstrip("="),
    "not_base64": "###",
    "missing_key": base64.urlsafe_b64encode(b'{"created_at":"2026-09-01T00:00:00Z"}')
    .decode()
    .rstrip("="),
    "extra_key": base64.urlsafe_b64encode(
        b'{"created_at":"2026-09-01T00:00:00Z","card_uuid":'
        b'"11111111-1111-1111-1111-111111111111","card_id":5}'
    )
    .decode()
    .rstrip("="),
}


async def test_every_broken_cursor_gets_identical_422(session: AsyncSession) -> None:
    """CR 7.5 H-1 (AC-06): все виды битого курсора → `422 invalid_cursor`, байт-в-байт.

    Мутация, которая краснит: `astimezone` вне `try` в `decode_history_cursor`
    (тогда `overflow` даёт `500`), либо отдельный код ошибки на класс разбора
    (тогда тела разойдутся).
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)

    bodies: list[bytes] = []
    async with build_client(session) as client:
        for name, cursor in BROKEN_CURSORS.items():
            response = await client.get(
                f"/api/v1/moderation/users/{participant.uuid}/attendance",
                headers=cookie_headers(moderator_token),
                params={"cursor": cursor},
            )
            assert response.status_code == 422, (name, response.status_code, response.text)
            bodies.append(response.content)

    assert len(set(bodies)) == 1, bodies
    assert bodies[0] == b'{"code":"invalid_cursor"}', bodies[0]


async def test_history_renders_all_four_statuses(session: AsyncSession) -> None:
    """CR 7.5 M-2 (таблица исходов): все четыре `status` присутствуют в истории.

    `pending` не встречался ни в одном тесте истории: это единственный статус,
    достижимый до ответа сторон, и единственный, у которого `confirmed_at
    IS NULL`. Пропущенная строка таблицы исходов — пропущенная ветка кода
    (урок CR 7.4).

    Мутация: `status: Literal["confirmed","no_show","disputed"]` в схеме —
    `pending` дал бы `500` при валидации ответа.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    moment = datetime.now(UTC) - timedelta(hours=5)
    expected = ("pending", "confirmed", "no_show", "disputed")
    for index, status in enumerate(expected):
        _, card_id = await make_card(
            session, author_id=moderator.id, title=status, starts_at=moment
        )
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=participant.id,
            status=status,
            created_at=moment + timedelta(minutes=index),
        )

    async with build_client(session) as client:
        response = await _call(client, token=moderator_token, user_uuid=participant.uuid)

    assert response.status_code == 200, response.text
    assert {item["status"] for item in response.json()["items"]} == set(expected)


async def test_deleted_cursor_anchor_does_not_swallow_tie_group(session: AsyncSession) -> None:
    """CR 7.5 H-1, D-7.5-10: удаление карточки-ЯКОРЯ не теряет tie-группу.

    Сценарий штатный: воркер 7.3 пишет запросы подтверждения одним
    `INSERT ... SELECT` в одной транзакции, а `now()` в PostgreSQL возвращает
    ВРЕМЯ ТРАНЗАКЦИИ — у пачки одинаковый `created_at`. Ретенция (10.6) удаляет
    карточки; если удалена ровно та, чей `(created_at, card_id)` попал в
    курсор, `COALESCE(..., 0)` отсекал ВСЮ группу (`created_at < :cur`), и
    остаток страницы пропадал навсегда.

    Мутация, которая краснит: вернуть `0` вместо `2**63-1` в sentinel —
    вторая страница вернёт пустой список, и `seen` перестанет быть множеством
    из пяти.
    """
    moderator, moderator_token = await make_user(session, role="moderator")
    participant, _ = await make_user(session)
    same_moment = datetime.now(UTC) - timedelta(hours=6)
    card_ids: list[int] = []
    for _ in range(5):
        _, card_id = await make_card(session, author_id=moderator.id, starts_at=same_moment)
        await add_attendance(
            session,
            card_id=card_id,
            participant_id=participant.id,
            created_at=same_moment,
        )
        card_ids.append(card_id)

    async with build_client(session) as client:
        first = await _call(client, token=moderator_token, user_uuid=participant.uuid, limit=2)
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        seen = [item["card_uuid"] for item in first.json()["items"]]

        # Якорь курсора — последняя выданная строка. Именно его удаляет ретенция:
        # в порядке `card_id DESC` это НАИМЕНЬШИЙ из выданных `card_id`.
        anchor_card_id = sorted(card_ids, reverse=True)[1]
        await session.execute(
            text("DELETE FROM event_cards WHERE id = :id"), {"id": anchor_card_id}
        )
        await session.flush()

        cursor_value: str | None = cursor
        while cursor_value is not None:
            page = await client.get(
                f"/api/v1/moderation/users/{participant.uuid}/attendance",
                headers=cookie_headers(moderator_token),
                params={"limit": 2, "cursor": cursor_value},
            )
            assert page.status_code == 200, page.text
            body = page.json()
            seen.extend(item["card_uuid"] for item in body["items"])
            cursor_value = body["next_cursor"]

    # Потерь нет: все пять карточек дошли до клиента (якорь мог повториться —
    # см. D-7.5-10: дубли внутри группы допустимы, ПОТЕРИ — нет).
    assert len(set(seen)) == 5, seen
