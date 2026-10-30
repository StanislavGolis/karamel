"""Текст SQL страницы «моих заявок» (стори 5а.3, Task 9.2; AC-03, AC-06, AC-07).

§18.2: проверяется ТЕКСТ запроса. Главный тест здесь — отсутствие
`not_selected`: FR81 нарушается одной строкой в предикате, и никакой
интеграционный тест с одной заявкой этого не заметит.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from app.repositories.join_requests import JoinRequestRepository


def _sql(statement: object) -> str:
    return str(statement).replace("\n", " ")


def _page(after: tuple[datetime, uuid_lib.UUID] | None = None, limit: int = 21) -> str:
    return _sql(
        JoinRequestRepository.build_my_requests_page_statement(
            applicant_id=7, city_slug="tomsk", after=after, limit=limit
        )
    )


def test_page_never_selects_not_selected() -> None:
    """AC-03 (FR81): `not_selected` отсутствует в тексте запроса ЦЕЛИКОМ.

    Краснеет, если заменить положительный список на `status <> 'not_selected'`
    или добавить статус в `JOIN_REQUEST_VISIBLE_STATUSES`: тогда отказ
    заявителя стал бы виден в «Моих заявках».
    """
    sql = _page()

    assert "not_selected" not in sql


def test_page_filters_by_visible_statuses() -> None:
    """AC-03: предикат статуса присутствует — иначе отдавалась бы вся история.

    Краснеет, если убрать `.where(status.in_(...))`: в выдачу попали бы
    заявки любого состояния, включая скрытые.
    """
    sql = _page()

    assert "join_requests.status IN" in sql


def test_page_scopes_by_applicant_and_city() -> None:
    """AC-03, AC-04: срез по заявителю и сообществу — в самом SQL.

    Краснеет, если убрать любой из предикатов: чужие заявки попадали бы
    в выдачу (`applicant_id`), а карточки другого сообщества — тоже
    (`city_slug`, ADR-01).
    """
    sql = _page()

    assert "join_requests.applicant_id = :applicant_id_1" in sql
    assert "event_cards.city_slug = :city_slug_1" in sql


def test_page_orders_by_submitted_at_and_uuid() -> None:
    """AC-06 (D-5a.3-6): `ORDER BY submitted_at, uuid` — ровно ключ курсора.

    Краснеет, если убрать `uuid`: при равных `submitted_at` (частая пачка
    подач) порядок не определён, и `WHERE (submitted_at, uuid) >` терял бы
    или дублировал строки между страницами.
    """
    sql = _page()

    assert "ORDER BY join_requests.submitted_at ASC, join_requests.uuid ASC" in sql


def test_page_applies_cursor_predicate_when_given() -> None:
    """AC-06: с курсором появляется сравнение кортежа, без него — нет.

    Краснеет, если сравнивать только `submitted_at`: строки с одинаковым
    `submitted_at` терялись бы на границе страниц.
    """
    assert "(join_requests.submitted_at, join_requests.uuid) >" not in _page()
    cursor_page = _page((datetime.now(UTC), uuid_lib.uuid4()))
    assert "(join_requests.submitted_at, join_requests.uuid) >" in cursor_page


def test_page_has_limit_and_joins_place_and_profile() -> None:
    """AC-06, AC-07: `LIMIT` присутствует; место и анкета — через OUTER JOIN.

    Краснеет, если заменить `outerjoin` на `join`: автор без анкеты или
    карточка без профиля выпали бы из выдачи целиком, и заявка исчезла бы
    из списка заявителя из-за пустой анкеты автора.
    """
    sql = _page(limit=21)

    assert "LIMIT :param_1" in sql
    assert "LEFT OUTER JOIN profiles" in sql
    assert "LEFT OUTER JOIN places" in sql


def test_count_has_no_cursor_and_same_filters() -> None:
    """AC-06 (D-5a.3-7): `total` не зависит от листания.

    Краснеет, если добавить курсор в `COUNT`: `total` уменьшался бы от
    страницы к странице, и клиент не смог бы показать счётчик.
    """
    sql = _sql(
        JoinRequestRepository.build_my_requests_count_statement(applicant_id=7, city_slug="tomsk")
    )

    assert "count(join_requests.id)" in sql
    assert "not_selected" not in sql
    assert "join_requests.submitted_at" not in sql
    assert "event_cards.city_slug = :city_slug_1" in sql
