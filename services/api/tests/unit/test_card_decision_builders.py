"""Текст SQL-билдеров автовыбора (стори 5б.3; AC-01, AC-03, AC-05).

§18.2: ассерт обязан падать от реальной правки. Форма этих двух запросов
НЕ наблюдаема через HTTP на подготовленных данных: отсутствие `FOR UPDATE`
отличимо только по тексту, а порядок `submitted_at, id` — только на равных
`submitted_at`, что в интеграционном тесте требует 20 прогонов.

§18.3: сами гонки — в `tests/integration/test_close_due_cards.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository

CARD_ID = 42
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
LIMIT = 200


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами.

    `dialect=postgresql.dialect()` обязателен: диалект по умолчанию рендерит
    `SELECT` иначе, и тест проверял бы не тот SQL, который уйдёт в БД
    (тот же довод, что в `test_select_builders`).
    """
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# ——— build_due_decision_ids_statement (AC-01, AC-03) ———


def test_due_decision_ids_selects_author_and_card() -> None:
    """AC-01: ядру нужны ОБА ключа — `id` карточки и `author_id`.

    Краснеет от:
    - удаления `author_id` из состава (ядро не смогло бы захватить строку
      автора ПЕРВОЙ, и порядок блокировок ADR-09 сместился бы);
    - замены `id` на `uuid` (ядро работает с внутренними ключами, D-2).
    """
    sql = _sql(EventCardRepository.build_due_decision_ids_statement(now=NOW, limit=LIMIT))
    assert "event_cards.id" in sql
    assert "event_cards.author_id" in sql


def test_due_decision_ids_predicate_matches_due_window() -> None:
    """AC-01, AC-03: предикат — ровно `published` + `expires_at <= now`.

    Краснеет от:
    - снятия `status = 'published'` (ядро трогало бы `draft`/`full`/`closed`);
    - снятия `expires_at <= ...` (автовыбор сработал бы на открытом окне —
      AC-03 требует 0 изменений);
    - замены `<=` на `<` (карточка ровно в момент истечения зависла бы
      навсегда: воркер берёт `expires_at <= now`, и `<` разошёлся бы с CAS).
    """
    sql = _sql(EventCardRepository.build_due_decision_ids_statement(now=NOW, limit=LIMIT))
    compact = sql.replace(" ", "")
    assert "event_cards.status='published'" in compact
    assert "event_cards.expires_at<=" in compact


def test_due_decision_ids_orders_and_limits() -> None:
    """AC-01: порядок `expires_at, id` и `LIMIT` — воркер обязан возвращать управление.

    Краснеет от снятия `LIMIT` (тысячи карточек в одном прогоне держали бы
    память и блокировки) и от снятия второго ключа `id` (нестабильный
    порядок при равных `expires_at`).
    """
    sql = _sql(EventCardRepository.build_due_decision_ids_statement(now=NOW, limit=LIMIT))
    compact = sql.replace(" ", "").replace("\n", "")
    assert "ORDERBYevent_cards.expires_at,event_cards.id" in compact
    assert "LIMIT" in sql
    assert f"LIMIT {LIMIT}" in sql.replace("\n", " ")


def test_due_decision_ids_has_no_for_update() -> None:
    """AC-07, D-5b.3-2: кандидаты БЕЗ `FOR UPDATE`.

    Краснеет от добавления `with_for_update(...)` в билдер: блокировка
    карточки ДО строки автора даёт встречную очередь с `join_open_card`
    и дедлок (урок ER 5а №3). Точка сериализации — `lock_for_update(author_id)`.
    """
    sql = _sql(EventCardRepository.build_due_decision_ids_statement(now=NOW, limit=LIMIT))
    assert "FOR UPDATE" not in sql.upper()
    assert "SKIP LOCKED" not in sql.upper()


def test_due_decision_ids_rejects_zero_limit() -> None:
    """AC-01: `limit < 1` — ошибка вызывающего, а не пустая выборка.

    Краснеет от снятия проверки: `LIMIT 0` дал бы «0 кандидатов» при живых
    просроченных окнах — молчаливый простой NFR21.
    """
    try:
        EventCardRepository.build_due_decision_ids_statement(now=NOW, limit=0)
    except ValueError:
        return
    raise AssertionError("limit=0 должен поднимать ValueError")


# ——— build_first_pending_statement (AC-01, AC-05) ———


def test_first_pending_orders_by_submitted_at_then_id() -> None:
    """AC-01, AC-05 (ADR-05): `ORDER BY submitted_at ASC, id ASC` — очередь дословно.

    Краснеет от:
    - снятия `id` (равные `submitted_at` → выбор зависел бы от физического
      порядка строк, AC-05 требует детерминизм 20 прогонов);
    - смены `ASC` на `DESC` (выбрался бы ПОСЛЕДНИЙ заявившийся — M2);
    - снятия `status='pending'` (в выборку попали бы `selected`/`cancelled`).
    """
    sql = _sql(JoinRequestRepository.build_first_pending_statement(card_id=CARD_ID))
    compact = sql.replace(" ", "").replace("\n", "")
    assert "join_requests.status='pending'" in compact
    assert "join_requests.card_id=42" in compact
    assert "ORDERBYjoin_requests.submitted_at,join_requests.id" in compact
    assert "DESC" not in sql.upper()


def test_first_pending_limits_to_one() -> None:
    """AC-01: `LIMIT 1` — ядру нужна ровно одна заявка.

    Краснеет от снятия `LIMIT`: запрос вернул бы всю очередь, и ядро взяло бы
    `first()` — тот же результат по значению, но N строк из БД вместо одной
    (NFR47) и скрытая зависимость от порядка применения `LIMIT`.
    """
    sql = _sql(JoinRequestRepository.build_first_pending_statement(card_id=CARD_ID))
    assert "LIMIT 1" in sql.replace("\n", " ")


def test_first_pending_has_no_for_update() -> None:
    """AC-07, D-5b.3-2 (OQ-5b.3-1): заявка БЕЗ `FOR UPDATE SKIP LOCKED`.

    Краснеет от добавления `with_for_update(skip_locked=True)`: `SKIP LOCKED`
    не сериализует выбор (два процесса прошли бы `SELECT` и оба заняли
    место), а порядок блокировок `users -> event_cards -> join_requests`
    требует, чтобы заявка читалась УЖЕ после строки автора.
    """
    sql = _sql(JoinRequestRepository.build_first_pending_statement(card_id=CARD_ID))
    assert "FOR UPDATE" not in sql.upper()
    assert "SKIP LOCKED" not in sql.upper()


def test_first_pending_selects_applicant_id_for_verification() -> None:
    """AC-01, AC-08: `applicant_id` в выборке — только для проверок, не наружу.

    Краснеет от удаления `applicant_id`: тесты состава не смогли бы отличить
    выбранного от проигравшего, не делая второй запрос по `id`. Наружу
    значение не выходит — его не возвращает `close_due_card`.
    """
    sql = _sql(JoinRequestRepository.build_first_pending_statement(card_id=CARD_ID))
    assert "join_requests.id" in sql
    assert "join_requests.applicant_id" in sql
