"""Клиенты OAuth-провайдеров (Task 3, 4).

Пакет изолирует HTTP-детали провайдеров: `auth_service` и роуты работают только
с `ExternalProfile` и `OAuthProviderError` и не знают, что внутри httpx.
Добавление SMS-провайдера в стори 1.4 не потребует правок `auth_service`.

D8: наружу отдаются только наши коды ошибок. `error_description` провайдера
может содержать `client_id` и `redirect_uri`, поэтому в ответ он не попадает.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.core.config import settings

ProviderName = Literal["yandex", "vk"]

# Task 3.4: единый таймаут на все внешние вызовы. Без него запрос к зависшему
# провайдеру держит воркер до таймаута прокси и превращается в очередь.
HTTP_TIMEOUT = 10.0

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Task 3.4: общий `AsyncClient`, а не новый на каждый запрос.

    Клиент живёт процесса: переиспользует пул соединений и TLS-сессии,
    иначе каждый вход платит полный handshake до провайдера.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    return _client


async def dispose_http_client() -> None:
    """Закрывается в lifespan рядом с пулом БД."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


@dataclass(frozen=True, slots=True)
class ExternalProfile:
    """Профиль от провайдера. `provider_user_id` — **строка** (AC-03).

    `email`/`display_name` не пишутся в БД и не логируются (AC-07); они здесь
    только чтобы аудит и будущие стори не ходили в провайдера повторно.
    """

    provider: str
    provider_user_id: str
    display_name: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class TokenResult:
    """Результат обмена кода. `access_token` в логи не попадает никогда."""

    access_token: str


def _transport_error_code(exc: Exception) -> str:
    """Фиксированные коды сетевых отказов вместо имён классов исключений.

    `type(exc).__name__` уносил в лог произвольные имена (`ReadTimeout`,
    `ConnectError`, `SSLError`, ...), из-за чего `error_code` переставал быть
    машинным контрактом.
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "provider_unreachable"
    return "provider_transport_error"


class OAuthProviderError(Exception):
    """Ошибка обмена или профиля. `stage` — единственное, что уходит в лог.

    `error_code` — машинный код провайдера (`invalid_grant`, `invalid_client`),
    а не `error_description`: текст может содержать реквизиты приложения.
    """

    def __init__(
        self,
        *,
        stage: Literal["callback", "token", "profile"],
        error_code: str,
        external_id: str | None = None,
    ) -> None:
        super().__init__(f"oauth provider error at {stage}: {error_code}")
        self.stage = stage
        self.error_code = error_code
        # Внешний id нужен для корреляции в логе (хешируется в log_oauth_event),
        # но в текст исключения не попадает: текст может уйти наружу.
        self.external_id = external_id


class OAuthClient(Protocol):
    """Контракт провайдера. Роут работает через него, не через конкретный модуль."""

    provider: str

    def build_authorize_url(self, state: str, code_challenge: str | None) -> str: ...

    async def exchange_code(
        self, code: str, *, device_id: str | None, code_verifier: str
    ) -> TokenResult: ...

    async def fetch_profile(self, access_token: str) -> ExternalProfile: ...


def _json_or_none(response: httpx.Response) -> object:
    """Тело ответа как JSON или None.

    Провайдеры отвечают JSON и на ошибки, но это не контракт: HTML-страница
    ошибки шлюза уронила бы обработчик вместо понятного `oauth_failed`.
    """
    try:
        return response.json()
    except ValueError:
        return None


def _oauth_error_code(payload: object, fallback: str) -> str:
    """Достаёт машинный `error` из тела ответа, не копируя `error_description`."""
    if isinstance(payload, dict):
        raw = payload.get("error")
        if isinstance(raw, str) and raw:
            return raw
    return fallback


def _redirect_uri(provider: ProviderName) -> str:
    """Task 11.1: `redirect_uri` = `{API_URL}/api/v1/auth/{provider}/callback`.

    Собирается из `API_URL`, а не из заголовка Host: заголовок подделывается
    клиентом, и тогда код уехал бы на чужой адрес.
    """
    return f"{settings.api_url.rstrip('/')}/api/v1/auth/{provider}/callback"


def get_client(provider: ProviderName) -> OAuthClient:
    """Фабрика провайдеров. Импорт внутри: модули тянут этот пакет за типами."""
    from app.services.oauth.vk import VkOAuthClient
    from app.services.oauth.yandex import YandexOAuthClient

    if provider == "yandex":
        return YandexOAuthClient()
    return VkOAuthClient()
