"""Доменная точка записи решения модератора (Task 8; AC-05, AC-06).

Здесь проверяется ЧИСТАЯ логика `apply_moderation_result`: целевой статус,
нормализация причины, границы и коды ошибок. Репозиторий подменяется
фейком, потому что эти правила от БД не зависят.

Проверки, которым нужна настоящая БД, лежат в
`tests/integration/test_photo_moderation_db.py`:

  * 8.7 — доказательство откатом через реальный CHECK
    `ck_profile_photos_rejection_reason` (`IntegrityError`, не доменная ошибка);
  * 6.1–6.4 — значения после `flush` без `refresh` и тест против `update()`.

Разделение вынужденное: `monkeypatch` репозитория не позволил бы увидеть,
что БД действительно отвергает пустую причину (§18.2 — иначе тест проверял бы
сам себя).

§18.1: каждый тест краснеет при удалении проверяемого правила.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.enums import PhotoStatus
from app.services import photo_service
from app.services.photo_service import (
    MAX_REJECTION_REASON_LEN,
    ModerationTransitionError,
    PhotoNotFoundError,
    apply_moderation_result,
)


class _FakeModerationRepo:
    """Репозиторий с одним фото: запоминает записанные значения."""

    def __init__(self, *, photo: Any | None) -> None:
        self._photo = photo
        self.written: dict[str, Any] | None = None
        self.locked = False

    async def get_by_uuid_for_update(self, photo_uuid: uuid_lib.UUID) -> Any | None:
        self.locked = True
        return self._photo

    async def set_moderation_result(
        self,
        photo: Any,
        *,
        status: PhotoStatus,
        rejection_reason: str | None,
        moderated_at: datetime,
    ) -> None:
        self.written = {
            "status": status,
            "rejection_reason": rejection_reason,
            "moderated_at": moderated_at,
        }


class _FakeSession:
    """Сессия-заглушка: `commit` в сервисе не вызывается (D-4 стори 1.2)."""

    async def commit(self) -> None:  # pragma: no cover - страховка
        raise AssertionError("commit() не должен вызываться сервисом")


def _photo() -> Any:
    return SimpleNamespace(
        uuid=uuid_lib.uuid4(), status="pending", rejection_reason=None, moderated_at=None
    )


def _install(monkeypatch: pytest.MonkeyPatch, *, photo: Any | None) -> _FakeModerationRepo:
    repository = _FakeModerationRepo(photo=photo)
    monkeypatch.setattr(photo_service, "ProfilePhotoRepository", lambda session: repository)
    return repository


# --- 8.1: APPROVED ---------------------------------------------------------------


async def test_approved_sets_moderated_at_and_clears_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 8.1 (AC-06): `APPROVED` → `approved`, причина `None`, время заполнено.

    §18.1: убрать `moderated_at = datetime.now(UTC)` — тест покраснеет на
    `written["moderated_at"] is None`. Отдельно проверяется, что причина
    сбрасывается: одобренное фото не должно нести текст прошлого отказа.
    """
    repository = _install(monkeypatch, photo=_photo())

    await apply_moderation_result(
        _FakeSession(),
        uuid_lib.uuid4(),
        status=PhotoStatus.APPROVED,
        rejection_reason="остаток прошлого отказа",
    )

    assert repository.written is not None
    assert repository.written["status"] is PhotoStatus.APPROVED
    assert repository.written["rejection_reason"] is None
    moderated_at = repository.written["moderated_at"]
    assert isinstance(moderated_at, datetime)
    assert moderated_at.tzinfo is not None


# --- 8.2: REJECTED с причиной ----------------------------------------------------


async def test_rejected_strips_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 8.2 (AC-05): причина сохраняется без крайних пробелов.

    AC-05: `btrim` в CHECK страхует только от полностью пустой строки;
    пробелы по краям попали бы в карточку владельца. §18.1: убрать
    `.strip(...)` — тест покраснеет на точном равенстве.
    """
    repository = _install(monkeypatch, photo=_photo())

    await apply_moderation_result(
        _FakeSession(),
        uuid_lib.uuid4(),
        status=PhotoStatus.REJECTED,
        rejection_reason="  не видно лица\t\n",
    )

    assert repository.written is not None
    assert repository.written["status"] is PhotoStatus.REJECTED
    assert repository.written["rejection_reason"] == "не видно лица"


# --- 8.3: плохая причина ---------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [None, "", "   ", "\t\r\n", "x" * (MAX_REJECTION_REASON_LEN + 1)],
)
async def test_invalid_reason_raises_domain_error(
    monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    """Task 8.3 (AC-05, AC-06): плохая причина — `moderation_reason_invalid`.

    Пустая/отсутствующая причина нарушила бы CHECK и дала бы `500`;
    слишком длинная записала бы в карточку килобайты текста.

    §18.1: убрать проверку — вместо доменной ошибки придёт `IntegrityError`
    из БД (это же и доказывает тест 8.7 на реальной БД).
    Отдельно проверяется, что строка не тронута: проверка идёт ДО записи.
    """
    repository = _install(monkeypatch, photo=_photo())

    with pytest.raises(ModerationTransitionError) as exc_info:
        await apply_moderation_result(
            _FakeSession(),
            uuid_lib.uuid4(),
            status=PhotoStatus.REJECTED,
            rejection_reason=reason,
        )

    assert exc_info.value.code == "moderation_reason_invalid"
    assert repository.written is None
    # Репозиторий даже не читал строку: причина проверяется раньше блокировки.
    assert repository.locked is False


# --- 8.4: цель PENDING -----------------------------------------------------------


async def test_pending_target_is_transition_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 8.4 (AC-06): цель `pending` — `moderation_transition_invalid`.

    Код отличается от «плохой причины» намеренно: кабинет модератора (3.7)
    показывает разные сообщения на «нельзя вернуть в очередь» и «причина
    не принята» (VS 2.4, Enhancement). §18.1: убрать ветку — тест покраснеет
    на `DID NOT RAISE`.
    """
    repository = _install(monkeypatch, photo=_photo())

    with pytest.raises(ModerationTransitionError) as exc_info:
        await apply_moderation_result(_FakeSession(), uuid_lib.uuid4(), status=PhotoStatus.PENDING)

    assert exc_info.value.code == "moderation_transition_invalid"
    assert repository.written is None


# --- 8.5: фото нет ---------------------------------------------------------------


async def test_missing_photo_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 8.5 (AC-06): несуществующий uuid — `PhotoNotFoundError`.

    Тот же тип, что у владельца (AC-09 стори 2.2): «фото нет» не подтверждается
    отдельным кодом, иначе перебором uuid можно было бы выяснять, что есть в БД.
    """
    _install(monkeypatch, photo=None)

    with pytest.raises(PhotoNotFoundError):
        await apply_moderation_result(
            _FakeSession(),
            uuid_lib.uuid4(),
            status=PhotoStatus.APPROVED,
        )


# --- 8.6: переходы между решёнными -----------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [("approved", PhotoStatus.REJECTED), ("rejected", PhotoStatus.APPROVED)],
)
async def test_decided_status_can_change(
    monkeypatch: pytest.MonkeyPatch, current: str, target: PhotoStatus
) -> None:
    """Task 8.6 (AC-06): `approved ↔ rejected` разрешены — модератор передумал.

    §18.1: добавить запрет перехода между решёнными статусами — тест
    покраснеет. Новый `moderated_at` перезаписывает прежний: история решения
    в MVP не ведётся, а старое время сделало бы «свежесть» решения ложной.
    """
    photo = _photo()
    photo.status = current
    photo.moderated_at = datetime(2020, 1, 1, tzinfo=UTC)
    repository = _install(monkeypatch, photo=photo)

    await apply_moderation_result(
        _FakeSession(),
        uuid_lib.uuid4(),
        status=target,
        rejection_reason="передумал" if target is PhotoStatus.REJECTED else None,
    )

    assert repository.written is not None
    assert repository.written["status"] is target
    assert repository.written["moderated_at"] > datetime(2020, 1, 1, tzinfo=UTC)


# --- 8.8: границы limit ----------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_pending_limit_out_of_range_raises(limit: int) -> None:
    """Task 8.8 (AC-08): `limit` вне `1..100` — `ValueError`.

    §18.1: убрать проверку — PostgreSQL молча вернёт пустой список на `LIMIT 0`,
    и «очередь пуста» станет неотличимо от «передали ноль». Тест краснеет,
    потому что исключения не будет.
    """
    from app.repositories.profile_photos import ProfilePhotoRepository

    with pytest.raises(ValueError):
        # Сессия не нужна: проверка идёт до выполнения запроса.
        import asyncio

        # CR 2.4 патч 12: цикл закрывается. `new_event_loop()` без `close()`
        # течёт файловым дескриптором и падает при `-W error::ResourceWarning`.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                ProfilePhotoRepository(None).list_pending(limit)  # type: ignore[arg-type]
            )
        finally:
            loop.close()


@pytest.mark.parametrize("limit", [1, 100])
def test_pending_limit_bounds_accepted(limit: int) -> None:
    """Task 8.8 (AC-08): границы `1` и `100` — валидны.

    Парный к предыдущему: без него проверка «вне диапазона» проходила бы
    и при `raise ValueError` на любом значении, то есть очередь сломалась бы
    полностью. Здесь запрос строится, но сессия-заглушка возвращает пусто.
    """
    import asyncio

    from app.repositories.profile_photos import ProfilePhotoRepository

    class _EmptySession:
        async def execute(self, statement: Any) -> Any:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            ProfilePhotoRepository(_EmptySession()).list_pending(limit)  # type: ignore[arg-type]
        )
    finally:
        loop.close()
    assert result == []


# --- 7.12: структура SQL очереди --------------------------------------------------


def _compiled_list_pending(limit: int = 50) -> str:
    """Захватывает `Select`, построенный `list_pending`, и компилирует его.

    Метод строит запрос и только потом выполняет, поэтому фейковая сессия
    ловит statement, а текст SQL доступен без БД (паттерн
    `test_profile_lock_statement.py`).
    """
    import asyncio

    from sqlalchemy.dialects import postgresql

    from app.repositories.profile_photos import ProfilePhotoRepository

    class _CapturingSession:
        def __init__(self) -> None:
            self.statement: Any = None

        async def execute(self, statement: Any) -> Any:
            self.statement = statement
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    session = _CapturingSession()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            ProfilePhotoRepository(session).list_pending(limit)  # type: ignore[arg-type]
        )
    finally:
        loop.close()
    assert session.statement is not None
    return str(session.statement.compile(dialect=postgresql.dialect()))


def test_list_pending_sql_has_no_gender_and_no_profiles_join() -> None:
    """Task 7.12 (AC-08, D-8): в SQL очереди нет `gender` и нет join на `profiles`.

    §18.1: добавить в `list_pending` join на `profiles` ради «приоритета
    по полу» — тест покраснеет на обоих ассертах. Это структурная половина
    AC-08; поведенческая (фактический порядок двух разнополых анкет) —
    `test_list_pending_order_is_fifo_and_gender_independent`.

    Проверяется и то, что очередь опирается на частичный индекс
    (`status = 'pending'`, project-context §7) и не тянет всю таблицу.
    """
    sql = _compiled_list_pending()

    assert "gender" not in sql
    # Join на `profiles` дал бы и `JOIN`, и квалифицированные колонки чужой
    # таблицы (`profiles.id`). Проверяются оба признака: одного `JOIN` мало,
    # если когда-нибудь появится подзапрос.
    assert "JOIN" not in sql.upper()
    assert "profiles." not in sql
    # D-8: FIFO по `created_at, id`. CR 2.4 патч 7: проверяется ТОЧНАЯ фраза
    # `ORDER BY`, а не подстроки `created_at`/`profile_photos.id` — они есть
    # в списке SELECT всегда, поэтому прежние ассерты не могли упасть ни при
    # удалении `order_by`, ни при потере второго ключа (тавтология §18.2).
    assert "ORDER BY profile_photos.created_at, profile_photos.id" in sql
    # project-context §7: условие частичного индекса — в `WHERE`, а не в SELECT.
    assert "WHERE profile_photos.status = " in sql
    assert "LIMIT " in sql.upper()


# --- CR 2.4: устойчивость точки записи к типу аргумента ---------------------------


@pytest.mark.parametrize("raw_status", ["pending", "PENDING"])
async def test_string_pending_is_rejected_like_enum(
    monkeypatch: pytest.MonkeyPatch, raw_status: str
) -> None:
    """CR 2.4 патч 1: строка `"pending"` не обходит запрет, как раньше.

    §18.1: убрать `status = PhotoStatus(status)` — сравнение `"pending" is
    PhotoStatus.PENDING` даёт `False`, запрет не срабатывает, и в БД уезжает
    строка, нарушающая CHECK (`IntegrityError` → `500` вместо доменного кода).
    Кабинет 3.7 получает статус из JSON и вполне может передать строку.
    """
    repository = _install(monkeypatch, photo=_photo())

    with pytest.raises(ModerationTransitionError) as exc_info:
        await apply_moderation_result(
            _FakeSession(),
            uuid_lib.uuid4(),
            status=raw_status,  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "moderation_transition_invalid"
    assert repository.written is None


async def test_string_approved_clears_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR 2.4 патч 1: строка `"approved"` даёт тот же результат, что и enum.

    §18.1: без нормализации причина из аргумента уехала бы в БД у одобренного
    фото и нарушила CHECK `status_consistency` (`IntegrityError` → `500`).
    """
    repository = _install(monkeypatch, photo=_photo())

    await apply_moderation_result(
        _FakeSession(),
        uuid_lib.uuid4(),
        status="approved",
        rejection_reason="мусор",  # type: ignore[arg-type]
    )

    assert repository.written is not None
    assert repository.written["status"] is PhotoStatus.APPROVED
    assert repository.written["rejection_reason"] is None


async def test_unknown_status_code_is_transition_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR 2.4 патч 1: неизвестный статус — доменная ошибка, не `ValueError` → `500`."""
    repository = _install(monkeypatch, photo=_photo())

    with pytest.raises(ModerationTransitionError) as exc_info:
        await apply_moderation_result(
            _FakeSession(),
            uuid_lib.uuid4(),
            status="deleted",  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "moderation_transition_invalid"
    assert repository.written is None


@pytest.mark.parametrize("reason", ["\u00a0", "\u2009\u202f", "\u3000"])
async def test_unicode_whitespace_reason_is_invalid(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """CR 2.4 патч 4: причина из юникод-пробелов не проходит как валидная.

    §18.1: вернуть явный набор `\" \\t\\r\\n\\f\\v\"` — NBSP не режется,
    причина считается непустой, и владелец видит пустую карточку отказа.
    """
    repository = _install(monkeypatch, photo=_photo())

    with pytest.raises(ModerationTransitionError) as exc_info:
        await apply_moderation_result(
            _FakeSession(),
            uuid_lib.uuid4(),
            status=PhotoStatus.REJECTED,
            rejection_reason=reason,
        )

    assert exc_info.value.code == "moderation_reason_invalid"
    assert repository.written is None


@pytest.mark.parametrize("bad_limit", [None, "50", 50.5, True])
def test_pending_limit_rejects_non_int(bad_limit: Any) -> None:
    """CR 2.4 патч 2: нечисловой `limit` — `ValueError`, а не `TypeError`/проход.

    §18.1: убрать проверку типа — `None`/`"50"` дают `TypeError` (→ `500`),
    а `True`/`50.5` проходят сравнение насквозь. Кабинет 3.6 получает `limit`
    из query-параметра, то есть значение приходит строкой.
    """
    import asyncio

    from app.repositories.profile_photos import ProfilePhotoRepository

    with pytest.raises(ValueError):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                ProfilePhotoRepository(None).list_pending(bad_limit)  # type: ignore[arg-type]
            )
        finally:
            loop.close()


# --- CR 2.4 патч 3: классификация IntegrityError -------------------------------


def _integrity_error(constraint: str | None) -> IntegrityError:
    """Собирает `IntegrityError` с тем же `orig.constraint_name`, что даёт asyncpg."""
    error = IntegrityError("INSERT ...", {}, Exception("duplicate key"))
    error.orig = SimpleNamespace(constraint_name=constraint)  # type: ignore[assignment]
    return error


def test_position_race_detected_by_constraint_name() -> None:
    """CR 2.4 патч 3: гонка за позицию распознаётся по имени ограничения.

    §18.1: убрать `_is_position_race` и вернуть безусловный `409` — тест
    `test_foreign_integrity_error_is_not_limit` ниже краснеет на `409`
    у нарушения CHECK вместо `500`.
    """
    is_race = photo_service._is_position_race
    assert is_race(_integrity_error("uq_profile_photos_profile_id_position"))
    assert is_race(_integrity_error("uq_profile_photos_storage_key"))
    assert not is_race(_integrity_error("ck_profile_photos_content_type"))
    assert not is_race(_integrity_error(None))


# --- CR 2.4 патч 15 (AC-10): Python-дефолт статуса ---------------------------------


def test_model_keeps_python_default_for_status() -> None:
    """AC-10: у `status` есть Python-дефолт, а не только `server_default`.

    Прежняя формулировка AC-10 требовала «тест, падающий при удалении
    `default=str(PhotoStatus.PENDING)`». Такой тест через БД невозможен:
    `server_default='pending'` возвращает значение через RETURNING, и INSERT
    проходит без Python-дефолта (DS зафиксировал `0` красных).

    Настоящий инвариант — не поведение БД, а наличие ORM-дефолта: он нужен,
    чтобы `photo.status` был заполнен сразу после `flush` без `refresh`
    (`PhotoOwn.from_photo` читает атрибут, а `PhotoStatus(None)` → `ValueError`).
    §18.1: убрать `default=str(PhotoStatus.PENDING)` — тест краснеет на `None`.
    """
    from app.models.profile_photo import ProfilePhoto

    column_default = ProfilePhoto.__table__.c.status.default
    assert column_default is not None
    assert column_default.arg == "pending"
