"""AC-04, AC-07: процессор редактирования логов и корреляция внешнего id.

Тест прямой: `structlog.testing.capture_logs` подменяет цепочку процессоров,
поэтому интеграционный тест PII проверял сигнатуру хелпера, а не фильтр.
Здесь процессор вызывается напрямую с грязным `event_dict`.
"""

from typing import Any

from app.core.logging import RedactingOAuthLogger, external_id_hash


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
