"""Тип встречи и активность в схемах карточки (стори 12.4; AC-3, AC-4, AC-6).

Краснеет при:
- снятии обязательности `kind`/`activity` в `EventCardCreate` — U-1;
- снятии валидатора `other` в `Create` или `Update` — U-4, U-5;
- появлении связи «`capacity=1` → `kind=date`» — U-6 (AC-4);
- возврате умолчания `capacity=2` — U-10 (`W-11.5-c`).

`other` — легаси-значение БД (`0037`), не выбор автора (FR118).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.enums import ACTIVITY_INPUT_VALUES, Activity, CardKind
from app.schemas.event_card import EventCardCreate, EventCardUpdate

PLACE_UUID = uuid_lib.UUID("11111111-1111-4111-8111-111111111111")
STARTS_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "title": "Кофе в „Праге“",
        "category": "cafe",
        "place_uuid": PLACE_UUID,
        "starts_at": STARTS_AT,
        "capacity": 2,
        "kind": "date",
        "activity": "coffee",
    }
    values.update(overrides)
    return values


def test_kind_and_activity_are_required() -> None:
    """AC-3 (FR117/FR118): без типа или активности карточка не создаётся.

    Краснеет, если у `kind`/`activity` появится умолчание: карточка
    без смысла встречи попала бы в ленту.
    """
    for field in ("kind", "activity"):
        payload = _payload()
        payload.pop(field)
        with pytest.raises(ValidationError) as error:
            EventCardCreate(**payload)
        assert field in str(error.value)


def test_kind_accepts_only_two_values() -> None:
    """AC-1 (FR117): тип встречи — ровно `date`/`company`."""
    assert {str(item) for item in CardKind} == {"date", "company"}
    with pytest.raises(ValidationError):
        EventCardCreate(**_payload(kind="meeting"))


@pytest.mark.parametrize("activity", [str(item) for item in ACTIVITY_INPUT_VALUES])
def test_activity_accepts_whitelist(activity: str) -> None:
    """AC-3 (FR118): все шесть значений белого списка принимаются."""
    card = EventCardCreate(**_payload(activity=activity))
    assert str(card.activity) == activity


def test_activity_other_is_rejected_on_create() -> None:
    """AC-3: легаси `other` — не выбор автора (U-4).

    Краснеет, если удалить валидатор в `EventCardCreate`: «неизвестная
    активность» стала бы седьмым значением, выбираемым через API.
    """
    with pytest.raises(ValidationError) as error:
        EventCardCreate(**_payload(activity="other"))
    assert "activity_other_reserved" in str(error.value)


def test_activity_other_is_rejected_on_update() -> None:
    """AC-3: `PATCH` — второй вход в то же поле (U-5).

    Краснеет, если валидатор есть только в `Create`: `PATCH` стал бы
    обходным путём к легаси-значению (класс `W-11.5-c`).
    """
    with pytest.raises(ValidationError) as error:
        EventCardUpdate(activity="other")
    assert "activity_other_reserved" in str(error.value)


def test_update_allows_whitelisted_activity() -> None:
    """AC-3: валидатор не блокирует легальные значения.

    Пара к тесту выше: без неё «всегда падает» не отличить от «`other`
    запрещён» (класс одностороннего теста, урок 11.10).
    """
    assert str(EventCardUpdate(activity="cinema").activity) == "cinema"
    assert EventCardUpdate().activity is None


def test_capacity_one_with_company_is_allowed() -> None:
    """AC-4 (ADR-20): `capacity=1` + `kind=company` — легальная карточка.

    Краснеет, если появится валидатор «`capacity=1` → `date`»: автор
    потерял бы право собрать компанию из одного собеседника (U-6).
    """
    card = EventCardCreate(**_payload(capacity=1, kind="company", decision_window_minutes=60))
    assert card.capacity == 1
    assert str(card.kind) == "company"


def test_capacity_is_required() -> None:
    """AC-6 (`W-11.5-c`): без `capacity` — отказ, а не умолчание 2 (U-10).

    Краснеет, если вернуть `capacity: int = Field(default=2, ...)`: забытое
    поле молча уводило бы карточку `capacity=1` в открытый набор.
    """
    payload = _payload()
    payload.pop("capacity")
    with pytest.raises(ValidationError) as error:
        EventCardCreate(**payload)
    assert "capacity" in str(error.value)


def test_capacity_one_without_window_is_still_rejected() -> None:
    """Пара к AC-6: обязательность `capacity` не отменила правило окна.

    Без этого теста «`capacity` обязателен» проходил бы и при сломанном
    `model_validator` режима заявок.
    """
    with pytest.raises(ValidationError) as error:
        EventCardCreate(**_payload(capacity=1))
    assert "decision_window" in str(error.value)


def test_activity_enum_keeps_legacy_other_but_input_does_not() -> None:
    """Граница «вход ≠ БД»: `other` есть в enum, нет в списке входа.

    Краснеет, если `ACTIVITY_INPUT_VALUES` соберут из `tuple(Activity)`:
    валидатор пропустит легаси, и `other` вернётся в форму.
    """
    assert Activity.OTHER in set(Activity)
    assert Activity.OTHER not in ACTIVITY_INPUT_VALUES
    assert len(ACTIVITY_INPUT_VALUES) == 6
