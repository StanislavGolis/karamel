"""Лимит активных карточек автора (стори 4.4; AC-01…AC-07).

§18.2: проверяется наблюдаемый результат — текст SQL, значения конфига,
код доменной ошибки. Каждый тест в docstring говорит, какая правка сделает
его красным (§18.1).

SQL проверяется без БД статическими билдерами репозитория: гонку на уровне
БД доказывает `tests/integration/test_card_publish_race.py` на двух
соединениях (§18.3).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import CARD_ACTIVE_LIMIT_MAX, CARD_ACTIVE_LIMIT_MIN, Settings
from app.models.enums import (
    ACTIVE_STATUSES,
    CardVerificationStatus,
    EventCardStatus,
)
from app.models.event_card import EventCard
from app.repositories.event_cards import EventCardRepository
from app.services.event_card_service import (
    CARD_LIMIT_REACHED_CODE,
    CardLimitReachedError,
    CardNotDraftError,
    CardRejectedError,
    CardStartsAtInPastError,
    CardVerificationPendingError,
    _raise_publish_conflict,
)

NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
STARTS_AT = NOW + timedelta(days=3)
REQUIRED_ENV = {
    "JWT_SECRET": "unit_test_jwt_secret_at_least_32_chars",
    "SMS_CODE_SECRET": "unit_test_sms_secret_at_least_32_chars",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Настройки из явного окружения, локальный `.env` игнорируется."""
    for name in ("CARD_ACTIVE_LIMIT", *env):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def _card(**overrides: Any) -> EventCard:
    """Карточка в памяти для резолвера конфликтов."""
    values: dict[str, Any] = {
        "author_id": 7,
        "place_id": 42,
        "community_slug": "tomsk",
        "title": "Настольные игры",
        "category": "cafe",
        "starts_at": STARTS_AT,
        "capacity": 3,
        "decision_window_minutes": 2880,
        "expires_at": STARTS_AT - timedelta(hours=48),
        "status": EventCardStatus.DRAFT,
        "verification_status": CardVerificationStatus.APPROVED,
        "filled_count": 0,
    }
    values.update(overrides)
    return EventCard(**values)


# --------------------------------------------------------------------------
# Task 1: конфигурация лимита (AC-02, FR91)
# --------------------------------------------------------------------------


def test_default_limit_is_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01, AC-02 (OQ-3): дефолт лимита — 2.

    Краснеет, если поменять дефолт: 1 сделал бы вторую карточку автора
    невозможной без явной настройки `.env`, 3 нарушил бы FR71.
    """
    assert _settings(monkeypatch).card_active_limit == 2


@pytest.mark.parametrize("value", ["1", "2"])
def test_limit_inside_range_passes(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-02 (FR71): значения 1 и 2 проходят и читаются из окружения.

    Краснеет, если диапазон сузить или игнорировать переменную: смена
    поведения без правок кода — прямое требование FR91.
    """
    settings = _settings(monkeypatch, CARD_ACTIVE_LIMIT=value)

    assert settings.card_active_limit == int(value)


@pytest.mark.parametrize("value", ["0", "3", "-1"])
def test_limit_outside_range_fails_start(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """AC-02 (мутация 8): значение вне `1..2` роняет СТАРТ.

    Краснеет, если снять `field_validator`: `0` запретил бы публикацию всем
    и автор видел бы `429` на валидной форме, `3` нарушил бы FR71. Ошибка
    конфигурационная и обязана быть видна при старте, а не при публикации.
    """
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CARD_ACTIVE_LIMIT=value)


def test_limit_range_constants_match_story() -> None:
    """AC-02 (FR71): границы диапазона — ровно 1 и 2.

    Краснеет, если расширить диапазон в константах: валидатор берёт значения
    отсюда, и `0`/`3` снова стали бы допустимыми.
    """
    assert (CARD_ACTIVE_LIMIT_MIN, CARD_ACTIVE_LIMIT_MAX) == (1, 2)


# --------------------------------------------------------------------------
# Task 3: набор активных статусов (AC-01, AC-05)
# --------------------------------------------------------------------------


def test_active_statuses_are_published_full_closed() -> None:
    """AC-01, AC-05 (OQ-1): активны ровно `published`, `full`, `closed`.

    Краснеет от любой из двух правок:
    - убрать `closed` (мутация 4) → закрытый набор не занимал бы слот,
      и автор публиковал третью карточку при двух идущих встречах;
    - добавить `cancelled`/`finished` (мутация 5) → отменённая карточка
      навсегда съедала бы слот (FR100).
    """
    assert set(ACTIVE_STATUSES) == {
        EventCardStatus.PUBLISHED,
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
    }


# --------------------------------------------------------------------------
# Task 3: билдер блокировки и подсчёта (AC-01, AC-04)
# --------------------------------------------------------------------------


def test_author_lock_statement_uses_for_update_on_users() -> None:
    """AC-04 (ADR-09, мутация 1): блокировка строки `users` через `FOR UPDATE`.

    Краснеет, если снять `.with_for_update()` (мутация 1) или заменить
    таблицу на `event_cards` — тогда гонка двух публикаций даёт два успешных
    `UPDATE` (AC-03), а порядок блокировок нарушает ADR-09.
    """
    sql = str(EventCardRepository.build_author_lock_statement(7))

    assert "FROM users" in sql
    assert "FOR UPDATE" in sql
    assert "event_cards" not in sql


def test_author_lock_statement_filters_by_author() -> None:
    """AC-04: блокируется строка КОНКРЕТНОГО автора, а не таблица целиком.

    Краснеет, если убрать `where`: `FOR UPDATE` без условия заблокировал бы
    всех авторов сразу и превратил публикацию в глобальную очередь.
    """
    sql = str(EventCardRepository.build_author_lock_statement(7))

    assert "users.id = :id_1" in sql


def test_active_count_counts_three_statuses() -> None:
    """AC-01, AC-05, AC-07: подсчёт — по трём статусам и конкретному автору.

    Краснеет, если набор статусов сузить/расширить (мутации 4, 5) или
    потерять `author_id` в подсчёте: тогда лимит считался бы по всем
    карточкам сообщества, и первый же автор блокировал остальных.
    """
    statement = EventCardRepository.build_active_count_statement(7)
    where = str(statement.whereclause)

    assert "event_cards.author_id" in where
    assert "event_cards.status IN" in where
    # Значения приходят POSTCOMPILE-параметром: проверяется сам набор
    # статусов, который в этот параметр попадёт.
    assert set(ACTIVE_STATUSES) == {
        EventCardStatus.PUBLISHED,
        EventCardStatus.FULL,
        EventCardStatus.CLOSED,
    }
    for excluded in (
        EventCardStatus.DRAFT,
        EventCardStatus.FINISHED,
        EventCardStatus.CANCELLED,
    ):
        assert excluded not in ACTIVE_STATUSES


def test_active_count_compiles_to_three_bound_statuses() -> None:
    """AC-01: скомпилированный SQL содержит три статуса и ни одного лишнего.

    Краснеет, если подставить в `in_` литеральную строку вместо константы:
    добавление статуса в правило не изменило бы запрос, и лимит считался бы
    по устаревшему набору.
    """
    # `literal_binds=True` обязателен: без него PostgreSQL-диалект оставляет
    # `IN (__[POSTCOMPILE_status_1])`, и тест падал бы на самом себе, а не
    # на дефекте (§18.2 — тавтология «проверка себя»).
    compiled = EventCardRepository.build_active_count_statement(7).compile(
        compile_kwargs={"literal_binds": True}
    )
    rendered = str(compiled)

    for status in ("published", "full", "closed"):
        assert status in rendered
    for excluded in ("draft", "finished", "cancelled"):
        assert excluded not in rendered


# --------------------------------------------------------------------------
# Task 4: предикат CAS (AC-06)
# --------------------------------------------------------------------------


def test_publish_cas_statement_contains_full_predicate() -> None:
    """AC-06 (OQ-6, мутации 2 и 3): CAS повторяет ВСЕ проверки Python.

    Краснеет от любой из правок:
    - снять `status = 'draft'` (мутация 2) → второй `UPDATE` перезапишет
      `published_at`;
    - снять `verification_status = 'approved'` (мутация 3) → отклонённая
      модератором карточка опубликуется, если решение принято между
      проверкой и `UPDATE`;
    - снять `starts_at > now` → опубликуется встреча из прошлого;
    - снять `author_id` → `UPDATE` мог бы затронуть чужую карточку.
    """
    statement = EventCardRepository.build_publish_cas_statement(
        card_id=11,
        author_id=7,
        now=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    where = str(statement.whereclause)

    assert "event_cards.id" in where
    assert "event_cards.author_id" in where
    assert "event_cards.status" in where
    assert "event_cards.verification_status" in where
    assert "event_cards.starts_at >" in where


def test_publish_cas_statement_writes_published_fields() -> None:
    """AC-06: `UPDATE` ставит `published`, `published_at` и `expires_at`.

    Краснеет, если убрать `published_at` из `values`: CHECK
    `ck_event_cards_published` отверг бы вставку, и публикация давала бы `500`.
    """
    expires_at = NOW + timedelta(hours=2)
    statement = EventCardRepository.build_publish_cas_statement(
        card_id=11, author_id=7, now=NOW, expires_at=expires_at
    )
    compiled = statement.compile()

    assert "published" in str(compiled)
    assert "published_at" in str(compiled)
    assert "expires_at" in str(compiled)
    assert expires_at in compiled.params.values()


def test_publish_cas_compiles_bound_draft_and_approved() -> None:
    """AC-06: предикат передаёт `draft` и `approved` параметрами, не литералами.

    Краснеет, если склеить значение f-строкой: тогда запрос нельзя переиспользовать
    и открывается дорога к инъекции (тот же урок, что в `build_get_owned_statement`).
    """
    compiled = EventCardRepository.build_publish_cas_statement(
        card_id=11, author_id=7, now=NOW, expires_at=NOW
    ).compile()

    assert EventCardStatus.DRAFT.value in compiled.params.values()
    assert CardVerificationStatus.APPROVED.value in compiled.params.values()


# --------------------------------------------------------------------------
# Task 5: резолвер причины отказа CAS (AC-06)
# --------------------------------------------------------------------------


def test_resolver_non_draft_card_gives_not_draft() -> None:
    """AC-06: карточку уже опубликовали → `CARD_NOT_DRAFT`.

    Краснеет, если резолвер проверяет статус последним: проигравший
    получил бы `409` с причиной модератора вместо «уже опубликовано».
    """
    card = _card(status=EventCardStatus.PUBLISHED, published_at=NOW)

    with pytest.raises(CardNotDraftError):
        _raise_publish_conflict(card, NOW)


def test_resolver_rejected_card_gives_reason() -> None:
    """AC-06 (FR113): отклонённая карточка → `CARD_REJECTED` с причиной.

    Краснеет, если ветку `rejected` убрать: причина отказа модератора
    потерялась бы, и автор не узнал бы, что править.
    """
    card = _card(
        verification_status=CardVerificationStatus.REJECTED,
        rejection_reason="Нецензурная лексика",
    )

    with pytest.raises(CardRejectedError) as error:
        _raise_publish_conflict(card, NOW)

    assert error.value.reason == "Нецензурная лексика"


def test_resolver_pending_card_gives_verification_pending() -> None:
    """AC-06 (FR101): карточка вернулась в проверку → `VERIFICATION_PENDING`.

    Краснеет, если отдавать `CARD_NOT_DRAFT`: автор пошёл бы исправлять
    статус, хотя карточка ждёт модератора.
    """
    card = _card(verification_status=CardVerificationStatus.PENDING)

    with pytest.raises(CardVerificationPendingError):
        _raise_publish_conflict(card, NOW)


def test_resolver_past_starts_at_gives_past_error() -> None:
    """AC-06 (FR69): время встречи прошло → `STARTS_AT_IN_PAST`.

    Краснеет, если убрать ветку `starts_at <= now`: публикация прошедшей
    встречи дала бы опубликованную карточку с истёкшим окном (CR 4.2, патч 3).
    """
    card = _card(starts_at=NOW - timedelta(hours=1), expires_at=NOW - timedelta(hours=50))

    with pytest.raises(CardStartsAtInPastError):
        _raise_publish_conflict(card, NOW)


def test_resolver_fallback_gives_not_draft() -> None:
    """AC-06: недостижимая ветка не отдаёт «голый» `500`.

    Краснеет, если последней строкой резолвера поставить `assert False`
    или ничего: вызывающий код получил бы необработанное исключение вместо
    доменной ошибки (тот же приём, что `_domain_error`).
    """
    card = _card()

    with pytest.raises(CardNotDraftError):
        _raise_publish_conflict(card, NOW)


# --------------------------------------------------------------------------
# Task 7: доменная ошибка лимита (AC-07)
# --------------------------------------------------------------------------


def test_limit_error_carries_only_numbers() -> None:
    """AC-07 (OQ-8): `details` — только `limit` и `active_count`.

    Краснеет, если положить в `details` `uuid` активных карточек: они раскрыли
    бы состав ленты автора и позволили бы перебор.
    """
    error = CardLimitReachedError(limit=2, active_count=2)

    assert error.code == CARD_LIMIT_REACHED_CODE
    assert error.details == {"limit": 2, "active_count": 2}
    assert set(error.details) == {"limit", "active_count"}
    # Ни один `uuid` не должен попасть в тело ответа.
    for value in error.details.values():
        assert not isinstance(value, uuid_lib.UUID)
