"""Схема `EventCardCreate` (Task 7.3; AC-02, AC-03).

§18.2: сравнение с ТОЧНЫМ ожидаемым значением, а не «часовой пояс не UTC»
и не «время изменилось». Тест вида `assert ts != value` проходил бы и при
сдвиге на произвольное число часов.

§18.1: каждый тест краснеет от конкретной правки — в docstring указано,
какой именно.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.event_card import EventCardCreate

PLACE_UUID = uuid_lib.UUID("11111111-1111-4111-8111-111111111111")


def _payload(**overrides: object) -> dict[str, object]:
    """Минимальный валидный вход: открытый набор на 3 места."""
    values: dict[str, object] = {
        "title": "Настольные игры в „Праге“",
        "category": "cafe",
        "place_uuid": PLACE_UUID,
        "starts_at": "2026-11-01T19:00:00+07:00",
        "capacity": 3,
        # Стори 12.4 (AC-3, FR117/FR118): тип и активность обязательны.
        "kind": "company",
        "activity": "boardgames",
    }
    values.update(overrides)
    return values


def test_aware_starts_at_is_converted_to_utc_once() -> None:
    """AC-02 (ADR-16): `+07:00` → ровно `12:00:00Z`.

    Краснеет, если валидатор вернуть без `astimezone(UTC)` (останется +07:00)
    или применить приведение дважды с неверным знаком. Точное ожидаемое
    значение ловит сдвиг на любое число часов, а не только на 7.
    """
    card = EventCardCreate(**_payload())

    assert card.starts_at == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert card.starts_at.tzinfo is UTC


def test_naive_starts_at_is_rejected() -> None:
    """AC-02: «наивная» строка отклоняется, а не трактуется как локальная.

    Краснеет, если убрать проверку `tzinfo is None`: Pydantic примет
    `2026-11-01T19:00:00` как `datetime` без зоны, и событие молча сдвинется
    на разницу поясов автора (Томск, +07:00) — выяснится только на встрече.
    """
    with pytest.raises(ValidationError) as error:
        EventCardCreate(**_payload(starts_at="2026-11-01T19:00:00"))

    assert "starts_at" in str(error.value)
    assert "часовой пояс" in str(error.value)


def test_utc_starts_at_is_accepted() -> None:
    """AC-02: значение уже в UTC принимается без изменений.

    Краснеет, если в валидаторе появится безусловный сдвиг (например
    `+ timedelta(hours=7)`): граница «уже UTC» должна быть тождеством.
    """
    card = EventCardCreate(**_payload(starts_at="2026-11-01T12:00:00Z"))

    assert card.starts_at == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("capacity", [0, 7])
def test_capacity_outside_range_is_rejected(capacity: int) -> None:
    """AC-01 (FR70): вместимость вне 1..6 отклоняется схемой.

    Краснеет, если снять `ge`/`le` у поля: значение дойдёт до CHECK
    `ck_event_cards_capacity` и станет `500` вместо `422` на валидном
    для формы вводе (0 мест или компания из 7 человек).

    Обе границы в одной параметризации: `ge` и `le` — разные аргументы
    `Field`, и падение одного не видно по второму.
    """
    with pytest.raises(ValidationError):
        EventCardCreate(**_payload(capacity=capacity))


def test_request_mode_requires_decision_window() -> None:
    """AC-03 (FR70/FR79): вместимость 1 без окна решения — ошибка.

    Краснеет, если убрать `model_validator`: карточка «возьму одного»
    создалась бы без срока выбора, и заявки висели бы бессрочно.
    """
    with pytest.raises(ValidationError) as error:
        EventCardCreate(**_payload(capacity=1))

    assert "decision_window_minutes" in str(error.value)


def test_request_mode_with_decision_window_is_accepted() -> None:
    """AC-03: с окном решения режим заявок проходит.

    Краснеет, если валидатор требовать окно при ЛЮБОЙ вместимости: легальный
    ввод с `capacity=1` и окном 30 минут отвергался бы.
    """
    card = EventCardCreate(**_payload(capacity=1, decision_window_minutes=30))

    assert card.capacity == 1
    assert card.decision_window_minutes == 30


def test_open_set_without_decision_window_is_accepted() -> None:
    """AC-03 (FR70): открытому набору окно не нужно — дефолт подставит 4.2.

    Краснеет, если валидатор требовать окно для всех режимов: форма
    открытого набора стала бы непроходимой без поля, которого в ней нет.
    """
    card = EventCardCreate(**_payload(capacity=3))

    assert card.decision_window_minutes is None


def test_blank_title_is_rejected_after_normalization() -> None:
    """AC-01: заголовок из пробельных символов нормализуется в пустую строку.

    Краснеет, если убрать валидатор `_strip`: `'   '` (три символа) прошло бы
    `min_length=1`, карточка попала бы в ленту с пустым заголовком, и
    единственным следом была бы жалоба участника.
    """
    with pytest.raises(ValidationError):
        EventCardCreate(**_payload(title=" \t\u00a0 "))


def test_server_side_field_is_forbidden() -> None:
    """AC-08, AC-01: серверные поля не принимаются от клиента.

    Краснеет, если заменить `extra="forbid"` на `"ignore"`: клиент передал бы
    `status='published'` и считал карточку опубликованной, тогда как сервер
    создал черновик — расхождение обнаружилось бы только в ленте.
    """
    with pytest.raises(ValidationError):
        EventCardCreate(**_payload(status="published"))


def test_title_is_normalized_before_length_check() -> None:
    """AC-01: нормализация идёт ДО проверки длины.

    Краснеет, если поменять `mode="before"` на `"after"`: заголовок из 100
    значимых символов с NBSP по краям считался бы 102-символьным и получал
    бы `422` на легальном вводе.
    """
    title = "и" * 100

    card = EventCardCreate(**_payload(title=f"  {title}\u00a0 "))

    assert card.title == title


def test_description_preserves_paragraph_breaks() -> None:
    """CR 4.1, патч 1 (AC-01): описание — многострочное, абзацы сохраняются.

    Дефект, найденный код-ревью НА ИСПОЛНЕНИИ: через `normalize_text`
    `'Первая строка\n\nВторой абзац'` превращалось в одну строку, и автор
    видел в ленте слипшийся текст. Краснеет, если вернуть `normalize_text`
    вместо `normalize_bio`: в проекте уже есть правильный нормализатор для
    многострочного поля (`profiles.bio`, 2.3).
    """
    card = EventCardCreate(**_payload(description="Первая строка\n\nВторой абзац\nтретья строка"))

    assert card.description == "Первая строка\n\nВторой абзац\nтретья строка"


def test_blank_description_becomes_none() -> None:
    """CR 4.1, патч 1 (AC-01, project-context §4): пусто → `null`, не `''`.

    Краснеет, если после нормализации оставлять `''`: клиент не отличит
    «описания нет» от «описание пустое», а §4 требует `null` для пустого
    значения. Заодно убирает мусорную строку из `Text`-колонки.
    """
    card = EventCardCreate(**_payload(description="   \n\t "))

    assert card.description is None


def test_event_card_ref_has_no_internal_ids() -> None:
    """AC-08 (X-13, D-2): в ответе нет `id`/`author_id`/`place_id`.

    Краснеет, если добавить любое внутреннее поле: `id` стал бы публичным
    идентификатором (перебор `/1..N` обошёл бы лимиты), `author_id`/`place_id`
    раскрыли бы внутренние связи и нарушили §7.2.
    """
    from app.schemas.event_card import EventCardRef

    assert set(EventCardRef.model_fields) == {
        "uuid",
        "status",
        "verification_status",
        "created_at",
    }


def test_event_card_update_rejects_server_fields() -> None:
    """AC-05, AC-08: серверные поля в `PATCH` → `422`, не молчаливый выброс.

    Краснеет, если заменить `extra="forbid"` на `ignore`: клиент передал бы
    `status='published'` и считал карточку опубликованной, тогда как сервер
    оставил черновик.
    """
    from app.schemas.event_card import EventCardUpdate

    with pytest.raises(ValidationError):
        EventCardUpdate(status="published")

    with pytest.raises(ValidationError):
        EventCardUpdate(author_id=1)

    with pytest.raises(ValidationError):
        EventCardUpdate(expires_at=datetime(2026, 11, 1, tzinfo=UTC))


def test_event_card_update_none_means_do_not_touch() -> None:
    """AC-05: `None` — «не трогать», все поля необязательны.

    Краснеет, если сделать хотя бы одно поле обязательным: `PATCH` только
    заголовка требовал бы передать остальные, и клиент затирал бы ими данные.
    """
    from app.schemas.event_card import EventCardUpdate

    update = EventCardUpdate()

    assert update.title is None
    assert update.capacity is None
    assert update.decision_window_minutes is None


def test_event_card_update_normalizes_title() -> None:
    """AC-05: заголовок нормализуется тем же кодом, что при создании.

    Краснеет, если убрать валидатор из `EventCardUpdate`: `'  Игры  '`
    сохранилось бы с пробелами, и `PATCH` дал бы строку, которую `POST`
    не пропускает.
    """
    from app.schemas.event_card import EventCardUpdate

    update = EventCardUpdate(title="  Игры  в  Праге  ")

    assert update.title == "Игры в Праге"


def test_event_card_update_keeps_paragraphs_in_description() -> None:
    """AC-05: описание — многострочное (тот же нормализатор, что в `Create`).

    Краснеет, если в `EventCardUpdate` оставить `normalize_text`:
    `PATCH` описания схлопнул бы абзацы, хотя `POST` их сохраняет.
    """
    from app.schemas.event_card import EventCardUpdate

    update = EventCardUpdate(description="Первый абзац\n\nВторой")

    assert update.description == "Первый абзац\n\nВторой"
