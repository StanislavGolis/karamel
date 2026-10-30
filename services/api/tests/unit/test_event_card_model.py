"""Модель `event_cards` (Task 7.2; AC-01, AC-03, AC-05, AC-06).

§18.2: проверяется наблюдаемое свойство, а не факт объявления. Имена
ограничений сверяются с `__table__.constraints`, потому что они — часть
контракта: `alembic check` и `downgrade` опираются на имена, и расхождение
модели с миграцией валит откат на несуществующем ограничении (§18.7).

Колонки координат проверяются отдельно: NFR34 запрещает хранить
местоположение в карточке в любом виде, а не только «в виде lat/lng».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.event_card import EventCard

FORBIDDEN_COORDINATE_COLUMNS = ("lat", "lng", "latitude", "longitude", "coords", "geo")


def _card(**overrides: object) -> EventCard:
    """Карточка без записи в БД: проверяется только поведение модели."""
    starts_at = datetime(2026, 11, 1, 19, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "author_id": 1,
        "place_id": 1,
        "city_slug": "tomsk",
        "title": "Настольные игры в „Праге“",
        "category": "cafe",
        "starts_at": starts_at,
        "capacity": 3,
        "decision_window_minutes": 2880,
        "expires_at": starts_at - timedelta(hours=48),
    }
    values.update(overrides)
    return EventCard(**values)  # type: ignore[arg-type]


def test_capacity_one_is_request_mode() -> None:
    """AC-03 (FR70): вместимость 1 — режим заявок.

    Краснеет, если `is_request_mode` сравнивать с чем-то кроме
    `CARD_REQUEST_MODE_CAPACITY`, например с диапазоном `capacity <= 2`:
    карточка на двоих стала бы «заявками», и 4.2 потребовал бы у неё окно
    решения, которого форма открытого набора не показывает.
    """
    assert _card(capacity=1, title="Кофе").is_request_mode is True


def test_capacity_above_one_is_open_set() -> None:
    """AC-03 (FR70): 2–6 — открытый набор, окно не обязательно.

    Краснеет, если границу сдвинуть на 3: карточка на двоих попала бы
    в режим заявок, и FR85 («компания 2–6 человек») стал бы недостижим
    для минимального размера компании.
    """
    assert _card(capacity=2).is_request_mode is False
    assert _card(capacity=6).is_request_mode is False


def test_constraint_names_follow_naming_convention() -> None:
    """AC-01, AC-05, AC-06, §18.7: имена ограничений — часть контракта.

    Краснеет, если в модели написать короткое `name="uuid"` у
    `UniqueConstraint`: у конвенции `uq` нет токена `%(constraint_name)s`
    (дефект 3.1), и имя стало бы `uuid` без префикса — миграция с
    `op.f("uq_event_cards_uuid")` разошлась бы с моделью, `alembic check`
    покраснел бы, а `downgrade` упал бы на несуществующем ограничении.
    """
    names = {constraint.name for constraint in EventCard.__table__.constraints}

    assert {
        "pk_event_cards",
        "uq_event_cards_uuid",
        "ck_event_cards_status",
        "ck_event_cards_verification_status",
        "ck_event_cards_category",
        "ck_event_cards_capacity",
        "ck_event_cards_filled_count",
        "ck_event_cards_decision_window",
        "ck_event_cards_expires_at",
        "ck_event_cards_city_slug",
        "ck_event_cards_published",
        "ck_event_cards_rejection_reason",
        "ck_event_cards_full_capacity",
        "ck_event_cards_request_mode",
    } <= names


def test_no_coordinate_columns() -> None:
    """AC-01, NFR34: в карточке нет ни одной колонки координат.

    Краснеет, если добавить `lat`/`lng`/`geo`/`coords`: местоположение
    карточки обязано быть ссылкой на проверенное место (FR98), а координаты
    — трансграничная передача персональных данных, запрещённая NFR34
    (геокодер для адреса не вызывается вовсе).
    """
    columns = {column.name for column in EventCard.__table__.columns}

    for forbidden in FORBIDDEN_COORDINATE_COLUMNS:
        assert forbidden not in columns


def test_index_names_are_full_strings() -> None:
    """AC-07: семь индексов карточки объявлены в модели с полными именами.

    Краснеет, если индекс переименовать только в модели или только
    в миграции: `alembic check` покажет расхождение, а частичный предикат
    перестанет совпадать с воркером, который по нему выбирает строки.

    Стори 4.4 добавила два индекса (AC-01, AC-04, X-16):
    `ix_event_cards_author_publishable` (подсчёт лимита под
    `FOR UPDATE users`) и `ix_event_cards_place_id` (FK `RESTRICT`
    без индекса даёт `Seq Scan`). Оба объявлены и в миграции `0010`.
    """
    names = {index.name for index in EventCard.__table__.indexes}

    assert names == {
        "ix_event_cards_city_starts",
        "ix_event_cards_author_active",
        "ix_event_cards_pending_verification",
        "ix_event_cards_due_windows",
        "ix_event_cards_prompt_due",
        # Стори 4.4.
        "ix_event_cards_author_publishable",
        "ix_event_cards_place_id",
        # Стори 5б.2 (AC-01, D-5b.2-1): уникальность чата компании. Индекс
        # ЧАСТИЧНЫЙ (`WHERE chat_uuid IS NOT NULL`), и имя обязано совпадать
        # с миграцией `0013` — иначе `alembic check` краснеет.
        "uq_event_cards_chat_uuid",
        # Стори 4.6 (AC-05): выборка воркера «что уже началось, но не в архиве».
        "ix_event_cards_started",
        # Стори 6.1 (AC-06, долг X-5b.1-1): предикат окна ленты с порядком
        # `(starts_at, uuid)`. Имя обязано совпадать с миграцией `0014`,
        # иначе `alembic check` краснеет, а страница ленты сортируется в памяти.
        "ix_event_cards_feed_visible",
    }
