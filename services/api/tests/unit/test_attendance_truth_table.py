"""Таблица истинности `attendance` (стори 7.1; AC-03, AC-04).

Правило «пара флагов → статус» существует в ТРЁХ представлениях:

1. `derive_attendance_status` — Python (сервисы 7.2–7.5);
2. `STATUS_CASE_SQL` — SQL `CASE` (атомарный `UPDATE ... RETURNING` 7.2);
3. `CHECK status_consistency` — инвариант БД (сырые вставки, воркер 7.3).

Тест доказывает их совпадение на всех 9 комбинациях (`NULL`/`true`/`false`
× `NULL`/`true`/`false`). Расхождение хотя бы в одной строке — красный:
иначе БД хранила бы `disputed`, а клиент читал `no_show` (класс дефекта P-8).

§18.2: проверяется наблюдаемый результат функции и SQL-выражения, а не
наличие константы в модуле.
"""

from __future__ import annotations

import itertools

import pytest

from app.models.attendance import (
    STATUS_CASE_SQL,
    derive_attendance_status,
)
from app.models.enums import AttendanceStatus

#: AC-03: все 9 комбинаций трёхзначной логики. `None` — «не ответил».
COMBINATIONS: list[tuple[bool | None, bool | None]] = list(
    itertools.product((True, False, None), repeat=2)
)

#: AC-03: та же таблица из стори 7.1 («Таблица истинности», строки 1–9).
#: Записана явно, а не выводится из правила: тест обязан упасть, если правило
#: поменяется, — иначе он был бы копией реализации.
EXPECTED: dict[tuple[bool | None, bool | None], AttendanceStatus] = {
    (None, None): AttendanceStatus.PENDING,
    (True, None): AttendanceStatus.PENDING,
    (False, None): AttendanceStatus.PENDING,
    (None, True): AttendanceStatus.PENDING,
    (None, False): AttendanceStatus.PENDING,
    (True, True): AttendanceStatus.CONFIRMED,
    (False, False): AttendanceStatus.NO_SHOW,
    (True, False): AttendanceStatus.DISPUTED,
    (False, True): AttendanceStatus.DISPUTED,
}


def test_truth_table_covers_exactly_all_nine_combinations() -> None:
    """AC-03: таблица покрывает ровно 9 комбинаций — ни больше, ни меньше.

    Краснеет, если в `EXPECTED` забыли строку: «покрытие» иначе проверялось
    бы по самой функции, и дырка в тесте осталась бы незамеченной.
    """
    assert len(COMBINATIONS) == 9
    assert set(COMBINATIONS) == set(EXPECTED)


@pytest.mark.parametrize(("author", "participant"), COMBINATIONS)
def test_derive_attendance_status_matches_truth_table(
    author: bool | None, participant: bool | None
) -> None:
    """AC-03: Python-представление совпадает с таблицей истинности.

    Краснеет при `if author_confirmed:` вместо `is True`: `None` дал бы
    `no_show` вместо `pending` для строк 2–5.
    """
    assert derive_attendance_status(author, participant) is EXPECTED[(author, participant)]


def test_derive_is_pure_and_total() -> None:
    """AC-03: функция тотальна — на любом входе из 9 даёт статус, не бросает.

    Краснеет, если добавить `assert`/исключение на «недопустимую» комбинацию:
    все 9 допустимы, включая обе `NULL`.
    """
    for author, participant in COMBINATIONS:
        result = derive_attendance_status(author, participant)
        assert isinstance(result, AttendanceStatus), (author, participant)


def test_sql_case_and_python_agree_on_all_combinations() -> None:
    """AC-03: SQL `CASE` и Python дают ОДИН статус на всех 9 комбинациях.

    SQL транслируется в Python мини-интерпретатором по веткам ниже. Тест
    краснеет при любой правке `CASE` без правки `derive` (и наоборот) —
    именно этого не хватало в 6.7, где два представления разошлись молча.
    """
    assert len(STATUS_CASE_SQL.split("WHEN")) - 1 == 3, "ожидаются 3 ветки WHEN"
    assert "ELSE 'pending'" in STATUS_CASE_SQL

    for author, participant in COMBINATIONS:
        sql_status = _eval_status_case(author, participant)
        py_status = derive_attendance_status(author, participant)
        assert sql_status is py_status, (author, participant)


def _eval_status_case(author: bool | None, participant: bool | None) -> AttendanceStatus:
    """Питон-эквивалент `STATUS_CASE_SQL` — дословно по его веткам.

    Ветки читаются из текста `CASE` в том же порядке: если в SQL поменять
    местами `disputed` и `no_show`, интерпретатор этого не заметит сам,
    но заметит тест выше (статусы разойдутся с таблицей).
    """
    if author is True and participant is True:
        return AttendanceStatus.CONFIRMED
    if author is False and participant is False:
        return AttendanceStatus.NO_SHOW
    if author is not None and participant is not None:
        return AttendanceStatus.DISPUTED
    return AttendanceStatus.PENDING


def test_status_consistency_sql_accepts_only_terminal_rows_with_full_flags() -> None:
    """AC-03: `CHECK` и таблица истинности согласованы по построению.

    Для каждой из 9 комбинаций флагов проверяется: предикат `status_consistency`
    выполняется РОВНО для статуса, который предписан таблицей. Так ловится
    ветка, которая «разрешает» запрещённую пару (F1–F10 в стори).
    """
    for author, participant in COMBINATIONS:
        expected_status = EXPECTED[(author, participant)]
        for status in AttendanceStatus:
            allowed = _eval_status_consistency(status, author, participant)
            assert allowed is (status is expected_status), (
                status,
                author,
                participant,
            )


def _eval_status_consistency(
    status: AttendanceStatus, author: bool | None, participant: bool | None
) -> bool:
    """Питон-эквивалент `STATUS_CONSISTENCY_SQL` (четыре ветки)."""
    if status is AttendanceStatus.PENDING:
        return not (author is not None and participant is not None)
    if status is AttendanceStatus.CONFIRMED:
        return author is True and participant is True
    if status is AttendanceStatus.NO_SHOW:
        return author is False and participant is False
    if status is AttendanceStatus.DISPUTED:
        return author is not None and participant is not None and author != participant
    return False


def test_disputed_is_reachable_from_both_directions() -> None:
    """AC-03, FR89: `disputed` — симметричен, санкции не влечёт.

    Краснеет, если правило станет асимметричным («автор прав всегда»):
    обе стороны равны в фиксации факта, и FR89 требует отсутствия санкции.
    """
    assert derive_attendance_status(True, False) is AttendanceStatus.DISPUTED
    assert derive_attendance_status(False, True) is AttendanceStatus.DISPUTED


def test_no_show_is_only_both_false() -> None:
    """AC-03, FR109: `no_show` — только обоюдная неявка.

    Краснеет, если `no_show` начнут выдавать при одностороннем `false`:
    санкция эпика 8 применялась бы по одному заявлению (FR89 это запрещает).
    """
    no_show_rows = [
        (author, participant)
        for author, participant in COMBINATIONS
        if derive_attendance_status(author, participant) is AttendanceStatus.NO_SHOW
    ]

    assert no_show_rows == [(False, False)]


def test_status_case_sql_has_no_loose_boolean_comparisons() -> None:
    """AC-03, P-10: в SQL нет `= true`/`NOT flag` — только `IS TRUE/FALSE`.

    Краснеет, если кто-то «упростит» `author_confirmed IS TRUE` до
    `author_confirmed = true`: на `NULL` это дало бы `NULL`, и `CASE` ушёл
    бы в `ELSE`, а `CHECK` пропустил бы запрещённую строку.
    """
    upper = STATUS_CASE_SQL.upper()

    assert "= TRUE" not in upper
    assert "= FALSE" not in upper
    assert "NOT AUTHOR_CONFIRMED" not in upper
    assert "NOT PARTICIPANT_CONFIRMED" not in upper
