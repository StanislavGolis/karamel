"""`cache-control: no-store` ставит САМ маршрут, а не middleware (стори 13.1, AC-5).

**Почему не интеграционный тест.** `NoStoreMiddleware` (`app/core/no_store.py:26`)
дописывает `cache-control: no-store` КАЖДОМУ ответу приложения. Значит
интеграционная проверка этого заголовка зеленеет и в том случае, если строка
`headers=_NO_STORE` из маршрута удалена целиком: заголовок придёт от
middleware. Мутация M12 (убрать `_NO_STORE` из успешного ответа) выжила
именно так — тест проверял заголовок, но не проверял ЕГО ИСТОЧНИК.

**Почему источник важен.** Ответ `/address/search` содержит `lat`/`lng`.
Middleware можно отключить на отдельном стенде, обернуть приложение иначе
или убрать из цепочки при рефакторинге — и тогда единственной защитой
останется строка в маршруте. Если её нет, координаты осядут в кэше прокси
(`NFR34`: координата окажется ХРАНИМА), и ни один тест не покраснеет.

Здесь вызывается сама функция-эндпоинт, без приложения и без middleware.
"""

from __future__ import annotations

import pytest

import app.api.v1.address as address_module
from app.api.v1.address import search_addresses_endpoint
from app.core.config import settings
from app.models.enums import PlaceCategory
from app.services.address_geocoder import AddressSuggestion


class _User:
    """Минимум `CurrentUser` для вызова функции: нужен только `city_slug`."""

    id = 1
    city_slug = "tomsk"


class _Limiter:
    """Лимитер без Redis: окно не исчерпано."""

    async def hit(self, user_id: int) -> int | None:
        return None


async def test_success_response_sets_no_store_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-5: успешный ответ несёт `no-store` из маршрута, а не из middleware.

    Краснеет при удалении `headers=_NO_STORE` из `JSONResponse` (мутация M12):
    заголовок не появится, потому что middleware в этом вызове не участвует.
    """

    async def _fake_search(query: str, *, city_slug: str) -> list[AddressSuggestion]:
        return [
            AddressSuggestion(
                name="Кинотеатр «Прага»",
                address_text="ул. Ленина, 12",
                display_name="Кинотеатр «Прага», улица Ленина, 12, Томск, Россия",
                category_hint=PlaceCategory.CINEMA,
                lat=56.4977,
                lng=84.9481,
            )
        ]

    monkeypatch.setattr(address_module, "search_addresses", _fake_search)

    response = await search_addresses_endpoint(
        user=_User(),  # type: ignore[arg-type]
        limiter=_Limiter(),  # type: ignore[arg-type]
        q="Прага",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    # Позитив: координаты в теле есть — иначе тест зеленел бы на пустом ответе.
    assert b"56.4977" in response.body


async def test_short_query_response_sets_no_store_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: `422 QUERY_TOO_SHORT` тоже `no-store` — пара к успешному ответу.

    Ошибка не содержит координат, но она содержит текст запроса. Кэш прокси
    сохранил бы введённый адрес, а `NFR34` запрещает хранение геоданных.
    """
    from app.services.address_geocoder import AddressQueryTooShortError

    async def _raise(query: str, *, city_slug: str) -> list[AddressSuggestion]:
        raise AddressQueryTooShortError

    monkeypatch.setattr(address_module, "search_addresses", _raise)

    response = await search_addresses_endpoint(
        user=_User(),  # type: ignore[arg-type]
        limiter=_Limiter(),  # type: ignore[arg-type]
        q="a",
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert b"QUERY_TOO_SHORT" in response.body


async def test_zero_retry_after_falls_back_to_window() -> None:
    """C16 CR 2026-10-31: `retry_after <= 0` заменяется окном настройки.

    Краснеет при `retry_after < 0`: ноль прошёл бы насквозь, и клиент получил
    бы `Retry-After: 0` — приказ повторить НЕМЕДЛЕННО (урок CR 1.4, патч 16).
    Клиент крутился бы в цикле, сжигая собственный лимит.
    """
    from app.api.v1.address import _rate_limited

    response = _rate_limited(0)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(settings.address_search_rate_window_seconds)
    assert b'"retry_after"' in response.body
