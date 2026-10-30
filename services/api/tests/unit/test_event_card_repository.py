"""Билдер запроса карточки автора (Task 2.4; AC-07).

§18.2: текст SQL проверяется без БД. Потеря `author_id` из условия —
реальный дефект: автор правил бы чужие карточки, а `403` вместо `404`
раскрыл бы их существование (ADR-13).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

import pytest

from app.repositories.event_cards import EventCardRepository

CARD_UUID = uuid_lib.UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


def test_get_owned_statement_checks_uuid_and_author() -> None:
    """AC-07: в предикате ДВА условия — `uuid` и `author_id`.

    Краснеет от любой из двух правок:
    - убрать `author_id` → автор получил бы чужую карточку и смог её править;
    - убрать `uuid` → запрос вернул бы произвольную карточку автора
      (первую по плану), и `PATCH` правил бы не ту.

    Сравнение по `whereclause`, а не по полному SQL: `select(EventCard)`
    выбирает все колонки, и `author_id` в SELECT-списке есть всегда —
    проверка полного текста была бы тавтологией (урок CR 4.1).
    """
    statement = EventCardRepository.build_get_owned_statement(7, CARD_UUID)
    where = str(statement.whereclause)

    assert "event_cards.uuid" in where
    assert "event_cards.author_id" in where


def test_get_owned_statement_has_no_ordering_or_limit() -> None:
    """AC-07: точечная выборка не сортирует и не ограничивает.

    Краснеет, если скопировать сюда `order_by`/`limit` из выдачи: сортировка
    одной строки бессмысленна, а `LIMIT 1` маскировал бы нарушение
    уникальности `uuid` (две строки с одним `uuid` вернули бы «первую»).
    """
    statement = EventCardRepository.build_get_owned_statement(7, CARD_UUID)
    sql = str(statement)

    assert "ORDER BY" not in sql
    assert "LIMIT" not in sql


@pytest.mark.parametrize("author_id", [1, 999])
def test_get_owned_statement_parameterizes_author(author_id: int) -> None:
    """AC-07: `author_id` — bind-параметр, а не литерал в тексте SQL.

    Краснеет, если значение склеить f-строкой: тогда в тексте появился бы
    литерал (`author_id = 999`), запрос нельзя переиспользовать, и открылась
    бы дорога к инъекции.

    Проверяется наличие ПЛЕЙСХОЛДЕРА `:author_id`, а не отсутствие цифры
    в тексте: `str(author_id) not in str(statement)` — тавтологичный ассерт,
    для `author_id=1` он падал на имени параметра `:author_id_1` (§18.2).
    """
    statement = EventCardRepository.build_get_owned_statement(author_id, CARD_UUID)
    compiled = statement.compile()

    assert author_id in compiled.params.values()
    assert ":author_id" in str(statement)


# --------------------------------------------------------------------------
# Стори 4.4: билдеры лимита и CAS (AC-01, AC-04, AC-06)
# --------------------------------------------------------------------------


def test_author_lock_uses_for_update_not_card_table() -> None:
    """AC-04 (ADR-09, мутация 1): блокировка `users`, не `event_cards`.

    Краснеет, если заменить таблицу: `FOR UPDATE` на карточке взял бы
    блокировку `cards` ДО `users` и нарушил порядок ADR-09 (дедлок с путями,
    которые блокируют `users` первыми — лайки, фото).
    """
    sql = str(EventCardRepository.build_author_lock_statement(7))

    assert "FOR UPDATE" in sql
    assert "FROM users" in sql
    assert "event_cards" not in sql


def test_active_count_has_no_city_slice() -> None:
    """AC-01: лимит — персональный, срез сообщества в подсчёт не входит.

    Краснеет, если добавить `city_slug`: автор, сменивший сообщество,
    получил бы новый пустой счётчик и обошёл лимит (перенос места между
    сообществами запрещён, но публикация — нет; R-05 касается `PlaceUpdate`).
    """
    where = str(EventCardRepository.build_active_count_statement(7).whereclause)

    assert "city_slug" not in where
    assert "event_cards.author_id" in where


def test_publish_cas_is_update_not_select() -> None:
    """AC-06 (X-21): CAS — `UPDATE`, а не `SELECT ... FOR UPDATE`.

    Краснеет, если вернуть `select(EventCard).with_for_update()`: явная
    блокировка строки карточки встала бы в порядке `cards → users`, что
    запрещено ADR-09. Неявная блокировка от `UPDATE` встаёт уже после `users`.
    """
    statement = EventCardRepository.build_publish_cas_statement(
        card_id=11, author_id=7, now=NOW, expires_at=NOW
    )
    sql = str(statement)

    assert sql.startswith("UPDATE event_cards")
    assert "FOR UPDATE" not in sql


class _FakeResult:
    """Минимальный `Result`: репозиторию нужен только `scalar_one()`."""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _FakeSession:
    """Сессия без БД: запоминает statement, возвращает заданное число.

    Проверяется ФАКТ запроса к БД, а не форма вызова: `count_overdue_windows`
    мог бы вернуть `0` литералом, и интеграционный тест заметил бы это только
    на живой PostgreSQL (а без `DATABASE_URL` он скипается — урок 11.4).
    """

    def __init__(self, value: int) -> None:
        self.value = value
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _FakeResult:
        self.statements.append(statement)
        return _FakeResult(self.value)


async def test_count_overdue_windows_executes_query_and_returns_scalar() -> None:
    """AC-3 стори 11.8: счётчик обязан СПРОСИТЬ БД и вернуть её ответ.

    Краснеет на мутации M-11.8c (`return 0` литералом вместо запроса):
    в журнале сессии не окажется statement, а вернулось бы 0 вместо
    подставленного 7. Без этого теста мутация выживала — wiring-тест
    патчит сам метод и его тело вне зоны видимости.
    """
    session = _FakeSession(value=7)

    result = await EventCardRepository(session).count_overdue_windows(now=NOW)  # type: ignore[arg-type]

    assert result == 7
    assert len(session.statements) == 1


async def test_count_overdue_windows_predicate_matches_adr14() -> None:
    """AC-3: предикат ADR-14 — `status='published'` И `expires_at < now - 5 мин`.

    Краснеет, если потерять запас (`grace_minutes`) или условие статуса:
    без запаса контроль поднимал бы ложный алерт на каждом прогоне, без
    статуса — считал бы законно закрытые окна (инцидент на здоровом воркере).
    """
    from datetime import timedelta  # noqa: PLC0415 — локальный импорт для ясности теста

    session = _FakeSession(value=0)
    await EventCardRepository(session).count_overdue_windows(now=NOW)  # type: ignore[arg-type]

    sql = str(session.statements[0])  # type: ignore[attr-defined]
    # CR 11.8, M-11.8-i: условие статуса обязано быть в WHERE. Без этого
    # ассерта выпадение фильтра `status='published'` оставляло тест зелёным —
    # контроль считал бы законно закрытые окна и поднимал ложный алерт.
    assert "event_cards.status" in sql, f"в запросе нет условия статуса: {sql}"

    params = session.statements[0].compile().params  # type: ignore[attr-defined]
    bound = [v for v in params.values() if isinstance(v, datetime)]
    assert bound, f"в запросе нет привязанного времени: {params}"
    assert NOW - timedelta(minutes=5) in bound, (
        f"в запросе не запас ADR-14 (NOW - 5 мин): {bound}"
    )
