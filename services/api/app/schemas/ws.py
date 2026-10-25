"""Схемы WebSocket-конверта (стори 6.5; AC-02, NFR60).

**Почему конверт живёт в схемах, а не в сервисе.** NFR60 фиксирует формат
`{type, payload, ts}` как единый для чата и карточек. Если бы его собирал
каждый сервис сам, «единый формат» держался бы на дисциплине: первая же
публикация карточки сделала бы `ts` без `Z` или `payload` массивом, и
клиент не смог бы разобрать событие. Одна модель — один источник истины.

**Почему `type` — регекс, а не `Enum`.** Список типов растёт от стори к
стори (`message.created`, `chat.restored`, `card.full`, `request.selected`,
`attendance.request`…). `Enum` потребовал бы правки схемы при каждом новом
событии и отвергал бы события будущих версий сервера на старом клиенте —
а NFR60 требует обратного: клиент обязан неизвестный тип ИГНОРИРОВАТЬ, а
не падать. Регекс проверяет форму (`сущность.действие`), не перечень.

**Почему `ts` — строка, а не `datetime`.** Pydantic сериализует
`datetime` как `+00:00`, а NFR60 и `message_cursor` требуют суффикс `Z`.
Приведение внутри модели было бы подменой формата в одном месте и
расхождением с курсором в другом.
"""

from __future__ import annotations

import re
import uuid as uuid_lib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "WS_EVENT_TYPES",
    "WS_TRANSITION_EVENT_TYPES",
    "WsEnvelope",
    "utc_timestamp",
]

#: NFR60: `type` — `сущность.действие`, нижний регистр, точка между частями.
TYPE_PATTERN = r"^[a-z_]+\.[a-z_]+$"
_TYPE_RE = re.compile(TYPE_PATTERN)

#: Реестр известных серверу типов (Task 1.3) — единый источник для сервера
#: и документации. НЕ ограничение протокола: клиент получает и типы, которых
#: здесь нет, если сервер их отправил (NFR60: игнорировать, не падать).
WS_EVENT_TYPES: tuple[str, ...] = (
    "message.created",
    "message.delivered",
    "chat.restored",
    "chat.subscribed",
    "card.full",
    "request.selected",
    # Стори 9.2 (AC-1, FR43): автору пришла заявка. НЕ входит в
    # `WS_TRANSITION_EVENT_TYPES`: это факт, а не переход состояния, и дедуп
    # отбросил бы повтор после реконнекта (D-9.2-10).
    "request.received",
    # Стори 9.2 (AC-3, FR45): автору собрана компания. Пара к `request.selected`;
    # в `WS_TRANSITION_EVENT_TYPES` не входит по той же причине — событие
    # адресное и повторяется при каждой следующей сборке по другой карточке.
    "company.assembled",
    "attendance.done",
    # Стори 7.3 (AC-10, D-7.3-9): приглашение подтвердить явку. НЕ входит в
    # `WS_TRANSITION_EVENT_TYPES`: это не переход состояния, а повторяемое
    # приглашение, и дедуп переходов не должен его отбрасывать.
    "attendance.request",
    # Стори 8.2 (AC-08, D-28): диалог скрыт жалобой. НЕ входит в
    # `WS_TRANSITION_EVENT_TYPES`: повторное скрытие идемпотентно, а дедуп
    # отбросил бы легитимное подтверждение после реконнекта.
    "chat.hidden",
    # Стори 8.2 (AC-06): след жалобы у обеих сторон. Повторная жалоба
    # отбивается `409` (AC-05), поэтому событие не повторяется.
    "report.created",
    # Стори 8.4 (AC-01, D-50): санкция вынесена. Публикуется ТОЛЬКО в канал
    # диалога (`chat:{uuid}`), поэтому для жалобы на неявку события нет:
    # пользовательского канала в системе не существует, уведомление живёт
    # строкой в `notifications` (эпик 9 его покажет).
    #
    # НЕ входит в `WS_TRANSITION_EVENT_TYPES`: санкция — факт, а не переход
    # состояния. Дедуп отбросил бы повторную доставку после реконнекта, и
    # обвиняемый не увидел бы решение.
    "sanction.issued",
    # Стори 8.4 (AC-01, D-50): решение по жалобе получено автором. Пара к
    # `sanction.issued`; в `WS_TRANSITION_EVENT_TYPES` не входит по той же
    # причине.
    "report.resolved",
)

#: D-2 (решение пакета): `event_uuid` ставится ТОЛЬКО событиям-ПЕРЕХОДАМ —
#: тем, чей повтор является дублем, а не новым фактом.
#:
#: Почему список, а не «всем событиям». `chat.subscribed` и `chat.restored`
#: описывают состояние СОЕДИНЕНИЯ: клиент может подписаться на тот же чат
#: дважды, и оба подтверждения обязаны дойти (иначе повторная подписка после
#: реконнекта теряет подтверждение — регрессия 6.5, `W-SUBSCRIBED-01`).
#: С обязательным `event_uuid` они начали бы дедупиться, и легитимный повтор
#: был бы отброшен как дубль.
#:
#: `attendance.done` — переход в терминальный статус явки (7.2);
#: `message.created`/`message.delivered` — переходы сообщения (6.4/6.5).
WS_TRANSITION_EVENT_TYPES: tuple[str, ...] = (
    "message.created",
    "message.delivered",
    "attendance.done",
)


def utc_timestamp(moment: datetime | None = None) -> str:
    """ISO-8601 UTC с суффиксом `Z` (NFR60, та же форма, что у курсора).

    Одна функция на все места, где формируется `ts`: `envelope`-сборка в
    сервисе, тесты и фронт читают одно и то же представление. `+00:00`
    вместо `Z` — расхождение с `message_cursor`, который клиент уже умеет
    разбирать.
    """
    value = moment if moment is not None else datetime.now(UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class WsEnvelope(BaseModel):
    """Событие WS по NFR60. `payload` — объект, не массив и не скаляр.

    `frozen=True`: событие неизменяемо после сборки. Мутация payload после
    валидации обошла бы проверку формы и разослала клиентам то, что никто
    не проверял.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(..., description="`сущность.действие`, нижний регистр")
    payload: dict[str, Any] = Field(..., description="объект полезной нагрузки")
    ts: str = Field(..., description="ISO-8601 UTC с суффиксом Z")
    # D-2: есть ТОЛЬКО у событий-переходов (`WS_TRANSITION_EVENT_TYPES`).
    # `None` у событий состояния соединения — не «забыли поставить», а
    # осознанное отсутствие: дедуп к ним не применяется.
    event_uuid: str | None = Field(
        default=None, description="uuid4 события-перехода; ключ дедупа `(type, event_uuid)`"
    )

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        """Форма типа, а не его наличие в реестре (см. модульный докстринг)."""
        if not _TYPE_RE.match(value):
            raise ValueError(
                "type обязан быть `сущность.действие` в нижнем регистре через точку (NFR60)"
            )
        return value

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Пустой объект допустим; массив и скаляр отвергаются типом поля."""
        return value

    @field_validator("ts")
    @classmethod
    def _validate_ts(cls, value: str) -> str:
        """`ts` обязан оканчиваться на `Z` — иначе клиент разберёт его иначе."""
        if not value.endswith("Z"):
            raise ValueError("ts обязан быть ISO-8601 UTC с суффиксом Z (NFR60)")
        return value

    @classmethod
    def build(
        cls,
        type_: str,
        payload: dict[str, Any],
        moment: datetime | None = None,
        *,
        event_uuid: str | None = None,
    ) -> WsEnvelope:
        """Единственная точка сборки события: `ts` и `event_uuid` ставятся здесь.

        **`event_uuid` — одной точкой, как `ts`** (D-7.2-6): uuid4 на КАЖДУЮ
        сборку события-перехода. Новый uuid на каждый кадр — это и есть
        корректная семантика: два разных перехода обязаны иметь разные ключи,
        а повторная ДОСТАВКА одного кадра дедупится брокером по тому же ключу.

        Событиям состояния соединения (`chat.subscribed`, `chat.restored`)
        `event_uuid` НЕ ставится (D-2): их повтор легитимен, и дедуп склеил бы
        два подтверждения подписки (`W-SUBSCRIBED-01`).

        Явная передача `event_uuid` нужна тестам двойной доставки: чтобы
        отправить тот же кадр дважды, ключ должен быть один и тот же.
        """
        resolved = event_uuid
        if resolved is None and type_ in WS_TRANSITION_EVENT_TYPES:
            resolved = str(uuid_lib.uuid4())
        return cls(type=type_, payload=payload, ts=utc_timestamp(moment), event_uuid=resolved)

    def to_frame(self) -> dict[str, Any]:
        """JSON-совместимый словарь для `send_json`/`json.dumps`.

        `event_uuid` попадает в кадр ТОЛЬКО когда он есть: у событий состояния
        соединения поля нет вовсе, а не `null`. Иначе клиент старой версии
        увидел бы новое поле с пустым значением и попытался бы дедупить по нему.
        """
        frame: dict[str, Any] = {"type": self.type, "payload": self.payload, "ts": self.ts}
        if self.event_uuid is not None:
            frame["event_uuid"] = self.event_uuid
        return frame
