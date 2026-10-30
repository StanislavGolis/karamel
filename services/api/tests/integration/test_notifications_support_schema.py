"""Схема `notifications` (расширение) и `support_tickets` (стори 9.1; AC-01…AC-03).

§18.1: каждое ограничение проверяется НАРУШЕНИЕМ, а не чтением каталога.
Наличие `CHECK` в `pg_constraint` не доказывает, что он что-то запрещает:
формулировка `answer IS NULL OR answer IS NOT NULL` тоже была бы строкой в
каталоге и пропускала бы всё. Каталог используется только для ИМЁН (`op.f`,
§18.7), состава индексов и `confdeltype` (его нарушением не проверишь —
удаление пользователя в тесте недопустимо дорого).

§18.8: каждый ожидаемый отказ — в своём `begin_nested()`: после
`IntegrityError` транзакция аборчена, и следующая проверка была бы про её
состояние, а не про ограничение.

**Про «через сервисный метод, а не прямым INSERT».** В стори 9.1 сервисного
слоя нет: 9.5/9.6 (API обращений) ещё не написаны, а путь записи уведомлений
7.3 — сырой `INSERT ... SELECT` в `NotificationsRepository`, не ORM. Прямой
INSERT здесь проверяет РОВНО ТО ЖЕ, что делает прод-код, — это не обход
сервиса, а его воспроизведение. Отклонение зафиксировано в Dev Agent Record.
"""

from __future__ import annotations

import os
import re
import uuid as uuid_lib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NOTIFICATION_TYPES, TICKET_STATUSES
from app.models.support_ticket import (
    ANSWER_ORDER_SQL,
    ANSWER_PAIR_SQL,
    BODY_NOT_BLANK_SQL,
    _in_clause,
)
from app.schemas.ws import TYPE_PATTERN

#: Корень сервиса: нужен для загрузки литерала миграции `0030` (папка
#: `alembic/versions` не является пакетом, поэтому `import` по имени невозможен).
API_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: AC-01: точный состав колонок. РАВЕНСТВО, а не подмножество: лишняя
#: колонка (например `subject` из архитектуры v1) обязана ронять тест, иначе
#: стори «без `subject`» (D-9.1-7) ничем не подтверждена.
NOTIFICATIONS_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "user_id": ("bigint", "NO"),
    "type": ("character varying", "NO"),
    "payload": ("jsonb", "NO"),
    "created_at": ("timestamp with time zone", "NO"),
    "delivered_at": ("timestamp with time zone", "YES"),
    "read_at": ("timestamp with time zone", "YES"),
}

SUPPORT_TICKETS_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("bigint", "NO"),
    "uuid": ("uuid", "NO"),
    "user_id": ("bigint", "NO"),
    "body": ("text", "NO"),
    "status": ("character varying", "NO"),
    "answer": ("text", "YES"),
    "answered_at": ("timestamp with time zone", "YES"),
    "answered_by_id": ("bigint", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
}

_INSERT_USER = (
    "INSERT INTO users (provider, provider_user_id, phone, role, gender, status, "
    "city_slug) "
    "VALUES ('phone', :p, :p, 'user', 'female', 'active', 'tomsk') RETURNING id"
)

_INSERT_NOTIFICATION = (
    "INSERT INTO notifications (user_id, type, payload) "
    "VALUES (:u, :t, CAST('{}' AS jsonb)) RETURNING id"
)

_INSERT_TICKET = "INSERT INTO support_tickets (user_id, body) VALUES (:u, :b) RETURNING id"


async def _create_user(session: AsyncSession) -> int:
    result = await session.execute(
        text(_INSERT_USER), {"p": f"+7911{uuid_lib.uuid4().int % 10**7:07d}"}
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# AC-01: состав колонок (M1: убрать uuid; M2 проверяется отдельно)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifications_columns_exact(session: AsyncSession) -> None:
    """M1: снятие `uuid` из миграции роняет этот тест.

    Мутация «добавить `subject`» тоже краснеет — равенство, не подмножество.
    """
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            " WHERE table_name = 'notifications' AND table_schema = current_schema()"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}
    assert actual == NOTIFICATIONS_COLUMNS


@pytest.mark.asyncio
async def test_support_tickets_columns_exact(session: AsyncSession) -> None:
    """M13-контекст: состав колонок обращения (AC-01 + D-9.1-6/7)."""
    result = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            " WHERE table_name = 'support_tickets' AND table_schema = current_schema()"
        )
    )
    actual = {row[0]: (row[1], row[2]) for row in result}
    assert actual == SUPPORT_TICKETS_COLUMNS


# ---------------------------------------------------------------------------
# AC-01: uuid уведомления и его дефолт (M1, M2, M3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_uuid_generated_without_explicit_value(session: AsyncSession) -> None:
    """M2: снятие `server_default` у `notifications.uuid` роняет этот тест.

    Вставка БЕЗ `uuid` — ровно то, что делает воркер 7.3 сырым
    `INSERT ... SELECT` в `NotificationsRepository`. Если дефолт снять,
    вставка упадёт с `NotNullViolation` — то есть прод-путь сломается
    на первом же уведомлении.
    """
    user_id = await _create_user(session)
    async with session.begin_nested():
        result = await session.execute(
            text(_INSERT_NOTIFICATION), {"u": user_id, "t": "attendance.request"}
        )
        notification_id = int(result.scalar_one())

    got = await session.execute(
        text("SELECT uuid FROM notifications WHERE id = :i"), {"i": notification_id}
    )
    value = got.scalar_one()
    assert value is not None, "uuid не сгенерирован серверным дефолтом"


@pytest.mark.asyncio
async def test_notification_uuid_is_unique(session: AsyncSession) -> None:
    """M3: снятие `UNIQUE uuid` роняет этот тест.

    Проверяется НАРУШЕНИЕМ, а не чтением каталога: явная вставка дубля
    должна упасть. Каталог подтверждает только ИМЯ (`uq_notifications_uuid`).
    """
    user_id = await _create_user(session)
    dup = uuid_lib.uuid4()
    async with session.begin_nested():
        await session.execute(
            text(
                "INSERT INTO notifications (uuid, user_id, type, payload) "
                "VALUES (:uu, :u, 'attendance.request', CAST('{}' AS jsonb))"
            ),
            {"uu": dup, "u": user_id},
        )

    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO notifications (uuid, user_id, type, payload) "
                    "VALUES (:uu, :u, 'attendance.request', CAST('{}' AS jsonb))"
                ),
                {"uu": dup, "u": user_id},
            )

    assert "uq_notifications_uuid" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-02: перечень типов и проверка формы (M4, M5, M6, M23)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("type_value", [str(t) for t in NOTIFICATION_TYPES])
async def test_notification_type_allowlist_accepts_each_value(
    session: AsyncSession, type_value: str
) -> None:
    """M4: удаление любого значения из CHECK роняет ЕГО параметр.

    Параметризация по каждому из 11 значений: если из перечня пропадёт
    `attendance.result`, покраснеет ровно один параметр, и в отчёте будет
    видно какой. Общий тест «хоть что-то проходит» этого не показал бы.
    """
    user_id = await _create_user(session)
    async with session.begin_nested():
        await session.execute(text(_INSERT_NOTIFICATION), {"u": user_id, "t": type_value})


@pytest.mark.asyncio
async def test_notification_type_allowlist_rejects_unknown(session: AsyncSession) -> None:
    """M5: расширение CHECK перечня без правки Python-перечня роняет его.

    `foo.bar` — валидная по ФОРМЕ, но чужая сущность. Именно этот класс
    ошибки не заметит никто: NFR60 велит клиенту игнорировать неизвестный тип.
    """
    user_id = await _create_user(session)
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(text(_INSERT_NOTIFICATION), {"u": user_id, "t": "foo.bar"})

    assert "ck_notifications_type" in str(excinfo.value)
    assert "format" not in str(excinfo.value), (
        "упало ограничение формы, а не перечня — значит перечень не создан"
    )


@pytest.mark.asyncio
async def test_notification_type_format_check_present_and_correct(
    session: AsyncSession,
) -> None:
    """M6: снятие `ck_notifications_type_format` роняет этот тест.

    **Почему проверка не поведенческая на реальной таблице.** Перечень
    `ck_notifications_type` СТРОГО СИЛЬНЕЕ проверки формы: любое значение,
    нарушающее регекс, отсутствует и в перечне. PostgreSQL останавливается
    на ПЕРВОМ нарушенном ограничении и сообщает `ck_notifications_type`
    (измерено прогоном: `BAD_FORMAT` даёт нарушение перечня).

    Формат проверяется двумя независимыми способами:
    1. формула в БД совпадает с формулой модели (probe-таблица с `TYPE_PATTERN`);
    2. ограничение существует под ожидаемым именем (`op.f`, §18.7).

    Оба краснеют, если `ck_notifications_type_format` убрать из миграции.
    """
    await session.execute(
        text(
            "CREATE TEMP TABLE probe_type_format ("
            "    type character varying(64) NOT NULL, "
            f"    CONSTRAINT probe_type_format_check CHECK (type ~ '{TYPE_PATTERN}') "
            ") ON COMMIT DROP"
        )
    )

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("INSERT INTO probe_type_format (type) VALUES ('BAD_FORMAT')")
            )

    result = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            " WHERE conname IN ('probe_type_format_check', 'ck_notifications_type_format')"
        )
    )
    definitions = {row[0]: row[1] for row in result}
    assert "ck_notifications_type_format" in definitions, (
        "ограничение формы отсутствует в БД — снято миграцией"
    )
    assert definitions["probe_type_format_check"] == definitions["ck_notifications_type_format"]


@pytest.mark.asyncio
async def test_notification_type_allowlist_matches_model_and_migration(
    session: AsyncSession,
) -> None:
    """M23: расхождение Python-перечня и литерала миграции роняет этот тест.

    Три источника: `NotificationType` (Python), `NOTIFICATION_TYPES`
    (литерал миграции `0030`) и фактический текст CHECK в БД. Сверка
    текстом — доказательство РАВЕНСТВА перечней, а не совпадения оформления.
    """
    from importlib.util import module_from_spec, spec_from_file_location

    path = API_ROOT / "alembic" / "versions" / "0030_extend_notifications_and_create_support.py"
    spec = spec_from_file_location("migration_0030", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    python_list = sorted(str(t) for t in NOTIFICATION_TYPES)
    migration_list = sorted(migration.NOTIFICATION_TYPES)
    assert python_list == migration_list

    result = await session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            " WHERE conname = 'ck_notifications_type'"
        )
    )
    definition = result.scalar_one()

    # **Извлечение ФАКТИЧЕСКОГО перечня из текста CHECK, а не проверка наличия.**
    # Мутация M5 («добавить `foo.bar` в CHECK, не трогая Python») выжила на
    # первой версии этого теста: оба цикла шли по `python_list`, поэтому
    # ЛИШНЕЕ значение в БД не проверялось никогда. Сравнивать надо МНОЖЕСТВА.
    actual = set(re.findall(r"'([a-z_]+\.[a-z_]+)'::", definition))
    assert actual == set(python_list), (
        f"перечень в БД разошёлся с Python: лишние {sorted(actual - set(python_list))}, "
        f"отсутствуют {sorted(set(python_list) - actual)}"
    )


# ---------------------------------------------------------------------------
# AC-03: индексы (M7, M8, M9, M22)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unread_index_exists_with_exact_column_order(session: AsyncSession) -> None:
    """M7 и M8: снятие индекса ИЛИ перестановка колонок роняют этот тест.

    Предикат выдачи — `WHERE user_id = :u AND read_at IS NULL`, поэтому
    ведущей обязана быть `user_id`: индекс `(read_at, user_id)` не обслужил
    бы его. Порядок колонок в AC-03 буквальный.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes  WHERE indexname = 'ix_notifications_user_read'")
    )
    definition = result.scalar_one()
    assert "(user_id, read_at)" in definition
    assert "WHERE" not in definition, "индекс оказался партиальным (D-9.1-10)"


@pytest.mark.asyncio
async def test_delivery_index_preserved(session: AsyncSession) -> None:
    """M9: удаление `ix_notifications_user_delivery` роняет этот тест.

    У него ДРУГОЙ предикат (`delivered_at IS NULL` в `count_undelivered`),
    он обслуживает доставку, а не прочтение (D-9.1-9). Соблазн «заменить
    один индекс другим» ломает план воркера 7.3.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes  WHERE indexname = 'ix_notifications_user_delivery'")
    )
    assert "(user_id, delivered_at)" in result.scalar_one()


@pytest.mark.asyncio
async def test_support_queue_index_composition(session: AsyncSession) -> None:
    """M22: снятие `status` из индекса очереди роняет этот тест.

    Очередь 9.6 — `WHERE status = 'open' ORDER BY created_at`: без ведущего
    `status` индекс не отсекает отвеченные обращения.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_support_tickets_queue'")
    )
    assert "(status, created_at)" in result.scalar_one()


@pytest.mark.asyncio
async def test_support_user_list_index_is_desc(session: AsyncSession) -> None:
    """Список своих обращений — `ORDER BY created_at DESC` (9.5)."""
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes  WHERE indexname = 'ix_support_tickets_user_created'")
    )
    definition = result.scalar_one()
    assert "(user_id, created_at DESC)" in definition


# ---------------------------------------------------------------------------
# AC-01: ограничения support_tickets — нарушением (M13…M18, M19, M20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_id_zero_rejected(session: AsyncSession) -> None:
    """M13: снятие `CHECK id > 0` роняет этот тест (класс дефекта 6.7, P-10).

    `Identity()` — это `GENERATED BY DEFAULT`: явная вставка `id = 0`
    физически проходит, и единственная защита — ограничение.
    """
    user_id = await _create_user(session)
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text("INSERT INTO support_tickets (id, user_id, body) VALUES (0, :u, 'x')"),
                {"u": user_id},
            )

    assert "ck_support_tickets_id_positive" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ticket_status_domain_enforced(session: AsyncSession) -> None:
    """M14: снятие `CHECK status` из миграции роняет этот тест.

    **Почему не поведенчески на реальной таблице.** Статус `weird` нарушает
    и `ck_support_tickets_answer_pair`: пара требует `status='open'` (тогда
    ответ NULL) либо `status IN ('answered','closed')`. Строки со статусом вне
    домена, удовлетворяющей паре, не существует, поэтому PostgreSQL всегда
    сообщает `answer_pair` (измерено прогоном).

    Домен проверяется изолированно: probe-таблица с формулой из модели +
    сверка текста с БД. Удаление `CHECK status` из миграции роняет сверку.
    """
    await session.execute(
        text(
            "CREATE TEMP TABLE probe_ticket_status ("
            "    status character varying(16) NOT NULL, "
            "    CONSTRAINT probe_ticket_status_check CHECK ("
            f"{_in_clause('status', TICKET_STATUSES)})"
            ") ON COMMIT DROP"
        )
    )

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text("INSERT INTO probe_ticket_status (status) VALUES ('weird')"))

    result = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            " WHERE conname IN ('probe_ticket_status_check', 'ck_support_tickets_status')"
        )
    )
    definitions = {row[0]: row[1] for row in result}
    assert "ck_support_tickets_status" in definitions, (
        "ограничение статуса отсутствует в БД — снято миграцией"
    )
    assert definitions["probe_ticket_status_check"] == definitions["ck_support_tickets_status"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "",  # пустая строка
        " ",  # один пробел U+0020
        "   ",  # несколько пробелов
        "\t\n ",  # управляющие ASCII
        "\u00a0",  # NBSP — НЕ входит в `[:space:]` при LC_CTYPE=C (CR 9.1 H-2)
        "\u0085",  # NEL — то же
        "\u3000",  # IDEOGRAPHIC SPACE
        "\u202f",  # NARROW NO-BREAK SPACE
        "\ufeff".lower(),  # BOM
        "\u00a0\u00a0\u3000",  # комбинация
    ],
    ids=[
        "empty",
        "one_space",
        "spaces",
        "ascii_controls",
        "nbsp",
        "nel",
        "ideographic",
        "narrow_nbsp",
        "bom",
        "combined",
    ],
)
async def test_ticket_blank_body_rejected(session: AsyncSession, body: str) -> None:
    """M15: снятие `CHECK body_not_blank` роняет КАЖДЫЙ из трёх параметров.

    Три формы пустоты: пустая строка, пробелы, управляющие символы. `btrim`
    без аргументов снимает только пробелы — `\t\n` ловятся `btrim` с
    умолчанием, но не голым `trim`, и параметризация это доказывает.
    """
    user_id = await _create_user(session)
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text("INSERT INTO support_tickets (user_id, body) VALUES (:u, :b)"),
                {"u": user_id, "b": body},
            )

    assert "ck_support_tickets_body_not_blank" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ticket_answered_without_answer_rejected(session: AsyncSession) -> None:
    """M16: ослабление бикондиционала до `answer IS NULL OR answer IS NOT NULL`
    роняет этот тест.

    Класс дефекта `ck_notifications_read_order` до CR 7.3 H-2: при
    `answer IS NULL` наивное выражение даёт `UNKNOWN`, а `CHECK` отвергает
    только `FALSE` — то есть `status='answered'` без ответа проходил бы, и
    9.5 показал бы «отвечено» с пустым телом.
    """
    user_id = await _create_user(session)
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO support_tickets (user_id, body, status) "
                    "VALUES (:u, 'x', 'answered')"
                ),
                {"u": user_id},
            )

    assert "ck_support_tickets_answer_pair" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ticket_open_with_answer_rejected(session: AsyncSession) -> None:
    """M16 (вторая половина): `status='open'` с ответом тоже запрещён.

    Без этой проверки бикондиционал можно «упростить» до односторонней
    импликации «answered ⇒ answer NOT NULL», и обратное нарушение
    (`open` + ответ) прошло бы: пользователь увидел бы ответ, но обращение
    осталось бы в очереди модератора как неотвеченное.
    """
    user_id = await _create_user(session)
    moderator_id = await _create_user(session)
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO support_tickets "
                    "  (user_id, body, status, answer, answered_at, answered_by_id) "
                    "VALUES (:u, 'x', 'open', 'ответ', now(), :m)"
                ),
                {"u": user_id, "m": moderator_id},
            )

    assert "ck_support_tickets_answer_pair" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ticket_answered_state_accepted(session: AsyncSession) -> None:
    """P-11: бикондиционал проверяется в ОБОИХ направлениях.

    Тест «запрещённое падает» без теста «разрешённое проходит» не отличил бы
    корректный CHECK от `CHECK (false)`, который запрещает всё.
    """
    user_id = await _create_user(session)
    moderator_id = await _create_user(session)
    async with session.begin_nested():
        result = await session.execute(
            text(
                "INSERT INTO support_tickets "
                "  (user_id, body, status, answer, answered_at, answered_by_id) "
                "VALUES (:u, 'x', 'answered', 'ответ', now(), :m) RETURNING id"
            ),
            {"u": user_id, "m": moderator_id},
        )
        assert int(result.scalar_one()) > 0


@pytest.mark.asyncio
async def test_ticket_answer_before_creation_rejected(session: AsyncSession) -> None:
    """M17: снятие `CHECK answered_at >= created_at` роняет этот тест."""
    user_id = await _create_user(session)
    moderator_id = await _create_user(session)
    with pytest.raises(IntegrityError) as excinfo:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO support_tickets "
                    "  (user_id, body, status, answer, answered_at, answered_by_id, created_at) "
                    "VALUES (:u, 'x', 'answered', 'ответ', now() - interval '1 day', "
                    "        :m, now())"
                ),
                {"u": user_id, "m": moderator_id},
            )

    assert "ck_support_tickets_answer_order" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ticket_status_defaults_to_open(session: AsyncSession) -> None:
    """M18: снятие/подмена `server_default='open'` роняет этот тест.

    9.5 создаёт обращение БЕЗ явного статуса. Дефолт `answered` сделал бы
    новое обращение невидимым в очереди модератора 9.6 — тихая потеря
    обращения, не заметная ни в одном логе.
    """
    user_id = await _create_user(session)
    async with session.begin_nested():
        result = await session.execute(text(_INSERT_TICKET), {"u": user_id, "b": "тело"})
        ticket_id = int(result.scalar_one())

    got = await session.execute(
        text("SELECT status FROM support_tickets WHERE id = :i"), {"i": ticket_id}
    )
    assert got.scalar_one() == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "expected"), [("user_id", "CASCADE"), ("answered_by_id", "SET NULL")]
)
async def test_ticket_fk_delete_actions(session: AsyncSession, column: str, expected: str) -> None:
    """M19/M20: смена `ondelete` роняет соответствующий параметр.

    `confdeltype` — единственная проверка, которую нельзя сделать вставкой:
    удаление пользователя в тесте уничтожило бы данные соседних тестов.
    Разные действия у двух FK намеренны: обращение без автора бессмысленно
    (`CASCADE`), но переживает удаление учётки модератора (`SET NULL`).
    """
    result = await session.execute(
        text(
            "SELECT confdeltype FROM pg_constraint "
            " WHERE conrelid = 'support_tickets'::regclass "
            "   AND contype = 'f' AND conname = :n"
        ),
        {"n": f"fk_support_tickets_{column}_users"},
    )
    # `confdeltype` — тип `"char"`, asyncpg отдаёт `bytes`; без декодирования
    # словарь искался бы по `b'c'` и тест падал бы `KeyError` на верной схеме.
    raw = result.scalar_one()
    code = raw.decode() if isinstance(raw, bytes) else str(raw)
    actual = {"c": "CASCADE", "n": "SET NULL", "a": "NO ACTION", "r": "RESTRICT"}
    assert actual[code] == expected


@pytest.mark.asyncio
async def test_deleting_moderator_nulls_author_and_keeps_ticket(
    session: AsyncSession,
) -> None:
    """CR 9.1 H-1: `answered_by_id ON DELETE SET NULL` РАБОТАЕТ.

    **Дефект, который ловит этот тест.** Прежний `ck_support_tickets_answer_pair`
    требовал `answered_by_id IS NOT NULL` для статусов `answered`/`closed`,
    а FK объявлен `SET NULL`. Два требования взаимно исключали друг друга:
    `DELETE FROM users` для любого ответившего модератора падал с
    `violates check constraint "ck_support_tickets_answer_pair"` → `500` и
    НЕУДАЛЯЕМАЯ учётка (нарушение ADR-07 «ретенция»).

    **Почему прежний тест был слепым.** `test_ticket_fk_delete_actions`
    читал `pg_constraint.confdeltype` — каталог. Каталог и сейчас показывает
    `confdeltype='n'` (SET NULL) и НЕ ЗНАЕТ, что действие упадёт (D-98: тест
    без мутации, которая его краснит, не принимается).

    **Семантика после фикса.** Бикондиционал связывает только
    `(answer, answered_at)` ↔ `status`: «отвечено» значит есть текст И дата.
    `answered_by_id` — независимо nullable атрибут авторства: после удаления
    учётки автора ответ остаётся фактом, но автора больше нет. Это честное
    состояние, а не потеря данных.
    """
    author_id = await _create_user(session)
    moderator_id = await _create_user(session)

    async with session.begin_nested():
        result = await session.execute(
            text(
                "INSERT INTO support_tickets "
                "  (user_id, body, status, answer, answered_at, answered_by_id) "
                "VALUES (:u, 'тело', 'answered', 'ответ', now(), :m) RETURNING id"
            ),
            {"u": author_id, "m": moderator_id},
        )
        ticket_id = int(result.scalar_one())

    # DELETE внутри begin_nested(): откатывается вместе с транзакцией теста,
    # поэтому соседние тесты не теряют пользователей.
    async with session.begin_nested():
        await session.execute(text("DELETE FROM users WHERE id = :m"), {"m": moderator_id})

    row = await session.execute(
        text(
            "SELECT answered_by_id, status, answer, answered_at FROM support_tickets  WHERE id = :i"
        ),
        {"i": ticket_id},
    )
    answered_by_id, status, answer, answered_at = row.one()

    assert answered_by_id is None, "FK SET NULL не сработал — автор остался"
    assert status == "answered", "статус изменился при удалении автора"
    assert answer == "ответ", "текст ответа потерян"
    assert answered_at is not None, "дата ответа потеряна"


# ---------------------------------------------------------------------------
# Сверка Python-формул с БД (§18.1: текст CHECK доказывает равенство формул)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe", "sql", "db_name"),
    [
        ("probe_answer_pair", ANSWER_PAIR_SQL, "ck_support_tickets_answer_pair"),
        ("probe_body_not_blank", BODY_NOT_BLANK_SQL, "ck_support_tickets_body_not_blank"),
        ("probe_answer_order", ANSWER_ORDER_SQL, "ck_support_tickets_answer_order"),
    ],
)
async def test_model_check_sql_matches_database(
    session: AsyncSession, probe: str, sql: str, db_name: str
) -> None:
    """Расхождение формулы в модели и в БД роняет этот тест.

    Модель описывает намерение, миграция — факт. Одинаковый текст
    `pg_get_constraintdef` для одинаковых типов доказывает равенство формул.
    """
    await session.execute(
        text(
            f"CREATE TEMP TABLE {probe} (\n"
            "    body text NOT NULL,\n"
            "    status character varying(16) NOT NULL,\n"
            "    answer text,\n"
            "    answered_at timestamp with time zone,\n"
            "    answered_by_id bigint,\n"
            "    created_at timestamp with time zone NOT NULL,\n"
            f"    CONSTRAINT {probe}_check CHECK ({sql})\n"
            ") ON COMMIT DROP"
        )
    )
    result = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            " WHERE conname IN (:p, :d)"
        ),
        {"p": f"{probe}_check", "d": db_name},
    )
    definitions = {row[0]: row[1] for row in result}
    assert definitions[f"{probe}_check"] == definitions[db_name]


# ---------------------------------------------------------------------------
# Сверка Python-домена статусов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_status_domain_matches_model(session: AsyncSession) -> None:
    """M24: расхождение `TicketStatus` и CHECK в БД роняет этот тест.

    Два направления проверки, и оба нужны:

    1. **Каждое значение из Python-домена принимается БД.** Мутация «убрать
       `closed` из CHECK миграции, оставив в `TicketStatus`» роняет этот шаг.
       Тело заполняется по правилу бикондиционала: `open` требует пустых
       `answer`/`answered_at`/`answered_by_id`, остальные — заполненных.
    2. **Текст CHECK содержит ровно эти три значения.** Мутация «добавить
       `weird` в CHECK миграции, не добавляя в `TicketStatus`» ловится
       сверкой формулы с probe-таблицей: строки, которой нет в Python-домене.
    """
    for value in (str(v) for v in TICKET_STATUSES):
        user_id = await _create_user(session)
        async with session.begin_nested():
            if value == "open":
                await session.execute(
                    text(
                        "INSERT INTO support_tickets (user_id, body, status) VALUES (:u, 'x', :s)"
                    ),
                    {"u": user_id, "s": value},
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO support_tickets "
                        "  (user_id, body, status, answer, answered_at, answered_by_id) "
                        "VALUES (:u, 'x', :s, 'ответ', now(), :u)"
                    ),
                    {"u": user_id, "s": value},
                )

    # Текстовая сверка: формула модели == формула в БД. Ловит ЛИШНЕЕ значение
    # в БД (которого нет в Python-домене), чего поведенческая часть не увидит.
    await session.execute(
        text(
            "CREATE TEMP TABLE probe_ticket_domain ("
            "    status character varying(16) NOT NULL, "
            "    CONSTRAINT probe_ticket_domain_check CHECK ("
            f"{_in_clause('status', TICKET_STATUSES)})"
            ") ON COMMIT DROP"
        )
    )
    result = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            " WHERE conname IN ('probe_ticket_domain_check', 'ck_support_tickets_status')"
        )
    )
    definitions = {row[0]: row[1] for row in result}
    assert definitions["probe_ticket_domain_check"] == definitions["ck_support_tickets_status"]
