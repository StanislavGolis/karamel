"""Текст CAS-билдеров отмены участия (стори 5а.6; AC-01, AC-02, AC-05, AC-11).

§18.2: ассерт обязан падать от реальной правки. Часть условий НЕ наблюдаема
через HTTP на подготовленных данных: `status IN ('published','full')`
отличим от его отсутствия только на закрытой карточке, а `filled_count > 0` —
только при счётчике 0, который штатно не встречается вместе с участием.
Без этих тестов снятие условия осталось бы незамеченным до инцидента.

§18.3 (гонки): текст проверяется здесь, сами гонки — в
`tests/integration/test_card_leave_races.py` на двух соединениях.
"""

from __future__ import annotations

from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository

CARD_ID = 42
APPLICANT_ID = 7


def _sql(statement: object) -> str:
    """Текст запроса с параметрами, подставленными для читаемости.

    `dialect=postgresql.dialect()` обязателен: диалект по умолчанию иначе
    рендерит `UPDATE` без части деталей, и тест проверял бы не тот SQL,
    который уйдёт в БД (тот же довод, что в `test_card_state_builders`).
    """
    from sqlalchemy.dialects import postgresql

    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_release_seat_statement_sets_published_and_decrements_in_one_update() -> None:
    """AC-01, AC-02 (FR104, ADR-10): место и `published` — ОДНИМ `UPDATE`.

    Краснеет от:
    - снятия `CASE` (вернулся бы двухшаговый путь: `full` остался бы
      с освободившимся местом → `500` от `ck_event_cards_full_capacity`);
    - замены `filled_count - 1 < capacity` на `filled_count < capacity`
      (статус не вернулся бы в `published` никогда);
    - снятия `filled_count > 0` (уход в отрицательное значение, NFR47);
    - снятия `status IN ('published','full')` (возврат в ленту закрытой
      карточки, AC-11);
    - потери `RETURNING` (ответ собирался бы вторым чтением).
    """
    sql = _sql(EventCardRepository.build_release_seat_statement(card_id=CARD_ID))

    assert "update" in sql.lower()
    assert "filled_count" in sql
    assert "case" in sql.lower()
    # Декремент, а не инкремент: `- 1` обязан быть в SET.
    assert "filled_count - " in sql or "filled_count-" in sql or "filled_count -" in sql
    # Возврат в ленту: `published` присваивается `CASE`.
    assert "published" in sql
    # Гард от отрицательного значения — единственный на этом пути.
    assert "filled_count >" in sql or "filled_count>" in sql
    # Закрытая карточка не возвращается в ленту (AC-11).
    assert "full" in sql
    # Ответ из `RETURNING`: второй `SELECT` прочитал бы чужую версию.
    assert "returning" in sql.lower()


def test_release_seat_statement_does_not_touch_card_of_other_community() -> None:
    """AC-08 (ADR-13): срез сообщества живёт в чтении, а не в `UPDATE`.

    Тест-инвариант: `build_release_seat_statement` работает по внутреннему
    `id` и НЕ должен обзавестись `community_slug` — иначе вторая копия
    среза разошлась бы с `get_card_for_join` при правке (класс дефекта 3.3).
    """
    sql = _sql(EventCardRepository.build_release_seat_statement(card_id=CARD_ID))

    assert "community_slug" not in sql
    assert "author_id" not in sql


def test_cancel_participation_statement_targets_selected_only() -> None:
    """AC-01, AC-05, AC-07 (FR104): CAS отменяет ТОЛЬКО `selected`.

    Краснеет от:
    - снятия `status = 'selected'` (маршрут участия отменял бы `pending`-заявку,
      и клиент терял бы место в очереди, D-5a.6-5);
    - снятия `decided_at` (аудит потерял бы момент выхода, NFR1–NFR4);
    - замены `card_id`/`applicant_id` в предикате на `if` в сервисе (§18.3).
    """
    sql = _sql(
        JoinRequestRepository.build_cancel_participation_statement(
            card_id=CARD_ID, applicant_id=APPLICANT_ID
        )
    )

    assert "update" in sql.lower()
    assert "selected" in sql
    assert "cancelled" in sql
    assert "decided_at" in sql
    # Пара «карточка + участник» в предикате, а не в `if` после выборки.
    assert "card_id" in sql
    assert "applicant_id" in sql


def test_participation_status_statement_has_no_status_filter() -> None:
    """AC-05, AC-06: диагностика читает ЛЮБОЙ статус пары.

    Краснеет от добавления `status = 'selected'` в предикат диагностики:
    тогда после неуспешного CAS `selected` не нашёлся бы (он уже `cancelled`),
    и идемпотентный повтор отдавал бы `404` вместо `204` (AC-06).
    """
    sql = _sql(
        JoinRequestRepository.build_participation_status_statement(
            card_id=CARD_ID, applicant_id=APPLICANT_ID
        )
    )

    assert "select" in sql.lower()
    assert "card_id" in sql
    assert "applicant_id" in sql
    assert "status =" not in sql or "status=" not in sql
