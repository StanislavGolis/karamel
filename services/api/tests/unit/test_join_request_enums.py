"""Enum заявок `JoinRequestStatus` (стори 5а.1, Task 5.1; AC-02, AC-04).

Проверяется наблюдаемое значение — набор строк, уходящий в `CHECK`
и в предикаты частичных индексов, а не факт объявления класса (§18.2).
"""

from __future__ import annotations

from app.models.enums import (
    JOIN_REQUEST_ACTIVE_STATUSES,
    JOIN_REQUEST_OCCUPYING_STATUSES,
    JoinRequestStatus,
)


def test_status_values_are_exact() -> None:
    """AC-02: ровно пять значений, посимвольно.

    Краснеет, если добавить/переименовать значение: предикаты частичных
    индексов (`status IN ('pending','selected')`, `status = 'pending'`)
    опираются на эти строки, и расхождение дало бы индекс, который
    никогда не совпадает, — лимит 2 и автовыбор молча перестали бы работать.
    """
    assert tuple(str(status) for status in JoinRequestStatus) == (
        "pending",
        "selected",
        "not_selected",
        "expired",
        "cancelled",
    )


def test_active_statuses_is_pending_only() -> None:
    """AC-04, FR77/NFR29: слот лимита занимает только `pending`.

    Краснеет, если добавить `selected`: заявитель, которого уже выбрали,
    не смог бы подать заявку на другую карточку — лимит «две активные»
    считал бы решения. Краснеет и при потере `pending`: лимит перестал бы
    считаться вовсе.
    """
    assert JOIN_REQUEST_ACTIVE_STATUSES == (JoinRequestStatus.PENDING,)


def test_occupying_statuses_are_pending_and_selected() -> None:
    """AC-04, FR82: частичный `uq` покрывает `pending` и `selected`.

    Краснеет, если оставить только `pending`: два `selected` на одной паре
    прошли бы, и на одну карточку оказалось бы две активные роли.
    Краснеет и при добавлении терминальных — тогда безусловный `UNIQUE`
    запретил бы повторную подачу после отмены (FR82).
    """
    assert JOIN_REQUEST_OCCUPYING_STATUSES == (
        JoinRequestStatus.PENDING,
        JoinRequestStatus.SELECTED,
    )
