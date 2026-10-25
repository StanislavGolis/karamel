"""Текст SQL отмены заявки (стори 5а.3, Task 9.1; AC-01, AC-04, AC-05).

§18.2: проверяется ТЕКСТ запроса, а не «функция вызвалась». Каждый тест
краснеет от снятия конкретной защиты — она названа в докстринге.
"""

from __future__ import annotations

import uuid as uuid_lib

from app.repositories.join_requests import JoinRequestRepository


def _sql(statement: object) -> str:
    return str(statement).replace("\n", " ")


def test_cancel_statement_updates_only_pending() -> None:
    """AC-05: предикат `status='pending'` обязателен.

    Краснеет, если снять его: отмена переписала бы `decided_at` у `selected`,
    то есть подменила бы момент решения автора (NFR1–NFR4).
    """
    sql = _sql(JoinRequestRepository.build_cancel_statement(uuid_lib.uuid4(), 7, "tomsk"))

    assert sql.startswith("UPDATE join_requests")
    assert "join_requests.status = :status_1" in sql


def test_cancel_statement_sets_cancelled_and_decided_at() -> None:
    """AC-01: `SET status=:status, decided_at=now()` — оба поля в одном `UPDATE`.

    Краснеет, если убрать `decided_at`: заявка вышла из `pending`, а момента
    решения нет (D-5a.1-6). Отдельным `UPDATE` это писать нельзя — между
    двумя запросами видна заявка в `cancelled` без `decided_at`.
    """
    sql = _sql(JoinRequestRepository.build_cancel_statement(uuid_lib.uuid4(), 7, "tomsk"))

    assert "SET status=:status, decided_at=now()" in sql


def test_cancel_statement_scopes_by_applicant() -> None:
    """AC-04 (ADR-13): предикат `applicant_id` в САМОМ SQL.

    Краснеет, если убрать: чужая заявка отменялась бы, а проверка «после
    выборки» забывается при первом рефакторинге (класс дефекта 3.3).
    """
    sql = _sql(JoinRequestRepository.build_cancel_statement(uuid_lib.uuid4(), 7, "tomsk"))

    assert "join_requests.applicant_id = :applicant_id_1" in sql
    assert "join_requests.uuid = :uuid_1" in sql


def test_cancel_statement_scopes_by_community() -> None:
    """AC-04 (ADR-01/ADR-13): карточка чужого сообщества не отменяется.

    Краснеет, если убрать подзапрос: заявка чужого сообщества отменялась бы
    по `uuid`, то есть `uuid` стал бы достаточным основанием для записи.
    """
    sql = _sql(JoinRequestRepository.build_cancel_statement(uuid_lib.uuid4(), 7, "tomsk"))

    assert "event_cards.community_slug = :community_slug_1" in sql
    assert "join_requests.card_id IN (SELECT event_cards.id" in sql


def test_status_lookup_has_same_three_predicates() -> None:
    """AC-04, AC-05: диагностика читает ТУ ЖЕ строку, которую мог тронуть CAS.

    Краснеет, если убрать любой из трёх предикатов: `404` подменялся бы `409`
    на чужой заявке, и существование чужой заявки раскрывалось бы кодом ответа.
    """
    sql = _sql(JoinRequestRepository.build_status_lookup_statement(uuid_lib.uuid4(), 7, "tomsk"))

    assert sql.startswith("SELECT join_requests.status")
    assert "join_requests.uuid = :uuid_1" in sql
    assert "join_requests.applicant_id = :applicant_id_1" in sql
    assert "event_cards.community_slug = :community_slug_1" in sql


def test_status_lookup_does_not_filter_by_status() -> None:
    """AC-05: фильтра по статусу НЕТ — иначе диагностика не увидела бы отказ.

    Краснеет, если добавить `status='pending'` в чтение: на `cancelled`
    вернулся бы `None`, и повторная отмена отвечала бы `404` вместо
    идемпотентного `204` (AC-05).
    """
    sql = _sql(JoinRequestRepository.build_status_lookup_statement(uuid_lib.uuid4(), 7, "tomsk"))

    assert "join_requests.status = " not in sql
