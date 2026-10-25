"""Гонка двух загрузок фото на двух соединениях (AC-03, D-7; §18.3).

§18.3: гонку в одной сессии доказать нельзя — второй запрос видит
незакоммиченные изменения первого. Поэтому здесь НЕ используется фикстура
`session` из `conftest.py` (она живёт в одной транзакции и запрещена
к правке, §18.8): данные готовятся собственным движком и коммитятся
по-настоящему, а запросы идут через реальный `get_session` — каждое `POST`
получает свою транзакцию и своё соединение.

CR 2.2, патч 2 (§18.1): первая редакция теста НЕ краснела без блокировки.
Коды ответов не различают ветки: с блокировкой второй запрос видит `count=3`
и отдаёт `409` из проверки лимита; без блокировки он проходит проверку,
падает на `uq_profile_id_position`, и сервис (Task 6.3) переводит
`IntegrityError` в тот же `409` — то есть `[201, 409]` получается в обоих
случаях. Проверено откатом: удаление `.with_for_update()` оставляло тест
зелёным. Различитель — число вызовов `ProfilePhotoRepository.create`:
под блокировкой до вставки доходит РОВНО одна транзакция, без неё — обе.
Поэтому счётчик вставок является доказательством блокировки, а не коды.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import app
from app.repositories.profile_photos import ProfilePhotoRepository
from app.repositories.users import UserRepository
from app.services import media_service
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"payload" * 20


@pytest.fixture
async def engine() -> AsyncIterator[Any]:
    """Собственный движок: настоящие коммиты, видимые всем соединениям."""
    database_engine = create_async_engine(DATABASE_URL, poolclass=None)
    try:
        yield database_engine
    finally:
        await database_engine.dispose()


@pytest.fixture
async def prepared_user(engine: Any) -> AsyncIterator[tuple[int, str]]:
    """Учётка с согласием, анкетой и двумя фото — закоммичена по-настоящему.

    Возвращает `(user_id, access_token)`. Убирается после теста вместе со
    всем каскадом (`users` → `profiles` → `profile_photos`), чтобы прогон
    не оставлял строк в базе разработчика.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as preparation:
        phone = f"+7906{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(preparation).create(
            provider="phone", provider_user_id=phone, phone=phone, gender="female"
        )
        await preparation.execute(
            text("UPDATE users SET age_confirmed = true, consent_given_at = now() WHERE id = :id"),
            {"id": user.id},
        )
        await preparation.refresh(user)
        profile = await preparation.execute(
            text(
                "INSERT INTO profiles (user_id, gender, status) "
                "VALUES (:user_id, 'female', 'draft') RETURNING id, uuid"
            ),
            {"user_id": user.id},
        )
        profile_id, profile_uuid = profile.one()
        # Два фото из трёх: следующий POST претендует на последний слот.
        for position in (1, 2):
            await preparation.execute(
                text(
                    "INSERT INTO profile_photos "
                    "(profile_id, storage_key, position, content_type, size_bytes, "
                    "consent_given_at) "
                    "VALUES (:pid, :key, :pos, 'image/jpeg', :size, now())"
                ),
                {
                    "pid": profile_id,
                    "key": f"photos/{profile_uuid}/{uuid_lib.uuid4()}.jpg",
                    "pos": position,
                    "size": len(JPEG),
                },
            )
        issued = await issue_session(preparation, user)
        await preparation.commit()

    try:
        yield (user.id, issued.access_token)
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await cleanup.commit()


@pytest.fixture
async def fake_media(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[list[dict[str, Any]], list[int]]]:
    """Хранилище подменено: сеть не используется (Task 9.8).

    `put_object` спит 50 мс: транзакция первой загрузки держит блокировку
    строки анкеты (`FOR UPDATE`) до `commit` в роутере, и вторая загрузка
    гарантированно ждёт её, а не проскакивает проверкой `count` раньше.
    """
    uploads: list[dict[str, Any]] = []

    async def _put(**kwargs: Any) -> None:
        await asyncio.sleep(0.05)
        uploads.append(kwargs)

    async def _delete(storage_key: str) -> None:
        return None

    def _signed(storage_key: str, *, ttl: int | None = None) -> str:
        return "https://storage.example.test/x?X-Amz-Signature=fake"

    # CR 2.2, патч 2: счётчик фактических INSERT — доказательство блокировки.
    insert_calls: list[int] = []
    original_create = ProfilePhotoRepository.create

    async def _counting_create(self: ProfilePhotoRepository, *args: Any, **kwargs: Any) -> Any:
        insert_calls.append(1)
        return await original_create(self, *args, **kwargs)

    monkeypatch.setattr(media_service, "put_object", _put)
    monkeypatch.setattr(media_service, "delete_object", _delete)
    monkeypatch.setattr(media_service, "get_signed_url", _signed)
    monkeypatch.setattr(ProfilePhotoRepository, "create", _counting_create)
    media_service.reset_client()
    _uploads_and_inserts = (uploads, insert_calls)
    try:
        yield _uploads_and_inserts  # type: ignore[misc]
    finally:
        media_service.reset_client()


async def test_two_parallel_uploads_give_201_and_409(
    engine: Any,
    prepared_user: tuple[int, str],
    fake_media: tuple[list[dict[str, Any]], list[int]],
) -> None:
    """AC-03, D-7: два параллельных `POST` при `count=2` — `201` + `409`.

    Ни `500`, ни два `201`: лимит считается под блокировкой строки анкеты,
    поэтому вторая транзакция ждёт освобождения и видит `count=3`.
    """
    user_id, token = prepared_user
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def upload() -> Any:
            return await client.post(
                "/api/v1/profiles/me/photos",
                files={"file": ("photo.jpg", JPEG, "image/jpeg")},
                data={"consent": "true"},
                cookies={ACCESS_COOKIE_NAME: token},
            )

        first, second = await asyncio.gather(upload(), upload())

    uploads, insert_calls = fake_media
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [201, 409], (first.text, second.text)
    # Ровно одна загрузка в хранилище: второй файл не ушёл (AC-03).
    assert len(uploads) == 1
    # CR 2.2, патч 2: и ровно одна ВСТАВКА. Без `FOR UPDATE` до `create`
    # доходят оба запроса (второй падает на `uq_profile_id_position` и
    # превращается в тот же `409`) — тогда счётчик равен двум и тест красный.
    assert len(insert_calls) == 1, (
        f"без блокировки анкеты обе загрузки доходят до INSERT: вставок {len(insert_calls)}"
    )
    # В БД ровно три фото — четвёртого не появилось.
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as check:
        count = await check.execute(
            text(
                "SELECT count(*) FROM profile_photos p "
                "JOIN profiles pr ON pr.id = p.profile_id "
                "WHERE pr.user_id = :uid"
            ),
            {"uid": user_id},
        )
        assert count.scalar_one() == 3
