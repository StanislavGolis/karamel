"""Unit-тесты сервиса удаления аккаунта (стори 10.2; AC-01, AC-03, AC-05, AC-06).

**БД не нужна.** Сервис меняет поля строки и зовёт `flush`; предмет теста —
арифметика окна и идемпотентность, а не SQL. Сессия подменена заглушкой,
которая считает вызовы: так проверяется, что `cancel_deletion` без пометки
не делает `flush` вовсе.

**P-19: граница окна проверяется ПО ОБЕ СТОРОНЫ.** `now - 7d + 1s` — окно
активно, доступ есть. `now - 7d - 1s` — истекло, доступ закрыт. Значение
`now()` без смещения не доказывает ничего: оно внутри окна.

**18.2: на каждый тест — какая правка сделает его красным.**
- `test_window_boundary_*` — красные при смене `DELETION_WINDOW_DAYS` на 0
  или при замене предиката на `is not None` (старое поведение);
- `test_request_deletion_*` — красные при продлении окна повторным `POST`;
- `test_cancel_*` — красные, если `cancel_deletion` начнёт звать `flush`
  без пометки или не снимет её.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.user import User
from app.services.account_deletion_service import (
    DELETION_WINDOW,
    DELETION_WINDOW_DAYS,
    DeletionAlreadyRequestedError,
    cancel_deletion,
    deletion_window_expired,
    purge_after,
    read_status,
    request_deletion,
)


class SessionStub:
    """Заглушка `AsyncSession`: считает `flush`, ничего не пишет.

    **P-1 (CR 10.2): появились `execute` и `refresh`.** `request_deletion`
    теперь захватывает строку `FOR UPDATE` перед проверкой метки, поэтому
    стабу нужны оба метода. Здесь они no-op НАМЕРЕННО: предмет unit-теста —
    арифметика окна и идемпотентность, а не блокировка. Сама блокировка
    доказывается гоночным тестом `test_account_deletion_races.py` на двух
    соединениях (§18.3) — в одной сессии `FOR UPDATE` ведёт себя как без него,
    и здесь он ничего не доказал бы.

    `refresh` НЕ обновляет поле (в заглушке нет БД): значение остаётся тем,
    что выставлено в объекте, поэтому последовательный путь проверяется как
    прежде — второй вызов видит метку, поставленную первым.
    """

    def __init__(self) -> None:
        self.flushes = 0
        self.locks = 0
        self.refreshes = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def execute(self, _statement: Any) -> None:
        self.locks += 1
        return None

    async def refresh(self, _instance: Any, attribute_names: Any = None) -> None:
        self.refreshes += 1
        return None


def _user(**overrides: Any) -> User:
    """Отсоединённый объект: ORM-сессии нет, поля заданы явно."""
    user = User(
        provider="phone",
        provider_user_id="+79001234567",
        phone="+79001234567",
        community_slug="tomsk",
    )
    user.uuid = uuid_lib.uuid4()
    user.status = "active"
    user.deletion_requested_at = overrides.get("deletion_requested_at")
    return user


# --- Окно: константа и обе стороны границы (P-19) ---


def test_window_is_seven_days() -> None:
    """NFR37, D-10.2-4: окно ровно 7 суток — продуктовое требование.

    Красным станет при `DELETION_WINDOW_DAYS = 0`: это ровно та подмена
    конфигом, от которой защищает решение «константа, не `Settings`».
    """
    assert DELETION_WINDOW_DAYS == 7
    assert timedelta(days=7) == DELETION_WINDOW


def test_window_boundary_inside_allows_access() -> None:
    """AC-01, P-19: `now - 7d + 1s` — окно активно, доступ есть.

    Красным станет, если предикат заменят на `is not None` (старое поведение
    D-10.2-1) — тогда любой запрос удаления закрывал бы доступ.
    """
    now = datetime.now(UTC)
    # Граница — ЖЁСТКИЕ 7 суток: тест на относительной границе двигал обе
    # стороны вместе с константой и выживал при мутации M6 (см. парный тест).
    user = _user(deletion_requested_at=now - timedelta(days=7) + timedelta(seconds=1))

    assert deletion_window_expired(user, now=now) is False


def test_window_boundary_outside_denies_access() -> None:
    """AC-06, P-19: `now - 7d - 1s` — окно истекло, доступ закрыт.

    **Границы заданы ЖЁСТКИМИ 7 сутками, а не `DELETION_WINDOW`.** Мутация
    `DELETION_WINDOW_DAYS = 30` на относительной границе выжила (M6 SURVIVED):
    тест двигал обе стороны вместе с константой и ничего не доказывал.
    Красным станет при любом изменении окна или знака сравнения.
    """
    now = datetime.now(UTC)
    user = _user(deletion_requested_at=now - timedelta(days=7) - timedelta(seconds=1))

    assert deletion_window_expired(user, now=now) is True


def test_window_boundary_exactly_seven_days_is_expired() -> None:
    """AC-06: ровно 7 суток — граница включительно (`<=`), окно закрыто.

    Красным станет при замене `<=` на `<`: ровно семидневная пометка
    считалась бы активной, и воркер не удалил бы такого пользователя никогда.
    """
    now = datetime.now(UTC)
    user = _user(deletion_requested_at=now - timedelta(days=7))

    assert deletion_window_expired(user, now=now) is True


def test_no_markup_never_expires() -> None:
    """AC-01: без пометки окно не истекает — доступ всегда есть."""
    assert deletion_window_expired(_user(), now=datetime.now(UTC)) is False


def test_naive_requested_at_treated_as_utc() -> None:
    """AC-06: naive-метка трактуется как UTC, а не как зона процесса.

    Красным станет, если убрать `replace(tzinfo=UTC)`: в зоне +09 сравнение
    сдвинулось бы на 9 часов и граница проверялась бы неверно.
    """
    now = datetime.now(UTC)
    naive = (now - timedelta(days=7) - timedelta(hours=1)).replace(tzinfo=None)
    user = _user(deletion_requested_at=naive)

    assert deletion_window_expired(user, now=now) is True


def test_purge_after_is_request_time_plus_window() -> None:
    """AC-01, AC-05: `purge_after` считается от даты запроса, не от `now`.

    Красным станет, если считать от текущего момента: после правки на `now`
    значение «поплывёт» между вызовами, и клиент увидит разные сроки.
    """
    requested = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    assert purge_after(requested) == requested + DELETION_WINDOW


# --- request_deletion: окно не продлевается (AC-01, D-10.2-7) ---


async def test_request_deletion_sets_markup_and_returns_pair() -> None:
    """AC-01: пометка ставится, возврат — `(requested_at, purge_after)`."""
    session = SessionStub()
    user = _user()
    now = datetime.now(UTC)

    requested_at, purge_at = await request_deletion(session, user, now=now)

    assert user.deletion_requested_at == now
    assert requested_at == now
    assert purge_at == now + DELETION_WINDOW
    assert session.flushes == 1


async def test_request_deletion_twice_does_not_extend_window() -> None:
    """AC-01, D-10.2-7: повторный `POST` окно НЕ продлевает.

    Возвращается исходный `purge_after`, а не новый. Красным станет, если
    сервис перезапишет `deletion_requested_at`: клиент смог бы отодвигать
    удаление бесконечно, то есть не удалять никогда.
    """
    session = SessionStub()
    original = datetime.now(UTC) - timedelta(days=2)
    user = _user(deletion_requested_at=original)

    with pytest.raises(DeletionAlreadyRequestedError) as exc:
        await request_deletion(session, user, now=datetime.now(UTC))

    assert exc.value.purge_after == original + DELETION_WINDOW
    assert user.deletion_requested_at == original
    assert session.flushes == 0


def test_already_requested_code() -> None:
    """AC-01, D-10.2-7: код ошибки — `deletion_already_requested` (не 200)."""
    assert DeletionAlreadyRequestedError(datetime.now(UTC)).code == "deletion_already_requested"


# --- cancel_deletion: идемпотентность (AC-03) ---


async def test_cancel_deletion_clears_markup() -> None:
    """AC-03: пометка снимается, данные целы."""
    session = SessionStub()
    user = _user(deletion_requested_at=datetime.now(UTC) - timedelta(days=1))

    await cancel_deletion(session, user)

    assert user.deletion_requested_at is None
    assert session.flushes == 1


async def test_cancel_deletion_twice_is_idempotent() -> None:
    """AC-03: повторная отмена — `204`, без записи в БД.

    Красным станет, если убрать ранний `return`: второй `DELETE` делал бы
    `UPDATE` без изменений, а при `flush` на пустом объекте — лишний запрос.
    """
    session = SessionStub()
    user = _user()

    await cancel_deletion(session, user)

    assert user.deletion_requested_at is None
    assert session.flushes == 0


# --- read_status: единственный источник даты (AC-05) ---


def test_read_status_without_markup_gives_nulls() -> None:
    """AC-05: без пометки оба поля `null`, а не отсутствие ключей."""
    status = read_status(_user())

    assert status.deletion_requested_at is None
    assert status.purge_after is None


def test_read_status_with_markup_gives_pair() -> None:
    """AC-05: с пометкой отдаются обе даты, `purge_after` — от даты запроса."""
    requested = datetime.now(UTC) - timedelta(days=3)
    status = read_status(_user(deletion_requested_at=requested))

    assert status.deletion_requested_at == requested
    assert status.purge_after == requested + DELETION_WINDOW


# --- P-12 (CR 10.2): нормализация `now` ---


def test_naive_now_is_treated_as_utc_in_predicate() -> None:
    """P-12: naive `now` не даёт `TypeError` и не сдвигает зону.

    До правки `requested_at <= now - DELETION_WINDOW` падал с `TypeError`
    при naive `now` (aware минус naive), то есть `run_account_purges(now=…)`
    с naive-значением давал 500 в гарде. Красным станет, если убрать
    `as_utc` хотя бы с одной стороны сравнения.
    """
    # `now` naive, метка aware: сравнение обязано пройти, а не упасть.
    aware_requested = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    user = _user(deletion_requested_at=aware_requested)
    naive_now = datetime(2026, 1, 20, 0, 0)

    # 19 суток > 7 — окно истекло; главное, что вызов НЕ падает.
    assert deletion_window_expired(user, now=naive_now) is True

    # Сторона «окно активно»: naive `now` трактуется как UTC, а не как локальное.
    assert deletion_window_expired(user, now=datetime(2026, 1, 5, 0, 0)) is False


def test_naive_now_normalized_when_writing_markup() -> None:
    """P-12: в БД пишется aware-значение, даже если `now` пришёл naive.

    Иначе `requested_at` в строке оказался бы naive, и SQL-предикаты
    репозитория (`deletion_requested_at <= now - 7d`) сдвинули бы зону
    на смещение процесса. Красным станет, если `as_utc(now)` убрать.
    """
    import asyncio

    user = _user(deletion_requested_at=None)
    session = SessionStub()

    requested_at, purge_after_value = asyncio.run(
        request_deletion(session, user, now=datetime(2026, 3, 1, 12, 0))
    )

    assert requested_at.tzinfo is not None
    assert requested_at == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert purge_after_value == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
