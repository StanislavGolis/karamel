"""Текст SQL репозитория удаления аккаунта (стори 10.2; AC-02, AC-07; 18.3).

**Зачем тест на ТЕКСТ запроса, а не на поведение.** `FOR UPDATE SKIP LOCKED`
в одной сессии ведёт себя ровно так же, как без него: блокировку никто не
держит, и оба варианта возвращают одни и те же строки. Мутация «убрать
`SKIP LOCKED`» на интеграционном тесте выжила (M2 SURVIVED, зафиксировано
в DAR). Правило 18.3 требует в таком случае unit на текст запроса: он и есть
единственное доказательство, что воркеры не блокируют друг друга
(D-10.2-3, NFR46–NFR49).

**Тест зовёт МЕТОД репозитория, а не копию запроса.** Первая версия
копировала `select(...)` в тест — такая копия не увидела бы правку
репозитория, и мутация снова выжила бы. Оператор вынесен в
`build_due_candidates_statement` именно ради этой проверки.

**Тот же приём, что в `test_chat_lock_for_update.py`** (стори 6.4): компиляция
оператора диалектом PostgreSQL и проверка подстроки.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.account_deletion import AccountDeletionRepository
from app.services.deletion_window import DELETION_WINDOW_DAYS


def _candidates_sql() -> str:
    """SQL оператора кандидатов — как его увидит PostgreSQL.

    Зовётся ИМЕННО метод репозитория: копия запроса в тесте не поймала бы
    мутацию в рабочем коде (урок M2 в этой стори).
    """
    statement = AccountDeletionRepository.build_due_candidates_statement(
        now=datetime.now(UTC), limit=200
    )
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_candidates_use_skip_locked() -> None:
    """AC-07, D-10.2-3: кандидаты берутся `FOR UPDATE SKIP LOCKED`.

    Красным станет при снятии `skip_locked=True`: два контейнера воркера
    начнут ждать друг друга на одной пачке (NFR46–NFR49). Интеграционный
    тест этого не ловит — см. докстринг модуля.
    """
    sql = _candidates_sql()

    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql


def test_candidates_filter_by_window_not_only_null() -> None:
    """AC-01: предикат кандидатов сравнивает с окном, а не только `IS NOT NULL`.

    Красным станет, если из `WHERE` уйдёт сравнение `<=`: воркер забрал бы
    всех, кто нажал «удалить» минуту назад (D-10.2-1).
    """
    sql = _candidates_sql()

    assert "deletion_requested_at IS NOT NULL" in sql
    assert "deletion_requested_at <=" in sql


def test_window_constant_is_seven_days() -> None:
    """NFR37: окно в SQL считается от константы 7 суток (D-10.2-4)."""
    assert DELETION_WINDOW_DAYS == 7


# --- P-15 (CR 10.2): валидация `limit` ---


def test_negative_limit_is_rejected_before_sql() -> None:
    """P-15: `limit < 1` отвергается ДО `LIMIT`, а не ловится как сбой БД.

    До правки `limit=-1` доходил до PostgreSQL (`LIMIT must not be negative`),
    воркер ловил это общим `except Exception` и логировал
    `account_purge_failed` КАЖДЫЙ цикл: настоящая причина тонула в шуме,
    а прогон не удалял никого. Красным станет, если валидацию убрать —
    тогда `ValueError` не поднимется и тест упадёт на `pytest.raises`.
    """
    with pytest.raises(ValueError, match="limit must be positive"):
        AccountDeletionRepository.build_due_candidates_statement(now=datetime.now(UTC), limit=-1)


def test_zero_limit_is_rejected() -> None:
    """P-15: `limit=0` — тоже ошибка вызывающего, а не «нечего делать».

    Молчаливый `0` скрыл бы дефект навсегда: воркер считал бы прогон
    успешным и не удалял никого.
    """
    with pytest.raises(ValueError, match="limit must be positive"):
        AccountDeletionRepository.build_due_candidates_statement(now=datetime.now(UTC), limit=0)


def test_positive_limit_builds_statement() -> None:
    """P-15: валидный `limit` проходит и попадает в SQL.

    Без этой проверки `_validate_limit` мог бы отвергать всё подряд,
    и два теста выше остались бы зелёными.
    """
    statement = AccountDeletionRepository.build_due_candidates_statement(
        now=datetime.now(UTC), limit=7
    )
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 7" in compiled
