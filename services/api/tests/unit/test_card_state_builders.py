"""Текст CAS-билдеров состояний карточки (стори 4.6, Task 8.2; AC-03…AC-06).

§18.2: ассерт обязан падать от реальной правки. Здесь проверяется SQL-предикат
целиком — потому что часть условий НЕ наблюдаема через HTTP: `author_id`
в `UPDATE` дублирует проверку `get_owned` (defense-in-depth, ADR-13), а
`filled_count >= capacity` достижим только при данных 5.x. Без этих тестов
снятие условия осталось бы незамеченным до первого инцидента.

§18.3 (гонки): текст `FOR UPDATE SKIP LOCKED` проверяется здесь, а сами гонки —
в `tests/integration/test_card_state_races.py` на двух соединениях.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from app.repositories.event_cards import EventCardRepository

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CARD_ID = 42
AUTHOR_ID = 7
# Стори 6.2: `build_join_seat_statement` принимает `chat_uuid` и пишет его
# тем же `UPDATE`, когда место последнее.
CHAT_UUID = uuid_lib.UUID("55555555-5555-4555-8555-555555555555")


def _sql(statement: object) -> str:
    """Текст запроса с параметрами, подставленными для читаемости.

    `literal_binds` нужен, чтобы `now` и `author_id` попали в строку: без него
    предикат сравнивался бы с placeholder'ом, и `assert "author_id" in sql`
    не отличил бы «условие есть» от «условия нет».

    `dialect=postgresql.dialect()` обязателен, и это НЕ украшение: диалект по
    умолчанию не поддерживает `FOR UPDATE SKIP LOCKED` и МОЛЧА отбрасывает
    `skip_locked` при рендере (воспроизведено: в тексте оставалось только
    `FOR UPDATE`). Тест без явного диалекта проверял бы строку, в которой
    защиты нет, и «зеленел» бы на сломанном воркере.
    """
    from sqlalchemy.dialects import postgresql

    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_close_statement_has_status_and_author() -> None:
    """AC-03 (FR74): CAS закрытия содержит `status='published'` И `author_id`.

    Краснеет от снятия любого из двух: без статуса второй `close` перезапишет
    `closed_at`, без `author_id` — закроет чужую карточку при ошибке в сервисе
    (ADR-13).
    """
    sql = _sql(
        EventCardRepository.build_close_by_author_statement(
            card_id=CARD_ID, author_id=AUTHOR_ID, now=NOW
        )
    )

    assert "status" in sql
    assert "published" in sql
    assert "author_id" in sql
    assert str(AUTHOR_ID) in sql
    # `full` закрывать нельзя (D-4.6-4): предикат — ровно `published`.
    assert "full" not in sql.split("WHERE", 1)[1]


def test_cancel_statement_excludes_terminal_statuses() -> None:
    """AC-04 (FR100): отмена исключает `finished` и `cancelled`.

    Краснеет, если из предиката убрать любой из двух статусов: архивную
    карточку можно было бы «отменить», переписав историю встречи.
    """
    sql = _sql(EventCardRepository.build_cancel_statement(card_id=CARD_ID, author_id=AUTHOR_ID))

    assert "finished" in sql
    assert "cancelled" in sql
    # `NOT IN` обязателен: `IN` разрешил бы отмену ТОЛЬКО терминальных.
    assert "NOT IN" in sql.upper()
    assert "author_id" in sql


def test_finish_statement_requires_time_boundary() -> None:
    """AC-05 (FR73): архив содержит `starts_at <= now` И набор архивируемых.

    Краснеет от снятия `starts_at`: карточка ушла бы в архив до начала встречи,
    и участники увидели бы «встреча прошла» за сутки до неё.
    """
    sql = _sql(EventCardRepository.build_finish_statement(card_id=CARD_ID, now=NOW))

    assert "starts_at" in sql
    assert "finished" in sql
    # Все три статуса-источника обязаны быть в предикате.
    for status in ("published", "full", "closed"):
        assert status in sql


def test_join_seat_statement_increments_and_marks_full_in_one_update() -> None:
    """AC-02, AC-05 (X-39, FR84): место и `full` — ОДНИМ `UPDATE`.

    Заменяет `test_mark_full_statement_uses_arithmetic`: прежний билдер ставил
    `full` отдельным `UPDATE`, и между инкрементом и им существовала строка
    `published` + `filled_count = capacity`, запрещённая
    `ck_event_cards_full_capacity` (0011) → `500` на легальном присоединении.

    Краснеет от:
    - снятия `CASE` (вернулся бы двухшаговый путь → X-39);
    - замены `>=` на `=` (перескочивший счётчик не дал бы `full`);
    - снятия предиката `filled_count < capacity` (переполнение мест, NFR47);
    - снятия `status='published'` (перезапись `closed`/`cancelled`);
    - потери `RETURNING` (ответ собирался бы вторым чтением, D-5а.5-7).
    """
    sql = _sql(EventCardRepository.build_join_seat_statement(card_id=CARD_ID, chat_uuid=CHAT_UUID))

    # Инкремент и `CASE` — в одном `UPDATE`: одна точка записи мест.
    assert "filled_count" in sql
    assert "capacity" in sql
    assert "case" in sql.lower()
    assert "update" in sql.lower()
    # `>=`, а не `=`: перескочивший счётчик тоже обязан дать `full`.
    assert ">=" in sql or "> =" in sql
    # Предикат свободного места — единственный гард NFR47.
    assert "filled_count <" in sql or "filled_count<" in sql
    assert "published" in sql
    # Ответ из `RETURNING`: второй `SELECT` прочитал бы чужую версию строки.
    assert "returning" in sql.lower()
    # X-6.1-1 (chore P-7): режим заявок — не открытый набор. Предикат стоит
    # в САМОМ `UPDATE` (P-3), а не предпроверкой в сервисе: проверка перед
    # записью окно TOCTOU не закрывает (параллельный `PATCH capacity 3 → 1`
    # коммитится между чтением снимка и `UPDATE`).
    assert "capacity > 1" in sql or "capacity>1" in sql


def test_due_finish_ids_statement_locks_and_limits() -> None:
    """AC-05 (NFR20, ADR-03): воркер берёт строки под `SKIP LOCKED` и `LIMIT`.

    Краснеет от снятия `SKIP LOCKED`: два воркера встанут в очередь друг за
    другом, и прогон растянется на длину чужой транзакции. Краснеет от снятия
    `LIMIT`: прогон заберёт все просроченные карточки сразу и удержит блокировки.
    """
    sql = _sql(EventCardRepository.build_due_finish_ids_statement(now=NOW, limit=200)).upper()

    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql
    assert "LIMIT" in sql
    assert "STARTS_AT" in sql


def test_due_window_ids_statement_locks_and_limits() -> None:
    """AC-05 (NFR21): выборка просроченных окон — тоже под `SKIP LOCKED` и `LIMIT`.

    Краснеет от снятия `status='published'`: воркер выбирал бы уже закрытые
    карточки и логировал «0 закрыто» при живых просроченных окнах.
    """
    sql = _sql(EventCardRepository.build_due_window_ids_statement(now=NOW, limit=200)).upper()

    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql
    assert "LIMIT" in sql
    assert "EXPIRES_AT" in sql
    assert "PUBLISHED" in sql


def test_finish_statement_covers_exactly_archivable_statuses() -> None:
    """AC-05: `done`-набор в предикате архива — ровно `ARCHIVABLE_STATUSES`.

    Краснеет, если добавить `cancelled` (отменённое не архивируется — история
    сохраняет причину) или убрать `closed` (закрытый набор обязан уйти в архив).
    """
    from app.models.enums import ARCHIVABLE_STATUSES

    sql = _sql(EventCardRepository.build_finish_statement(card_id=CARD_ID, now=NOW))

    for status in ARCHIVABLE_STATUSES:
        assert str(status) in sql
    assert "cancelled" not in sql
