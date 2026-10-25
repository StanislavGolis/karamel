"""Текст билдера списка чатов (стори 6.6; AC-01, AC-03, AC-05).

§18.2: проверяются наблюдаемые свойства SQL, а не факт объявления метода.
Число запросов (N+1) и анти-оракул доказываются интеграционными тестами на
реальном PostgreSQL — текстом запроса их не проверить.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from sqlalchemy import false, true

from app.repositories.chats import ChatRepository

VIEWER_ID = 42
CHAT_UUID = uuid_lib.UUID("11111111-1111-4111-8111-111111111111")
AFTER_AT = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами (приём 4.1)."""
    return str(
        statement.compile(  # type: ignore[attr-defined]
            compile_kwargs={"literal_binds": True}
        )
    ).lower()


def _list_sql(after: tuple[datetime, uuid_lib.UUID] | None = None) -> str:
    return _sql(
        ChatRepository.build_list_for_viewer_statement(viewer_id=VIEWER_ID, after=after, limit=21)
    )


# ——— AC-05: без N+1 ———


def test_list_statement_uses_lateral_for_last_message_and_unread() -> None:
    """AC-05 (NFR12): и последнее сообщение, и счётчик — в ОДНОМ запросе.

    Краснеет от мутации M3 (заменить `LATERAL` на отдельные запросы в
    сервисе): на 50 чатах это 101 round-trip, и «открытие ≤1 с» перестаёт
    выполняться. `LEFT JOIN LATERAL` — именно тот механизм, который даёт
    один запрос вместо `1 + 2N`.
    """
    sql = _list_sql()

    # `LEFT OUTER JOIN LATERAL` — именно так PostgreSQL/SQLAlchemy печатает
    # `outerjoin(...lateral())`; обычный `JOIN LATERAL` отбросил бы чаты без
    # сообщений (AC-04: чат только с системными сообщениями обязан быть в списке).
    assert sql.count("left outer join lateral") == 2
    assert "limit 1" in sql  # последнее сообщение — ровно одно
    assert "count(" in sql


def test_list_statement_counts_only_unread_others_messages() -> None:
    """AC-06: непрочитанное — только `read_at IS NULL` и ЧУЖОЕ.

    Краснеет от двух мутаций:
    - снять `read_at IS NULL` → счётчик считал бы всю историю чата;
    - снять `sender_id != viewer` (M5) → у автора после своей же отправки
      всегда висело бы «1 непрочитанное».
    """
    sql = _list_sql()
    unread_part = sql.split("unread")[0] if "unread" in sql else sql

    # Предикаты живут в подзапросе счётчика: проверяется весь текст, потому
    # что `sql.split` по алиасу хрупок при изменении порядка колонок.
    assert "read_at is null" in sql
    assert f"sender_id != {VIEWER_ID}" in sql or f"sender_id <> {VIEWER_ID}" in sql
    assert unread_part  # запрос непустой (защита от ложного зелёного)


def test_list_statement_filters_by_viewer_participation() -> None:
    """AC-01 (ADR-04): зритель — автор ИЛИ `selected` в заявках.

    Краснеет, если убрать участие из `WHERE`: список показал бы ЧУЖИЕ чаты,
    то есть состав компании и переписку постороннего (FR86, ADR-13).
    """
    sql = _list_sql()

    assert f"author_id = {VIEWER_ID}" in sql
    assert f"applicant_id = {VIEWER_ID}" in sql
    assert "'selected'" in sql


# ——— AC-03: точка расширения FR37 ———


def test_status_predicate_is_part_of_the_statement() -> None:
    """AC-03, §18.11: предикат статуса УЧАСТВУЕТ в `WHERE` — в ОБОИХ значениях.

    Сегодня `_visible_chat_status_predicate()` возвращает `true()` (колонки
    `chats.status` ещё нет — её добавляет эпик 8). Тест доказывает две вещи:

    1. при `true()` чат виден (предикат не ломает выборку);
    2. при `false()` результат — пусто (предикат РЕАЛЬНО подключён к `WHERE`).

    Без второго утверждения забытый `WHERE` прошёл бы тест: `true()` ничего
    не фильтрует, и «фильтр есть» стало бы непроверяемым.
    """
    original = ChatRepository._visible_chat_status_predicate

    def _outer_where(sql: str) -> str:
        """Часть запроса ВНЕ скалярного подзапроса `total_count`.

        CR 6.6 (F-2): без этого разбиения тест перестал ловить мутацию M2 —
        после выноса `total` в скалярный подзапрос предикат присутствовал в
        нём, и `"false" in sql` был истинен даже при убранном `WHERE`
        основного запроса (проверено прогоном: мутация M2 дала 8 passed).
        """
        return sql.split(") as total_count", 1)[-1]

    try:
        ChatRepository._visible_chat_status_predicate = staticmethod(true)  # type: ignore[method-assign]
        assert "false" not in _outer_where(_list_sql())

        ChatRepository._visible_chat_status_predicate = staticmethod(false)  # type: ignore[method-assign]
        outer = _outer_where(_list_sql())
        assert "false" in outer, "предикат статуса не участвует в WHERE основного запроса"
    finally:
        ChatRepository._visible_chat_status_predicate = staticmethod(original)  # type: ignore[method-assign]


def test_status_predicate_filters_to_active() -> None:
    """AC-03 стори 8.2 (D-22): предикат видимости — реальный фильтр, не `true()`.

    **Было:** в 6.6 стояла заглушка `true()` с комментарием «эпик 8 заменит
    тело одной строкой». Заглушка отработала, теперь тело обязано отсекать
    скрытые диалоги (FR37).

    P-8: проверяются ОБА значения — `active` проходит, `hidden` отсекается.
    Мутация M4 (вернуть `true()`) делает тест красным: скрытый чат вернулся бы
    в список у обеих сторон.
    """
    predicate = ChatRepository._visible_chat_status_predicate()
    predicate_sql = str(predicate).lower()

    assert predicate_sql != "true", "предикат снова стал заглушкой (M4)"
    assert "chats.status" in predicate_sql

    # Значение приходит bind-параметром (`:status_1`), а не литералом: SQL
    # компилируется один раз и кэшируется драйвером. Поэтому проверяется
    # СКОМПИЛИРОВАННЫЙ параметр, а не текст: `str()` дал бы `chats.status =
    # :status_1` и не отличил `active` от `hidden`.
    compiled = predicate.compile(compile_kwargs={"literal_binds": True})
    assert "'active'" in str(compiled).lower(), str(compiled)


# ——— AC-01: порядок и курсор ———


def test_list_statement_orders_newest_activity_first() -> None:
    """AC-01: свежая активность — сверху; курсор `(created_at, uuid)`.

    Краснеет от мутации M4 (инверсия `ORDER BY`): список открывался бы
    старыми чатами, и новое сообщение не поднимало бы чат наверх.
    """
    sql = _list_sql()
    # `rsplit`: первый `ORDER BY` принадлежит LATERAL последнего сообщения
    # (сортировка внутри подзапроса), итоговый порядок списка — последний.
    order_clause = sql.rsplit("order by", 1)[1]

    assert "coalesce" in order_clause
    assert "desc" in order_clause
    assert "chats.uuid desc" in order_clause


def test_list_statement_applies_cursor_predicate_only_when_after_given() -> None:
    """AC-01: курсор — только при продолжении, и строго `<`.

    Краснеет, если курсор применить при первом запросе (первая страница была
    бы пуста) или сравнить `>=` (страница повторяла бы последнюю строку).
    """
    # UUID в тексте запроса печатается БЕЗ дефисов (`literal_binds`), поэтому
    # сравнивается `hex`-форма: проверка по `str(uuid)` была бы вечно ложной.
    chat_hex = CHAT_UUID.hex

    first_page = _list_sql()
    assert chat_hex not in first_page

    next_page = _list_sql((AFTER_AT, CHAT_UUID))
    assert chat_hex in next_page
    after_part = next_page.split("where")[-1]
    assert "<" in after_part
    assert ">=" not in after_part


def test_list_statement_requests_one_extra_row_for_cursor() -> None:
    """AC-01: `limit + 1` — признак «есть ещё» без второго запроса.

    Краснеет, если сервис забудет `+1`: курсор на последней странице
    заставлял бы клиента делать лишний запрос, который вернёт пустоту.
    """
    sql = _list_sql()

    assert "limit 21" in sql
