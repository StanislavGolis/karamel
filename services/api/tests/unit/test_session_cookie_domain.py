"""Домен session-cookie `SESSION_COOKIE_DOMAIN` (Task 11.8; AC-01, D-11).

Закрывает Решение 3 код-ревью стори 1.3: cookie host-only по умолчанию
и явный переключатель, если web и API живут на поддоменах одного домена.
Проверяется нормализация (в `.env` копируют адрес целиком) и fail-fast
на кривом значении — иначе ошибка всплывает на первом выходе из аккаунта.
"""

from __future__ import annotations

import pytest
from fastapi import Response
from pydantic import ValidationError

from app.core.config import Settings
from app.services.session_service import IssuedSession, clear_session_cookies, set_session_cookies


def _settings(**overrides: str) -> Settings:
    """Настройки с валидными секретами — иначе валидаторы не пропустят."""
    base = {
        "jwt_secret": "a" * 32,
        "sms_code_secret": "b" * 32,
        "sms_provider": "stub",
        "env": "development",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


ISSUED = IssuedSession(access_token="access-value", refresh_token="refresh-value")


def test_empty_domain_means_host_only() -> None:
    """AC-01/D-11: пусто = host-only, это валидное состояние по умолчанию."""
    assert _settings().session_cookie_domain is None
    assert _settings(session_cookie_domain="").session_cookie_domain is None
    assert _settings(session_cookie_domain="   ").session_cookie_domain is None


def test_domain_is_normalized() -> None:
    """Task 6.2: схема, слэш, регистр и ведущая точка срезаются."""
    assert _settings(session_cookie_domain="https://Karamel.RU/").session_cookie_domain == (
        "example.com"
    )
    assert _settings(session_cookie_domain=".example.com").session_cookie_domain == "example.com"
    assert _settings(session_cookie_domain="example.com").session_cookie_domain == "example.com"


def test_localhost_is_allowed() -> None:
    """Локальная разработка: `localhost` — валидный host без точки."""
    assert _settings(session_cookie_domain="localhost").session_cookie_domain == "localhost"


def test_scheme_with_ip_is_normalized() -> None:
    """Task 6.2: адрес целиком — тоже домен, схема просто срезается.

    IP-адрес не ошибка: это ровно тот host, который окажется в `Host`
    запроса. Запрещать его значило бы ломать локальные стенды, где web
    и API живут на одном IP.
    """
    assert _settings(session_cookie_domain="http://127.0.0.1").session_cookie_domain == (
        "127.0.0.1"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com:3000",
        "example.com:3000",
        "user:pass@example.com",
        "notadomain",
        "https://",
    ],
)
def test_invalid_domain_fails_on_start(value: str) -> None:
    """Task 6.4: кривой домен падает на старте с именем переменной в тексте."""
    with pytest.raises(ValidationError) as exc:
        _settings(session_cookie_domain=value)

    assert "SESSION_COOKIE_DOMAIN" in str(exc.value)


def test_host_only_set_cookies_have_no_domain() -> None:
    """AC-01: без домена атрибут `Domain` не ставится вовсе."""
    response = Response()
    set_session_cookies(response, ISSUED)

    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    assert all("domain=" not in header.lower() for header in headers)
    # AC-01: HttpOnly и SameSite=Lax на обеих cookie.
    assert all("httponly" in header.lower() for header in headers)
    assert all("samesite=lax" in header.lower() for header in headers)


def test_clear_cookies_repeat_issue_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-8: удаление повторяет `Path`/`Domain`/`Secure`, иначе cookie остаётся.

    Cookie идентифицируется тройкой (имя, домен, путь): `delete_cookie`
    с другим доменом — это другая cookie, и старая продолжает отправляться.
    """
    from app.services import session_service

    monkeypatch.setattr(session_service.settings, "session_cookie_domain", "example.com")
    response = Response()
    clear_session_cookies(response)

    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    assert all("domain=example.com" in header.lower() for header in headers)
    # Max-Age=0 — признак удаления.
    assert all("max-age=0" in header.lower() for header in headers)
