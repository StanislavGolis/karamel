"""Текст билдера состава компании (стори 6.3; AC-02, AC-03, AC-04).

§18.2: проверяется наблюдаемое свойство SQL (предикат статуса, порядок,
состав выборки), а не факт объявления метода. Гард доступа и анти-оракул
доказываются интеграционными тестами, здесь — форма запроса.

Гарда участия в этом файле нет намеренно: сервис переиспользует уже
существующий признак `CardFeedItem.viewer_participates` (5б.2,
D-5b.2-7), а не заводит второй предикат `selected` — две копии одного
правила разошлись бы при добавлении статуса.
"""

from __future__ import annotations

from app.repositories.chats import ChatRepository

CARD_ID = 42


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами (приём 4.1)."""
    return str(
        statement.compile(  # type: ignore[attr-defined]
            compile_kwargs={"literal_binds": True}
        )
    ).lower()


# ——— U1: предикат состава положительный ———


def test_participants_statement_selects_only_selected() -> None:
    """AC-02, D-6.3-2: состав — `status = 'selected'`, не инверсия.

    Краснеет от мутации M1 (`status <> 'not_selected'`): инверсия впустила бы
    `pending`-заявителя в состав компании, которой ещё нет (FR30, FR81).
    """
    sql = _sql(ChatRepository.build_participants_statement(card_id=CARD_ID, city_slug="tomsk"))

    assert "join_requests.status = 'selected'" in sql
    assert "not_selected" not in sql
    assert "<>" not in sql
    assert "!=" not in sql


# ——— U2: порядок состава ———


def test_participants_statement_orders_by_submitted_at_then_id() -> None:
    """AC-03, ADR-05: порядок `submitted_at ASC, id ASC`.

    Краснеет от мутации M3 (снять `ORDER BY`): состав отдавался бы в
    физическом порядке строк, и два запроса давали бы разные списки.
    """
    sql = _sql(ChatRepository.build_participants_statement(card_id=CARD_ID, city_slug="tomsk"))

    assert "order by" in sql
    order_clause = sql.split("order by", 1)[1]
    assert "submitted_at" in order_clause
    assert "join_requests.id" in order_clause
    assert order_clause.index("submitted_at") < order_clause.index("join_requests.id")


# ——— U3: состав не хранится ———


def test_participants_statement_has_no_stored_membership_columns() -> None:
    """AC-04, ADR-04: колонок участников в схеме нет — их нет и в запросе.

    Краснеет, если состав начнут читать из денормализованной колонки: она
    разошлась бы с `join_requests` при отмене участия (5а.6).
    """
    sql = _sql(ChatRepository.build_participants_statement(card_id=CARD_ID, city_slug="tomsk"))

    for forbidden in ("user_a_id", "user_b_id", "participant_id", "members"):
        assert forbidden not in sql, forbidden


# ——— U4: срез сообщества — второй рубеж ADR-13 ———


def test_participants_statement_slices_city() -> None:
    """AC-02, ADR-13: срез сообщества живёт в SQL.

    Второй рубеж на случай ошибки сервиса: без среза состав чужой карточки
    читался бы по прямому `uuid`.
    """
    sql = _sql(ChatRepository.build_participants_statement(card_id=CARD_ID, city_slug="tomsk"))

    assert "event_cards.city_slug" in sql
    assert "'tomsk'" in sql


# ——— U5: состав сворачивается в профиль, а не в идентификаторы ———


def test_participants_statement_joins_profile_left() -> None:
    """AC-01: анкета — LEFT JOIN (онбординг может быть не пройден).

    Краснеет от INNER JOIN: участник без анкеты выпал бы из состава, и
    компания «потеряла» бы человека, который в ней реально состоит.
    """
    sql = _sql(ChatRepository.build_participants_statement(card_id=CARD_ID, city_slug="tomsk"))

    assert "left outer join profiles" in sql
    assert "join users" in sql
    for exposed in ("participant_uuid", "participant_name", "participant_age"):
        assert exposed in sql
