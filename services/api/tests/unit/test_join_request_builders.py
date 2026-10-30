"""Текст SQL-билдеров заявок (стори 5а.2, Task 7.2; AC-03, AC-07, AC-09).

§18.2: ассерт обязан падать от реальной правки. Проверяется именно SQL, потому
что часть условий не наблюдаема через HTTP: `FOR UPDATE` на `users` виден
только по количеству вставок в гонке, а срез `city_slug` — по совпадению
тела `404` с несуществующей карточкой (косвенно).
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy.dialects import postgresql

from app.repositories.join_requests import JoinRequestRepository
from app.repositories.users import UserRepository

COMMUNITY = "tomsk"
CARD_UUID = uuid_lib.UUID("11111111-1111-1111-1111-111111111111")


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами и диалектом PostgreSQL.

    Без явного диалекта `FOR UPDATE` рендерится, но `skip_locked`/`of` теряются
    молча (приём `test_card_state_builders.py`).
    """
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_lock_statement_targets_users_with_for_update() -> None:
    """AC-09 (ADR-09): блокировка актора — `users` + `FOR UPDATE`.

    Краснеет, если снять `.with_for_update()`: гонка на двух карточках даст три
    вставки вместо двух (AC-08), и лимит «2 активных» перестанет выполняться.
    """
    sql = _sql(UserRepository.build_lock_statement(7))
    assert "FROM users" in sql
    assert "FOR UPDATE" in sql


def test_author_lock_statement_is_the_same_statement() -> None:
    """Task 2.2: 4.4 делегирует в тот же билдер — одна реализация блокировки.

    Краснеет, если вернуть в `event_cards.py` вторую копию `SELECT ... FOR UPDATE`:
    две реализации под одним порядком ADR-09 разошлись бы при правке и дали бы
    дедлок между публикацией и подачей заявки.
    """
    from app.repositories.event_cards import EventCardRepository

    assert _sql(EventCardRepository.build_author_lock_statement(7)) == _sql(
        UserRepository.build_lock_statement(7)
    )


def test_active_count_statement_counts_only_pending() -> None:
    """AC-03 (FR77, NFR29): в подсчёте лимита — только `pending`.

    Краснеет, если использовать `JOIN_REQUEST_OCCUPYING_STATUSES` (с `selected`):
    выбранный участник занимал бы слот лимита и не смог бы подать заявку на
    другую карточку.
    """
    sql = _sql(JoinRequestRepository.build_active_count_statement(7))
    assert "FROM join_requests" in sql
    assert "'pending'" in sql
    assert "'selected'" not in sql


def test_get_for_request_statement_has_city_slice_and_no_lock() -> None:
    """AC-07 + D-5a.2-3: срез сообщества есть, `FOR UPDATE` нет.

    Краснеет двумя правками: (1) убрать `city_slug` — карточка чужого
    сообщества стала бы доступна (утечка, ADR-13); (2) добавить
    `.with_for_update()` — 5а.2 начала бы блокировать `event_cards`, нарушив
    порядок ADR-09 и создав дедлок с 4.4.
    """
    sql = _sql(JoinRequestRepository.build_get_for_request_statement(CARD_UUID, COMMUNITY))
    assert "FROM event_cards" in sql
    assert "city_slug" in sql
    assert COMMUNITY in sql
    assert "FOR UPDATE" not in sql


def test_get_for_request_statement_has_no_joins() -> None:
    """AC-07: заявке хватает колонок `event_cards`.

    Краснеет, если добавить `JOIN place/profile`: ответ `201` не отдаёт ни
    место, ни автора (D-5a.2-9), и JOIN удорожал бы каждую подачу.
    """
    sql = _sql(JoinRequestRepository.build_get_for_request_statement(CARD_UUID, COMMUNITY))
    assert "JOIN" not in sql.upper()
