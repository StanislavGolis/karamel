"""Клиент VK ID (Task 4, AC-02, AC-04).

Три отличия от Яндекса, зафиксированные в D2 и Task 4.2:
- обмен кода требует PKCE (`code_verifier`), приложение публичное;
- `client_secret` в обмене **не** передаётся;
- callback приносит `device_id`, без него обмен невозможен.

Проверено `curl` 2026-10-21: `id.vk.ru/authorize` с `code_challenge_method=S256`
отвечает 200, `oauth2/user_info` с фиктивным `client_id` —
`{"error":"invalid_request","error_description":"client_id is invalid"}`.
Имена полей профиля (`user.user_id`) подтверждаются при первом живом входе (Task 11.3).
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

AUTHORIZE_URL = "https://id.vk.ru/authorize"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"
PROFILE_URL = "https://id.vk.ru/oauth2/user_info"


class VkOAuthClient:
    """VK ID с PKCE. `client_secret` не передаётся ни в одном запросе."""

    provider = "vk"

    def build_authorize_url(self, state: str, code_challenge: str | None) -> str:
        """Task 4.1 (AC-02): обязательные `code_challenge` и `code_challenge_method=S256`.

        Без `code_challenge` VK ID вернёт ошибку авторизации, а не кода:
        для публичных клиентов PKCE — требование, а не опция.
        """
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.vk_client_id,
                "redirect_uri": _redirect_uri("vk"),
                "state": state,
                "code_challenge": code_challenge or "",
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self, code: str, *, device_id: str | None, code_verifier: str
    ) -> TokenResult:
        """Task 4.2: форма без `client_secret`, с `device_id` и `code_verifier`.

        Task 4.4: без `device_id` обмен не выполняется вообще — иначе получили бы
        невнятную ошибку провайдера вместо `invalid_callback`.
        """
        if not device_id:
            raise OAuthProviderError(stage="token", error_code="missing_device_id")
        try:
            response = await get_http_client().post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.vk_client_id,
                    "device_id": device_id,
                    "code_verifier": code_verifier,
                },
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
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
        """Task 4.3: `POST /oauth2/user_info` формой, `provider_user_id` из `user.user_id`."""
        try:
            response = await get_http_client().post(
                PROFILE_URL,
                data={"client_id": settings.vk_client_id, "access_token": access_token},
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
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) or user.get("user_id") is None:
            raise OAuthProviderError(stage="profile", error_code="no_provider_user_id")
        first_name = user.get("first_name")
        email = user.get("email")
        return ExternalProfile(
            provider="vk",
            provider_user_id=str(user["user_id"]),
            display_name=first_name if isinstance(first_name, str) else None,
            email=email if isinstance(email, str) else None,
        )
