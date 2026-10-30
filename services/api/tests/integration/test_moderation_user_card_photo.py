"""Фото в карточке модератора (стори 12.10; AC-1, AC-9, AC-10).

**Главное утверждение:** модератор видит подписанный URL главного
одобренного фото, и только его. `pending`/`rejected` не подписываются:
модератор не видит того, что продукт не показал другим (NFR36).

**Почему подпись подменяется.** `get_signed_url` в тестовой среде без
реквизитов S3 бросил бы `MediaUnavailableError`, и `photo_url = null`
получился бы по ЛОЖНОЙ причине — тест зеленел бы на любой мутации
`status='approved'` в SQL. Подмена возвращает детерминированную строку,
содержащую `storage_key`, и тогда «подписан только approved» проверяется
по значению, а не по `null`.

**Почему `U1`–`U4` живут здесь.** Это четыре разных утверждения одного
контракта: поле есть, фильтр по `approved`, порядок `position`, пара
«ссылка + срок». Один файл — одна ось.
"""

from __future__ import annotations

import json
import os

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import media_service, moderation_user_card_service
from tests.integration.moderation_user_card_helpers import (
    build_client,
    get_card,
    make_moderator,
    make_target_user,
    set_profile,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _profile_id(session: AsyncSession, *, user_id: int) -> int:
    """`profiles.id` по `users.id` — карточка отдаёт наружу только `uuid`."""
    result = await session.execute(
        text("SELECT id FROM profiles WHERE user_id = :u"), {"u": user_id}
    )
    return int(result.scalar_one())


async def _add_photo(
    session: AsyncSession,
    *,
    profile_id: int,
    storage_key: str,
    position: int,
    status: str = "approved",
) -> None:
    """Фото с заданным статусом и позицией.

    `ck_profile_photos_status_consistency` требует: `pending` — без
    `moderated_at` и без причины; `approved` — с `moderated_at`, без причины;
    `rejected` — и с `moderated_at`, и с причиной.
    """
    moderated_at = "NULL" if status == "pending" else "now()"
    rejection_reason = "'не по правилам'" if status == "rejected" else "NULL"
    await session.execute(
        text(
            "INSERT INTO profile_photos (profile_id, storage_key, position, status, "
            "content_type, size_bytes, consent_given_at, moderated_at, rejection_reason) "
            f"VALUES (:p, :k, :pos, :st, 'image/jpeg', 1000, now(), "
            f"{moderated_at}, {rejection_reason})"
        ),
        {"p": profile_id, "k": storage_key, "pos": position, "st": status},
    )
    await session.flush()


async def _with_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подмена подписи: возвращает ключ в виде URL.

    Подменяются ОБА имени: сервис импортирует `get_signed_url` по имени
    (приём тестов 12.7), и подмена только модуля `media_service` оставила бы
    локальную ссылку нетронутой.
    """

    def _sign(storage_key: str, *, ttl: int | None = None) -> str:
        expires = 900 if ttl is None else ttl
        return f"https://storage.test/{storage_key}?expires={expires}"

    monkeypatch.setattr(media_service, "get_signed_url", _sign)
    monkeypatch.setattr(moderation_user_card_service, "get_signed_url", _sign)


async def test_approved_photo_is_signed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1, AC-9: у одобренного фото появляется подписанный URL и срок."""
    await _with_signer(monkeypatch)
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    profile_id = await _profile_id(session, user_id=target.id)
    await _add_photo(session, profile_id=profile_id, storage_key="photos/a/1.jpg", position=1)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    profile = body["profile"]
    assert profile["photo_url"] == "https://storage.test/photos/a/1.jpg?expires=900"
    assert profile["photo_expires_in"] == 900


async def test_pending_and_rejected_are_not_signed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9: `pending`/`rejected` не подписываются — фото нет вовсе."""
    await _with_signer(monkeypatch)
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    profile_id = await _profile_id(session, user_id=target.id)
    await _add_photo(
        session, profile_id=profile_id, storage_key="photos/b/1.jpg", position=1, status="pending"
    )
    await _add_photo(
        session, profile_id=profile_id, storage_key="photos/b/2.jpg", position=2, status="rejected"
    )
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        profile = response.json()["profile"]

    assert profile["photo_url"] is None
    assert profile["photo_expires_in"] is None


async def test_lowest_position_wins(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-9: главное фото — минимальная `position`, а не любое одобренное.

    **Порядок ключей ПРОТИВОПОЛОЖЕН порядку позиций** (CR H-3). Прежняя
    фикстура (`first.jpg` на `position=1`) давала одинаковый результат при
    сортировке по `position` и по `storage_key`, поэтому мутация
    `ORDER BY ph.position` → `ORDER BY ph.storage_key` проходила зелёной.
    Здесь `position=1` — это `zebra.jpg`, а `alpha.jpg` лежит на `position=2`:
    сортировка по ключу выбрала бы `alpha.jpg` и покраснела.
    """
    await _with_signer(monkeypatch)
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    profile_id = await _profile_id(session, user_id=target.id)
    await _add_photo(session, profile_id=profile_id, storage_key="photos/c/alpha.jpg", position=2)
    await _add_photo(session, profile_id=profile_id, storage_key="photos/c/zebra.jpg", position=1)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        profile = response.json()["profile"]

    assert profile["photo_url"] == "https://storage.test/photos/c/zebra.jpg?expires=900"


async def test_profile_without_photo_gets_200(session: AsyncSession) -> None:
    """AC-14 8.5: профиль без фото — `200` и `null`, не `404`."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        profile = response.json()["profile"]

    assert profile["photo_url"] is None
    assert profile["photo_expires_in"] is None


async def test_storage_failure_does_not_break_card(session: AsyncSession) -> None:
    """AC-10, D-12.10-3: сбой подписи → `photo_url = null`, карточка жива."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, name="Анна", city="Томск")
    profile_id = await _profile_id(session, user_id=target.id)
    await _add_photo(session, profile_id=profile_id, storage_key="photos/d/1.jpg", position=1)
    _, token = await make_moderator(session)

    import app.services.moderation_user_card_service as service  # noqa: PLC0415

    def _boom(storage_key: str, *, ttl: int | None = None) -> str:
        raise media_service.MediaStorageError("storage down")

    original = service.get_signed_url
    service.get_signed_url = _boom  # type: ignore[assignment]
    try:
        async with build_client(session) as client:
            response = await get_card(client, token=token, user_uuid=target.uuid)
            assert response.status_code == 200, response.text
            body = response.json()
    finally:
        service.get_signed_url = original  # type: ignore[assignment]

    assert body["profile"]["photo_url"] is None
    assert body["profile"]["photo_expires_in"] is None
    # Остальная карточка цела: имя и город на месте.
    assert body["profile"]["name"] == "Анна"
    assert body["profile"]["city"] == "Томск"


async def test_storage_key_never_leaves(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10, D-11: `storage_key` не появляется в сыром теле ответа."""
    await _with_signer(monkeypatch)
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    profile_id = await _profile_id(session, user_id=target.id)
    await _add_photo(session, profile_id=profile_id, storage_key="photos/e/1.jpg", position=1)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        raw = response.text

    assert "storage_key" not in raw
    assert '"photos/e/1.jpg"' not in raw


async def test_storage_key_never_leaves_in_logs(session: AsyncSession) -> None:
    """AC-10, D-11, CR M-1: `storage_key` не появляется И В ЛОГАХ.

    AC-10 формулирует «ни в теле, **ни в логах**». Вторая половина не была
    заперта: добавление `storage_key=storage_key` в `logger.warning` оставляло
    весь файл зелёным (проверено CR индивидуально).

    Ловится именно событие сбоя подписи: только на нём сервис пишет в лог, и
    только там ключ мог бы утечь. `capture_logs` structlog-овский — обычный
    `caplog` не видит processорный вывод.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    profile_id = await _profile_id(session, user_id=target.id)
    secret_key = "photos/log-leak/secret.jpg"
    await _add_photo(session, profile_id=profile_id, storage_key=secret_key, position=1)
    _, token = await make_moderator(session)

    import app.services.moderation_user_card_service as service  # noqa: PLC0415

    def _boom(storage_key: str, *, ttl: int | None = None) -> str:
        raise media_service.MediaStorageError("storage down")

    original = service.get_signed_url
    service.get_signed_url = _boom  # type: ignore[assignment]
    try:
        with structlog.testing.capture_logs() as captured:
            async with build_client(session) as client:
                response = await get_card(client, token=token, user_uuid=target.uuid)
                assert response.status_code == 200, response.text
    finally:
        service.get_signed_url = original  # type: ignore[assignment]

    # Положительная половина: событие сбоя действительно записано, иначе
    # проверка «ключа нет» проходила бы на пустом списке логов.
    assert [event["event"] for event in captured] == ["moderation_user_card.photo.sign_failed"]
    serialized = json.dumps(captured, ensure_ascii=False, default=str)
    assert secret_key not in serialized, serialized
    assert "storage_key" not in serialized, serialized


async def test_storage_unavailable_does_not_break_card(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10, D-12.10-3, CR H-2: `MediaUnavailableError` — своя ветка `except`.

    **Почему отдельный тест.** `MediaUnavailableError` (реквизиты S3 не заданы —
    штатное состояние dev/стейджа) НЕ наследник `MediaStorageError`. Удаление
    его из кортежа `except` оставляло 6 тестов зелёными, а карточка на dev
    падала бы `500` — ровно тот сценарий, который комментарий сервиса
    объявляет предотвращённым (P-21: пара тестов на оба класса исключений).
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, name="Анна", city="Томск")
    profile_id = await _profile_id(session, user_id=target.id)
    await _add_photo(session, profile_id=profile_id, storage_key="photos/f/1.jpg", position=1)
    _, token = await make_moderator(session)

    def _raise_unavailable(storage_key: str, *, ttl: int | None = None) -> str:
        """Именно `MediaUnavailableError`, не наследник `MediaStorageError`."""
        raise media_service.MediaUnavailableError("S3 credentials are not configured")

    monkeypatch.setattr(media_service, "get_signed_url", _raise_unavailable)
    monkeypatch.setattr(moderation_user_card_service, "get_signed_url", _raise_unavailable)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["profile"]["photo_url"] is None
    assert body["profile"]["photo_expires_in"] is None
    # Остальная карточка цела: модератор обязан видеть анкету и историю.
    assert body["profile"]["name"] == "Анна"
    assert body["profile"]["city"] == "Томск"
