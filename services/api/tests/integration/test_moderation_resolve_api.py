"""Решение по жалобе: контракт маршрута (стори 8.4; AC-01, AC-05, AC-07, AC-09, AC-10).

**Главные утверждения:**

- `200` и строка `sanctions` с `moderator_id` (AC-01);
- `reason` доходит до ДВУХ таблиц (AC-09, D-53) и до `payload` уведомления;
- `dismissed` не создаёт санкцию и не уведомляет обвиняемого (AC-07, M11);
- повторное решение — `409 REPORT_ALREADY_RESOLVED` (AC-05, D-48);
- мусор в теле отбивает схема до сервиса (AC-10).

**Почему проверяется `user_id` санкции.** D-57 вычисляет обвиняемого запросом;
если адресат подменён, санкция уйдёт не тому человеку — и это единственный
тест, который такую подмену поймает.
"""

from __future__ import annotations

import os
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_resolve_helpers import (
    REASON,
    build_client,
    dismiss_report,
    make_message_report,
    make_moderator,
    make_no_show_report,
    post_resolve,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _sanctions(session: AsyncSession) -> list[tuple[str, str, int | None, str]]:
    """Все санкции: `(type, reason, moderator_id, user_uuid)`. Порядок — по id."""
    result = await session.execute(
        text(
            "SELECT s.type, s.reason, s.moderator_id, u.uuid "
            "  FROM sanctions AS s JOIN users AS u ON u.id = s.user_id "
            " ORDER BY s.id"
        )
    )
    return [(r[0], r[1], r[2], str(r[3])) for r in result.all()]


async def _notifications(session: AsyncSession) -> list[tuple[int, str, dict]]:
    """Все уведомления: `(user_id, type, payload)`."""
    result = await session.execute(
        text("SELECT user_id, type, payload FROM notifications ORDER BY id")
    )
    return [(int(r[0]), r[1], r[2]) for r in result.all()]


async def test_warning_creates_sanction_and_returns_200(session: AsyncSession) -> None:
    """AC-01: `warning` → `200`, строка санкции с модератором и причиной."""
    report = await make_no_show_report(session)
    moderator, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="warning"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "resolved_warning"
        assert body["resolution"] == "warning"
        assert body["sanction_uuid"] is not None
        assert body["report_uuid"] == str(report["report_uuid"])

    rows = await _sanctions(session)
    assert len(rows) == 1, rows
    sanction_type, reason, moderator_id, user_uuid = rows[0]
    assert sanction_type == "warning"
    assert reason == REASON
    # AC-01: автор решения — вызвавший модератор, а не кто-то иной.
    assert moderator_id == moderator.id
    # D-57: обвиняемый — участник пары, а не жалобщик.
    assert user_uuid == str(report["accused"].uuid)


async def test_report_row_is_consistent_with_sanction(session: AsyncSession) -> None:
    """AC-05, AC-09, D-53: статус, resolution, reason, resolved_at согласованы."""
    report = await make_message_report(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert response.status_code == 200, response.text

    result = await session.execute(
        text(
            "SELECT status, resolution, reason, resolved_at IS NOT NULL "
            "  FROM reports WHERE uuid = :u"
        ),
        {"u": report["report_uuid"]},
    )
    status, resolution, reason, has_resolved_at = result.one()
    assert status == "resolved_ban"
    assert resolution == "ban"
    assert reason == REASON
    assert has_resolved_at is True

    # Тот же текст — в санкции: две записи, одна причина.
    assert [r[1] for r in await _sanctions(session)] == [REASON]


async def test_dismissed_creates_no_sanction_and_notifies_reporter_only(
    session: AsyncSession,
) -> None:
    """AC-07, D-46, M11: `dismissed` — без санкции и без уведомления обвиняемому."""
    report = await make_no_show_report(session, pair_status="disputed")
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await dismiss_report(client, token=token, report_uuid=report["report_uuid"])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "dismissed"
        assert body["sanction_uuid"] is None

    assert await _sanctions(session) == []

    rows = await _notifications(session)
    # Ровно одна строка — автору жалобы. Обвиняемому её быть не должно:
    # `sanction.issued` утверждало бы ограничение, которого нет (FR39).
    assert len(rows) == 1, rows
    user_id, type_, payload = rows[0]
    assert user_id == report["reporter"].id
    assert type_ == "report.resolved"
    assert payload["resolution"] == "dismissed"
    assert payload["sanction_uuid"] is None
    # Причина при закрытии без санкции тоже нужна: модератор объяснил отказ.
    assert payload["reason"] == REASON


async def test_both_sides_notified_on_sanction(session: AsyncSession) -> None:
    """AC-01: два уведомления с разными типами и правильными адресатами."""
    report = await make_message_report(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert response.status_code == 200, response.text

    rows = await _notifications(session)
    assert len(rows) == 2, rows
    by_user = {user_id: (type_, payload) for user_id, type_, payload in rows}
    accused_type, accused_payload = by_user[report["accused"].id]
    reporter_type, reporter_payload = by_user[report["reporter"].id]
    assert accused_type == "sanction.issued"
    assert reporter_type == "report.resolved"
    # FR39: обвиняемый видит ПРИЧИНУ, а не только факт.
    assert accused_payload["reason"] == REASON
    assert accused_payload["sanction_uuid"] is not None
    assert reporter_payload["sanction_uuid"] == accused_payload["sanction_uuid"]


async def test_second_resolve_returns_409(session: AsyncSession) -> None:
    """AC-05, D-48: повторное решение — `409`, и вторая санкция не создаётся."""
    report = await make_no_show_report(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        first = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="warning"
        )
        assert first.status_code == 200, first.text
        second = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "REPORT_ALREADY_RESOLVED"

    rows = await _sanctions(session)
    assert len(rows) == 1
    assert rows[0][0] == "warning"


async def test_unknown_report_returns_404(session: AsyncSession) -> None:
    """AC-10: несуществующая жалоба — `404 report_not_found`."""
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client, token=token, report_uuid=uuid_lib.uuid4(), resolution="warning"
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "report_not_found"


@pytest.mark.parametrize(
    ("resolution", "reason"),
    [
        # D-45: вне домена — отбивает Pydantic, сервис не вызывается.
        ("warn", REASON),
        ("", REASON),
        # Минимум длины причины: «ок» причиной не является.
        ("warning", "ок"),
    ],
)
async def test_invalid_body_is_422(session: AsyncSession, resolution: str, reason: str) -> None:
    """AC-10, D-45: мусор в теле — `422` от схемы, БД не тронута."""
    report = await make_no_show_report(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client,
            token=token,
            report_uuid=report["report_uuid"],
            resolution=resolution,
            reason=reason,
        )
        assert response.status_code == 422, response.text

    # Схема отбила запрос ДО сервиса: ни санкции, ни уведомлений.
    assert await _sanctions(session) == []
    assert await _notifications(session) == []


async def test_reason_is_trimmed(session: AsyncSession) -> None:
    """AC-09, D-45: причина обрезается по краям, а не сохраняется как пришла."""
    report = await make_no_show_report(session)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await post_resolve(
            client,
            token=token,
            report_uuid=report["report_uuid"],
            resolution="warning",
            reason="  Нарушение порядка  ",
        )
        assert response.status_code == 200, response.text

    rows = await _sanctions(session)
    assert rows[0][1] == "Нарушение порядка"


async def test_accused_lookup_is_one_query_not_n_plus_one(session: AsyncSession) -> None:
    """D-57: обвиняемый вычисляется ОДНИМ запросом, а не N+1 по ветвям.

    **Почему считаем SQL, а не читаем код.** `_ACCUSED_SQL` — четыре `UNION`-ветви
    (два типа жалобы × две стороны). Реализация «по ветке на тип» дала бы
    2 запроса для `message` и 2 для `no_show`, и разница была бы невидима
    функционально — только по числу обращений. Тот же приём, что ось NFR39
    в 7.4/8.2 (там считали SQL у негативных ответов).

    Считается при `echo` на уровне соединения: каждый `execute` сервиса
    увеличивает счётчик ровно на один. Проверяются ОБА типа жалобы — ветвь
    `no_show` иначе осталась бы без замера.
    """
    from sqlalchemy import event

    for factory in (make_no_show_report, make_message_report):
        report = await factory(session)
        _, token = await make_moderator(session)

        statements: list[str] = []

        # `statements=statements` — привязка к списку ЭТОЙ итерации. Без неё
        # ruff B023: замыкание захватывает переменную цикла, и при двух
        # итерациях обработчик писал бы в список последней.
        def _count(
            conn: object,
            cursor: object,
            statement: str,
            *args: object,
            _sink: list[str] = statements,
        ) -> None:
            _sink.append(statement)

        async with build_client(session) as client:
            # `get_bind()` возвращает то `AsyncEngine`, то sync `Engine` — в
            # зависимости от того, чем связана сессия (в тестах она приходит из
            # фикстуры §18.8). `getattr` покрывает оба случая: у `AsyncEngine`
            # есть `.sync_engine`, у sync `Engine` обработчик ставится прямо.
            bind = session.get_bind()
            sync_engine = getattr(bind, "sync_engine", bind)
            event.listen(sync_engine, "before_cursor_execute", _count)
            try:
                response = await post_resolve(
                    client,
                    token=token,
                    report_uuid=report["report_uuid"],
                    resolution="warning",
                )
            finally:
                event.remove(sync_engine, "before_cursor_execute", _count)
            assert response.status_code == 200, response.text

        accused_queries = [s for s in statements if "accused.user_id" in s]
        assert len(accused_queries) == 1, [s[:60] for s in accused_queries]


async def test_reason_is_not_logged(session: AsyncSession) -> None:
    """NFR32: текст причины не попадает в лог, а адрес жалобы — попадает.

    **Почему не `caplog`.** Проект логирует через `structlog`, а он не пишет в
    стандартный `logging`-handler напрямую: `caplog.records` остался ПУСТЫМ
    (`assert '' in ''`), и тест на `caplog` был бы зелёным при утечке —
    проверено прогоном. `structlog.testing.capture_logs()` перехватывает
    записи на уровне процессора, до форматтеров, и видит `event_dict` целиком.

    **Что проверяется.** (1) причины нет ни в одном событии; (2) `report_uuid`
    есть — иначе инцидент не расследовать, и «приватность» достигалась бы
    удалением всего лога.
    """
    from structlog.testing import capture_logs

    report = await make_message_report(session)
    _, token = await make_moderator(session)
    secret_reason = "УникальнаяПричинаНеДляЛогов42"

    with capture_logs() as captured:
        async with build_client(session) as client:
            response = await post_resolve(
                client,
                token=token,
                report_uuid=report["report_uuid"],
                resolution="warning",
                reason=secret_reason,
            )
            assert response.status_code == 200, response.text

    joined = " ".join(str(entry) for entry in captured)
    assert captured, "ни одной записи не перехвачено — тест ничего не проверяет"
    assert secret_reason not in joined, joined
    assert str(report["report_uuid"]) in joined, joined


async def test_already_resolved_wins_over_not_applicable(session: AsyncSession) -> None:
    """AC-05, D-48: на уже решённой жалобе `409`, а НЕ `422`.

    **Найдено CR откатом (R3).** Снятие проверки `status != 'open'` под
    блокировкой не краснило ни один тест: корректность держали
    `UNIQUE(report_id)` и CAS. Но проверка статуса — не только защита, она
    ещё и определяет КОД ответа. Без неё повторное решение по
    `disputed`-жалобе упирается в проверку применимости и отдаёт `422`, хотя
    правильный ответ — `409`: жалоба уже разобрана, и вопрос «применима ли
    санкция» к ней больше не задаётся.

    **Почему именно `disputed`.** На `no_show`-паре порядок ветвей виден:
    проверка применимости стоит ПОСЛЕ проверки статуса. На любой другой паре
    обе ветви дали бы `409` и без первой проверки — тест был бы слепым.
    """
    report = await make_no_show_report(session, pair_status="disputed")
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        first = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="dismissed"
        )
        assert first.status_code == 200, first.text
        # Вторая попытка с САНКЦИЕЙ: жалоба уже закрыта, поэтому `409` (не `422`),
        # хотя санкция за `disputed` и не была бы применима.
        second = await post_resolve(
            client, token=token, report_uuid=report["report_uuid"], resolution="ban"
        )
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "REPORT_ALREADY_RESOLVED"
