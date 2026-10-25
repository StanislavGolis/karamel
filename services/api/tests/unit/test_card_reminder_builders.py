"""Текст SQL-билдеров напоминания (стори 5б.4; AC-01, AC-03, AC-04, AC-05).

§18.2: ассерт обязан падать от реальной правки. Форма этих запросов НЕ
наблюдаема через HTTP: отсутствие `FOR UPDATE` отличимо только по тексту,
а строгие границы окна (`> now`, `<= now + порог`) — только на точном
равенстве, которое интеграционный тест строит с точностью до микросекунды.

§18.3: сами гонки и поведение — в `tests/integration/test_decision_reminders.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from app.repositories.event_cards import EventCardRepository

CARD_ID = 42
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
LIMIT = 200
THRESHOLD = 15


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами.

    `dialect=postgresql.dialect()` обязателен: диалект по умолчанию рендерит
    `SELECT` иначе, и тест проверял бы не тот SQL, который уйдёт в БД
    (тот же довод, что в `test_card_decision_builders`).
    """
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# ——— build_due_reminder_ids_statement (AC-01, AC-03, AC-04, AC-05) ———


def test_reminder_candidates_filter_published_only() -> None:
    """AC-04: `full`/`closed`/`cancelled` напоминание не получают.

    Краснеет при снятии `status='published'`: автор, уже сделавший выбор,
    получил бы «успей выбрать» по закрытой карточке.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "event_cards.status = " in sql
    assert "'published'" in sql


def test_reminder_candidates_require_null_marker() -> None:
    """AC-03, ADR-15: маркер уже стоящий — не кандидат.

    Краснеет при снятии `decision_reminded_at IS NULL`: карточка попадала бы
    в выборку каждые 60 секунд и слала повторные напоминания.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "event_cards.decision_reminded_at IS NULL" in sql


def test_reminder_candidates_use_strict_left_bound() -> None:
    """AC-05: просроченная карточка напоминания НЕ получает.

    Левая граница СТРОГАЯ (`>`). Краснеет при замене на `>=`: карточка
    с `expires_at == now` (окно только что истекло) получила бы «успей
    выбрать», хотя решение уже принято автовыбором 5б.3.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "event_cards.expires_at > " in sql
    assert "event_cards.expires_at >= " not in sql


def test_reminder_candidates_use_inclusive_right_bound() -> None:
    """AC-01, D-5b.4-2: ровно за порог минут — уже напоминаем.

    Правая граница ВКЛЮЧИТЕЛЬНАЯ (`<=`). Краснеет при замене на `<`:
    карточка с `expires_at == now + 15 минут` не получила бы напоминание
    ни на этом прогоне, ни на следующем (окно уже закрыто автовыбором),
    то есть FR103 молча терялся бы на точной границе.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "event_cards.expires_at <= " in sql


def test_reminder_candidates_deadline_is_now_plus_threshold() -> None:
    """AC-02: порог приходит параметром, а не хардкодится в SQL.

    Краснеет, если порог вернуть константой или `make_interval`: тогда
    `CARD_REMINDER_THRESHOLD_MINUTES` перестал бы влиять на поведение,
    а тест со своим порогом молча проверял бы дефолт.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    expected = NOW.replace(tzinfo=None)  # literal_binds рендерит без таймзоны
    del expected
    # Порог 15 минут: `2026-10-24 12:15:00` — верхняя граница окна.
    assert "2026-10-24 12:15:00" in sql


def test_reminder_candidates_have_no_row_lock() -> None:
    """D-5b.4-3, ADR-09: напоминание ничего не блокирует.

    Краснеет при добавлении `FOR UPDATE`: блокировка карточки раньше `users`
    дала бы встречную очередь с `close_due_card`/`join_open_card` и дедлок.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "FOR UPDATE" not in sql.upper()


def test_reminder_candidates_are_ordered_and_limited() -> None:
    """Воркер обязан возвращать управление: порядок стабилен, размер ограничен.

    Краснеет при снятии `ORDER BY`: при равных `expires_at` порядок стал бы
    зависеть от плана запроса, и тесты/логи «какая карточка первая» поплыли.
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "ORDER BY event_cards.expires_at, event_cards.id" in sql
    assert f"LIMIT {LIMIT}" in sql


def test_reminder_candidates_do_not_join_users_or_requests() -> None:
    """D-5b.4-3: предикат живёт только в `event_cards`.

    Краснеет при добавлении `JOIN join_requests`/`JOIN users`: воркер начал бы
    читать чужие заявки на пути, где это не требуется, и безличность 5б.2
    оказалась бы под угрозой (лишний источник идентификаторов заявителей).
    """
    sql = _sql(
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=THRESHOLD, limit=LIMIT
        )
    )
    assert "join_requests" not in sql
    assert "FROM users" not in sql


def test_rejects_non_positive_threshold() -> None:
    """AC-02: порог меньше минуты — ошибка вызова, а не тихое поведение."""
    import pytest

    with pytest.raises(ValueError, match="threshold_minutes"):
        EventCardRepository.build_due_reminder_ids_statement(
            now=NOW, threshold_minutes=0, limit=LIMIT
        )


# ——— build_mark_reminded_statement (AC-01, AC-03) ———


def test_mark_reminded_is_cas_on_null_marker() -> None:
    """AC-03, ADR-15: маркер ставится CAS-ом, а не безусловным `UPDATE`.

    Краснеет при снятии `decision_reminded_at IS NULL` из `WHERE`: второй
    прогон перезаписывал бы маркер и считал себя успешным, а счётчик
    `reminded` рос бы при каждом цикле воркера.
    """
    sql = _sql(EventCardRepository.build_mark_reminded_statement(card_id=CARD_ID, now=NOW))
    assert "UPDATE event_cards SET decision_reminded_at=" in sql
    assert "decision_reminded_at IS NULL" in sql
    assert "RETURNING event_cards.uuid" in sql
    # CR 5б.4, патч 1: статус — часть предиката CAS. Без него маркер вставал
    # бы на карточку, закрытую между чтением состояния и `UPDATE`.
    assert "event_cards.status = " in sql
    assert "event_cards.status IS NULL" not in sql


def test_mark_reminded_returns_only_uuid() -> None:
    """AC-08: наружу (в лог) уходит `uuid`, а не `author_id`.

    Краснеет, если в `RETURNING` добавить `author_id`: безличность 5б.2
    запрещает выводить идентификаторы пользователей в события воркера.
    """
    sql = _sql(EventCardRepository.build_mark_reminded_statement(card_id=CARD_ID, now=NOW))
    assert "author_id" not in sql


# ——— build_reminder_state_statement (AC-04, AC-05) ———


def test_reminder_state_reads_reminded_marker() -> None:
    """D-5b.4-5: сервису нужен `decision_reminded_at`, которого нет в `decision_state`.

    Краснеет, если сервис начнёт переиспользовать `build_decision_state_statement`
    5б.3: там нет маркера, и проверка AC-03 в сервисе стала бы невозможной
    (пришлось бы полагаться только на CAS, теряя `SKIPPED` до `UPDATE`).
    """
    sql = _sql(EventCardRepository.build_reminder_state_statement(card_id=CARD_ID))
    assert "event_cards.decision_reminded_at" in sql
    assert "event_cards.expires_at" in sql
    assert "event_cards.status" in sql
    assert "FOR UPDATE" not in sql.upper()


def test_reminder_state_joins_users_for_author_uuid() -> None:
    """AC-01 9.4, D-9.4-5 (M3-класс): адрес автора приходит из ТОГО ЖЕ запроса.

    Краснеет, если из запроса убрать `JOIN users`: `publish_to_user` нужен
    публичный `user_uuid`, а в карточке лежит только внутренний `author_id`.
    Второй `SELECT` по автора отвергнут (D-9.4-5): между двумя чтениями автор
    мог смениться, и уведомление ушло бы не тому.

    Проверяются ОБА идентификатора: `author_id` нужен `create_one` (адресация
    строки), `users.uuid` — каналу `ws:user:{uuid}`. Отсутствие любого из них
    делает доставку FR110 невозможной, и падать это должно здесь, а не в проде.
    """
    sql = _sql(EventCardRepository.build_reminder_state_statement(card_id=CARD_ID))
    # Сравнение в ОДНОМ регистре: SQLAlchemy рендерит имена в нижнем, поэтому
    # `"JOIN users" in sql.upper()` не мог совпасть никогда (дефект первого
    # прогона: ассерт был ложным в обе стороны).
    upper = sql.upper()
    assert "JOIN USERS" in upper, sql
    assert "USERS.ID = EVENT_CARDS.AUTHOR_ID" in upper, sql
    assert "USERS.UUID" in upper, sql
    assert "EVENT_CARDS.AUTHOR_ID" in upper, sql
    assert "FOR UPDATE" not in upper
