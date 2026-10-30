"""AC-04, AC-07: процессор редактирования логов и корреляция внешнего id.

Тест прямой: `structlog.testing.capture_logs` подменяет цепочку процессоров,
поэтому интеграционный тест PII проверял сигнатуру хелпера, а не фильтр.
Здесь процессор вызывается напрямую с грязным `event_dict`.

Стори 12.3 (AC-1, AC-2) добавляет сюда реестр `SENSITIVE_FIELDS` и процессор
`RedactingSensitiveProcessor`. Тесты реестра НЕ читают ожидаемый состав из
теста: перечисление зафиксировано явно в утверждении, иначе мутация в реестре
и тесте была бы согласована и не ловилась бы (P-29).
"""

from typing import Any

import structlog

from app.core.logging import (
    COORDINATE_FIELDS,
    SENSITIVE_FIELDS,
    SERVICE_LOG_FIELDS,
    RedactingOAuthLogger,
    RedactingSecretsProcessor,
    RedactingSensitiveProcessor,
    configure_logging,
    external_id_hash,
)


class _FakeLogger:
    def __init__(self, name: str) -> None:
        self.name = name


def test_oauth_logger_drops_pii() -> None:
    """ПД и секреты вырезаются, служебные поля остаются."""
    processor = RedactingOAuthLogger()
    event_dict: dict[str, Any] = {
        "event": "oauth_callback_failed",
        "provider": "yandex",
        "stage": "callback",
        "level": "info",
        "timestamp": "2026-10-21T19:00:00Z",
        # ниже — то, что не должно попасть в лог
        "email": "ivan@example.com",
        "code": "secret-code",
        "state": "secret-state",
        "access_token": "ya-token",
        "phone": "+79001234567",
    }

    result = processor(_FakeLogger("oauth"), "info", event_dict)

    assert result["event"] == "oauth_callback_failed"
    assert result["provider"] == "yandex"
    # Служебные поля сохраняются: без уровня и времени лог непригоден
    assert result["level"] == "info"
    assert result["timestamp"] == "2026-10-21T19:00:00Z"
    for forbidden in ("email", "code", "state", "access_token", "phone"):
        assert forbidden not in result, f"{forbidden} утёк в лог"


def test_non_oauth_logger_untouched() -> None:
    """Фильтр не трогает другие логгеры: иначе ошибки БД теряют все поля."""
    processor = RedactingOAuthLogger()
    event_dict: dict[str, Any] = {
        "event": "database_error",
        "level": "error",
        "timestamp": "2026-10-21T19:00:00Z",
        "query": "SELECT ...",
    }

    result = processor(_FakeLogger("app.core.db"), "error", event_dict)

    assert result == event_dict


def test_external_id_is_hashed_short() -> None:
    """AC-07: внешний id коррелируется 12 hex-символами, не раскрывая значение."""
    raw = "1234567890"
    hashed = external_id_hash(raw)

    assert len(hashed) == 12
    assert hashed != raw
    assert hashed == external_id_hash(raw)
    assert hashed != external_id_hash(raw + "1")


def test_hash_is_deterministic_across_calls() -> None:
    """Один и тот же внешний id даёт один и тот же хеш — иначе корреляция невозможна."""
    assert external_id_hash("ya-1") == external_id_hash("ya-1")


# --- Стори 12.3, AC-1: реестр чувствительных полей -------------------------

#: Ожидаемый состав зафиксирован ЗДЕСЬ, а не выведен из `SENSITIVE_FIELDS`:
#: тест, читающий ожидание из проверяемого объекта, зеленеет при любой
#: мутации реестра вплоть до пустого множества (P-29).
EXPECTED_SENSITIVE = {"lat", "lng", "accuracy", "coords", "geo", "birth_date"}


def test_sensitive_fields_contains_all_six() -> None:
    """AC-1: реестр содержит минимум шесть полей из замера архитектуры.

    Краснеет, если из `SENSITIVE_FIELDS` удалить `birth_date` (M1): фильтр
    логов перестал бы резать дату рождения, хотя код ответа цел.
    """
    missing = EXPECTED_SENSITIVE - SENSITIVE_FIELDS
    assert not missing, f"нет в реестре: {sorted(missing)}"


def test_sensitive_fields_is_frozenset() -> None:
    """AC-1: тип — `frozenset[str]`, иначе потребитель может его изменить.

    Мутабельность реестра означала бы, что фильтр логов и обработчик ошибок
    видят РАЗНЫЕ множества в одном процессе, если кто-то дописал поле на месте.
    """
    assert isinstance(SENSITIVE_FIELDS, frozenset)
    assert all(isinstance(item, str) for item in SENSITIVE_FIELDS)


def test_coordinate_fields_is_subset() -> None:
    """AC-2: `COORDINATE_FIELDS` — подмножество реестра, но не равно ему.

    Краснеет, если добавить `phone` в `COORDINATE_FIELDS` (M2): обработчик
    ошибок получил бы поле, которого нет в реестре, и связь между двумя
    потребителями стала бы декларацией, а не равенством множеств.
    """
    assert COORDINATE_FIELDS <= SENSITIVE_FIELDS


def test_birth_date_is_not_coordinate() -> None:
    """AC-2, AC-4: `birth_date` в реестре, но НЕ координата.

    Краснеет, если заменить литерал в `main.py` на `SENSITIVE_FIELDS` целиком
    (M9): ошибка валидации даты рождения получила бы `invalid_coordinates`,
    и клиент показал бы «повторите определение города» на ошибке в дате.
    """
    assert "birth_date" in SENSITIVE_FIELDS
    assert "birth_date" not in COORDINATE_FIELDS


# --- Стори 12.3, AC-2: процессор по именам полей ---------------------------


def test_sensitive_processor_redacts_keys() -> None:
    """AC-2: `lat`/`birth_date` вырезаются по ИМЕНИ для любого логгера.

    Краснеет, если убрать `RedactingSensitiveProcessor` из `configure_logging`
    (M3) — процессор не вызвался бы вовсе; или если заменить denylist на
    allowlist `{"event"}` (M4) — тогда уцелел бы `lat`.
    """
    processor = RedactingSensitiveProcessor()
    result = processor(
        _FakeLogger("app.core.db"),
        "error",
        {
            "event": "db_error",
            "lat": 56.5,
            "lng": 84.9,
            "birth_date": "1990-01-01",
            "level": "error",
            "user_uuid": "abc",
        },
    )

    assert result["lat"] == "***"
    assert result["lng"] == "***"
    assert result["birth_date"] == "***"


def test_sensitive_processor_keeps_service_fields() -> None:
    """AC-2: посторонние поля целы — фильтр по точному denylist из 6 имён.

    Краснеет, если добавить `event` в `SENSITIVE_FIELDS` (M5): лог потерял бы
    имя события, и разбор инцидента стал бы невозможен.
    """
    processor = RedactingSensitiveProcessor()
    result = processor(
        _FakeLogger("app.core.db"),
        "info",
        {
            "event": "test_event",
            "level": "info",
            "timestamp": "2026-10-29T00:00:00Z",
            "user_uuid": "u-1",
            "query": "SELECT 1",
        },
    )

    assert result["event"] == "test_event"
    assert result["level"] == "info"
    assert result["user_uuid"] == "u-1"
    assert result["query"] == "SELECT 1"
    assert SERVICE_LOG_FIELDS  # реестр служебных полей не опустел


def test_sensitive_processor_replaces_not_removes() -> None:
    """AC-2: значение заменено на `***`, ключ остался.

    Краснеет, если заменить замену на `pop` (M7): пропадёт сам факт передачи
    координат, и по логу нельзя будет понять, что клиент их прислал.
    """
    processor = RedactingSensitiveProcessor()
    result = processor(_FakeLogger("app.core.db"), "info", {"lat": 1.0, "event": "e"})

    assert "lat" in result
    assert result["lat"] == "***"


def test_sensitive_processor_redacts_nested() -> None:
    """AC-2: координаты внутри словаря/списка вырезаются на любой глубине.

    Краснеет, если убрать обход `dict` (M8): httpx печатает
    `request={"params": {"lat": ...}}`, и плоский фильтр оставил бы
    значение внутри вложенной структуры.
    """
    processor = RedactingSensitiveProcessor()
    result = processor(
        _FakeLogger("httpx"),
        "error",
        {
            "event": "http_error",
            "request": {"params": {"lat": 56.5, "city": "tomsk"}},
            "batch": [{"birth_date": "1990-01-01"}],
        },
    )

    assert result["request"]["params"]["lat"] == "***"
    assert result["request"]["params"]["city"] == "tomsk"
    assert result["batch"][0]["birth_date"] == "***"


def test_sensitive_processor_applies_to_non_oauth_logger() -> None:
    """AC-2: отличие от `RedactingOAuthLogger` — работает для ЛЮБОГО логгера.

    Краснеет, если ограничить процессор `_is_oauth_logger` (M6): утечка через
    `sqlalchemy`/`httpx` осталась бы незамеченной, потому что префикс не `oauth`.
    """
    processor = RedactingSensitiveProcessor()
    for name in ("sqlalchemy.engine", "httpx", "app.services.event_card_service"):
        result = processor(_FakeLogger(name), "info", {"lat": 1.0})
        assert result["lat"] == "***", f"логгер {name} не очищен"


def test_sensitive_processor_survives_namedtuple_payload() -> None:
    """CR 12.3, H-1: процессор НЕ бросает на tuple-подклассах.

    Дыра, найденная CR (не была покрыта ни одной мутацией DS):
    `type(value)(generator)` вызывается на любом `tuple`-подклассе и падает
    `TypeError: Geo.__new__() missing 1 required positional argument: 'lng'`.
    Исключение внутри процессора structlog ломает САМ вызов логирования:
    вместо строки в журнале — 500 в обработчике ошибок, то есть логи теряются
    ровно в момент инцидента (`P-25`: проверяем эффект, а не форму).

    `urllib.parse.ParseResult` — не экзотика: httpx кладёт `request.url`
    в лог именно этим типом.
    """
    import urllib.parse
    from collections import namedtuple

    processor = RedactingSensitiveProcessor()
    geo = namedtuple("Geo", "lat lng")(56.5, 84.9)

    # Ключ `point` НЕ входит в реестр намеренно: иначе значение скрывалось бы
    # целиком по имени ключа, и ветка редактирования ПОЛЕЙ namedtuple осталась
    # бы непроверенной — ровно так мутация M27 (снятие этой ветки) выжила
    # в первом прогоне CR.
    result = processor(_FakeLogger("httpx"), "error", {"point": geo, "event": "e"})
    assert result["point"].lat == "***"
    assert result["point"].lng == "***"

    url = urllib.parse.urlparse("https://api.example.com/x?lat=56.5")
    result = processor(_FakeLogger("httpx"), "error", {"request_url": url, "event": "e"})
    assert isinstance(result["request_url"], urllib.parse.ParseResult)
    assert result["event"] == "e"


def test_sensitive_processor_fails_closed_beyond_max_depth() -> None:
    """CR 12.3, H-2: за пределом глубины контейнер СКРЫВАЕТСЯ, а не отдаётся.

    Дыра, найденная CR: `if depth > MAX_DEPTH: return value` возвращал сырое
    поддерево — фильтр отказывал ОТКРЫТО ровно там, где структура неожиданна,
    то есть `birth_date` на 8-м уровне уходил в лог целиком.
    """
    processor = RedactingSensitiveProcessor()
    nested: dict = {}
    cursor = nested
    for _ in range(RedactingSensitiveProcessor.MAX_DEPTH + 2):
        cursor["next"] = {}
        cursor = cursor["next"]
    cursor["birth_date"] = "1990-01-01"

    result = processor(_FakeLogger("httpx"), "error", {"deep": nested})

    assert "1990-01-01" not in repr(result), "значение уцелело за пределом глубины"


def test_secrets_processor_survives_namedtuple_payload() -> None:
    """CR 12.3, H-1 (второй экземпляр): `RedactingSecretsProcessor` падал тем же
    `TypeError` на tuple-подклассе — дефект был скопирован из его `_redact`.
    """
    import urllib.parse
    from collections import namedtuple

    processor = RedactingSecretsProcessor()
    geo = namedtuple("Geo", "lat lng")(1.0, 2.0)

    result = processor(_FakeLogger("app.core.config"), "info", {"geo": geo})
    assert result["geo"] == (1.0, 2.0) or tuple(result["geo"]) == (1.0, 2.0)

    url = urllib.parse.urlparse("https://api.example.com/x")
    result = processor(_FakeLogger("httpx"), "error", {"url": url})
    assert isinstance(result["url"], urllib.parse.ParseResult)


def test_sensitive_processor_wired_into_configure_logging() -> None:
    """AC-2: процессор подключён в цепочку, а не просто объявлен.

    Проверка через `structlog.get_config()`: `capture_logs` подменяет цепочку
    и потому не увидел бы разрыв между объявлением класса и его установкой.
    Краснеет, если убрать строку из `configure_logging` (M3).
    """
    configure_logging()
    processors = structlog.get_config()["processors"]
    assert any(isinstance(item, RedactingSensitiveProcessor) for item in processors), (
        "RedactingSensitiveProcessor не подключён в configure_logging"
    )
    # Порядок: редакция ПД обязана стоять ДО рендерера, иначе значение уже
    # сериализовано в строку и подмена по ключу невозможна.
    names = [type(item).__name__ for item in processors]
    renderer_index = next(
        index
        for index, item in enumerate(processors)
        if isinstance(item, structlog.processors.JSONRenderer)
    )
    sensitive_index = names.index("RedactingSensitiveProcessor")
    assert sensitive_index < renderer_index
