"""Сервис настроек канала уведомлений (стори 9.3; AC-01, AC-02; FR46).

**Почему сервис, а не роут.** «Перечень 11 типов с `enabled` и `required`» —
одно правило, которое должны видеть и API, и будущий экран истории (эпик 10),
и тест сверки трёх источников перечня. Если бы сборка ответа жила в роуте,
второй потребитель собрал бы её иначе, и `required` разошёлся бы с гардом
`409` — то есть UI показал бы переключатель, который API отвергает.

**Порядок типов — `NOTIFICATION_TYPES`, а не БД** (D-9.3-8). `GET` обязан
отдавать ПОЛНЫЙ перечень в фиксированном порядке: клиент не хранит список
типов у себя (NFR60 велит игнорировать неизвестное, и локальный перечень
тихо отстал бы от серверного). Сортировка по алфавиту или по `id` строки
в таблице дала бы разный порядок экрана при каждом изменении настроек:
пользователь видел бы «прыгающие» переключатели.

**`enabled` вычисляется вычитанием отключённых** (D-9.3-3). Хранится
отключённое, поэтому `enabled = type not in muted`. Отсутствие строки —
«включено», и дефолт нового типа получается сам собой (мутация M11 —
инверсия дефолта в `false` — ломает это свойство).

**Доменные ошибки, не HTTP-коды** (project-context §9):
`UnknownNotificationTypeError` → `422 invalid_notification_type`,
`NotificationTypeRequiredError` → `409 notification_type_required`.
Маппинг живёт в роуте, чтобы сервис не знал про HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NOTIFICATION_TYPES, REQUIRED_NOTIFICATION_TYPES
from app.repositories.notification_settings import NotificationSettingsRepository

__all__ = [
    "NotificationSettingItem",
    "NotificationTypeRequiredError",
    "UnknownNotificationTypeError",
    "list_settings",
    "set_enabled",
]

#: D-9.3-5: неотключаемые типы как множество значений — проверка гарда и
#: сборка `required` в ответе обязаны читать ОДИН источник. Второй кортеж
#: разошёлся бы с `CHECK` в БД, и API разрешил бы то, что схема отвергает.
_REQUIRED: frozenset[str] = frozenset(str(t) for t in REQUIRED_NOTIFICATION_TYPES)


class UnknownNotificationTypeError(Exception):
    """AC-01: тип вне перечня 11 (или не строка) → `422 invalid_notification_type`.

    Отдельный класс, а не `ValueError`: `ValueError` в этом проекте ловят
    широко, и `422` получил бы любой сбой валидации внутри сервиса.
    """

    def __init__(self, notification_type: object) -> None:
        self.notification_type = notification_type
        super().__init__(f"unknown notification type: {notification_type!r}")


class NotificationTypeRequiredError(Exception):
    """AC-01, D-9.3-5: попытка отключить неотключаемый тип → `409`.

    Это КОНФЛИКТ с инвариантом (FR38/FR39 обязаны доставляться), а не ошибка
    формы: неизвестный тип даёт `422`, неотключаемый — `409`. Разные коды
    дают клиенту разные действия: `422` — баг клиента, `409` — «этот
    переключатель надо показать выключенным и недоступным».
    """

    def __init__(self, notification_type: str) -> None:
        self.notification_type = notification_type
        super().__init__(f"notification type is required: {notification_type}")


@dataclass(frozen=True, slots=True)
class NotificationSettingItem:
    """Один тип уведомления в ответе настроек (AC-01).

    `required=True` означает «нельзя отключить»: UI рисует переключатель
    включённым и `disabled`, а API отвечает `409` при попытке выключить.
    """

    type: str
    enabled: bool
    required: bool


async def list_settings(session: AsyncSession, *, user_id: int) -> list[NotificationSettingItem]:
    """AC-01, AC-02: полный перечень 11 типов в порядке `NOTIFICATION_TYPES`.

    Один запрос к БД (`list_muted`), остальное — вычисление. Порядок берётся
    из Python-перечня: он зафиксирован и не зависит от того, какие строки
    лежат в `notification_mutes` (их порядок БД не гарантирует).
    """
    muted = await NotificationSettingsRepository(session).list_muted(user_id=user_id)
    return [
        NotificationSettingItem(
            type=str(notification_type),
            # D-9.3-3, M11: отсутствие строки = включено. Инверсия дефолта
            # (opt-in) сделала бы нового пользователя глухим ко всем
            # уведомлениям без единой строки в настройках.
            enabled=str(notification_type) not in muted,
            required=str(notification_type) in _REQUIRED,
        )
        for notification_type in NOTIFICATION_TYPES
    ]


async def set_enabled(
    session: AsyncSession, *, user_id: int, notification_type: object, enabled: bool
) -> NotificationSettingItem:
    """AC-01: переключает ОДИН тип и возвращает его новое состояние (D-9.3-8).

    Порядок проверок важен: сначала «тип вообще существует» (`422`), потом
    «тип неотключаемый и его выключают» (`409`). Обратный порядок на мусорном
    типе дал бы `409` вместо `422`.

    **`enabled=True` для неотключаемого типа разрешён** и является no-op: это
    не конфликт, а повтор уже действующего состояния. Запрет на включение
    сломал бы идемпотентность PATCH (D-9.3-12): клиент, повторяющий
    `{required, true}`, получал бы `409` на корректный запрос.

    `notification_type` принимается как `object`: схема запроса может отдать
    строку любой формы, и проверка вхождения — задача сервиса, а не Pydantic
    (иначе `422` пришёл бы от FastAPI с другим телом ошибки).
    """
    known = {str(t) for t in NOTIFICATION_TYPES}
    if not isinstance(notification_type, str) or notification_type not in known:
        raise UnknownNotificationTypeError(notification_type)

    if not enabled and notification_type in _REQUIRED:
        raise NotificationTypeRequiredError(notification_type)

    repo = NotificationSettingsRepository(session)
    if enabled:
        await repo.set_unmuted(user_id=user_id, type=notification_type)
    else:
        await repo.set_muted(user_id=user_id, type=notification_type)

    return NotificationSettingItem(
        type=notification_type,
        enabled=enabled,
        required=notification_type in _REQUIRED,
    )


#: AC-01: перечень типов как `tuple[str, ...]` для схем ответа и тестов сверки.
#: Не второй источник: вычисляется из `NOTIFICATION_TYPES`.
NOTIFICATION_TYPE_NAMES: tuple[str, ...] = tuple(str(t) for t in NOTIFICATION_TYPES)
