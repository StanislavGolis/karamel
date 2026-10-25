"""Unit-тесты сервиса согласия (Task 4; AC-01 … AC-04).

БД не нужна: сервис меняет поля заблокированной строки и вызывает `flush`.
Сессия подменяется заглушкой, которая запоминает вызовы — так проверяется,
что сервис не создаёт посторонних записей (`applications`, AC-01/AC-02).

Патчи 2 и 3 код-ревью 1.6 добавили `SELECT ... FOR UPDATE`: заглушка обязана
поддержать `execute`, иначе тесты падали бы на отсутствии метода, а не на
логике. Возвращается та же строка, что передана в конструктор — это ровно то,
что вернула бы БД, если бы блокировка никого не ждала.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models.user import User
from app.schemas.consent import ConsentRequest
from app.services.consent_service import (
    ConsentRequiredError,
    GenderLockedError,
    GenderRequiredError,
    lock_statement,
    record_consent,
)


class ResultStub:
    """Минимальный `Result`: сервису нужен только `scalar_one_or_none`."""

    def __init__(self, value: User | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> User | None:
        return self._value


class SessionStub:
    """Заглушка `AsyncSession`: фиксирует записи, `flush` и взятие блокировки."""

    def __init__(self, locked: User | None = None) -> None:
        self.flushed = 0
        self.added: list[Any] = []
        self.locked_reads = 0
        self._locked = locked

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, _statement: Any) -> ResultStub:
        """Патч 2: сервис перечитывает строку под `FOR UPDATE` перед решением."""
        self.locked_reads += 1
        return ResultStub(self._locked)


def make_user(*, gender: str | None = None, consent_given_at: datetime | None = None) -> User:
    """Новая учётка: `age_confirmed` не выставлен (значение БД-дефолта) — False."""
    user = User(
        provider="phone",
        provider_user_id="+79001234567",
        phone="+79001234567",
        gender=gender,
    )
    user.uuid = uuid_lib.uuid4()
    user.consent_given_at = consent_given_at
    return user


def session_for(user: User) -> SessionStub:
    """Заглушка, которая «читает под блокировкой» ту же строку."""
    return SessionStub(locked=user)


async def test_missing_age_confirmation_raises_and_changes_nothing() -> None:
    """AC-03: без подтверждения 18+ сервис отказывает и не пишет поля."""
    user = make_user()
    session = session_for(user)

    with pytest.raises(ConsentRequiredError):
        await record_consent(
            session,  # type: ignore[arg-type]
            user,
            ConsentRequest(age_confirmed=False, consent=True, gender="female"),
        )

    assert user.gender is None
    assert user.consent_given_at is None
    assert not user.age_confirmed
    assert session.flushed == 0
    # AC-03: отказ до блокировки — БД не трогается вовсе.
    assert session.locked_reads == 0


async def test_missing_pd_consent_raises_and_changes_nothing() -> None:
    """AC-03: без согласия на обработку ПД сервис отказывает и не пишет поля."""
    user = make_user()
    session = session_for(user)

    with pytest.raises(ConsentRequiredError):
        await record_consent(
            session,  # type: ignore[arg-type]
            user,
            ConsentRequest(age_confirmed=True, consent=False, gender="female"),
        )

    assert user.gender is None
    assert user.consent_given_at is None
    assert not user.age_confirmed
    assert session.flushed == 0
    assert session.locked_reads == 0


async def test_female_consent_records_age_time_and_gender() -> None:
    """AC-01, AC-04: женщина получает `age_confirmed`, время согласия и пол.

    Статус не меняется: он про санкции (AR30), а не про модерацию.
    """
    user = make_user()
    session = session_for(user)
    status_before = user.status

    updated, is_new = await record_consent(
        session,  # type: ignore[arg-type]
        user,
        ConsentRequest(age_confirmed=True, consent=True, gender="female"),
    )

    assert updated is user
    assert is_new is True
    assert user.age_confirmed is True
    assert user.gender == "female"
    assert user.consent_given_at is not None
    assert user.consent_given_at.tzinfo is not None
    assert user.status == status_before
    assert session.flushed == 1
    # Патч 2: решение о поле принимается по перечитанной строке.
    assert session.locked_reads == 1
    # AC-01/AC-02: сервис не создаёт никаких записей (в том числе `applications`).
    assert session.added == []


async def test_male_consent_records_gender_without_queue_entry() -> None:
    """AC-02: мужчина фиксируется с `gender=male`, заявка не создаётся."""
    user = make_user()
    session = session_for(user)

    await record_consent(
        session,  # type: ignore[arg-type]
        user,
        ConsentRequest(age_confirmed=True, consent=True, gender="male"),
    )

    assert user.gender == "male"
    assert user.age_confirmed is True
    # AC-02: заявки (FR2–FR4) — эпик 3; здесь ни одной записи.
    assert session.added == []


async def test_repeat_consent_keeps_first_timestamp() -> None:
    """AC-04: повторное подтверждение не перезаписывает время первого согласия.

    Патч 6 код-ревью 1.6: прежний ассерт
    `assert consent_given_at != first_time + timedelta(seconds=1)` не мог
    упасть по делу — при перезаписи время стало бы `now`, что почти никогда
    не равно `first_time + 1s`. Проверка заменена на сравнение с исходной
    меткой: она краснеет ровно тогда, когда метка сдвинулась.
    """
    first_time = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    user = make_user(gender="female", consent_given_at=first_time)
    session = session_for(user)

    _, is_new = await record_consent(
        session,  # type: ignore[arg-type]
        user,
        ConsentRequest(age_confirmed=True, consent=True, gender="female"),
    )

    assert is_new is False
    assert user.consent_given_at == first_time


async def test_repeat_consent_does_not_move_timestamp_forward() -> None:
    """AC-04: время согласия — аудиторский факт, а не время последнего нажатия."""
    old_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    user = make_user(gender="female", consent_given_at=old_time)
    session = session_for(user)

    await record_consent(
        session,  # type: ignore[arg-type]
        user,
        ConsentRequest(age_confirmed=True, consent=True, gender="female"),
    )

    assert user.consent_given_at == old_time
    assert user.consent_given_at.year == 2026
    assert user.consent_given_at.month == 1


async def test_gender_change_is_locked() -> None:
    """AC-04: смена пола запрещена — `GenderLockedError`, поля не меняются."""
    user = make_user(gender="female", consent_given_at=datetime(2026, 10, 1, tzinfo=UTC))
    session = session_for(user)

    with pytest.raises(GenderLockedError):
        await record_consent(
            session,  # type: ignore[arg-type]
            user,
            ConsentRequest(age_confirmed=True, consent=True, gender="male"),
        )

    assert user.gender == "female"
    assert session.flushed == 0


async def test_unknown_gender_is_rejected() -> None:
    """AC-03: пол вне набора `female`/`male` — `GenderRequiredError`."""
    user = make_user()
    session = session_for(user)

    with pytest.raises(GenderRequiredError):
        await record_consent(
            session,  # type: ignore[arg-type]
            user,
            ConsentRequest(age_confirmed=True, consent=True, gender="other"),
        )

    assert user.gender is None
    assert session.flushed == 0


def test_lock_statement_takes_row_lock() -> None:
    """Патч 2: запрос обязан содержать `FOR UPDATE`.

    Это единственный детерминированный способ доказать блокировку. Через API
    гонку не воспроизвести: оба запроса идут в одной сессии и одной транзакции,
    поэтому второй видит изменения первого независимо от блокировки —
    интеграционные тесты на «параллельную запись» проходят и без `FOR UPDATE`
    (проверено откатом патча: они остались зелёными).

    Без блокировки два параллельных `POST /consent` читают `gender=NULL` оба,
    оба проходят проверку «пол не зафиксирован» и оба пишут — побеждает
    последний, `409 gender_locked` не срабатывает ни у одного.
    """
    compiled = str(lock_statement(1).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled.upper()


def test_lock_statement_populates_existing_attributes() -> None:
    """Патч 2/3: запрос обязан перечитывать атрибуты, а не брать их из кеша сессии.

    `get_current_user` уже загрузил эту строку в сессию, и без
    `populate_existing` ORM вернул бы объект из identity map со старыми
    значениями. Блокировка была бы взята, но решение «пол уже зафиксирован»
    и подсчёт `is_new` принимались бы по снимку до неё — то есть оба дефекта,
    которые патчи закрывают, остались бы на месте.

    Проверяется через `execution_options`: это видимое свойство запроса,
    в отличие от поведения ORM, которое в unit-тесте не воспроизвести.
    """
    statement = lock_statement(1)
    assert statement.get_execution_options().get("populate_existing") is True


def test_lock_statement_targets_single_row_by_id() -> None:
    """Патч 2: блокируется ровно одна строка, по внутреннему `id`.

    `FOR UPDATE` без `WHERE` заблокировал бы всю таблицу `users`; блокировка
    по `uuid` из запроса открыла бы адресацию чужой учётки. Значение приходит
    из прочитанной строки (`user.id`), а не из тела запроса.
    """
    compiled = str(lock_statement(42).compile(dialect=postgresql.dialect()))
    assert "users.id" in compiled
    assert "WHERE" in compiled.upper()
