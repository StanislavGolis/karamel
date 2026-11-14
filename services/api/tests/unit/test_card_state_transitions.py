"""Матрица переходов карточки (стори 4.6, Task 8; AC-01…AC-06, AC-08, AC-10).

Архитектура 5.1 задаёт шесть статусов и РАЗРЕШЁННЫЕ переходы между ними.
Задача теста — проверить не «удачный путь», а ВСЕ пары `from → to`, включая
запрещённые: ошибка в предикате CAS обычно разрешает лишний переход, а не
ломает разрешённый, и «зелёный» тест на один переход этого не видит.

§18.2: проверяется наблюдаемое следствие (предикат SQL / доменная ошибка),
а не факт вызова. §18.1: каждый тест падает от снятия конкретного условия —
в docstring указано, какого.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.enums import (
    ARCHIVABLE_STATUSES,
    EDITABLE_STATUSES,
    EventCardStatus,
)
from app.models.event_card import EventCard
from app.schemas.event_card import EventCardUpdate
from app.services.event_card_service import (
    CARD_ALREADY_JOINED_CODE,
    CARD_EDIT_REQUIRES_REVIEW_CODE,
    CARD_NO_SEATS_CODE,
    CARD_NOT_CANCELLABLE_CODE,
    CARD_NOT_EDITABLE_CODE,
    CARD_NOT_PUBLISHED_CODE,
    CardAlreadyJoinedError,
    CardEditRequiresReviewError,
    CardNoSeatsError,
    CardNotCancellableError,
    CardNotEditableError,
    CardNotFoundError,
    CardNotPublishedError,
    cancel_card,
    close_card,
    publish_card,
    update_card,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
STARTS_AT = NOW + timedelta(days=3)
CARD_UUID = uuid_lib.UUID("44444444-4444-4444-8444-444444444444")


class _Settings:
    """Минимум настроек сервиса: поля протокола `CardSettings` без `.env`."""

    default_city_slug = "tomsk"
    city_slugs = ("tomsk",)
    card_default_decision_window_minutes = 2880
    card_content_stop_words = ("казино", "займ")
    card_active_limit = 2


class _FakeSession:
    """Сессия-заглушка: `commit` считает вызовы, БД не трогает."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _StateRepository:
    """Подмена репозитория, знающая МАТРИЦУ переходов.

    В отличие от простого стаба, CAS здесь проверяет ПРЕДИКАТ по статусу:
    разрешённый переход возвращает 1 и меняет статус, запрещённый — 0.
    Это единственный способ проверить все пары `from → to` без БД и без
    повторения SQL в тесте (§18.2: проверяем поведение, а не текст).

    Настоящий SQL-предикат проверяется отдельно — в `test_card_state_builders`.
    """

    def __init__(self, card: EventCard) -> None:
        self.card = card
        self.calls: list[str] = []
        self.commits = 0
        # X-6.1-1 (chore P-7): значения `for_update`, с которыми сервис
        # запрашивал карточку. Отдельное поле, а не `calls`: часть тестов
        # сравнивает `calls` точно, и лишняя запись сломала бы их контракт.
        self.for_update_requests: list[bool] = []

    async def get_owned(
        self, author_id: int, card_uuid: uuid_lib.UUID, *, for_update: bool = False
    ) -> EventCard | None:
        """X-6.1-1 (chore P-7): сигнатура повторяет репозиторий.

        `for_update` принимается и запоминается: `update_card` читает карточку
        под блокировкой, и без параметра `PATCH` падал бы `TypeError`.
        Текст `FOR UPDATE` подменой БД не проверяется — это делает
        интеграционный тест на `before_cursor_execute`.
        """
        self.for_update_requests.append(for_update)
        return self.card if self.card.author_id == author_id else None

    async def close_by_author(self, *, card_id: int, author_id: int, now: datetime) -> int:
        self.calls.append("close")
        if self.card.status != EventCardStatus.PUBLISHED:
            return 0
        if self.card.author_id != author_id:
            return 0
        self.card.status = EventCardStatus.CLOSED
        self.card.closed_at = now
        return 1

    async def cancel_by_author(self, *, card_id: int, author_id: int) -> int:
        self.calls.append("cancel")
        if self.card.status in (EventCardStatus.FINISHED, EventCardStatus.CANCELLED):
            return 0
        if self.card.author_id != author_id:
            return 0
        self.card.status = EventCardStatus.CANCELLED
        return 1

    async def finish_card(self, *, card_id: int, now: datetime) -> int:
        self.calls.append("finish")
        if self.card.status not in ARCHIVABLE_STATUSES:
            return 0
        if self.card.starts_at > now:
            return 0
        self.card.status = EventCardStatus.FINISHED
        return 1

    async def publish_if_draft(
        self, *, card_id: int, author_id: int, now: datetime, expires_at: datetime
    ) -> int:
        self.calls.append("publish")
        if self.card.status != EventCardStatus.DRAFT:
            return 0
        self.card.status = EventCardStatus.PUBLISHED
        self.card.published_at = now
        return 1

    async def lock_author(self, author_id: int) -> None:
        self.calls.append("lock")

    async def count_active(self, author_id: int) -> int:
        return 0

    async def join_seat(self, *, card_id: int, chat_uuid: uuid_lib.UUID | None = None) -> Any:
        """AC-02 (X-39): инкремент и `full` — одной операцией, как в репозитории.

        Стори 6.2: сигнатура получила `chat_uuid` (репозиторий пишет чат тем же
        `UPDATE`). Подмена возвращает `chat_uuid=None` всегда: эти тесты — о
        переходах состояния, а открытие чата проверяется отдельными файлами;
        `None` держит guard `if seat.chat_uuid is not None` закрытым, иначе
        сервис пошёл бы в настоящий `ChatRepository` на `_FakeSession`.
        """
        self.calls.append("join_seat")
        if self.card.status != EventCardStatus.PUBLISHED:
            return None
        if self.card.filled_count >= self.card.capacity:
            return None
        self.card.filled_count += 1
        if self.card.filled_count >= self.card.capacity:
            self.card.status = EventCardStatus.FULL
        return SimpleNamespace(
            filled_count=self.card.filled_count,
            capacity=self.card.capacity,
            status=self.card.status,
            # Стори 6.2: сервис читает признак сборки из `RETURNING`.
            chat_uuid=None,
        )

    async def due_finish_ids(self, *, now: datetime, limit: int) -> list[int]:
        if self.card.status in ARCHIVABLE_STATUSES and self.card.starts_at <= now:
            return [self.card.id or 1]
        return []


def _card(**overrides: Any) -> EventCard:
    """Карточка в памяти в заданном состоянии."""
    values: dict[str, Any] = {
        "id": 1,
        "author_id": 7,
        "place_id": 42,
        "city_slug": "tomsk",
        # Стори 13.2 (AC-7, ADR-26): title в БД NOT NULL, но приходит не от
        # клиента, а от генератора. В фабрике модели он нужен явно.
        "title": "Настольные игры",
        "description": None,
        "category": "cafe",
        "starts_at": STARTS_AT,
        "capacity": 3,
        "decision_window_minutes": 2880,
        "expires_at": STARTS_AT - timedelta(hours=48),
        "status": EventCardStatus.DRAFT,
        "verification_status": "approved",
        "filled_count": 0,
    }
    values.update(overrides)
    return EventCard(**values)


# --- AC-03: закрытие набора ---------------------------------------------------


@pytest.mark.parametrize("status", [EventCardStatus.PUBLISHED])
@pytest.mark.asyncio
async def test_close_allowed_from_published(status: EventCardStatus) -> None:
    """AC-03 (FR74): `published → closed` разрешён.

    Краснеет, если в предикате CAS поменять статус на `draft`/`full`:
    разрешённый переход перестанет срабатывать.
    """
    card = _card(status=status)
    closed = await close_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert closed.status == EventCardStatus.CLOSED
    assert closed.closed_at == NOW


@pytest.mark.parametrize(
    "status",
    [
        EventCardStatus.DRAFT,
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
        EventCardStatus.FINISHED,
        EventCardStatus.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_close_forbidden_from_other_statuses(status: EventCardStatus) -> None:
    """AC-03: закрыть можно ТОЛЬКО опубликованную карточку.

    `full` запрещён намеренно (D-4.6-4): набор уже остановлен по вместимости,
    и `closed` исказил бы причину. Краснеет, если разрешить закрытие из
    любого статуса, кроме терминальных.
    """
    card = _card(status=status)

    with pytest.raises(CardNotPublishedError) as error:
        await close_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=CARD_UUID,
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_NOT_PUBLISHED_CODE
    assert card.status == status


@pytest.mark.asyncio
async def test_close_foreign_card_is_not_found() -> None:
    """AC-03 (ADR-13): чужая карточка → `404`, не `403`.

    Краснеет, если закрывать по `card_id` без проверки автора: перебор `uuid`
    позволил бы закрывать чужие наборы.
    """
    card = _card(status=EventCardStatus.PUBLISHED)

    with pytest.raises(CardNotFoundError):
        await close_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=999,
            card_uuid=CARD_UUID,
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert card.status == EventCardStatus.PUBLISHED


# --- AC-04: отмена ------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        EventCardStatus.DRAFT,
        EventCardStatus.PUBLISHED,
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
    ],
)
@pytest.mark.asyncio
async def test_cancel_allowed_from_non_terminal(status: EventCardStatus) -> None:
    """AC-04 (FR100): отмена разрешена из `draft`/`published`/`full`/`closed`.

    Краснеет, если предикат перечисляет только `published`: автор не смог бы
    отменить черновик или уже закрытый набор.
    """
    card = _card(status=status)
    cancelled = await cancel_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert cancelled.status == EventCardStatus.CANCELLED


@pytest.mark.parametrize("status", [EventCardStatus.FINISHED, EventCardStatus.CANCELLED])
@pytest.mark.asyncio
async def test_cancel_forbidden_from_terminal(status: EventCardStatus) -> None:
    """AC-04: архив и уже отменённая карточка не отменяются.

    `finished` — история (FR73): отмена переписала бы прошедшую встречу, и
    история явки (5.x) ссылалась бы на отменённое событие. Повторная отмена —
    конфликт состояния (OQ-15). Краснеет, если разрешить отмену из терминальных.
    """
    card = _card(status=status)

    with pytest.raises(CardNotCancellableError) as error:
        await cancel_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=CARD_UUID,
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_NOT_CANCELLABLE_CODE
    assert card.status == status


# --- AC-01, AC-02: правка -----------------------------------------------------


@pytest.mark.parametrize("status", [EventCardStatus.PUBLISHED, EventCardStatus.FULL])
@pytest.mark.asyncio
async def test_update_published_without_joins_is_allowed(status: EventCardStatus) -> None:
    """AC-01 (FR99): правка опубликованной до первого присоединения.

    Краснеет, если вернуть запрет по статусу: FR99 разрешает правку «до
    первого присоединения», а не «только в черновике».
    """
    card = _card(status=status, filled_count=0)

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        payload=EventCardUpdate(description="Новое название"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert updated.description == "Новое название"


@pytest.mark.parametrize("status", [EventCardStatus.PUBLISHED, EventCardStatus.FULL])
@pytest.mark.asyncio
async def test_update_after_join_is_rejected(status: EventCardStatus) -> None:
    """AC-01 (FR99): после первого присоединения правка запрещена.

    «Присоединение» на этом шаге = `filled_count > 0` (D-4.6-1). Краснеет,
    если снять проверку `filled_count`: автор менял бы встречу под участниками.
    """
    card = _card(status=status, filled_count=1)

    with pytest.raises(CardAlreadyJoinedError) as error:
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=CARD_UUID,
            payload=EventCardUpdate(description="Новое"),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_ALREADY_JOINED_CODE
    assert card.title == "Настольные игры"


@pytest.mark.parametrize(
    "status",
    [EventCardStatus.CLOSED, EventCardStatus.FINISHED, EventCardStatus.CANCELLED],
)
@pytest.mark.asyncio
async def test_update_terminal_card_is_not_editable(status: EventCardStatus) -> None:
    """AC-01: терминальные состояния не правятся.

    Краснеет, если пустить правку любого не-`draft` статуса: закрытый набор
    можно было бы переписать и вернуть в ленту под старым `uuid`.
    """
    card = _card(status=status)

    with pytest.raises(CardNotEditableError) as error:
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=CARD_UUID,
            payload=EventCardUpdate(description="Новое"),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_NOT_EDITABLE_CODE
    assert card.title == "Настольные игры"


@pytest.mark.asyncio
async def test_update_published_with_stop_word_changes_nothing() -> None:
    """AC-02 (FR101): провал автопроверки на опубликованной — отказ БЕЗ мутаций.

    Краснеет, если проверять текст ПОСЛЕ присвоения полей: карточка ушла бы
    в ленту с запрещённым текстом, а ответ был бы `409` — то есть база и
    ответ разошлись бы. Краснеет и от возврата `pending`: карточка исчезла бы
    у участников.
    """
    card = _card(status=EventCardStatus.PUBLISHED, title="Спокойный заголовок")

    with pytest.raises(CardEditRequiresReviewError) as error:
        await update_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=CARD_UUID,
            payload=EventCardUpdate(description="Казино на районе"),
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_EDIT_REQUIRES_REVIEW_CODE
    assert card.title == "Спокойный заголовок"
    assert card.verification_status == "approved"


@pytest.mark.asyncio
async def test_update_draft_with_stop_word_keeps_edit_but_goes_pending() -> None:
    """AC-02: у ЧЕРНОВИКА неудача автопроверки не отменяет правку (поведение 4.3).

    Краснеет, если распространить отказ опубликованной на черновик: автор не
    смог бы сохранить текст и увидеть, что именно модератор попросит исправить.
    """
    card = _card(status=EventCardStatus.DRAFT, title="Спокойный заголовок")

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        payload=EventCardUpdate(description="Казино на районе"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert updated.description == "Казино на районе"
    assert updated.verification_status == "pending"


# --- AC-08: стирание описания -------------------------------------------------


def test_description_empty_string_clears_field() -> None:
    """AC-08 (X-19): `""` — команда стереть, а не «не трогать».

    Краснеет, если вернуть `normalized or None` в схеме: пустая строка стала бы
    `None`, а `None` в `Update` означает «поле не передано», и стереть описание
    стало бы невозможно в принципе.
    """
    update = EventCardUpdate(description="")

    assert update.description == ""
    assert "description" in update.model_fields_set


@pytest.mark.asyncio
async def test_update_description_empty_string_writes_null() -> None:
    """AC-08 (X-19): `PATCH {"description": ""}` обнуляет колонку.

    Краснеет, если сервис проверяет `payload.description is not None` без
    `model_fields_set`: пустая строка прошла бы проверку и записала `''`
    в `Text`-колонку вместо `NULL` (project-context §4: пусто = `null`).
    """
    card = _card(status=EventCardStatus.DRAFT, description="Было описание")

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        payload=EventCardUpdate(description=""),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert updated.description is None


@pytest.mark.asyncio
async def test_update_without_description_keeps_it() -> None:
    """AC-08: поле не передано — описание не трогается.

    Краснеет, если стирать описание при любом `PATCH`: правка заголовка
    уничтожала бы текст, который автор не трогал.
    """
    card = _card(status=EventCardStatus.DRAFT, description="Было описание")

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        # Стори 13.2 (AC-7): `title` из контракта удалён, поэтому «правка
        # другого поля» передаётся через `capacity` — оно точно не описание,
        # и смысл теста сохранён: не переданное поле не затирается.
        payload=EventCardUpdate(capacity=2),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert updated.description == "Было описание"


# --- AC-07: состав констант ----------------------------------------------------


def test_archivable_and_editable_sets_are_exact() -> None:
    """AC-05, AC-01: наборы статусов — ровно те, что в правилах архитектуры 5.1.

    `ARCHIVABLE_STATUSES` — источник и для воркера, и для предиката `finished`.
    Краснеет, если добавить `cancelled` (отменённое не архивируется) или
    убрать `closed` (закрытый набор обязан уйти в архив по времени).
    """
    assert set(ARCHIVABLE_STATUSES) == {
        EventCardStatus.PUBLISHED,
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
    }
    assert set(EDITABLE_STATUSES) == {EventCardStatus.PUBLISHED, EventCardStatus.FULL}
    # `draft` не входит ни в один: он не архивируется (время ещё не пришло
    # по правилу публикации), но и правится всегда — отдельной ветвью.
    assert EventCardStatus.DRAFT not in ARCHIVABLE_STATUSES
    assert EventCardStatus.DRAFT not in EDITABLE_STATUSES


# --- AC-09: публикация при полной вместимости ---------------------------------


@pytest.mark.asyncio
async def test_publish_without_seats_is_rejected() -> None:
    """AC-09 (X-22): публикация при `filled_count >= capacity` → `CARD_NO_SEATS`.

    Через БД состояние недостижимо (CHECK `ck_event_cards_full_capacity`
    запрещает «черновик без мест»), поэтому защита проверяется на уровне
    сервиса — как defense-in-depth. Краснеет, если убрать проверку: при
    ослабленном CHECK вставка прошла бы, а `publish` упёрся бы в ограничение
    и вернул `500` вместо доменного `409`.
    """
    card = _card(
        status=EventCardStatus.DRAFT,
        filled_count=3,
        capacity=3,
        verification_status="approved",
    )

    with pytest.raises(CardNoSeatsError) as error:
        await publish_card(
            _FakeSession(),  # type: ignore[arg-type]
            author_id=7,
            card_uuid=CARD_UUID,
            settings=_Settings(),  # type: ignore[arg-type]
            now=NOW,
            repository=_StateRepository(card),  # type: ignore[arg-type]
        )

    assert error.value.code == CARD_NO_SEATS_CODE
    assert card.status == EventCardStatus.DRAFT


@pytest.mark.asyncio
async def test_patch_published_does_not_extend_decision_window() -> None:
    """CR 4.6, патч 1 (AC-01, ADR-02, NFR14): правка НЕ продлевает окно решения.

    Дефект, найденный ревью на исполнении: `expires_at` пересчитывался от `now`
    для любого статуса, и `PATCH {"title": ...}` сдвигал границу «решу до ...»
    на время, прошедшее с публикации (воспроизведено: +24 ч).

    Краснеет, если вернуть безусловный пересчёт: участник, рассчитавший срок,
    получил бы другую границу без предупреждения. Обратное свойство — окно
    МОЖНО сократить — проверяется отдельным кейсом ниже.
    """
    # Опубликована сутки назад с окном 48 ч: старый `expires_at` = публикация + 48 ч.
    old_expires = NOW - timedelta(days=1) + timedelta(minutes=2880)
    card = _card(
        status=EventCardStatus.PUBLISHED,
        starts_at=NOW + timedelta(days=5),
        expires_at=old_expires,
        decision_window_minutes=2880,
    )

    updated = await update_card(
        _FakeSession(),  # type: ignore[arg-type]
        author_id=7,
        card_uuid=CARD_UUID,
        payload=EventCardUpdate(description="Только заголовок"),
        settings=_Settings(),  # type: ignore[arg-type]
        now=NOW,
        repository=_StateRepository(card),  # type: ignore[arg-type]
    )

    assert updated.expires_at == old_expires
    assert updated.description == "Только заголовок"
