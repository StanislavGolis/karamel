"""WS-типы стори 8.2 (AC-06, AC-08; D-26, D-28).

`chat.hidden` — событие СКРЫТИЯ диалога; `report.created` — след жалобы.
Оба обязаны быть в реестре (иначе клиент не знает, что получил), и ни один
не входит в `WS_TRANSITION_EVENT_TYPES`: их повтор легитимен.

**Почему `chat.hidden` не переход.** Скрытие идемпотентно: повторная
жалоба отбивается `409`, но подтверждение после реконнекта обязано дойти
(тот же довод, что у `chat.restored`, D-28). С `event_uuid` дедуп отбросил
бы второе подтверждение как дубль — регрессия `W-SUBSCRIBED-01` из 6.5.
"""

from __future__ import annotations

import re

from app.schemas.ws import (
    TYPE_PATTERN,
    WS_EVENT_TYPES,
    WS_TRANSITION_EVENT_TYPES,
    WsEnvelope,
)


def test_chat_hidden_in_registry() -> None:
    """AC-08: тип известен серверу и документации."""
    assert "chat.hidden" in WS_EVENT_TYPES


def test_report_created_in_registry() -> None:
    """AC-06: тип уведомления о жалобе."""
    assert "report.created" in WS_EVENT_TYPES


def test_neither_event_is_a_transition() -> None:
    """D-28: дедуп к ним не применяется — иначе подтверждение теряется."""
    assert "chat.hidden" not in WS_TRANSITION_EVENT_TYPES
    assert "report.created" not in WS_TRANSITION_EVENT_TYPES


def test_both_types_match_nfr60_pattern() -> None:
    """NFR60: `сущность.действие` в нижнем регистре. Проверка ОБОИХ значений."""
    pattern = re.compile(TYPE_PATTERN)
    for type_ in ("chat.hidden", "report.created"):
        assert pattern.match(type_), type_


def test_envelope_without_event_uuid_for_chat_hidden() -> None:
    """D-28: `build` НЕ ставит `event_uuid` событию, которого нет в перечне."""
    envelope = WsEnvelope.build(
        "chat.hidden", {"chat_uuid": "00000000-0000-0000-0000-000000000001", "report_uuid": "x"}
    )
    assert envelope.event_uuid is None


def test_envelope_payload_is_object() -> None:
    """NFR60: `payload` — объект, не массив и не скаляр."""
    envelope = WsEnvelope.build("report.created", {"report_uuid": "r", "chat_uuid": "c"})
    assert isinstance(envelope.payload, dict)
