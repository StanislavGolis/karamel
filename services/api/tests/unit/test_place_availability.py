"""Гард доступности места для карточки (Task 7.4; AC-04).

Два уровня проверки:

1. ТЕКСТ SQL статического билдера — без БД (§18.2). Проверяется наличие
   трёх условий (`uuid`, `is_active`, `community_slug`) и отсутствие
   `ORDER BY`/`LIMIT`: это проверка существования, а не выборка.
2. ПОВЕДЕНИЕ сервиса через подменённый репозиторий — обе ветки без БД.

§18.3: гонку «место скрыли между проверкой и вставкой» здесь доказать
нельзя и не нужно — она не разрушает данные (FK цел, «скрытость»
обратима по FR68). Поэтому тест на два соединения не пишется.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

import pytest

from app.repositories.places import PlaceRepository
from app.services.event_card_place import (
    PLACE_NOT_AVAILABLE_CODE,
    PlaceNotAvailableError,
    ensure_place_available,
)

PLACE_UUID = uuid_lib.UUID("22222222-2222-4222-8222-222222222222")


class _StubRepository:
    """Подмена `PlaceRepository`: возвращает заданный ответ, сессии не требует."""

    def __init__(self, *, available: bool) -> None:
        self._available = available
        self.calls: list[dict[str, Any]] = []

    async def is_available_for_card(
        self, *, place_uuid: uuid_lib.UUID, community_slug: str
    ) -> bool:
        self.calls.append({"place_uuid": place_uuid, "community_slug": community_slug})
        return self._available


def test_availability_statement_checks_all_three_conditions() -> None:
    """AC-04 (FR98, ADR-12): в предикате ровно три условия.

    Краснеет от любой из трёх правок:
    - убрать `is_active` → скрытое место примет новую карточку (FR68 нарушен);
    - убрать `community_slug` → карточка Томска сошлётся на кемеровское кафе;
    - убрать `uuid` → условие перестанет отбирать конкретное место.
    """
    statement = PlaceRepository.build_availability_statement(
        place_uuid=PLACE_UUID, community_slug="tomsk"
    )
    # CR 4.1: `whereclause`, а НЕ `str(statement)`. `select(Place)` выбирает все
    # колонки модели, поэтому `places.is_active` есть в SELECT-списке ВСЕГДА, и
    # проверка по полному SQL была тавтологией (§18.2): мутация «убрать
    # `is_active` из WHERE» оставалась зелёной. Поймано доказательством
    # откатом, а не рассуждением.
    where = str(statement.whereclause)

    assert "places.uuid" in where
    assert "places.is_active" in where
    assert "places.community_slug" in where


def test_availability_statement_has_no_ordering_or_limit() -> None:
    """AC-04: проверка существования не сортирует и не ограничивает выборку.

    Краснеет, если скопировать сюда `order_by`/`limit` из
    `build_list_statement`: `ORDER BY` без индекса заставил бы СУБД
    сортировать, а `LIMIT 1` — материализовать строку там, где хватает
    `EXISTS`. Тест держит разделение билдеров: выдача (3.3) и проверка (4.1)
    не должны слиться обратно.
    """
    statement = PlaceRepository.build_availability_statement(
        place_uuid=PLACE_UUID, community_slug="tomsk"
    )
    sql = str(statement)

    assert "ORDER BY" not in sql
    assert "LIMIT" not in sql


async def test_unavailable_place_raises_domain_error() -> None:
    """AC-04: недоступное место → `PlaceNotAvailableError` с кодом и `uuid`.

    Краснеет, если сервис вернуть без исключения (карточка создалась бы
    на скрытое место) или бросить общий `Exception` без кода — роутер 4.2
    не смог бы отобразить это в `422 PLACE_NOT_AVAILABLE`, и клиент получил
    бы `500`.
    """
    repository = _StubRepository(available=False)

    with pytest.raises(PlaceNotAvailableError) as error:
        await ensure_place_available(
            None,  # type: ignore[arg-type]
            place_uuid=PLACE_UUID,
            community_slug="tomsk",
            repository=repository,  # type: ignore[arg-type]
        )

    assert error.value.code == PLACE_NOT_AVAILABLE_CODE
    assert error.value.details == {"place_uuid": str(PLACE_UUID)}


def test_unavailable_error_does_not_reveal_reason() -> None:
    """AC-04 (ADR-13): в теле ошибки нет причины отказа.

    Краснеет, если добавить в `details` признак «скрыто» или «чужое
    сообщество»: перебором `uuid` клиент узнал бы, какие места существуют
    в чужих сообществах. Ровно об этом ADR-13 («404 вместо 403»).

    Проверяется равенство словарю, а не вхождение ключа: ЛЮБОЕ новое поле
    в теле ошибки обязано пройти через это решение.
    """
    error = PlaceNotAvailableError(PLACE_UUID)

    assert error.details == {"place_uuid": str(PLACE_UUID)}


async def test_available_place_does_not_raise() -> None:
    """AC-04: активное место своего сообщества проходит гард.

    Краснеет, если инвертировать условие (`if available: raise`): все
    легальные карточки перестали бы создаваться, а тест на отказ остался бы
    зелёным — поэтому нужна вторая ветка.
    """
    repository = _StubRepository(available=True)

    await ensure_place_available(
        None,  # type: ignore[arg-type]
        place_uuid=PLACE_UUID,
        community_slug="tomsk",
        repository=repository,  # type: ignore[arg-type]
    )

    assert repository.calls == [{"place_uuid": PLACE_UUID, "community_slug": "tomsk"}]
