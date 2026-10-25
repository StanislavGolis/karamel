"""Подача жалобы на неявку: API-сценарии (стори 7.4; AC-03…AC-06, AC-12).

**Компания из ТРЁХ человек** (урок CR 7.2): автор + два участника. На одном
участнике «жалоба на пару» неотличима от «жалобы на карточку», и дефект
вычисления `participant_id` (жалоба на чужую пару той же карточки) прошёл бы
незамеченным. Второй участник — адресат, на которого жалуются, но пары
«автор ↔ он» не существует в смысле жалобы: у автора пара есть с КАЖДЫМ
участником, поэтому его жалоба адресна.

**Статус пары задаётся данными** (`report_helpers.make_company`), а не вызовом
`confirm_attendance`: тест жалобы не должен падать из-за регрессии 7.2.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_helpers import (
    build_client,
    make_company,
    make_user,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _reports(session: AsyncSession, card_id: int) -> list[tuple[int, int, str, str]]:
    """Все жалобы по карточке: `(reporter_id, attendance_id, target_type, status)`."""
    rows = await session.execute(
        text(
            "SELECT r.reporter_id, r.attendance_id, r.target_type, r.status "
            "  FROM reports r "
            "  JOIN attendance a ON a.id = r.attendance_id "
            " WHERE a.card_id = :c "
            " ORDER BY r.id"
        ),
        {"c": card_id},
    )
    return [(int(r[0]), int(r[1]), str(r[2]), str(r[3])) for r in rows.all()]


async def test_author_reports_participant_no_show(session: AsyncSession) -> None:
    """AC-03: `no_show` + жалоба автора на участника → `201` и одна строка пары.

    Мутация M1 (`participant_id = viewer_id` вместо `accused_id` в ветке
    автора) делает тест красным: строка привязалась бы к несуществующей паре
    и `ON CONFLICT` не сработал бы вовсе — вставка упала бы на FK, а не
    дала `201`.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert response.status_code == 201
    body = response.json()
    assert body["target_type"] == "no_show"
    assert body["status"] == "open"
    assert uuid_lib.UUID(body["uuid"])

    rows = await _reports(session, company["card_id"])
    assert len(rows) == 1
    reporter_id, attendance_id, target_type, status = rows[0]
    assert reporter_id == company["author"].id
    # AC-06: строка привязана к ПАРЕ именно того участника, на кого жалоба.
    assert attendance_id == company["attendance"][accused.id]
    assert (target_type, status) == ("no_show", "open")


async def test_member_reports_author_no_show(session: AsyncSession) -> None:
    """AC-03, AC-09: жалоба участника на АВТОРА карточки тоже даёт `201`.

    Симметрия важна: у пары две стороны, и «жаловаться может только автор»
    было бы скрытым ограничением домена, которого нет в FR90.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    member = company["members"][1]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=member["token"], accused_uuid=company["author"].uuid
        )

    assert response.status_code == 201
    rows = await _reports(session, company["card_id"])
    assert len(rows) == 1
    assert rows[0][0] == member["user"].id
    assert rows[0][1] == company["attendance"][member["user"].id]


async def test_pending_pair_gives_422(session: AsyncSession) -> None:
    """AC-04 (D-7.4-7): `pending` → `422 ATTENDANCE_PENDING`, строк не создаётся.

    Мутация M2 (`status == "pending"` убрано) делает тест красным: жалоба
    создалась бы до фиксации факта, то есть санкция могла бы последовать за
    встречу, которую никто ещё не подтвердил.
    """
    company = await make_company(session, pair_status="pending", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert response.status_code == 422
    assert response.json() == {"code": "ATTENDANCE_PENDING"}
    assert await _reports(session, company["card_id"]) == []


async def test_disputed_pair_allows_both_sides(session: AsyncSession) -> None:
    """AC-09 (D-7.4-10, NFR39): при `disputed` жалуются ОБЕ стороны → `201`.

    Сервер НЕ проверяет, «пришёл ли» жалобщик. Проверка вернула бы `403`
    одной из двух сторон, и по коду она восстановила бы чужой флаг (класс
    D-7.2-5). Мутация M3 (добавить чтение `participant_confirmed` и отказ
    при `False`) делает этот тест красным ровно на второй жалобе.
    """
    company = await make_company(session, pair_status="disputed", members=2)
    member = company["members"][0]

    async with build_client(session) as client:
        first = await post_report(
            client, company, token=company["author_token"], accused_uuid=member["user"].uuid
        )
        second = await post_report(
            client, company, token=member["token"], accused_uuid=company["author"].uuid
        )

    assert first.status_code == 201
    assert second.status_code == 201
    rows = await _reports(session, company["card_id"])
    assert len(rows) == 2
    # Обе строки — по РАЗНЫМ жалобщикам, но по ОДНОЙ паре.
    assert {r[0] for r in rows} == {company["author"].id, member["user"].id}
    assert len({r[1] for r in rows}) == 1


async def test_registered_stranger_gets_404(session: AsyncSession) -> None:
    """AC-12 (ADR-13): посторонний — `404 card_not_found`.

    Тот же ответ, что «карточки нет»: разный код дал бы перебором `card_uuid`
    знание о существовании встреч и о составе компании.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    stranger, stranger_token = await make_user(session)

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=stranger_token, accused_uuid=company["author"].uuid
        )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}
    assert await _reports(session, company["card_id"]) == []
    assert stranger.id != company["author"].id


async def test_accused_not_from_pair_gets_404(session: AsyncSession) -> None:
    """AC-06, AC-12: `accused_uuid` вне пары → `404`, не `422`/`400`.

    Участник жалуется на ДРУГОГО участника: пара «он ↔ другой» существует в
    карточке, но жалоба участника адресуется только автору (ADR-04). Ответ
    тот же `404`: `422` подтвердил бы, что оба в карточке.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    first, second = company["members"]

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=first["token"], accused_uuid=second["user"].uuid
        )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}
    assert await _reports(session, company["card_id"]) == []


async def test_unknown_accused_uuid_gets_404(session: AsyncSession) -> None:
    """AC-03, AC-12: `accused_uuid` несуществующего пользователя → `404`.

    Иначе перебором `uuid` можно было бы выяснить, кто зарегистрирован.
    """
    company = await make_company(session, pair_status="no_show", members=2)

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=uuid_lib.uuid4()
        )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


async def test_self_report_gets_404(session: AsyncSession) -> None:
    """AC-06 (3.3): жалоба на себя — `404`, не `422`.

    `422` подтвердил бы участие зрителя в карточке. Мутация M4 (убрать
    `viewer_id == accused_id`) делает тест красным: автор нашёл бы пару
    `participant_id = author_id`, guard `participant_id <> :author_id` вернул бы
    `None`, и всё равно `404` — но ЛИШЬ для автора. Участник же получил бы
    `201` с жалобой на самого себя, что и ловит тест ниже через участника.
    """
    company = await make_company(session, pair_status="no_show", members=2)

    async with build_client(session) as client:
        as_author = await post_report(
            client, company, token=company["author_token"], accused_uuid=company["author"].uuid
        )
        member = company["members"][0]
        as_member = await post_report(
            client, company, token=member["token"], accused_uuid=member["user"].uuid
        )

    assert as_author.status_code == 404
    assert as_member.status_code == 404
    assert as_author.json() == as_member.json() == {"code": "card_not_found"}
    assert await _reports(session, company["card_id"]) == []


async def test_self_report_does_not_read_attendance(session: AsyncSession) -> None:
    """AC-06 (3.3, P-8): жалоба на себя отбивается ДО чтения пары.

    Мутация M4 (убрать `viewer_id == accused_id` из сервиса) НЕ меняет код
    ответа: автор всё равно получил бы `404` от SQL-guard
    `participant_id <> :author_id`, а участник — от проверки «обвиняемый не
    автор». Мутация эквивалентна по телу ответа, поэтому проверять её надо
    тем, что она реально меняет, — ЧИСЛОМ запросов: без Python-guard сервис
    сходил бы в `attendance` за парой «сам с собой».

    Это и есть P-8: предикат `viewer_id == accused_id` обязан быть покрыт,
    иначе он — второй, непроверяемый источник того же ответа.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any, **kwargs: Any) -> None:
        statements.append(" ".join(statement.split()))

    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", _capture)
    try:
        async with build_client(session) as client:
            response = await post_report(
                client, company, token=company["author_token"], accused_uuid=company["author"].uuid
            )
    finally:
        event.remove(listen_engine, "before_cursor_execute", _capture)

    assert response.status_code == 404
    assert not [s for s in statements if "FROM attendance" in s], statements


async def test_unknown_card_gets_404(session: AsyncSession) -> None:
    """AC-12: несуществующая карточка → тот же `404 card_not_found`."""
    company = await make_company(session, pair_status="no_show", members=2)
    fake = uuid_lib.uuid4()

    async with build_client(session) as client:
        response = await client.post(
            f"/api/v1/cards/{fake}/report-no-show",
            json={"accused_uuid": str(company["members"][0]["user"].uuid)},
            headers={"cookie": f"{_cookie_name()}={company['author_token']}"},
        )

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


def _cookie_name() -> str:
    """Имя access-cookie (импорт внутри функции: тест не должен зависеть от порядка)."""
    from app.services.session_service import ACCESS_COOKIE_NAME

    return ACCESS_COOKIE_NAME


async def test_extra_fields_in_body_rejected(session: AsyncSession) -> None:
    """AC-03/X-7.4-3: `reason` в теле — `422`, а не тихое игнорирование.

    Клиент не должен думать, что передал текст жалобы, которого API не
    принимает: причина заполняется модератором в 8.4.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await client.post(
            f"/api/v1/cards/{company['card_uuid']}/report-no-show",
            json={"accused_uuid": str(accused.uuid), "reason": "не пришёл"},
            headers={"cookie": f"{_cookie_name()}={company['author_token']}"},
        )

    assert response.status_code == 422
    assert await _reports(session, company["card_id"]) == []


async def test_reports_without_consent_rejected(session: AsyncSession) -> None:
    """AC-03: без согласия на обработку ПД (`ConsentUser`) — отказ, не `201`.

    Жалоба связывает двух людей, поэтому гард тот же, что у `/attendance`.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]
    # `ck_users_consent_pair` = `age_confirmed = (consent_given_at IS NOT NULL)`,
    # а `age_confirmed` NOT NULL: снять согласие можно только вместе с
    # подтверждением возраста (`False`), иначе UPDATE упал бы на ограничении,
    # и тест проверял бы схему, а не гард `ConsentUser`.
    company["author"].consent_given_at = None
    company["author"].age_confirmed = False
    await session.flush()

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    # CR 7.4 M-2: точное ожидание вместо множества `(403, 409, 422, 428)`.
    # Множество принимало почти любой ошибочный ответ (в том числе `422` от
    # валидации тела), то есть тест был зелёным при сломанном гарде.
    # `deps.py:166` — единственный источник: `403` + `CONSENT_REQUIRED`.
    assert response.status_code == 403
    assert response.json() == {"code": "consent_required"}
    assert await _reports(session, company["card_id"]) == []


async def test_target_type_and_status_in_database(session: AsyncSession) -> None:
    """AC-05, AC-10 (NFR52): в БД `target_type='no_show'`, `status='open'`, `resolved_at` NULL.

    Код не создаёт разобранных жалоб: срок 20 часов и пометку «просрочено»
    считает кабинет 8.3 (NFR63), а не `INSERT`.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        assert (
            await post_report(
                client, company, token=company["author_token"], accused_uuid=accused.uuid
            )
        ).status_code == 201

    row = await session.execute(
        text(
            "SELECT r.target_type, r.status, r.resolved_at, r.reason, r.reported_at "
            "  FROM reports r JOIN attendance a ON a.id = r.attendance_id "
            " WHERE a.card_id = :c"
        ),
        {"c": company["card_id"]},
    )
    target_type, status, resolved_at, reason, reported_at = row.one()
    assert (target_type, status) == ("no_show", "open")
    assert resolved_at is None
    assert reason is None
    assert reported_at is not None


async def test_target_type_message_rejected_by_schema(session: AsyncSession) -> None:
    """AC-05 (D-7.4-8): `target_type` не принимается от клиента вовсе.

    Поле не описано в запросе, поэтому попытка передать его даёт `422`
    (`extra="forbid"`), а не молчаливое создание жалобы другого типа.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    accused = company["members"][0]["user"]

    async with build_client(session) as client:
        response = await client.post(
            f"/api/v1/cards/{company['card_uuid']}/report-no-show",
            json={"accused_uuid": str(accused.uuid), "target_type": "message"},
            headers={"cookie": f"{_cookie_name()}={company['author_token']}"},
        )

    assert response.status_code == 422
    assert await _reports(session, company["card_id"]) == []


async def test_reports_row_references_pair_not_card(session: AsyncSession) -> None:
    """AC-06: `attendance_id` — пара автора с ОБВИНЯЕМЫМ, не любая пара карточки.

    Мутация M5 (`ORDER BY id LIMIT 1` вместо фильтра по `participant_id`)
    делает тест красным: жалоба автора на второго участника привязалась бы
    к паре первого.
    """
    company = await make_company(session, pair_status="no_show", members=2)
    first, second = company["members"]

    async with build_client(session) as client:
        assert (
            await post_report(
                client, company, token=company["author_token"], accused_uuid=second["user"].uuid
            )
        ).status_code == 201

    rows = await _reports(session, company["card_id"])
    assert len(rows) == 1
    assert rows[0][1] == company["attendance"][second["user"].id]
    assert rows[0][1] != company["attendance"][first["user"].id]


async def test_confirmed_pair_gives_422_attendance_confirmed(session: AsyncSession) -> None:
    """AC-03 (CR 7.4 H-1): пара `confirmed` — оба пришли — жалобу не принимает.

    До правки блокировался только `pending`, и `confirmed` («оба подтвердили,
    что пришли») получал `201` на жалобу «на неявку» — ложную по построению.
    Тело проверяется байт-в-байт: отдельный код от `ATTENDANCE_PENDING`,
    потому что подтверждение ЗАВЕРШЕНО, просто успешно.

    Заодно фиксируется, что отказ не пишет в `notifications` (AC-13):
    в отказной ветке уведомление сообщило бы модератору о жалобе, которой нет.
    """
    company = await make_company(session, pair_status="confirmed", members=2)
    accused = company["members"][0]["user"]
    before = int((await session.execute(text("SELECT count(*) FROM notifications"))).scalar_one())

    async with build_client(session) as client:
        response = await post_report(
            client, company, token=company["author_token"], accused_uuid=accused.uuid
        )

    assert response.status_code == 422
    assert response.text == '{"code":"ATTENDANCE_CONFIRMED"}'
    assert response.headers["cache-control"] == "no-store"
    assert await _reports(session, company["card_id"]) == []
    after = int((await session.execute(text("SELECT count(*) FROM notifications"))).scalar_one())
    assert after == before


async def test_four_negative_scenarios_are_indistinguishable(session: AsyncSession) -> None:
    """AC-12, D-7.4-9, ось O-6 (CR 7.4 M-1): четыре `404` неразличимы.

    «Нет карточки» / «не участник» / «пары нет» / «жалоба на себя» обязаны
    совпадать **байт-в-байт** — код, тело и ВСЕ заголовки. Отдельные тесты
    (по одному сценарию на тест) этого не доказывают: каждый сверяет свой
    ответ с константой, и если один из путей начнёт отдавать другой набор
    заголовков (`vary`, `content-length`) или другой код, ни один не
    покраснеет. Здесь ответы сравниваются МЕЖДУ СОБОЙ, попарно.

    Число SQL намеренно не сравнивается: расхождение 2/3/4/4 унаследовано
    из 7.2 (`X-7.4-4`), см. `deferred-work.md`.
    """
    company = await make_company(session, pair_status="no_show", members=3)
    first, second_member = company["members"][0], company["members"][1]
    stranger, stranger_token = await make_user(session)

    async with build_client(session) as client:
        # 1) карточки нет
        no_card = await post_report(
            client,
            {"card_uuid": str(uuid_lib.uuid4())},
            token=company["author_token"],
            accused_uuid=first["user"].uuid,
        )
        # 2) не участник: зарегистрирован, но вне компании
        not_member = await post_report(
            client, company, token=stranger_token, accused_uuid=first["user"].uuid
        )
        # 3) пары нет: участник жалуется на ДРУГОГО участника (пара — только с автором)
        no_pair = await post_report(
            client, company, token=second_member["token"], accused_uuid=first["user"].uuid
        )
        # 4) жалоба на себя
        self_report = await post_report(
            client, company, token=company["author_token"], accused_uuid=company["author"].uuid
        )

    responses = {
        "нет карточки": no_card,
        "не участник": not_member,
        "пары нет": no_pair,
        "на себя": self_report,
    }
    for name, response in responses.items():
        assert response.status_code == 404, f"{name}: {response.status_code} {response.text}"

    items = list(responses.items())
    reference_name, reference = items[0]
    for name, response in items[1:]:
        assert response.text == reference.text, (
            f"тело различается: «{reference_name}»={reference.text!r} vs «{name}»={response.text!r}"
        )
        # Полный набор заголовков, а не только `cache-control`: лишний
        # `etag`/`last-modified`/`x-*` — тоже оракул (TD 7.2, ось O-4).
        assert sorted(response.headers.items()) == sorted(reference.headers.items()), (
            f"заголовки различаются: «{reference_name}»={sorted(reference.headers.items())} "
            f"vs «{name}»={sorted(response.headers.items())}"
        )
    assert stranger.id not in {company["author"].id} | {
        member["user"].id for member in company["members"]
    }
    assert await _reports(session, company["card_id"]) == []
