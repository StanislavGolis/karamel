"""Сервис карточки: создание, правка, публикация (Task 3.8, 4.8, 5.7).

§18.2: проверяется наблюдаемый результат (значения полей, коды ошибок),
а не факт вызова. Репозиторий и работа с местом подменяются — тест идёт без БД.

§18.1: в каждом docstring указано, какая правка сделает тест красным.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.enums import CardVerificationStatus, EventCardStatus
from app.models.event_card import EventCard
from app.schemas.event_card import EventCardCreate, EventCardUpdate
from app.services.event_card_service import (
    CARD_EDIT_REQUIRES_REVIEW_CODE,
    CARD_LIMIT_REACHED_CODE,
    CARD_REJECTED_CODE,
    CARD_STARTS_AT_TOO_FAR_CODE,
    CARD_VERIFICATION_PENDING_CODE,
    CardDecisionWindowRequiredError,
    CardEditRequiresReviewError,
    CardLimitReachedError,
    CardNotDraftError,
    CardNotFoundError,
    CardRejectedError,
    CardStartsAtInPastError,
    CardStartsAtTooFarError,
    CardVerificationPendingError,
    create_card,
    publish_card,
    update_card,
)

PLACE_UUID = uuid_lib.UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
STARTS_AT = NOW + timedelta(days=3)


class _Settings:
    """Минимум настроек: четыре поля, без чтения `.env`.

    `card_content_stop_words` появилось в 4.3 (AC-02): автопроверка читает
    стоп-слова из настроек, и без поля `create_card`/`update_card` падают
    `AttributeError` (то есть тест ловит реальную несовместимость протокола).
    """

    default_city_slug = "tomsk"
    city_slugs = ("tomsk",)
    card_default_decision_window_minutes = 2880
    card_content_stop_words = ("казино", "займ")
    # Стори 4.4: лимит активных карточек. Дефолт 2 — верхняя граница MVP.
    card_active_limit = 2


class _FakeSession:
    """Сессия-заглушка: `commit` считает вызовы, БД не трогает."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _FakeRepository:
    """Подмена `EventCardRepository`: отдаёт заданную карточку, запоминает `add`."""

    def __init__(
        self,
        card: EventCard | None = None,
        *,
        active_count: int = 0,
        cas_rowcount: int = 1,
        sequence: list[EventCard] | None = None,
    ) -> None:
        self.card = card
        self.added: list[EventCard] = []
        self.active_count = active_count
        self.cas_rowcount = cas_rowcount
        self.calls: list[str] = []
        self.sequence: list[EventCard] = list(sequence or [])
        # X-6.1-1 (chore P-7): значения `for_update`, с которыми сервис
        # запрашивал карточку. Отдельное поле, а не `calls`: три теста
        # сравнивают `calls` точно, и лишняя запись сломала бы их контракт.
        self.for_update_requests: list[bool] = []
        # Последняя ВЫДАННАЯ карточка: CAS пишет в неё (при `sequence`
        # `self.card` может быть пустым — путь проигравшего выбирает
        # состояние из последовательности).
        self.last: EventCard | None = card

    def add(self, card: EventCard) -> None:
        self.added.append(card)

    async def get_owned(
        self, author_id: int, card_uuid: uuid_lib.UUID, *, for_update: bool = False
    ) -> EventCard | None:
        """Отдаёт `sequence` по порядку (для пути `rowcount == 0`), иначе `card`.

        Ветка проигравшего CAS перечитывает карточку: без последовательности
        подмена вернула бы тот же объект, и резолвер получил бы исходное
        состояние вместо актуального.

        `for_update` (X-6.1-1, chore P-7) принимается и запоминается в
        ОТДЕЛЬНОМ поле: сигнатура обязана совпадать с
        `EventCardRepository.get_owned`, иначе `PATCH` падает `TypeError`.
        В `calls` значение не пишется намеренно — три теста сравнивают `calls`
        точно (`["lock", "count", "cas"]`), и лишняя запись сломала бы
        контракт проверки порядка. Сам текст `FOR UPDATE` доказывается
        интеграционным тестом на `before_cursor_execute`.
        """
        self.for_update_requests.append(for_update)
        if self.sequence:
            self.last = self.sequence.pop(0)
        else:
            self.last = self.card
        return self.last

    # --- Стори 4.4: контракт публикации под лимитом -------------------------
    # Подмена повторяет ТОЛЬКО то, что вызывает сервис: сам SQL проверяется
    # в `test_card_active_limit.py` без БД (§18.2).
    async def lock_author(self, author_id: int) -> None:
        """AC-04: блокировка автора — только журнал порядка."""
        self.calls.append("lock")

    async def count_active(self, author_id: int) -> int:
        """AC-01: число активных карточек задаётся тестом."""
        self.calls.append("count")
        return self.active_count

    async def publish_if_draft(
        self,
        *,
        card_id: int,
        author_id: int,
        now: datetime,
        expires_at: datetime,
    ) -> int:
        """AC-06: CAS. `rowcount` задаётся тестом; успех пишет поля карточки."""
        self.calls.append("cas")
        if self.cas_rowcount == 0:
            return 0
        assert self.last is not None
        self.last.status = EventCardStatus.PUBLISHED
        self.last.published_at = now
        self.last.expires_at = expires_at
        return 1


def _payload(**overrides: Any) -> EventCardCreate:
    values: dict[str, Any] = {
        "category": "cafe",
        "place_uuid": PLACE_UUID,
        "starts_at": STARTS_AT,
        "capacity": 3,
        # Стори 12.4 (AC-3, FR117/FR118): тип и активность обязательны.
        "kind": "company",
        "activity": "boardgames",
    }
    values.update(overrides)
    return EventCardCreate(**values)


def _card(**overrides: Any) -> EventCard:
    """Черновик в памяти: проверяется поведение сервиса, не БД."""
    values: dict[str, Any] = {
        "author_id": 7,
        "place_id": 42,
        "city_slug": "tomsk",
        # Стори 13.2 (AC-7): title — NOT NULL в БД, в фабрике модели задаётся явно.
        "title": "Настольные игры",
        # Стори 12.4 (AC-1): поля NOT NULL в модели.
        "kind": "company",
        "activity": "boardgames",
        "category": "cafe",
        "starts_at": STARTS_AT,
        "capacity": 3,
        "decision_window_minutes": 2880,
        "expires_at": STARTS_AT - timedelta(hours=48),
        "status": EventCardStatus.DRAFT,
        "verification_status": CardVerificationStatus.PENDING,
        "filled_count": 0,
    }
    values.update(overrides)
    return EventCard(**values)


@pytest.fixture(autouse=True)
def _patch_place_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заменяет все точки работы с местом: гард, резолв `place_id` и `_resolve_place`.

    Все три обращаются к БД, поэтому все три подменяются в модуле сервиса.
    `_resolve_place` появился в стори 13.2 (AC-3): заголовку нужна категория
    места, а не только `place_id`, поэтому сервис читает строку целиком.
    Заглушка отдаёт объект с теми же тремя полями, которые читает прод-код
    (`id`, `category`, `name`) — тест не должен зависеть от модели `Place`.
    """

    class _Place:
        """Минимум полей, которые читает сервис: `id`, `category`, `name`."""

        id = 42
        category = "cafe"
        name = "Прага"

    async def _ensure(*args: Any, **kwargs: Any) -> None:
        return None

    async def _resolve_id(*args: Any, **kwargs: Any) -> int:
        return 42

    async def _resolve(*args: Any, **kwargs: Any) -> _Place:
        return _Place()

    monkeypatch.setattr("app.services.event_card_service.ensure_place_available", _ensure)
    monkeypatch.setattr("app.services.event_card_service._resolve_place_id", _resolve_id)
    monkeypatch.setattr("app.services.event_card_service._resolve_place", _resolve)


def _run(coro: Any) -> Any:
    """Синхронный запуск корутины в тестах создания (без `pytest-asyncio`)."""
    return asyncio.run(coro)


def test_create_open_set_uses_config_window() -> None:
    """AC-04 (FR70): при вместимости 2–6 окно — дефолт конфига (2880).

    Краснеет, если окно брать из payload (для открытого набора он `None`)
    или подставлять константу вместо настройки — смена дефолта в `.env`
    перестала бы работать.
    """
    card = _run(
        create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            payload=_payload(capacity=3),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )
    )

    assert card.decision_window_minutes == 2880
    assert card.expires_at == NOW + timedelta(minutes=2880)


def test_create_request_mode_uses_client_window() -> None:
    """AC-04 (FR70/FR79): при вместимости 1 окно — клиентское.

    Краснеет, если подставлять дефолт и в режиме заявок: автор указал
    «30 минут», а карточка закрылась бы через 48 часов.
    """
    card = _run(
        create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            payload=_payload(capacity=1, decision_window_minutes=30),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )
    )

    assert card.decision_window_minutes == 30
    assert card.expires_at == NOW + timedelta(minutes=30)


def test_create_expires_at_is_capped_by_starts_at() -> None:
    """AC-04 (ADR-02): окно не закрывается позже начала (CHECK в БД).

    Краснеет, если убрать `min`: окно 48 ч при встрече через час дало бы
    `expires_at > starts_at`, и вставка упала бы `IntegrityError` (`500`).
    """
    card = _run(
        create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            payload=_payload(capacity=1, decision_window_minutes=60),
            settings=_Settings(),  # type: ignore[arg-type]
            now=STARTS_AT - timedelta(minutes=30),
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )
    )

    assert card.expires_at == STARTS_AT


def test_create_rejects_past_starts_at() -> None:
    """AC-03: время начала в прошлом → `CardStartsAtInPastError`.

    Краснеет, если сравнение заменить на `>=` (равное «сейчас» пройдёт) или
    убрать проверку — карточка создалась бы на прошедшую встречу.
    """
    with pytest.raises(CardStartsAtInPastError):
        _run(
            create_card(
                _FakeSession(),  # type: ignore[arg-type]
                author_id=7,
                payload=_payload(starts_at=NOW - timedelta(minutes=1)),
                settings=_Settings(),  # type: ignore[arg-type]
                now=NOW,
                repository=_FakeRepository(),  # type: ignore[arg-type]
            )
        )


def test_create_initial_state_is_draft_with_auto_check() -> None:
    """AC-01 стори 4.3 (ADR-08): `draft` + результат автопроверки.

    До 4.3 здесь ожидался `pending` для любого текста: автопроверки не было
    (D-4.2-5, X-20). Теперь чистый текст синхронно становится `approved`
    с `verified_at` и `verified_by IS NULL` (NULL = автопроверка).

    Краснеет, если убрать `run_auto_check` из `create_card`: текст остался бы
    `pending`, и публикация требовала бы SQL-правки.
    """
    card = _run(
        create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            payload=_payload(),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )
    )

    assert card.status == EventCardStatus.DRAFT
    assert card.verification_status == CardVerificationStatus.APPROVED
    assert card.verified_at == NOW
    assert card.verified_by is None
    assert card.filled_count == 0
    assert card.city_slug == "tomsk"


def test_create_stop_word_stays_pending() -> None:
    """AC-01, AC-02 стори 4.3: стоп-слово оставляет карточку `pending`.

    Краснеет, если автопроверка ставит `approved` всегда или ставит `rejected`:
    авто-`rejected` без человека нарушил бы FR113 и NFR54 (OQ-2).
    """
    card = _run(
        create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            # AC-7 стори 13.2: title от автора не принимается, поэтому стоп-слово
            # проверяется на описании — автопроверка читает склейку title+description.
            payload=_payload(description="Казино вечером"),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )
    )

    assert card.verification_status == CardVerificationStatus.PENDING
    assert card.verified_at is None
    assert card.verified_by is None
    assert card.rejection_reason is None


async def test_update_foreign_card_is_not_found() -> None:
    """AC-07 (ADR-13): чужая карточка неразличима с несуществующей.

    Краснеет, если сервис бросает `403`-подобную ошибку: перебором `uuid`
    клиент узнал бы о существовании чужих карточек.
    """
    with pytest.raises(CardNotFoundError):
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            payload=EventCardUpdate(description="Новое"),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=None),  # type: ignore[arg-type]
        )


async def test_update_published_card_without_joins_is_allowed() -> None:
    """AC-01 (FR99, стори 4.6): опубликованная до первого присоединения правится.

    До 4.6 здесь ожидался `409 CARD_NOT_DRAFT` (D-4.2-2). Контракт изменён
    сознательно: FR99 разрешает правку «до первого присоединения», а не
    «только в черновике». Краснеет, если снова запретить правку по статусу
    без проверки `filled_count`.
    """
    card = _card(status=EventCardStatus.PUBLISHED, filled_count=0)

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(description="До присоединений"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    # AC-7 стори 13.2: заголовок теперь генерируется, поэтому «правка прошла»
    # проверяется на description — единственном свободном текстовом поле.
    assert updated.description == "До присоединений"


async def test_update_title_only_keeps_window_and_capacity() -> None:
    """AC-05 (Task 4.6): `PATCH` только `title` не сбрасывает окно.

    Краснеет, если итоговое окно считать как «payload или дефолт»: правка
    заголовка черновика на 1 место сбросила бы окно на 2880.
    """
    card = _card(capacity=1, decision_window_minutes=45)

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(description="Новое название"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.description == "Новое название"
    assert updated.capacity == 1
    assert updated.decision_window_minutes == 45
    assert updated.expires_at == NOW + timedelta(minutes=45)


async def test_update_capacity_to_request_mode_requires_window() -> None:
    """AC-05 (FR79): переход 2–6 → 1 без окна — ошибка.

    Краснеет, если подставлять текущее окно открытого набора: режим заявок
    получил бы 48 часов «по наследству», хотя FR79 требует осознанного выбора.
    """
    card = _card(capacity=3, decision_window_minutes=2880)

    with pytest.raises(CardDecisionWindowRequiredError):
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            payload=EventCardUpdate(capacity=1),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )


async def test_update_capacity_to_open_set_uses_default_window() -> None:
    """AC-05 (FR70): переход 1 → 3 без окна подставляет дефолт конфига.

    Краснеет, если оставить клиентское окно 30 минут: открытый набор
    унаследовал бы окно режима заявок и закрылся бы через полчаса.
    """
    card = _card(capacity=1, decision_window_minutes=30)

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(capacity=3),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.decision_window_minutes == 2880


async def test_update_starts_at_in_past_is_rejected() -> None:
    """AC-03: правка на прошедшее время → `CardStartsAtInPastError`.

    Краснеет, если проверку времени делать только в `create_card`: автор
    мог бы «перенести» черновик в прошлое.
    """
    card = _card()

    with pytest.raises(CardStartsAtInPastError):
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            payload=EventCardUpdate(starts_at=NOW - timedelta(minutes=5)),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )


async def test_publish_pending_card_is_rejected() -> None:
    """AC-06 (FR101, OQ-1): публикация при `pending` → `422 VERIFICATION_PENDING`.

    Краснеет, если пустить непроверенный текст в ленту: NFR51 обещает автору
    проверку до публикации.
    """
    card = _card(verification_status=CardVerificationStatus.PENDING)

    with pytest.raises(CardVerificationPendingError) as error:
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_VERIFICATION_PENDING_CODE


async def test_publish_rejected_card_returns_reason() -> None:
    """AC-06 (FR113): отклонённая карточка → `409 CARD_REJECTED` + причина.

    Краснеет, если отдавать `VERIFICATION_PENDING` вместо `CARD_REJECTED`:
    автор не отличил бы «ещё проверяем» от «отклонили, исправь».
    """
    card = _card(
        verification_status=CardVerificationStatus.REJECTED,
        rejection_reason="Нецензурная лексика",
    )

    with pytest.raises(CardRejectedError) as error:
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_REJECTED_CODE
    assert error.value.reason == "Нецензурная лексика"


async def test_publish_already_published_conflicts() -> None:
    """AC-06: повторная публикация → `409 CARD_NOT_DRAFT`.

    Краснеет, если делать публикацию идемпотентной: второй `published_at`
    исказил бы историю, и «когда опубликовано» стало бы неверным.
    """
    card = _card(
        status=EventCardStatus.PUBLISHED,
        published_at=NOW,
        verification_status=CardVerificationStatus.APPROVED,
    )

    with pytest.raises(CardNotDraftError):
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )


async def test_publish_recomputes_expires_at_from_publish_moment() -> None:
    """AC-04 (D-4.2-3, OQ-2): `expires_at` считается от момента публикации.

    Краснеет, если оставить значение, посчитанное при создании: черновик,
    пролежавший неделю, публиковался бы с уже истёкшим окном.
    """
    stale_expires = NOW - timedelta(days=7) + timedelta(minutes=60)
    card = _card(
        verification_status=CardVerificationStatus.APPROVED,
        decision_window_minutes=60,
        expires_at=stale_expires,
    )

    published = await publish_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert published.status == EventCardStatus.PUBLISHED
    assert published.published_at == NOW
    assert published.expires_at == NOW + timedelta(minutes=60)


async def test_publish_past_draft_is_rejected() -> None:
    """CR 4.2, патч 3 (AC-06 + FR73): черновик с прошедшим `starts_at` не публикуется.

    Дефект, найденный ревью на исполнении: карточка получала `published`
    с `expires_at = min(starts_at, now + окно)` — то есть с УЖЕ истёкшим окном,
    и первый прогон воркера архива (FR73) переводил её в `finished`. Автор видел
    «опубликовано» на встречу, которая прошла.

    Краснеет, если убрать проверку `card.starts_at <= moment` в `publish_card`.
    """
    card = _card(
        starts_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=50),
        verification_status=CardVerificationStatus.APPROVED,
    )

    with pytest.raises(CardStartsAtInPastError):
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )


async def test_update_title_keeps_custom_window_of_open_set() -> None:
    """CR 4.2, патч 5 (AC-05): `PATCH` только `title` не сбрасывает ЯВНОЕ окно.

    Дефект, найденный ревью на исполнении: у карточки открытого набора
    с окном 120 минут правка заголовка сбрасывала окно на дефолт 2880 —
    `_resolve_decision_window` видел `None` и подставлял настройку, хотя
    вместимость не менялась. AC-05 требует «меняются только переданные поля».

    Краснеет, если убрать сохранение текущего окна при неизменном режиме.
    """
    card = _card(capacity=3, decision_window_minutes=120)

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(description="Новое название"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.decision_window_minutes == 120


# --------------------------------------------------------------------------
# Стори 4.4: лимит активных карточек внутри транзакции (AC-01…AC-07)
# --------------------------------------------------------------------------


def _settings_with_limit(limit: int) -> Any:
    """Настройки с явным лимитом: `_Settings` — класс, подменяем поле на экземпляре."""
    settings = _Settings()
    settings.card_active_limit = limit
    return settings


async def test_publish_under_limit_publishes() -> None:
    """AC-01: активных меньше лимита → публикация проходит.

    Краснеет, если сравнивать `active_count >= limit` строго с `>`: при одном
    активном и лимите 2 карточка осталась бы черновиком.
    """
    card = _card(verification_status=CardVerificationStatus.APPROVED)
    repo = _FakeRepository(card=card, active_count=1)

    published = await publish_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        settings=_settings_with_limit(2),  # type: ignore[arg-type]
        now=NOW,
        repository=repo,  # type: ignore[arg-type]
    )

    assert published.status == EventCardStatus.PUBLISHED
    assert published.published_at == NOW


async def test_publish_at_limit_is_rejected_with_details() -> None:
    """AC-01, AC-07 (мутация 8 в части поведения): лимит достигнут → `429`-ошибка.

    Проверяется не только факт ошибки, но и `details`: клиент показывает
    «N из M». Краснеет, если убрать проверку лимита или не передать счётчик.
    """
    card = _card(verification_status=CardVerificationStatus.APPROVED)
    repo = _FakeRepository(card=card, active_count=2)

    with pytest.raises(CardLimitReachedError) as error:
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_settings_with_limit(2),  # type: ignore[arg-type]
            now=NOW,
            repository=repo,  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_LIMIT_REACHED_CODE
    assert error.value.details == {"limit": 2, "active_count": 2}
    # CAS не выполнялся: лимит проверяется ДО записи (Task 6).
    assert "cas" not in repo.calls


async def test_draft_rejection_comes_before_limit_check() -> None:
    """AC-01, AC-06: непубликуемая карточка получает свою ошибку, не `429`.

    Краснеет, если проверку лимита поставить до проверок состояния: автор
    пошёл бы «освобождать слот», хотя карточка отклонена модератором
    (Task 6, Dev Notes — порядок шагов 3 и 4).
    """
    card = _card(
        verification_status=CardVerificationStatus.REJECTED,
        rejection_reason="Спам",
    )
    repo = _FakeRepository(card=card, active_count=2)

    with pytest.raises(CardRejectedError):
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_settings_with_limit(1),  # type: ignore[arg-type]
            now=NOW,
            repository=repo,  # type: ignore[arg-type]
        )

    # `COUNT` до проверок состояния не выполнялся вовсе.
    assert repo.calls == ["lock"]


async def test_publish_calls_lock_before_count_before_cas() -> None:
    """AC-03, AC-04 (мутация 7): порядок lock → count → CAS.

    Краснеет, если `COUNT` уехал до `FOR UPDATE` (мутация 7): две транзакции
    видели бы `count = limit - 1` и обе публиковали (гонка (а) даёт 2 `UPDATE`).
    """
    card = _card(verification_status=CardVerificationStatus.APPROVED)
    repo = _FakeRepository(card=card, active_count=0)

    await publish_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        settings=_settings_with_limit(2),  # type: ignore[arg-type]
        now=NOW,
        repository=repo,  # type: ignore[arg-type]
    )

    assert repo.calls == ["lock", "count", "cas"]


async def test_rowcount_zero_is_resolved_by_reread() -> None:
    """AC-06 (мутация 6): `rowcount == 0` → резолвер по ПЕРЕЧИТАННОЙ карточке.

    Краснеет, если убрать проверку `rowcount` (мутация 6): проигравший CAS
    получил бы `200` и отдал клиенту карточку, которую он не публиковал.
    Резолвер читает актуальное состояние — в тесте это уже `published`.
    """
    stale = _card(verification_status=CardVerificationStatus.APPROVED)
    fresh = _card(
        status=EventCardStatus.PUBLISHED,
        published_at=NOW,
        verification_status=CardVerificationStatus.APPROVED,
    )
    repo = _FakeRepository(active_count=0, cas_rowcount=0, sequence=[stale, fresh])

    with pytest.raises(CardNotDraftError):
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            settings=_settings_with_limit(2),  # type: ignore[arg-type]
            now=NOW,
            repository=repo,  # type: ignore[arg-type]
        )

    assert repo.calls == ["lock", "count", "cas"]


async def test_publish_reads_card_again_after_cas() -> None:
    """AC-06: после CAS сервис отдаёт АКТУАЛЬНУЮ карточку, а не снимок.

    Краснеет, если вернуть объект, прочитанный до CAS: `synchronize_session=False`
    оставляет его в состоянии `draft`, и клиент увидел бы «не опубликовано»
    на успешный `200`.
    """
    before = _card(verification_status=CardVerificationStatus.APPROVED)
    after = _card(
        status=EventCardStatus.PUBLISHED,
        published_at=NOW,
        verification_status=CardVerificationStatus.APPROVED,
    )
    repo = _FakeRepository(active_count=0, sequence=[before, after])

    published = await publish_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        settings=_settings_with_limit(2),  # type: ignore[arg-type]
        now=NOW,
        repository=repo,  # type: ignore[arg-type]
    )

    assert published is after
    assert published.status == EventCardStatus.PUBLISHED


# --- Стори 12.5: горизонт 72 часа (AC-01, AC-02, FR116, ADR-21) ------------
#
# `now` — параметр сервиса, а не `datetime.now()` внутри: иначе граница
# воспроизводима только в день написания (урок 12.2). Граница ВКЛЮЧИТЕЛЬНАЯ:
# ровно 72 часа — допустимо, `+1 с` — отказ.


async def test_create_rejects_starts_at_beyond_horizon() -> None:
    """AC-01: `now + 72 ч + 1 с` → `CardStartsAtTooFarError` (U-2).

    Краснеет, если убрать проверку горизонта из `create_card` (U-2) или
    поменять знак сравнения так, что «дальше» станет допустимым.
    """
    with pytest.raises(CardStartsAtTooFarError) as error:
        await create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            payload=_payload(starts_at=NOW + timedelta(hours=72, seconds=1)),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_STARTS_AT_TOO_FAR_CODE
    assert error.value.code == "STARTS_AT_TOO_FAR"


async def test_create_accepts_exactly_horizon_boundary() -> None:
    """AC-01: ровно `now + 72 ч` → успех (U-1).

    Пара к `test_create_rejects_starts_at_beyond_horizon`: без неё мутация
    «сравнение `<` вместо `<=`» не отличается от «роут всегда 422».
    Краснеет, если `<` заменить на `<=` ИЛИ сдвинуть константу вниз.
    """
    repo = _FakeRepository()

    card = await create_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        payload=_payload(starts_at=NOW + timedelta(hours=72)),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=repo,  # type: ignore[arg-type]
    )

    assert card.starts_at == NOW + timedelta(hours=72)
    assert len(repo.added) == 1


async def test_create_accepts_just_inside_horizon() -> None:
    """AC-01: `now + 72 ч - 1 с` → успех.

    Краснеет, если горизонт уменьшить (например, 71 час): «почти граница»
    перестала бы создаваться, и автор терял бы законный час.
    """
    repo = _FakeRepository()

    card = await create_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        payload=_payload(starts_at=NOW + timedelta(hours=72, seconds=-1)),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=repo,  # type: ignore[arg-type]
    )

    assert len(repo.added) == 1
    assert card.starts_at == NOW + timedelta(hours=72, seconds=-1)


async def test_create_past_wins_over_too_far() -> None:
    """AC-01, U-5: порядок проверок «прошлое → далеко».

    Прошедшая дата при одновременном нарушении горизонта даёт `InPast`:
    причина «уже прошло» информативнее для человека.
    Краснеет, если переставить блоки: на прошедшей дате придёт `TOO_FAR`.
    """
    with pytest.raises(CardStartsAtInPastError):
        await create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            payload=_payload(starts_at=NOW - timedelta(days=30)),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )


async def test_update_rejects_starts_at_beyond_horizon() -> None:
    """AC-01, U-3: `PATCH` не обходит горизонт.

    Краснеет, если убрать проверку из `update_card`: «создать нельзя,
    сдвинуть можно» — обход правила.
    """
    with pytest.raises(CardStartsAtTooFarError) as error:
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            payload=EventCardUpdate(starts_at=NOW + timedelta(hours=72, seconds=1)),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=_card()),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_STARTS_AT_TOO_FAR_CODE


async def test_update_without_starts_at_still_works() -> None:
    """AC-01, U-4: `PATCH` заголовка без времени не требует `starts_at`.

    Пара к `test_update_rejects_starts_at_beyond_horizon`: без неё мутация
    «вынести проверку из `if`» не отличается от «`PATCH` сломан целиком».
    Краснеет, если гард горизонта вынести за `if payload.starts_at is not None`.
    """
    card = _card()

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(description="Новое название"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.description == "Новое название"
    assert updated.starts_at == card.starts_at


# --- Стори 13.2 (CR 13.2 H-1, H-3): ЗАГОЛОВОК В СЕРВИСНОМ ПУТИ -----------------
#
# **Зачем отдельный блок.** До CR ревью заголовок проверялся только как чистая
# функция (`test_card_title.py`). Сервисный путь — `create_card` и `update_card` —
# не был заперт ничем: мутации M12 (смена места не пересобирает), M13 (`other`
# перетирается), M14 (генерации при создании нет) ВЫЖИВАЛИ, а `PATCH` только
# активности не пересобирал заголовок вовсе (H-1, блокер вердикта).
#
# Продуктовое следствие H-1: автор менял «кофе» на «кино», а карточка в ленте
# продолжала называться «Кофе в кафе „Прага“» — AC-3 нарушен на пути правки.


def _patch_place_by_id(monkeypatch: pytest.MonkeyPatch, *, name: str, category: str) -> None:
    """Подменяет `PlaceRepository.get_by_id` — единственный запрос блока пересборки.

    При `PATCH` только активности место не перепроверяется: `place_id` карточки
    уже валиден, а `payload` места не содержит. Сервис читает строку по `id`,
    поэтому подменяется именно этот метод, а не `_resolve_place` (тот вызывается
    лишь при смене места).
    """
    from app.repositories.places import PlaceRepository

    class _Place:
        id = 42

    async def _get_by_id(self: Any, place_id: int) -> Any:
        place = _Place()
        place.category = category
        place.name = name
        return place

    monkeypatch.setattr(PlaceRepository, "get_by_id", _get_by_id)


def test_create_builds_title_from_activity_and_place() -> None:
    """AC-3, H-3 M14: `create_card` САМ собирает заголовок из активности и места.

    Краснеет, если убрать вызов `generate_card_title` из `create_card` или
    подставить вместо него заглушку: `title` остался бы пустым (или упало бы
    `NOT NULL` в БД), а карточка ушла бы в `pending` по правилу `length`.
    """
    card = _run(
        create_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            # Активность НЕ дефолтная: дефолт фабрики — `boardgames`, и совпадение
            # с ним не отличило бы «взято из активности» от «взято из константы».
            payload=_payload(activity="coffee"),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(),  # type: ignore[arg-type]
        )
    )

    assert card.title == "Кофе в кафе „Прага“"


@pytest.mark.asyncio
async def test_patch_activity_only_rebuilds_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3, V-2, CR H-1 (БЛОКЕР): `PATCH` ТОЛЬКО активности пересобирает заголовок.

    **Воспроизведённый дефект.** `effective_activity` читался как
    `Activity(card.activity)` — ДО присвоения `card.activity` (оно ниже, после
    автопроверки). `PATCH {"activity": "cinema"}` при `coffee` оставлял
    заголовок «Кофе в кафе „Прага“».

    Краснеет при возврате к `Activity(card.activity)`: заголовок остаётся
    старым, а `capability` карточки меняется — лента называет встречу кофе,
    хотя внутри кино.
    """
    _patch_place_by_id(monkeypatch, name="Прага", category="cafe")
    card = _card(activity="coffee", title="Кофе в кафе „Прага“")

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(activity="cinema"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.activity == "cinema"
    assert updated.title == "Кино в кафе „Прага“", f"заголовок не пересобран: {updated.title!r}"


@pytest.mark.asyncio
async def test_patch_place_only_rebuilds_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3, V-2, H-3 M12: `PATCH` только места пересобирает заголовок.

    Краснеет, если снять блок пересборки (`if False:`): карточка «Кофе в кафе
    „Прага“» переживёт смену места на бар и будет врать о месте встречи.
    """
    from app.services import event_card_service as service_module

    class _Place:
        id = 99
        category = "bar"
        name = "Бар Прага"

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        return _Place()

    # Место читается `_resolve_place` (смена места), а не `get_by_id` —
    # подменяется именно этот путь, иначе тест ушёл бы в БД.
    monkeypatch.setattr(service_module, "_resolve_place", _resolve)
    card = _card(activity="coffee", title="Кофе в кафе „Прага“")

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(place_uuid=uuid_lib.uuid4()),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.place_id == 99
    assert updated.title == "Кофе в баре „Бар Прага“", (
        f"заголовок не пересобран под новое место: {updated.title!r}"
    )


@pytest.mark.asyncio
async def test_patch_place_on_legacy_other_keeps_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6, H-3 M13: легаси `other` НЕ перетирает существующий заголовок.

    Генератор на `Activity.OTHER` бросает `ValueError` — если снять проверку
    `is not Activity.OTHER`, `PATCH` места у легаси-карточки падал бы `500`.
    Проверяется и то, что заголовок остался прежним: перетирать его нельзя,
    у такой карточки активности нет, и собрать осмысленный заголовок не из чего.
    """
    card = _card(activity="other", title="Старый заголовок")

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=uuid_lib.uuid4(),
        payload=EventCardUpdate(place_uuid=uuid_lib.uuid4()),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_FakeRepository(card=card),  # type: ignore[arg-type]
    )

    assert updated.title == "Старый заголовок", f"легаси-заголовок перетёрт: {updated.title!r}"


@pytest.mark.asyncio
async def test_patch_place_with_stop_word_checks_rebuilt_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N-1 (CR 13.2 r2): автопроверка видит ПЕРЕСОБРАННЫЙ заголовок, не старый.

    **Дыра, которую закрывает тест.** Порядок в `update_card` верный: заголовок
    пересобирается (`:739-743`) ВЫШЕ автопроверки (`:756-761`), и в `CardContent`
    уходит уже новый текст. Но это не было заперто ничем: заме�� CR — подмена
    `CardContent(title=card.title, ...)` на константу дала **62 passed**.

    **Почему это важно.** Если блок пересборки перенесут НИЖЕ автопроверки
    (естественный рефакторинг «сначала присвоить поля, потом проверить»),
    карточка, переехавшая на место с запрещённым именем, пройдёт проверку по
    СТАРОМУ тексту и останется в ленте с «Казино» в заголовке.

    Сценарий: ОПУБЛИКОВАННАЯ карточка («Настольные игры», место «Прага»)
    переезжает на место «Казино». Новый заголовок «Настольные игры в кафе
    „Казино“» содержит стоп-слово (`card_content_stop_words` теста = «казино»).
    Публикация не редактируема без повторной проверки → `CardEditRequiresReviewError`,
    и заголовок в модели остаётся прежним (отказ без мутаций).

    Краснеет, если проверять старый `title`: «Настольные игры» правило пройдёт,
    отказа не будет, и карточка уйдёт в ленту с запрещённым именем места.
    """
    from app.services import event_card_service as service_module

    class _StopWordPlace:
        id = 77
        category = "cafe"
        name = "Казино"

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        return _StopWordPlace()

    monkeypatch.setattr(service_module, "_resolve_place", _resolve)
    card = _card(
        status=EventCardStatus.PUBLISHED,
        title="Настольные игры",
        filled_count=0,
        verification_status="approved",
    )

    session = _FakeSession()
    with pytest.raises(CardEditRequiresReviewError) as error:
        await update_card(
            session,  # type: ignore[arg-type]
            author_id=7,
            card_uuid=uuid_lib.uuid4(),
            payload=EventCardUpdate(place_uuid=uuid_lib.uuid4()),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_FakeRepository(card=card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_EDIT_REQUIRES_REVIEW_CODE
    # **Замер фактического поведения (не желаемого).** В ORM-объекте `card.title`
    # УЖЕ перетёрт новым значением: пересборка (`:739-743`) стоит выше
    # автопроверки, иначе проверялся бы старый текст — а это и есть предмет N-1.
    # В БД изменение НЕ попадает: роут на этом пути возвращает `409` БЕЗ
    # `session.commit()`, а `get_session` не коммитит сам («коммит делает роут»,
    # `app/core/db.py:45-50`). Поэтому проверяется ФАКТ отсутствия коммита, а не
    # чистота объекта в памяти.
    assert session.commits == 0, "сервис закоммитил отказ от автопроверки"
