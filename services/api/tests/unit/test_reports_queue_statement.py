"""SQL очереди жалоб: порядок, предикат keyset, фильтр (стори 8.3; AC-01, AC-04).

**Почему проверяется SQL-константа, а не компиляция `Select`.** Очередь написана
сырым `text(...)`: у неё нет ORM-выражения, и единственный носитель контракта —
строка `_QUEUE_SQL`. Тест читает её через публичный метод сборки
(`ReportsRepository.list_queue` собирает строку подстановкой `keyset`), поэтому
изменение запроса в прод-коде краснит тест, а изменение теста не может
«подогнать» прод.

**Почему порядок проверяется по обеим колонкам.** `ORDER BY reported_at`
без `uuid` даёт нестабильный порядок внутри tie-группы (мутация M1), а
следствие — потеря и дубли строк при постраничном чтении. Это дефект данных,
а не стиля, и он невидим на одиночной странице.
"""

from __future__ import annotations

import inspect
import uuid as uuid_lib
from datetime import UTC, datetime

from app.repositories.reports import _COUNT_QUEUE_SQL, _QUEUE_SQL, ReportsRepository
from app.services.reports_queue_cursor import decode_queue_cursor, encode_queue_cursor


def test_order_by_has_both_keys() -> None:
    """AC-04, D-9, мутация M1: порядок — `reported_at ASC, uuid ASC`.

    Второй ключ обязателен: `reported_at` у двух жалоб может совпасть (`now()`
    в одной транзакции), и без `uuid` порядок зависел бы от физического
    расположения строк.
    """
    normalized = " ".join(_QUEUE_SQL.split())
    assert "ORDER BY r.reported_at ASC, r.uuid ASC" in normalized


def test_keyset_is_tuple_comparison_not_two_or_branches() -> None:
    """AC-04, мутация M2: keyset — КОРТЕЖНОЕ сравнение.

    Две `OR`-ветви (`reported_at > :a OR (reported_at = :a AND uuid > :b)`)
    дают тот же результат только при аккуратных скобках; при ошибке в
    приоритетах теряется вся tie-группа. Кортежное сравнение выражает
    намерение одной конструкцией и ошибиться в нём нечем.
    """
    # Ключевое место: `_QUEUE_SQL` — ШАБЛОН с плейсхолдером `{keyset}`, и сам
    # предикат собирается в методе. Проверять надо метод, иначе тест смотрит
    # на место, где условия ещё нет, и зеленеет при любом его виде.
    source = inspect.getsource(ReportsRepository.list_queue)
    normalized = " ".join(source.split())
    assert "(r.reported_at, r.uuid) > (:after_at, :after_uuid)" in normalized
    # Ни одной OR-ветви по ключам сортировки: появление `r.reported_at >` или
    # `r.reported_at =` выдаёт разложение кортежа на две ветви.
    for broken in ("r.reported_at >", "r.reported_at =", "r.uuid >", "r.uuid ="):
        assert broken not in normalized, f"keyset разложен на ветви: {normalized}"


def test_status_open_predicate_matches_partial_index() -> None:
    """AC-04, D-31: предикат `status='open'` совпадает с частичным индексом.

    Без него планировщик не выберет `ix_reports_open_reported_at_uuid`, и
    очередь на объёме деградирует до seq scan + Sort.
    """
    normalized = " ".join(_QUEUE_SQL.split())
    assert "WHERE r.status = 'open'" in normalized
    assert "WHERE r.status = 'open'" in " ".join(_COUNT_QUEUE_SQL.split())


def test_target_type_filter_is_nullable_with_explicit_cast() -> None:
    """AC-01, D-4.3-4: фильтр по типу — `NULL` означает «все».

    `CAST(:target_type AS varchar)` обязателен: без него asyncpg не может
    вывести тип параметра из `:target_type IS NULL` и падает
    `could not determine data type of parameter $1` — то есть маршрут
    отвечает `500` на ЛЮБОЙ запрос без фильтра. Дефект найден прогоном
    интеграционных тестов 8.3.
    """
    normalized = " ".join(_QUEUE_SQL.split())
    assert (
        "CAST(:target_type AS varchar) IS NULL OR r.target_type = CAST(:target_type AS varchar)"
        in normalized
    )


def test_limit_is_parameter_not_literal() -> None:
    """AC-04: `LIMIT :limit` — размер страницы приходит из роута (1..50).

    Литерал зафиксировал бы размер и сделал `?limit=` неработающим.
    """
    assert "LIMIT :limit" in " ".join(_QUEUE_SQL.split())


def test_count_uses_same_predicate_as_page() -> None:
    """AC-04: `total` считается ТЕМ ЖЕ `WHERE`, что у страницы.

    Расхождение предикатов дало бы счётчик, не соответствующий выдаче:
    модератор планировал бы работу по числу, которого не увидит.
    """
    page = " ".join(_QUEUE_SQL.split())
    count = " ".join(_COUNT_QUEUE_SQL.split())
    for predicate in ("WHERE r.status = 'open'", "CAST(:target_type AS varchar) IS NULL"):
        assert predicate in page
        assert predicate in count
    assert "ORDER BY" not in count
    assert "LIMIT" not in count


def test_queue_sql_has_no_plain_status_filter_on_resolved() -> None:
    """AC-04, D-31: архивные состояния не перечисляются — их ОТСУТСТВИЕ выражено.

    Запрос вида `status IN ('open','resolved_warning')` вернул бы разобранные
    жалобы; единственный корректный предикат — `status = 'open'`, который
    одновременно совпадает с частичным индексом.
    """
    normalized = " ".join(_QUEUE_SQL.split())
    for archived in ("resolved_warning", "resolved_ban", "dismissed"):
        assert archived not in normalized


def test_cursor_round_trip_preserves_microseconds_and_utc() -> None:
    """AC-04: курсор возвращает ТУ ЖЕ позицию — с микросекундами и в UTC.

    **CR 8.3, H-2: переписан.** Первая версия проверяла `isinstance(datetime)`
    и `isinstance(UUID)` на кортеже, собранном в самом тесте: тавтология
    (§18.2) — она не могла покраснеть ни от одной правки прод-кода.

    Теперь проверяется КОДЕК: усечение до секунд отсекло бы строки с тем же
    `reported_at` (сортировка идёт именно по нему), а потеря пояса дала бы
    сдвиг чтения при разборе в другой зоне.
    """
    moment = datetime(2026, 10, 25, 12, 34, 56, 789012, tzinfo=UTC)
    report_uuid = uuid_lib.uuid4()

    restored_at, restored_uuid = decode_queue_cursor(encode_queue_cursor(moment, report_uuid))

    assert restored_at == moment, "микросекунды потеряны — tie-группа отсечётся"
    assert restored_at.tzinfo is not None, "пояс потерян — сдвиг чтения в другой зоне"
    assert restored_uuid == report_uuid
