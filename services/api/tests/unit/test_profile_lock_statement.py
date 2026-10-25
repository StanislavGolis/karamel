"""Блокировка строки при правке анкеты (Task 5.3; AC-03, D-8, §18.3).

§18.3: гонку двух `PATCH` в одной сессии доказать нельзя — вторая транзакция
в том же соединении не начнётся. Поэтому проверяется текст запроса: наличие
`FOR UPDATE` и `populate_existing`. Это не тавтология (§18.2): тест краснеет,
если блокировку уберут из метода, — ровно то состояние, в котором гонка
возвращается.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.models.profile import Profile
from app.repositories.profiles import ProfileRepository


def _compiled() -> str:
    """SQL метода `get_by_user_id_for_update` без обращения к БД.

    Метод строит `Select` и только потом выполняет его, поэтому текст запроса
    доступен без сессии. Все тесты этого файла зовут этот же хелпер: раньше
    он принимал имя метода и игнорировал его — тест «на метод» был тавтологией
    (§18.2) и зеленел бы при переименовании.
    """
    statement = ProfileRepository._build_for_update_statement(1)  # type: ignore[attr-defined]
    return str(statement.compile(dialect=postgresql.dialect()))


def test_for_update_statement_locks_row() -> None:
    """D-8: `PATCH` читает анкету с `FOR UPDATE`.

    Без блокировки два параллельных `PATCH` читают один снимок, каждый
    считает полноту по своему набору полей, и второй пишет `status='draft'`
    при фактически заполненной анкете. Тот же класс дефекта чинили в 1.6.
    """
    sql = _compiled()

    assert "FOR UPDATE" in sql


def test_for_update_statement_filters_by_user_id() -> None:
    """Блокировка не должна захватывать чужие строки.

    `SELECT ... FOR UPDATE` без `WHERE` заблокировал бы всю таблицу анкет:
    один `PATCH` останавливал бы сохранение всех пользователей.
    """
    sql = _compiled()

    assert "profiles.user_id" in sql
    assert "FOR UPDATE" in sql


def test_for_update_statement_refreshes_identity_map() -> None:
    """CR 2.1, патч 11: `populate_existing` обязателен вместе с блокировкой.

    Без него объект, уже лежащий в identity map, вернётся снимком: сервис
    пересчитает `status` по старым значениям, и правка «потеряется» —
    при этом блокировка создаст иллюзию защиты.
    """
    statement = ProfileRepository._build_for_update_statement(1)  # type: ignore[attr-defined]

    assert statement.get_execution_options().get("populate_existing") is True
    assert Profile.__tablename__ in str(statement)
