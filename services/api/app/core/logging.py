"""Структурированное логирование OAuth-событий (Task 9.1–9.3; AC-04, AC-07).

Принцип: логировать можно только то, что перечислено в параметрах
`log_oauth_event`. Хелпер **не принимает** `**kwargs` — иначе ПД утекают «сами»,
когда кто-то передаст `payload` или `email` «для отладки».

Стори 12.3 (AC-1, AC-2; ADR-18, NFR66): здесь же живёт РЕЕСТР чувствительных
полей `SENSITIVE_FIELDS` и процессор `RedactingSensitiveProcessor`, который
режет их по ИМЕНАМ для всех логгеров. Прежний комментарий обещал фильтр
геолокации «в стори 8.2» — фильтра не существовало (класс P-27), он создан
здесь.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import MutableMapping, Sequence
from typing import Any, Literal, cast

import structlog

from app.core.config import settings

LOG_LEVELS = {"development": logging.DEBUG, "test": logging.WARNING}

# AC-07: единственные содержательные поля OAuth-событий.
ALLOWED_LOG_FIELDS = frozenset(
    {"event", "provider", "stage", "error_code", "user_uuid", "duration_ms", "external_id"}
)

# Служебные поля structlog. Их нельзя вырезать: без уровня и времени лог
# непригоден для разбора инцидента, а низкоуровневые логгеры (uvicorn, sqlalchemy)
# теряют вообще все поля, если фильтр применён глобально.
SERVICE_LOG_FIELDS = frozenset({"level", "timestamp", "event", "logger", "exc_info", "stack_info"})

# Стори 12.3, AC-1 (ADR-18, NFR66): ЕДИНЫЙ реестр полей, которые не покидают
# сервер ни в read-ответе, ни в логе. Оба потребителя (фильтр логов и, через
# `COORDINATE_FIELDS`, обработчик ошибок валидации) читают отсюда, а не держат
# литерал на месте: иначе новое ПД-поле пришлось бы дописывать в двух местах,
# и одно из них рано или поздно отстало бы.
#
# Состав зафиксирован замером архитектуры (`architecture-v2-action-first.md:1062`)
# и эпиком 12: `lat`/`lng`/`accuracy` — координаты устройства,
# `coords`/`geo` — их контейнеры, `birth_date` — дата рождения (ADR-17).
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {"lat", "lng", "accuracy", "coords", "geo", "birth_date"}
)

# Стори 12.3, AC-2: подмножество для ДОМЕННОГО КОДА ошибки. НЕ равно реестру:
# `birth_date` — персональные данные (в лог не пишется), но это НЕ координата.
# Если подставить сюда `SENSITIVE_FIELDS`, ошибка валидации даты рождения
# получит код `invalid_coordinates`, и клиент по контракту §4 покажет
# «геолокация вернула мусор, повторите определение города» — вместо
# «исправьте дату». Мутационное доказательство — пара тестов Task 3.4.
COORDINATE_FIELDS: frozenset[str] = frozenset({"lat", "lng", "accuracy"})


def _is_oauth_logger(logger: Any) -> bool:
    """Фильтр применяется только к логгеру `oauth`.

    `structlog.configure` настраивает цепочку для всего процесса; глобальный
    whitelist вырезал бы поля у любого другого логгера — включая ошибки БД.
    """
    name = getattr(logger, "name", None)
    if isinstance(name, str):
        return name.startswith("oauth")
    return False


class RedactingOAuthLogger:
    """Процессор structlog: вырезает ПД из OAuth-событий, сохраняя служебные поля.

    Второй рубеж после строгой сигнатуры хелпера: даже если кто-то вызовет
    `structlog.get_logger("oauth")` напрямую и передаст лишнее, фильтр это уберёт.
    """

    def __call__(
        self, logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        if not _is_oauth_logger(logger):
            return event_dict
        return {
            k: v
            for k, v in event_dict.items()
            if k in ALLOWED_LOG_FIELDS or k in SERVICE_LOG_FIELDS
        }


# Task 2.2a стори 2.2 (AC-02, D-8): секреты хранилища попадают в `Settings`,
# а `Settings` целиком печатается при диагностике конфигурации (`repr(settings)`,
# `model_dump()`). `RedactingOAuthLogger` тут не помогает: он режет ПОЛЯ по
# allowlist и привязан к префиксу `oauth`, а утечка идёт через значение поля
# с обычным именем. Поэтому второй процессор — по значению.
_SECRET_FIELDS: tuple[str, ...] = (
    "jwt_secret",
    "sms_code_secret",
    "smsru_api_id",
    "s3_access_key",
    "s3_secret_key",
    # Стори 10.7, Task 1.3 (AC-01, D-10.7-4): токен бота уходит в URL запроса
    # `POST api.telegram.org/bot<token>/sendMessage`. Ошибка `httpx` печатает
    # URL целиком, поэтому без этой строки токен попадал бы в лог при первом
    # же сетевом сбое — то есть ровно тогда, когда логи читают. Chat ID сюда
    # НЕ входит: это идентификатор получателя, не секрет, и он нужен в
    # диагностике (какой чат не принял сообщение).
    "alert_telegram_bot_token",
)

# Замена короткая и не содержит исходного префикса: по ней видно, что значение
# скрыто осознанно, а не потеряно при форматировании.
REDACTED_PLACEHOLDER = "***"

# Короче этого значения заменять нечего: `""` встречается в любом тексте,
# и подстановка `***` вместо пустой строки испортила бы все логи целиком.
_MIN_SECRET_LENGTH = 4


def _secret_values() -> tuple[str, ...]:
    """Текущие значения секретов из `Settings`, пригодные для замены.

    Читается при каждом вызове, а не кэшируется: тесты подменяют `settings`
    через `monkeypatch`, и кэш оставил бы в редакции значения предыдущего
    теста — то есть проверял бы не тот объект.
    """
    values = (getattr(settings, name, "") for name in _SECRET_FIELDS)
    return tuple(v for v in values if isinstance(v, str) and len(v) >= _MIN_SECRET_LENGTH)


class RedactingSecretsProcessor:
    """Процессор structlog: вырезает значения секретов из любого события.

    Работает по значению, а не по имени поля: секрет утекает строкой внутри
    `repr(settings)`, сообщения исключения boto3 или текста диагностики, где
    имя поля может быть любым. Применяется ко ВСЕМ логгерам — в отличие от
    `RedactingOAuthLogger`, который намеренно ограничен префиксом `oauth`.

    Замена идёт по самым длинным значениям первыми: иначе короткий секрет,
    являющийся подстрокой длинного, разрезал бы длинный на части и оставил
    хвост в логе.
    """

    def __call__(
        self, _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        secrets = sorted(_secret_values(), key=len, reverse=True)
        if not secrets:
            return event_dict
        return {key: self._redact(value, secrets) for key, value in event_dict.items()}

    @classmethod
    def _redact(cls, value: Any, secrets: Sequence[str]) -> Any:
        """Строки и контейнеры строк; прочие типы остаются как есть.

        `repr` вложенных структур (`args` исключения, `context`) тоже содержит
        секрет, поэтому обходятся кортежи, списки и словари — но не глубже
        разумного: рекурсивный обход произвольного объекта дал бы циклы.
        """
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, REDACTED_PLACEHOLDER)
            return value
        if isinstance(value, tuple | list):
            items = [cls._redact(item, secrets) for item in value]
            if isinstance(value, list):
                return items
            # CR 12.3, H-1 (тот же класс, что в `RedactingSensitiveProcessor`):
            # `type(value)(iterable)` падал `TypeError` на tuple-подклассе —
            # `namedtuple`, `urllib.parse.ParseResult` из httpx. Исключение
            # внутри процессора ломает сам вызов логирования.
            if type(value) is tuple:
                return tuple(items)
            fields = getattr(value, "_fields", None)
            if fields is not None:
                return type(value)(*items)
            return tuple(items)
        if isinstance(value, dict):
            return {key: cls._redact(item, secrets) for key, item in value.items()}
        return value


class RedactingSensitiveProcessor:
    """Стори 12.3, AC-2: режет чувствительные поля ПО ИМЕНИ для всех логгеров.

    Отличие от `RedactingOAuthLogger` — намеренное. Тот работает по allowlist и
    привязан к префиксу `oauth`: глобальный allowlist вырезал бы поля у любого
    логгера, включая ошибки БД, и лог потерял бы диагностику. Здесь наоборот —
    точный denylist из шести имён (`SENSITIVE_FIELDS`): посторонние ключи
    (`event`, `level`, `user_uuid`, `query`) остаются нетронутыми, ложных
    срабатываний нет.

    Замена, а не удаление: `lat=***` показывает, что поле передавалось, и
    позволяет заметить сам факт передачи координат. Удаление ключа скрыло бы
    и значение, и событие.

    Регистр сравнивается как есть (`birth_date`, не `Birth-Date`) — нормализация
    ключей ломала бы `event_dict` и расходилась бы с именами полей схем.
    """

    #: Предел глубины обхода. Логи содержат структуры на 2–3 уровня; более
    #: глубокий обход на произвольном объекте дал бы циклы и съел время
    #: в момент инцидента, когда логи нужны быстрее всего.
    MAX_DEPTH = 6

    def __call__(
        self, _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        return cast(MutableMapping[str, Any], self._redact(event_dict, depth=0))

    @classmethod
    def _redact(cls, value: Any, *, depth: int) -> Any:
        """Ключи из реестра → `***`; контейнеры обходятся вглубь до `MAX_DEPTH`.

        Обход нужен потому, что координаты приходят не только плоским
        `lat=...`: ошибка httpx печатает `request={"params": {"lat": ...}}`,
        и без рекурсии значение уцелело бы внутри словаря.
        """
        # CR 12.3, H-2: за пределом глубины возвращалось СЫРОЕ поддерево —
        # то есть фильтр отказывал открыто (fail-open) ровно там, где структура
        # неожиданна. Контейнер глубже предела скрывается целиком: не зная, что
        # внутри, безопаснее не логировать ничего, чем логировать всё.
        if depth > cls.MAX_DEPTH:
            return REDACTED_PLACEHOLDER if isinstance(value, dict | tuple | list) else value
        if isinstance(value, dict):
            return {
                key: (
                    REDACTED_PLACEHOLDER
                    if isinstance(key, str) and key in SENSITIVE_FIELDS
                    else cls._redact(item, depth=depth + 1)
                )
                for key, item in value.items()
            }
        if isinstance(value, tuple | list):
            items = [cls._redact(item, depth=depth + 1) for item in value]
            if isinstance(value, list):
                return items
            # CR 12.3, H-1: `type(value)(iterable)` падал `TypeError` на любом
            # tuple-подклассе — `namedtuple`, `urllib.parse.ParseResult` (его
            # кладёт в лог httpx как `request.url`). Исключение внутри
            # процессора ломает САМ вызов логирования: вместо строки в журнале
            # получаем 500 в обработчике ошибок, то есть логи теряются ровно
            # в момент инцидента. Здесь кортеж собирается безопасно.
            if type(value) is tuple:
                return tuple(items)
            fields = getattr(value, "_fields", None)
            if fields is not None:
                # Поля namedtuple режутся по ИМЕНИ так же, как ключи словаря:
                # `Geo(lat=..., lng=...)` — тот же носитель координат.
                return type(value)(
                    *(
                        REDACTED_PLACEHOLDER
                        if isinstance(name, str) and name in SENSITIVE_FIELDS
                        else item
                        for name, item in zip(fields, items, strict=False)
                    )
                )
            return tuple(items)
        return value


def configure_logging() -> None:
    """Task 9.1: минимальная настройка structlog — JSON, уровень из `ENV`.

    JSON обязателен: логи собираются в контейнере и разбираются машиной,
    а не человеком. Консольный вывод с цветами в JSON-пайплайн не ложится.
    """
    level = LOG_LEVELS.get(settings.env, logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Task 2.2a: редакция секретов по значению — до JSONRenderer и
            # после всех процессоров, добавляющих поля. Позиция после
            # `RedactingOAuthLogger` не важна: тот режет имена, этот значения.
            RedactingSecretsProcessor(),
            # Стори 12.3, AC-2: редакция ПД по ИМЕНАМ полей (`SENSITIVE_FIELDS`),
            # для ВСЕХ логгеров. Стоит после `RedactingSecretsProcessor`:
            # секреты режутся по значению, ПД — по ключу, порядок между ними
            # не важен, но оба обязаны стоять до `JSONRenderer`.
            RedactingSensitiveProcessor(),
            RedactingOAuthLogger(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def external_id_hash(value: str) -> str:
    """Task 9.3 (AC-07): корреляция внешнего id без раскрытия самого id.

    SHA-256 без соли и секрета: значение нужно, чтобы сопоставить два лога
    одного пользователя, а не восстановить id. 12 hex-символов = 48 бит,
    коллизии при пилоте (сотни пользователей) исключены.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def log_oauth_event(
    event: str,
    *,
    provider: str,
    stage: Literal[
        "start",
        "callback",
        "token",
        "profile",
        "session",
        # Стори 1.4 (D-4): стадии телефонного входа. Имя логгера не меняется —
        # фильтр `RedactingOAuthLogger` привязан к префиксу `oauth`, и новый
        # логгер остался бы без очистки ПД.
        "request",
        "verify",
        "provider",
        # Стори 1.5: обновление сессии и выход. Отдельные стадии нужны, чтобы
        # в логе было видно, чем именно закончился запрос к `/refresh` —
        # истёкшим refresh или баном; по одному `event` это не различить.
        "refresh",
        "logout",
        # Стори 1.6: подтверждение 18+ и согласия на ПД. Отдельная стадия,
        # чтобы в журнале отличать отказ `consent_required` от отказа сессии.
        "consent",
    ],
    error_code: str | None = None,
    user_uuid: str | None = None,
    duration_ms: int | None = None,
    external_id: str | None = None,
) -> None:
    """Task 9.1–9.2 (AC-04, AC-07): единственная точка записи OAuth-событий.

    Параметры перечислены явно и `**kwargs` отсутствует намеренно: любой новый
    ключ требует правки этой сигнатуры, то есть осознанного решения, а не
    случайной утечки `email` через `extra=payload`.

    `external_id` принимается как **сырое** значение и хешируется здесь:
    вызывающий код не должен помнить про хеширование, иначе AC-07 не соблюдается.
    """
    structlog.get_logger("oauth").info(
        event,
        provider=provider,
        stage=stage,
        error_code=error_code,
        user_uuid=user_uuid,
        duration_ms=duration_ms,
        external_id=external_id_hash(external_id) if external_id else None,
    )
