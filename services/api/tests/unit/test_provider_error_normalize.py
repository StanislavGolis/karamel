"""Allowlist кодов провайдера в логе (патч 5 код-ревью 1.6; CR 1.5 патч 21).

Этот дефект пережил **две** правки: обе версии функции выглядели
исправленными, потому что в них был виден allowlist и regex. Фактически
allowlist не влиял ни на что — значение вне allowlist всё равно
возвращалось, если проходило regex. Тест написан так, чтобы падать
именно на этом: он сравнивает результат для кода ВНЕ allowlist
с `unknown`, а не просто проверяет, что функция не падает.
"""

from __future__ import annotations

import pytest

from app.api.v1.auth import _normalize_provider_error


@pytest.mark.parametrize(
    "raw",
    [
        "access_denied",
        "invalid_request",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
    ],
)
def test_allowlisted_code_is_preserved(raw: str) -> None:
    """Известный код провайдера остаётся собой: он нужен в журнале как есть."""
    assert _normalize_provider_error(raw) == raw


def test_access_denied_specifically_not_unknown() -> None:
    """Патч 5: `access_denied` — самый частый отказ (пользователь нажал «Отмена»).

    Именно он проверялся руками и именно он терялся: в логе оказывался
    `unknown`, и отличить отказ пользователя от сбоя провайдера было нельзя.
    Без allowlist-условия этот тест падает — первая же строка возвращает
    `access_denied` как «валидное по форме», и проверка на `unknown` краснеет.
    """
    assert _normalize_provider_error("access_denied") != "unknown"


@pytest.mark.parametrize(
    "raw",
    [
        "some_random_thing",
        "unknown_code",
        "not_in_allowlist",
        "access_denied_but_longer",
        "ACCESS_DENIED_EXTRA",
    ],
)
def test_code_outside_allowlist_becomes_unknown(raw: str) -> None:
    """Патч 5: значение вне allowlist сводится к `unknown`, даже если его форма валидна.

    Прежняя реализация возвращала такое значение как есть: `?error=` управляется
    клиентом, поэтому в журнал попадал произвольный текст до 64 символов.
    """
    assert _normalize_provider_error(raw) == "unknown"


@pytest.mark.parametrize("raw", ["", "   ", "<script>", "код с пробелами", "a" * 100])
def test_garbage_becomes_unknown(raw: str) -> None:
    """Мусор (пусто, спецсимволы, длиннее 64 символов) — `unknown`."""
    assert _normalize_provider_error(raw) == "unknown"


def test_case_and_whitespace_normalized() -> None:
    """Регистр и пробелы нормализуются до сравнения с allowlist."""
    assert _normalize_provider_error("  Access_Denied  ") == "access_denied"
