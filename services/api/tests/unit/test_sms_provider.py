"""Unit-тесты клиента SMS.ru (Task 9.4; AC-01, AC-07).

Сеть не используется: транспорт подменяется `httpx.MockTransport`, по образцу
`test_oauth_clients.py`. Реального `SMSRU_API_ID` в тестах нет и быть не должно.

Контракт SMS.ru (проверено `curl` 2026-10-21): HTTP всегда 200, а результат —
в поле `status`/`status_code` JSON-тела. HTTP-код о успехе не говорит.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.services import oauth
from app.services.sms import SmsProviderError
from app.services.sms.smsru import SmsRuProvider

PHONE = "+79001234567"
TEXT = "Карамель: код 123456. Никому не сообщайте."


def _install(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> None:
    """Подменяет общий HTTP-клиент транспортом с готовым обработчиком."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(oauth, "_client", client)


async def test_success_status_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01: `status=OK` (код 100) — сообщение принято провайдером."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(200, json={"status": "OK", "status_code": 100, "sms": {}}),
    )

    await SmsRuProvider().send_code(PHONE, TEXT)  # не бросает


async def test_request_shape_matches_provider_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01: форма запроса — `api_id`, `to` без `+`, `msg`, `json=1`.

    `to` без `+`: SMS.ru принимает номер в национальном формате, а `+7…`
    в поле `to` возвращает ошибку формата, которую легко принять за отказ
    провайдера и искать причину не там.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"status": "OK", "status_code": 100})

    _install(monkeypatch, handler)
    await SmsRuProvider().send_code(PHONE, TEXT)

    assert captured["to"] == "79001234567"
    assert captured["json"] == "1"
    assert captured["msg"] == TEXT
    assert "api_id" in captured


async def test_invalid_api_key_maps_to_our_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-07: код 200 → `sms_invalid_key`; `status_text` провайдера наружу не идёт."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={"status": "ERROR", "status_code": 200, "status_text": "Неправильный api_id"},
        ),
    )

    with pytest.raises(SmsProviderError) as exc_info:
        await SmsRuProvider().send_code(PHONE, TEXT)

    assert exc_info.value.error_code == "sms_invalid_key"
    # AC-07: текст провайдера в исключение не копируется — он может содержать api_id.
    assert "api_id" not in str(exc_info.value)
    assert "Неправильный" not in str(exc_info.value)


async def test_no_funds_maps_to_our_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Код 201 (нет средств) отличается от прочих ошибок: владельцу нужен сигнал."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(200, json={"status": "ERROR", "status_code": 201}),
    )

    with pytest.raises(SmsProviderError) as exc_info:
        await SmsRuProvider().send_code(PHONE, TEXT)

    assert exc_info.value.error_code == "sms_no_funds"


async def test_unknown_error_maps_to_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неизвестный код провайдера — общий `sms_provider_error`, не 500."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(200, json={"status": "ERROR", "status_code": 999}),
    )

    with pytest.raises(SmsProviderError) as exc_info:
        await SmsRuProvider().send_code(PHONE, TEXT)

    assert exc_info.value.error_code == "sms_provider_error"


async def test_non_json_body_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML-страница шлюза вместо JSON — понятный отказ, а не `ValueError`."""
    _install(monkeypatch, lambda _: httpx.Response(200, text="<html>502</html>"))

    with pytest.raises(SmsProviderError) as exc_info:
        await SmsRuProvider().send_code(PHONE, TEXT)

    assert exc_info.value.error_code == "sms_provider_error"


async def test_timeout_maps_to_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Таймаут — отдельный код: он не значит, что сообщение не ушло."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    _install(monkeypatch, handler)

    with pytest.raises(SmsProviderError) as exc_info:
        await SmsRuProvider().send_code(PHONE, TEXT)

    assert exc_info.value.stage == "provider"
    assert exc_info.value.error_code == "provider_timeout"


async def test_connect_error_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сеть недоступна — `provider_unreachable`, тот же машинный контракт."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    _install(monkeypatch, handler)

    with pytest.raises(SmsProviderError) as exc_info:
        await SmsRuProvider().send_code(PHONE, TEXT)

    assert exc_info.value.error_code == "provider_unreachable"


def test_provider_error_has_stage() -> None:
    """D-4: `stage` — часть машинного контракта лога (`request|verify|provider`)."""
    error = SmsProviderError(stage="provider", error_code="sms_provider_error")

    assert error.stage == "provider"
    assert error.error_code == "sms_provider_error"
