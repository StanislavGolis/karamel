"""Клиент Яндекс ID (Task 3, AC-01, AC-04).

Проверено `curl` 2026-10-21: `oauth.yandex.ru/authorize` отвечает 302 на passport,
`oauth.yandex.ru/token` возвращает плоский JSON `{error, error_description}`,
`login.yandex.ru/info?format=json` без токена — 401.
Имена полей профиля (`id`, `display_name`, `default_email`) взяты из документации
и подтверждаются при первом живом входе (Task 11.3).
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.oauth import (
    HTTP_TIMEOUT,
    ExternalProfile,
    OAuthProviderError,
    TokenResult,
    _json_or_none,
    _oauth_error_code,
    _redirect_uri,
    _transport_error_code,
    get_http_client,
)

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
PROFILE_URL = "https://login.yandex.ru/info"


class YandexOAuthClient:
    """OAuth 2.0 Яндекс ID. PKCE не требуется: приложение конфиденциальное."""

    provider = "yandex"

    def build_authorize_url(self, state: str, code_challenge: str | None) -> str:
        """Task 3.1 (AC-01): `code_challenge` игнорируется — Яндекс его не требует (D2)."""
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.yandex_client_id,
                "redirect_uri": _redirect_uri("yandex"),
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self, code: str, *, device_id: str | None, code_verifier: str
    ) -> TokenResult:
        """Task 3.2: `POST /token` формой. `device_id`/`code_verifier` не передаются."""
        try:
            response = await get_http_client().post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.yandex_client_id,
                    "client_secret": settings.yandex_client_secret,
                },
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:  # таймауты, обрыв, TLS
            raise OAuthProviderError(stage="token", error_code=_transport_error_code(exc)) from exc
        payload = _json_or_none(response)
        if not 200 <= response.status_code < 300:
            raise OAuthProviderError(
                stage="token", error_code=_oauth_error_code(payload, "http_error")
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            raise OAuthProviderError(stage="token", error_code="no_access_token")
        return TokenResult(access_token=payload["access_token"])

    async def fetch_profile(self, access_token: str) -> ExternalProfile:
        """Task 3.3: `GET /info?format=json` с Bearer. Внешний id приводится к строке."""
        try:
            response = await get_http_client().get(
                PROFILE_URL,
                params={"format": "json"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise OAuthProviderError(
                stage="profile", error_code=_transport_error_code(exc)
            ) from exc
        payload = _json_or_none(response)
        if not 200 <= response.status_code < 300:
            raise OAuthProviderError(
                stage="profile", error_code=_oauth_error_code(payload, "http_error")
            )
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise OAuthProviderError(stage="profile", error_code="no_provider_user_id")
        raw_id = payload["id"]
        display_name = payload.get("display_name")
        email = payload.get("default_email")
        return ExternalProfile(
            provider="yandex",
            provider_user_id=str(raw_id),
            display_name=display_name if isinstance(display_name, str) else None,
            email=email if isinstance(email, str) else None,
        )
