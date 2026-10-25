"""Unit-тесты HTTP-клиентов провайдеров (Task 10.2; AC-01, AC-02, AC-04).

D-стандарт стори: unit-тесты не ходят в сеть. Провайдеры подменяются
`httpx.MockTransport` — иначе тесты флакуют от доступности VK/Яндекса
и молча пропускают ошибки, когда провайдер лежит.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.services import oauth
from app.services.oauth import OAuthProviderError
from app.services.oauth.vk import VkOAuthClient
from app.services.oauth.yandex import YandexOAuthClient


def _install(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> None:
    """Подменяет общий клиент на транспорт с готовым обработчиком."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(oauth, "_client", client)


# --- Яндекс ---


async def test_yandex_exchange_code_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01: успешный обмен отдаёт `access_token`."""
    _install(monkeypatch, lambda _: httpx.Response(200, json={"access_token": "ya-token"}))

    result = await YandexOAuthClient().exchange_code(
        "the-code", device_id=None, code_verifier="ignored"
    )

    assert result.access_token == "ya-token"


async def test_yandex_exchange_code_sends_client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3.2: Яндекс — конфиденциальный клиент, `client_secret` передаётся."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "t"})

    _install(monkeypatch, handler)
    await YandexOAuthClient().exchange_code("c", device_id=None, code_verifier="v")

    assert "client_secret" in captured
    assert captured["grant_type"] == "authorization_code"


async def test_yandex_exchange_code_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04: `invalid_grant` превращается в `OAuthProviderError` со стадией `token`."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(400, json={"error": "invalid_grant", "error_description": "x"}),
    )

    with pytest.raises(OAuthProviderError) as exc:
        await YandexOAuthClient().exchange_code("c", device_id=None, code_verifier="v")

    assert exc.value.stage == "token"
    assert exc.value.error_code == "invalid_grant"


async def test_yandex_timeout_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04: таймаут не выходит наружу как `httpx.HTTPError` — только наш тип."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    _install(monkeypatch, handler)

    with pytest.raises(OAuthProviderError) as exc:
        await YandexOAuthClient().exchange_code("c", device_id=None, code_verifier="v")

    assert exc.value.stage == "token"


async def test_yandex_profile_maps_id_to_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03: `id` приходит числом, но в `provider_user_id` попадает строкой."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(
            200, json={"id": 12345, "display_name": "Тест", "default_email": "a@b.c"}
        ),
    )

    profile = await YandexOAuthClient().fetch_profile("token")

    assert profile.provider_user_id == "12345"
    assert profile.provider == "yandex"


# --- VK ---


async def test_vk_exchange_code_omits_client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 4.2: VK — публичный клиент, `client_secret` уходить не должен.

    Утечка секрета в запросе к публичному клиенту — это раскрытие ключа,
    которое провайдер не заблокирует: запрос всё равно успешен.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "vk-token"})

    _install(monkeypatch, handler)
    await VkOAuthClient().exchange_code("c", device_id="dev-1", code_verifier="verifier")

    assert "client_secret" not in captured
    assert captured["device_id"] == "dev-1"
    assert captured["code_verifier"] == "verifier"


async def test_vk_exchange_code_without_device_id_fails_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 4.4: без `device_id` обмен не выполняется — сеть не трогаем."""
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    _install(monkeypatch, handler)

    with pytest.raises(OAuthProviderError) as exc:
        await VkOAuthClient().exchange_code("c", device_id=None, code_verifier="v")

    assert exc.value.error_code == "missing_device_id"
    assert called is False


async def test_vk_profile_without_user_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04: ответ без `user` — ошибка стадии `profile`, а не пустой профиль."""
    _install(monkeypatch, lambda _: httpx.Response(200, json={"error": "invalid_request"}))

    with pytest.raises(OAuthProviderError) as exc:
        await VkOAuthClient().fetch_profile("token")

    assert exc.value.stage == "profile"
    assert exc.value.error_code == "no_provider_user_id"


async def test_vk_profile_maps_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-02: `user.user_id` приводится к строке."""
    _install(
        monkeypatch,
        lambda _: httpx.Response(200, json={"user": {"user_id": 777, "first_name": "И"}}),
    )

    profile = await VkOAuthClient().fetch_profile("token")

    assert profile.provider_user_id == "777"
    assert profile.provider == "vk"


# --- authorize URL ---


def test_vk_authorize_url_has_pkce() -> None:
    """AC-02: в URL VK обязательны `code_challenge` и `code_challenge_method=S256`."""
    url = VkOAuthClient().build_authorize_url("state-1", "challenge-1")

    assert "code_challenge=challenge-1" in url
    assert "code_challenge_method=S256" in url
    assert url.startswith("https://id.vk.ru/authorize?")


def test_yandex_authorize_url_has_no_pkce() -> None:
    """D2/AC-01: Яндекс PKCE не требует — параметров быть не должно."""
    url = YandexOAuthClient().build_authorize_url("state-1", "challenge-1")

    assert "code_challenge" not in url
    assert "state=state-1" in url
    assert url.startswith("https://oauth.yandex.ru/authorize?")
