"""Сервис «моих заявок»: подпись фото и разбор курсора (CR 5а.3; AC-06, AC-07).

§18.2: проверяется НАБЛЮДАЕМОЕ поведение, а не «функция вызвалась». Оба
теста — регрессии на дефекты, найденные код-ревью: молчаливый `500` на
экране при ненастроенном S3 и тихий сброс листания на пустом курсоре.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

import pytest

from app.services import join_request_service, media_service
from app.services.join_request_service import (
    _sign_author_photos,
    list_my_requests,
)
from app.services.place_cursor import InvalidCursorError


class _Settings:
    default_community_slug = "pilot"
    join_request_active_limit = 2
    s3_signed_url_ttl = 900


class _Repo:
    """Репозиторий-заглушка: курсор разбирается ДО первого обращения к нему."""

    async def list_my_requests(self, **_kwargs: Any) -> list[Any]:
        raise AssertionError("репозиторий не должен вызываться на битом курсоре")

    async def count_my_requests(self, **_kwargs: Any) -> int:
        raise AssertionError("репозиторий не должен вызываться на битом курсоре")


def test_sign_author_photos_survives_unconfigured_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-07 (CR 5а.3, патч P1): незаданные реквизиты S3 — не ошибка выдачи.

    Краснеет, если ловить только `MediaStorageError`: `MediaUnavailableError`
    НЕ его наследник, он уходил бы в общий обработчик → `500` на весь экран
    «Мои заявки», хотя заявка и карточка полезны без фото (NFR34).
    """

    def _boom(_storage_key: str, *, ttl: int | None = None) -> str:
        raise media_service.MediaUnavailableError("S3 credentials are not configured")

    monkeypatch.setattr(join_request_service, "get_signed_url", _boom)

    signed = _sign_author_photos({7: "photos/7/1.jpg"}, _Settings())

    assert signed == {}


def test_sign_author_photos_keeps_working_photos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-07: сбой подписи ОДНОГО автора не отменяет подпись остальных."""

    def _flaky(storage_key: str, *, ttl: int | None = None) -> str:
        if storage_key == "bad":
            raise media_service.MediaStorageError("AccessDenied")
        return f"https://s3/{storage_key}?X-Amz-Signature=sig"

    monkeypatch.setattr(join_request_service, "get_signed_url", _flaky)

    signed = _sign_author_photos({1: "bad", 2: "ok"}, _Settings())

    assert signed == {2: "https://s3/ok?X-Amz-Signature=sig"}


async def test_empty_cursor_is_invalid_not_first_page() -> None:
    """AC-06 (CR 5а.3, патч P4): `?cursor=` — `422`, а не первая страница.

    Краснеет, если вернуть truthiness-проверку `if cursor`: пустая строка
    молча давала бы первую страницу, тогда как `/places` отвечает на тот же
    ввод `422 invalid_cursor`. Два поведения на один дефект клиента —
    скрытая ошибка листания.
    """
    with pytest.raises(InvalidCursorError):
        await list_my_requests(
            session=None,  # type: ignore[arg-type]
            applicant_id=1,
            settings=_Settings(),
            cursor="",
            repository=_Repo(),  # type: ignore[arg-type]
        )


async def test_absent_cursor_is_first_page() -> None:
    """AC-06: `cursor=None` — законная первая страница (защита от переусердствования)."""
    repo = _Repo()
    with pytest.raises(AssertionError):
        await list_my_requests(
            session=None,  # type: ignore[arg-type]
            applicant_id=1,
            settings=_Settings(),
            cursor=None,
            repository=repo,  # type: ignore[arg-type]
        )


def test_encode_still_accepts_uuid_objects() -> None:
    """AC-06: кодек не сузился патчами — `uuid` остаётся `UUID`."""
    from datetime import UTC, datetime

    from app.services.join_request_cursor import (
        decode_my_requests_cursor,
        encode_my_requests_cursor,
    )

    value = uuid_lib.uuid4()
    decoded_at, decoded_uuid = decode_my_requests_cursor(
        encode_my_requests_cursor(datetime(2026, 10, 24, 3, 0, tzinfo=UTC), value)
    )
    assert decoded_uuid == value
    assert decoded_at.tzinfo is not None
