"""Текст SQL-билдеров выбора автора (стори 5б.2; AC-01, AC-04, AC-06, AC-09).

§18.2: ассерт обязан падать от реальной правки. Часть условий НЕ наблюдаема
через HTTP на подготовленных данных: предикат `filled_count < capacity`
отличим от его отсутствия только на карточке без мест, а `id <> :except`
в reject-шаге — только повторным вызовом. Без этих тестов снятие условия
осталось бы незамеченным до инцидента.

§18.3: сами гонки — в `tests/integration/test_select_races.py`.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy.dialects import postgresql

from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository

CARD_ID = 42
REQUEST_UUID = uuid_lib.UUID("11111111-2222-3333-4444-555555555555")
CHAT_UUID = uuid_lib.UUID("66666666-7777-8888-9999-000000000000")
APPLICANT_ID = 7


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами.

    `dialect=postgresql.dialect()` обязателен: диалект по умолчанию рендерит
    `UPDATE` иначе, и тест проверял бы не тот SQL, который уйдёт в БД
    (тот же довод, что в `test_card_leave_builders`).
    """
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_select_seat_statement_is_one_update_with_chat_uuid() -> None:
    """AC-01 (X-39, FR30): место, `full` и чат — ОДНИМ `UPDATE`.

    Краснеет от:
    - разделения на два `UPDATE` (в тексте появился бы второй запрос —
      проверяется единственность `UPDATE event_cards`);
    - удаления `chat_uuid = ...` из `SET` (чат не записался бы, а `200`
      обещает его в ответе);
    - снятия предиката `status = 'published'` (собрали бы `draft`/`closed`);
    - снятия `filled_count < capacity` (переполнили бы карточку).
    """
    statement = EventCardRepository.build_select_seat_statement(
        card_id=CARD_ID, chat_uuid=CHAT_UUID
    )
    sql = _sql(statement)
    assert sql.count("UPDATE event_cards") == 1
    assert "chat_uuid" in sql
    compact = sql.replace(" ", "")
    # `filled_count=(event_cards.filled_count+1)` — инкремент в том же `SET`.
    assert "filled_count=(event_cards.filled_count+1)" in compact
    assert "status='published'" in compact
    assert "filled_count<event_cards.capacity" in compact
    assert "RETURNING" in sql
    # `full` выставляется тем же `SET`, а не отдельным запросом (X-39).
    assert "CASE WHEN" in sql
    assert "'full'" in sql
    # X-6.1-1 (chore P-7): выбор автора законен ТОЛЬКО в режиме заявок (FR70).
    # Предикат в самом `UPDATE` (P-3): предпроверка в сервисе не закрывает окно,
    # в котором параллельный `PATCH capacity 1 → 3` уже закоммичен.
    assert "capacity=1" in compact


def test_select_seat_statement_returns_chat_uuid() -> None:
    """AC-01: ответ строится из записанных значений, включая `chat_uuid`.

    Краснеет, если из `RETURNING` убрать `chat_uuid`: сервис не смог бы
    подтвердить, что в БД легло именно сгенерированное значение.
    """
    statement = EventCardRepository.build_select_seat_statement(
        card_id=CARD_ID, chat_uuid=CHAT_UUID
    )
    sql = _sql(statement)
    returning = sql.split("RETURNING", 1)[1]
    assert "chat_uuid" in returning
    assert "filled_count" in returning
    assert "status" in returning


def test_select_request_statement_has_pending_predicate_and_card_id() -> None:
    """AC-01, AC-06: CAS `pending -> selected` с предикатом карточки.

    Краснеет от:
    - снятия `status = 'pending'` (выбрали бы уже отменённую заявку);
    - снятия `card_id` (заявка другой карточки прошла бы CAS — обход ADR-13);
    - замены `status='selected'` на что-то иное (заявка не стала бы выбранной).
    """
    sql = _sql(
        JoinRequestRepository.build_select_statement(request_uuid=REQUEST_UUID, card_id=CARD_ID)
    ).replace(" ", "")
    assert "UPDATEjoin_requests" in sql
    assert "status='selected'" in sql
    assert "status='pending'" in sql
    assert "card_id=" in sql
    assert "decided_at=now()" in sql


def test_reject_pending_statement_excludes_selected_and_keeps_pending_predicate() -> None:
    """AC-01 (FR81): остальные `pending` → `not_selected`, выбранная не тронута.

    Краснеет от:
    - снятия `id != :except` (выбранная заявка стала бы `not_selected`, и
      `200` вернулся бы при пустом составе — класс X-39);
    - снятия `status = 'pending'` (история — `cancelled`/`expired` — была бы
      перезаписана, и `/my/requests` потерял бы отменённые заявки);
    - появления `RETURNING` (число отклонённых утекло бы наружу — FR81).
    """
    sql = _sql(
        JoinRequestRepository.build_reject_pending_statement(card_id=CARD_ID, except_id=99)
    ).replace(" ", "")
    assert "status='not_selected'" in sql
    assert "status='pending'" in sql
    assert "id!=99" in sql
    assert "RETURNING" not in sql


def test_viewer_participation_statement_is_batched_and_selected_only() -> None:
    """AC-09 (D-5b.2-7): пакетный запрос, только `selected`.

    Краснеет от:
    - замены `IN (...)` на одиночное сравнение (N+1 вернулся бы);
    - снятия `status='selected'` (кнопка «Отменить участие» появилась бы у
      `pending`-заявителя, а «Присоединиться» — исчезла бы у наблюдателя);
    - добавления `applicant_id` в выборку (наружу ушёл бы чужой признак).
    """
    sql = _sql(
        JoinRequestRepository.build_viewer_participation_statement(
            applicant_id=APPLICANT_ID, card_ids=[1, 2, 3]
        )
    ).replace(" ", "")
    assert "SELECTjoin_requests.card_id" in sql
    assert "status='selected'" in sql
    assert "card_idIN(1,2,3)" in sql
    assert "applicant_id=7" in sql


def test_get_card_for_select_statement_slices_city_in_sql() -> None:
    """AC-04 (ADR-13): срез сообщества в SQL, а не в `if` после выборки.

    Краснеет, если убрать `city_slug`: заявка чужого сообщества стала бы
    отличима от несуществующей (`404` с другим кодом).
    """
    sql = _sql(
        JoinRequestRepository.build_get_card_for_select_statement(REQUEST_UUID, "pilot")
    ).replace(" ", "")
    assert "event_cards.city_slug='pilot'" in sql
    assert "join_requests.uuid=" in sql
