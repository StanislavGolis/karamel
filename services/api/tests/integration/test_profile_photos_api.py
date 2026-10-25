"""API загрузки фото (Task 9.6, 9.7; AC-02..AC-09).

S3 в тестах не поднимается и сеть не используется (Task 9.8): фикстура
`fake_media` подменяет функции `media_service`, а `reset_client()` сбрасывает
кэш boto3-клиента (Task 3.7). Без сброса подмена сработала бы только в первом
тесте модуля, а следующие ушли бы в реальную сеть.

§18.2: проверяется состав ключей ответа (`storage_key` и `id` отсутствуют),
фактические аргументы `put_object` и конкретные коды ошибок — не «фото
загружено и не None».
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import media_service
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

# Настоящие сигнатуры: сервис проверяет первые байты, а не заявленный MIME.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"payload" * 20
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"payload" * 20
# AC-01, AC-11, D-13: ответ владельца содержит ровно эти ключи. Равенство,
# а не подмножество: появление `storage_key`, `profile_id` или `id` делает
# тест красным. Task 7.1 стори 2.4: +2 поля модерации — они видны ТОЛЬКО
# владельцу (D-4), публичной схемы ещё нет (эпик 4).
EXPECTED_PHOTO_KEYS = {
    "uuid",
    "position",
    "status",
    "url",
    "expires_in",
    "content_type",
    "rejection_reason",
    "created_at",
    "moderated_at",
}

# CR 2.2, патч 4: форма URL повторяет РЕАЛЬНЫЙ path-style presigned URL
# (`s3v4` + `addressing_style="path"`, D-2): имя бакета стоит в пути. Прежняя
# редакция использовала URL без бакета, поэтому ассерт «бакета нет в ответе»
# не мог упасть — тавтология (§18.2). Решение 2 CR: имя бакета в подписанной
# ссылке неизбежно и секретом не является (доступ без подписи закрыт, AC-04),
# а не должно утекать именно `storage_key` — он и проверяется.
FAKE_BUCKET = "bucket-must-not-leak"
FAKE_URL = (
    f"https://storage.example.test/{FAKE_BUCKET}/photos/fake.jpg"
    "?X-Amz-Signature=fake&X-Amz-Expires=900"
)


@dataclass
class FakeMedia:
    """Запоминает вызовы хранилища: тесты проверяют фактические аргументы."""

    uploads: list[dict[str, Any]] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    fail_put: bool = False
    # Task 7.3, 7.4 (AC-03): журнал вызовов подписи. Счётчик, а не флаг:
    # проверяется, что для `pending` `get_signed_url` не вызывался ВООБЩЕ
    # (в `GET` и в `POST`), а не «вызвался и результат не отдали».
    signed_url_calls: list[str] = field(default_factory=list)
    # CR 2.4 патч 4: сбой подписи ОДНОГО объекта не должен валить весь список.
    fail_sign_for: set[str] = field(default_factory=set)

    async def put_object(self, **kwargs: Any) -> None:
        if self.fail_put:
            raise media_service.MediaStorageError("S3 put_object failed: AccessDenied")
        self.uploads.append(kwargs)

    async def delete_object(self, storage_key: str) -> None:
        self.deletions.append(storage_key)

    def get_signed_url(self, storage_key: str, *, ttl: int | None = None) -> str:
        # Подпись не проверяется здесь: `X-Amz-Expires` в API-тесте недостижим
        # (Task 9.6). Она проверяется в `test_media_service.py` (Task 9.2).
        self.signed_url_calls.append(storage_key)
        if storage_key in self.fail_sign_for:
            raise media_service.MediaStorageError("S3 generate_presigned_url failed: AccessDenied")
        return FAKE_URL


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой зависимостью сессии."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def fake_media(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeMedia]:
    """Task 9.8: подмена хранилища — S3 не поднимается, сеть не используется.

    `reset_client()` обязателен (Task 3.7): без него кэш boto3 вернул бы
    настоящий клиент после первого теста. Подмена идёт через `monkeypatch`,
    поэтому восстанавливается автоматически даже при падении теста.
    """
    media = FakeMedia()
    monkeypatch.setattr(media_service, "put_object", media.put_object)
    monkeypatch.setattr(media_service, "delete_object", media.delete_object)
    monkeypatch.setattr(media_service, "get_signed_url", media.get_signed_url)
    media_service.reset_client()
    try:
        yield media
    finally:
        media_service.reset_client()


async def _new_user(session: AsyncSession, *, gender: str | None = "female") -> tuple[User, str]:
    """Создаёт учётку с согласием и живой сессией; возвращает `(user, token)`."""
    phone = f"+7904{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, gender=gender
    )
    await session.execute(
        text("UPDATE users SET age_confirmed = true, consent_given_at = now() WHERE id = :id"),
        {"id": user.id},
    )
    await session.refresh(user)
    issued = await issue_session(session, user)
    return user, issued.access_token


def _files(content: bytes = JPEG) -> dict[str, tuple[str, bytes, str]]:
    """Поле `file` в форме multipart."""
    return {"file": ("photo.jpg", content, "image/jpeg")}


# --- AC-02: успешная загрузка ---------------------------------------------------


async def test_upload_returns_photo_without_storage_key(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-03, AC-11, D-13: `201` с `uuid`, `url=null`, без `storage_key`.

    AC-03 стори 2.4: только что созданное фото всегда `pending`, а `pending`
    не отдаётся — `url` равен `null`. Подпись для него не считается вовсе.

    `storage_key` в ответе позволил бы клиенту пытаться подобрать путь
    к чужому фото; он не нужен ни для показа, ни для удаления.
    """
    media = fake_media
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == EXPECTED_PHOTO_KEYS
    assert body["status"] == "pending"
    assert body["position"] == 1
    assert body["content_type"] == "image/jpeg"
    # AC-03: `url` и поля модерации для `pending` пусты.
    assert body["url"] is None
    assert body["rejection_reason"] is None
    assert body["moderated_at"] is None
    # D-2: `expires_in` остаётся числом даже без ссылки.
    assert body["expires_in"] == settings.s3_signed_url_ttl
    uuid_lib.UUID(body["uuid"])  # публичный id — валидный uuid
    # AC-02, AC-07: строка создана, время согласия сохранено вместе с фото.
    row = await session.execute(
        text("SELECT status, consent_given_at, storage_key FROM profile_photos")
    )
    status, consent_given_at, storage_key = row.one()
    assert status == "pending"
    assert consent_given_at is not None
    assert storage_key.startswith("photos/")
    assert len(media.uploads) == 1


async def test_upload_rejects_without_consent(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-06: без `consent=true` — `422 photo_consent_required`, файл не уходит.

    Без `Form(False)` в роутере (Task 7.2) ответ был бы `422 invalid_request`
    от Pydantic, и клиент не отличил бы отсутствие согласия от битой формы.
    """
    media = fake_media
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "photo_consent_required"}
    assert media.uploads == []
    count = await session.execute(text("SELECT count(*) FROM profile_photos"))
    assert count.scalar_one() == 0


async def test_upload_rejects_non_image(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-05: текст с `image/jpeg` — `422 photo_not_image`.

    Заявленный MIME не доказательство (D-5): проверяется сигнатура.
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files={"file": ("photo.jpg", b"<?php echo 1; ?>", "image/jpeg")},
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "photo_not_image"}


async def test_upload_rejects_oversize_file(
    client: AsyncClient,
    session: AsyncSession,
    fake_media: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-05, AC-08: файл больше `MAX_PHOTO_BYTES` — `413 request_too_large`.

    `max_photo_bytes` уменьшается, чтобы не гнать 5 МиБ через тест: логика
    читает настройку при вызове, поэтому подмена видна сервису.
    """
    media = fake_media
    monkeypatch.setattr(settings, "max_photo_bytes", 64)
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(JPEG + b"x" * 100),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 413
    assert response.json() == {"code": "request_too_large"}
    assert media.uploads == []


async def test_fourth_photo_returns_409(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-03: четвёртое фото — `409 photo_limit_reached`, файл не загружается.

    Проверяется и число загрузок в хранилище: при отказе объект не должен
    появиться в бакете (иначе сиротский файл до эпика 8).
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}

    for _ in range(3):
        ok = await client.post(
            "/api/v1/profiles/me/photos",
            files=_files(),
            data={"consent": "true"},
            cookies=cookies,
        )
        assert ok.status_code == 201, ok.text

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(PNG),
        data={"consent": "true"},
        cookies=cookies,
    )

    assert response.status_code == 409
    assert response.json() == {"code": "photo_limit_reached"}
    assert len(media.uploads) == 3


async def test_upload_without_cookie_is_401(client: AsyncClient) -> None:
    """AC-02: без сессии — `401 unauthenticated`."""
    response = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}
    )

    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated"}


# --- AC-04: список с подписанными ссылками ---------------------------------------


async def _set_status(
    session: AsyncSession,
    photo_uuid: str,
    *,
    status: str,
    rejection_reason: str | None = None,
) -> None:
    """Task 7.2: переводит фото в решённый статус напрямую в БД.

    Через SQL, а не через `apply_moderation_result`: HTTP-маршрута модерации
    в 2.4 нет (D-5), а API-тест проверяет ФОРМУ ОТВЕТА на готовых данных,
    не повторяя доменную запись (она покрыта Task 8).
    """
    await session.execute(
        text(
            "UPDATE profile_photos SET status = :status, "
            "rejection_reason = :reason, moderated_at = now() "
            "WHERE uuid = :uuid"
        ),
        {"status": status, "reason": rejection_reason, "uuid": photo_uuid},
    )


async def test_list_pending_has_no_url(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.2, 7.3 (AC-03): `pending` → `url = null`, подпись не считается.

    Проверяется и форма ответа, и СЧЁТЧИК вызовов `get_signed_url`: «посчитать
    и не отдать» оставило бы валидную ссылку в памяти на 900 с. `pending`
    не должен трогать хранилище вообще.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    calls_before = len(media.signed_url_calls)

    response = await client.get("/api/v1/profiles/me/photos", cookies=cookies)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert set(item) == EXPECTED_PHOTO_KEYS
    assert item["status"] == "pending"
    assert item["url"] is None
    # CR 2.4 патч 14 (AC-01): путь `GET` — основной для владельца, но именно
    # здесь не проверялись поля решения. Для `POST` такие ассерты были, для
    # `GET` — нет, хотя именно `GET` читает сохранённое фото.
    assert item["rejection_reason"] is None
    assert item["moderated_at"] is None
    # AC-03: подпись не запрашивалась. `POST` тоже не должен её звать.
    assert len(media.signed_url_calls) == calls_before == 0
    # D-2: `expires_in` — число всегда, даже без ссылки.
    assert item["expires_in"] == settings.s3_signed_url_ttl


async def test_list_approved_returns_signed_url(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.2 (AC-04): `approved` → подписанный `url` и `expires_in`.

    Парный к `test_list_pending_has_no_url`: одно и то же фото до и после
    решения модератора. Без второй половины «pending не подписывается»
    проходило бы и при полностью отключённой подписи.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    await _set_status(session, created.json()["uuid"], status="approved")

    response = await client.get("/api/v1/profiles/me/photos", cookies=cookies)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "approved"
    assert item["url"] == FAKE_URL
    assert item["expires_in"] == settings.s3_signed_url_ttl
    assert item["rejection_reason"] is None
    # AC-04: `moderated_at` — ISO 8601 UTC с `Z` (project-context §4).
    assert item["moderated_at"].endswith("Z")
    assert len(media.signed_url_calls) == 1


async def test_list_is_empty_without_profile(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-04: анкеты нет — `200` с пустым списком, не `404`.

    Экран анкеты рисует «фото нет»; отдельный код ошибки заставлял бы
    клиент различать два пустых состояния.
    """
    _, token = await _new_user(session)

    response = await client.get("/api/v1/profiles/me/photos", cookies={ACCESS_COOKIE_NAME: token})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


# --- AC-02, AC-04: приватность ---------------------------------------------------


async def test_response_has_no_bucket_or_unsigned_url(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-03, AC-11, D-13: в ответе нет `storage_key`, `POST` не подписывает.

    CR 2.2, патч 4 (Решение 2): имя бакета в path-style presigned URL
    присутствует физически, и требование «бакета нет в ответе» было
    невыполнимым — прежний ассерт проходил только потому, что фиктивный URL
    бакета не содержал. Секрет, который нельзя раскрывать, — `storage_key`:
    он задаёт точный объект. Здесь проверяется именно он.

    AC-03 стори 2.4: `POST` возвращает `pending`, поэтому `url = null`
    и подпись не считается вовсе — `signed_url_calls` пуст.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}

    response = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )

    raw = response.text
    body = response.json()
    # Ключ объекта не утекает ни как поле, ни как подстрока (D-13).
    storage_key = media.uploads[0]["storage_key"]
    assert storage_key not in raw
    assert "storage_key" not in raw
    assert "storage_key" not in body
    # AC-03: ссылки нет вовсе — подпись для `pending` не считалась.
    assert body["url"] is None
    assert "X-Amz-Signature" not in raw
    assert media.signed_url_calls == []
    # Ключ объекта уходит в хранилище с префиксом `photos/` (D-3).
    assert media.uploads[0]["storage_key"].startswith("photos/")


# --- AC-09: удаление и владение ---------------------------------------------------


async def test_delete_removes_photo_and_object(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-09, D-10: `DELETE` → `204`, затем `GET` без фото, объект удалён.

    D-10: ключ возвращается сервисом до `commit` и удаляется в хранилище
    после — проверяется, что `delete_object` вызван с тем же ключом.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    photo_uuid = created.json()["uuid"]

    response = await client.delete(f"/api/v1/profiles/me/photos/{photo_uuid}", cookies=cookies)

    assert response.status_code == 204
    assert media.deletions == [media.uploads[0]["storage_key"]]
    listed = await client.get("/api/v1/profiles/me/photos", cookies=cookies)
    assert listed.json()["total"] == 0


async def test_delete_foreign_photo_is_404(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-09: чужое фото — `404 photo_not_found`, объект не удаляется.

    Существование чужого фото не подтверждается: `404` и на чужое,
    и на несуществующее.
    """
    media = fake_media
    _, token = await _new_user(session)
    _, other_token = await _new_user(session)
    created = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )
    foreign_uuid = created.json()["uuid"]

    response = await client.delete(
        f"/api/v1/profiles/me/photos/{foreign_uuid}",
        cookies={ACCESS_COOKIE_NAME: other_token},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "photo_not_found"}
    assert media.deletions == []


async def test_delete_garbage_uuid_is_422(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.4: мусор в пути — `422 invalid_request`, а не `404`.

    Клиент различает «опечатка в пути» и «фото нет» по коду.
    """
    _, token = await _new_user(session)

    response = await client.delete(
        "/api/v1/profiles/me/photos/not-a-uuid", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


# --- ошибка хранилища ------------------------------------------------------------


async def test_storage_error_rolls_back_row(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """D-9: ошибка S3 — `503`, строки в БД не остаётся.

    Обратный порядок («S3 → INSERT») оставлял бы сиротские объекты; здесь
    проверяется, что при сбое загрузки строка откатана и список пуст.
    """
    media = fake_media
    media.fail_put = True
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}

    response = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    # D-9: строки в БД не остаётся — транзакция откатана.
    count = await session.execute(text("SELECT count(*) FROM profile_photos"))
    assert count.scalar_one() == 0
    # И в хранилище ничего не загружено: `put_object` упал, а не «прошёл молча».
    assert media.uploads == []
    # Второй `GET` здесь НЕ проверяется: откат в роутере отменяет и подготовку
    # теста (он живёт в одной транзакции с `create_savepoint`), поэтому cookie
    # становится невалидной. В проде сессия принадлежит запросу, и откат
    # затрагивает только его — это свойство фикстуры, а не дефект роутера.


async def test_upload_does_not_sign_url(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.4 (AC-03): `POST` не подписывает URL — вызовов `0`.

    ЗАМЕНЯЕТ `test_signed_url_failure_is_503_without_duplicate`
    (CR 2.2, патч 1). Причина: после Task 3.3 подписи в `POST` нет вовсе,
    и прежний тест проверял бы недостижимую ветку — он стал бы вечно-зелёным
    (§18.2: ассерт, который не может упасть по делу, хуже отсутствующего).
    Снятие зафиксировано в `deferred-work.md`.

    §18.1: вернуть `get_signed_url` в `upload_my_photo` — тест краснеет
    на `signed_url_calls == []`.
    """
    media = fake_media
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 201, response.text
    assert response.json()["url"] is None
    # Объект в бакет ушёл (штатная загрузка), подписи не было ни одной.
    assert len(media.uploads) == 1
    assert media.signed_url_calls == []


# --- AC-07: согласие сохранено ---------------------------------------------------


async def test_consent_time_persisted(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-07: `consent_given_at` заполнен у каждой загруженной строки.

    Без сохранения времени согласия нельзя доказать, что фото показывается
    другим пользователям с согласия владельца.
    """
    user, token = await _new_user(session)
    await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    row = await session.execute(
        text(
            "SELECT p.consent_given_at FROM profile_photos p "
            "JOIN profiles pr ON pr.id = p.profile_id WHERE pr.user_id = :uid"
        ),
        {"uid": user.id},
    )

    assert row.scalar_one() is not None


# --- AC-05, AC-08: причина отказа и общая очередь ----------------------------------


async def test_list_rejected_shows_reason_and_moderated_at(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.6 (AC-04, AC-05): `rejected` — причина видна владельцу, ссылка есть.

    AC-05: причина непустая и отдаётся ТОЛЬКО владельцу (`PhotoOwn`).
    AC-04: отклонённое фото подписывается так же, как одобренное — иначе
    владелец не понял бы, что именно отклонили.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    await _set_status(
        session, created.json()["uuid"], status="rejected", rejection_reason="не видно лица"
    )

    response = await client.get("/api/v1/profiles/me/photos", cookies=cookies)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "rejected"
    assert item["rejection_reason"] == "не видно лица"
    assert item["url"] == FAKE_URL
    assert item["moderated_at"].endswith("Z")
    assert len(media.signed_url_calls) == 1


async def test_male_and_female_answers_have_same_pending_shape(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.9 (AC-02, FR63): пол не влияет ни на статус, ни на набор ключей.

    §18.1: если в `list_photos`/схеме появится ветвление по `gender`,
    ответы разойдутся и тест покраснеет. Проверяются оба ответа целиком:
    одинаковый набор ключей и `pending` у обоих.
    """
    bodies: list[dict[str, Any]] = []
    for gender in ("male", "female"):
        _, token = await _new_user(session, gender=gender)
        cookies = {ACCESS_COOKIE_NAME: token}
        created = await client.post(
            "/api/v1/profiles/me/photos",
            files=_files(),
            data={"consent": "true"},
            cookies=cookies,
        )
        assert created.status_code == 201, created.text
        listed = await client.get("/api/v1/profiles/me/photos", cookies=cookies)
        bodies.append(listed.json()["items"][0])

    assert bodies[0]["status"] == bodies[1]["status"] == "pending"
    assert set(bodies[0]) == set(bodies[1]) == EXPECTED_PHOTO_KEYS
    assert bodies[0]["url"] is None and bodies[1]["url"] is None


# --- AC-04, AC-08: репозиторные выборки -------------------------------------------


async def test_list_approved_by_profile_excludes_pending_and_rejected(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.10 (AC-04): чужая выборка возвращает ТОЛЬКО `approved`.

    §18.1: убрать `status == approved` из `list_approved_by_profile` —
    тест покраснеет на `len(found) == 1`. Это и есть защита от утечки
    `pending`/`rejected` в выборку эпика 4 (D-7).
    """
    from app.repositories.profile_photos import ProfilePhotoRepository

    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    uuids: list[str] = []
    for _ in range(3):
        created = await client.post(
            "/api/v1/profiles/me/photos",
            files=_files(),
            data={"consent": "true"},
            cookies=cookies,
        )
        assert created.status_code == 201, created.text
        uuids.append(created.json()["uuid"])
    await _set_status(session, uuids[0], status="approved")
    await _set_status(session, uuids[1], status="rejected", rejection_reason="размыто")

    profile_id_row = await session.execute(text("SELECT id FROM profiles LIMIT 1"))
    profile_id = profile_id_row.scalar_one()
    found = await ProfilePhotoRepository(session).list_approved_by_profile(profile_id)

    assert len(found) == 1
    assert found[0].status == "approved"
    assert str(found[0].uuid) == uuids[0]


async def test_list_pending_order_is_fifo_and_gender_independent(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.11 (AC-08): очередь FIFO по `created_at` — женская раньше мужской.

    §18.1, D-8: тавтологией было «нет аргумента `gender` → нет приоритета».
    Здесь проверяется ФАКТИЧЕСКИЙ порядок: женская анкета загружена первой
    и приходит первой, хотя мужская «приоритетнее» по продуктовым мифам.
    `approved` в очередь не попадает (AC-08).
    """
    from app.repositories.profile_photos import ProfilePhotoRepository

    female_uuids: list[str] = []
    male_uuids: list[str] = []
    for gender, bucket in (("female", female_uuids), ("male", male_uuids)):
        _, token = await _new_user(session, gender=gender)
        created = await client.post(
            "/api/v1/profiles/me/photos",
            files=_files(),
            data={"consent": "true"},
            cookies={ACCESS_COOKIE_NAME: token},
        )
        assert created.status_code == 201, created.text
        bucket.append(created.json()["uuid"])

    pending = await ProfilePhotoRepository(session).list_pending(100)
    pending_uuids = [str(photo.uuid) for photo in pending]

    # Обе анкеты в очереди; чужих `approved`/`rejected` в ней нет по построению.
    assert set(pending_uuids) == {female_uuids[0], male_uuids[0]}
    # D-8: порядок по `created_at, id` — женская загружена раньше.
    assert pending_uuids.index(female_uuids[0]) < pending_uuids.index(male_uuids[0])


# --- CR 2.4 патч 4: частичная деградация подписи ---------------------------------


async def test_signing_failure_hides_only_broken_photo(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """AC-03: сбой подписи на `approved` не делает `pending` ошибкой запроса.

    Прежнее поведение: `MediaStorageError` из середины цикла превращал весь
    `GET /me/photos` в `503`, и владелец терял из виду все фото — включая
    `pending`, для которых ссылка вообще не нужна.

    §18.1: убрать `try/except` вокруг `get_signed_url` в `list_photos` —
    тест краснеет на `503` вместо `200`.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    uuids: list[str] = []
    for _ in range(2):
        created = await client.post(
            "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
        )
        assert created.status_code == 201, created.text
        uuids.append(created.json()["uuid"])

    await _set_status(session, uuids[0], status="approved")
    # Битым объявляется именно `approved`-объект; `pending` подписи не требует.
    session_photos = await session.execute(text("SELECT storage_key, uuid FROM profile_photos"))
    broken_key = None
    for key, photo_uuid in session_photos.all():
        if str(photo_uuid) == uuids[0]:
            broken_key = key
    assert broken_key is not None
    media.fail_sign_for.add(broken_key)

    response = await client.get("/api/v1/profiles/me/photos", cookies=cookies)

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    by_status = {item["status"]: item for item in items}
    # Ссылка не собралась — фото отдаётся как «нет изображения», а не ошибкой.
    assert by_status["approved"]["url"] is None
    # `pending` не пострадал и вообще не касался хранилища.
    assert by_status["pending"]["url"] is None


# --- стори 2.5: замена фото (AC-02, AC-05) ----------------------------------------


async def _make_photo_approved(session: AsyncSession, photo_uuid: str) -> None:
    """Ставит фото в `approved` SQL-ом: маршрута модерации нет до 3.7.

    Нужно, чтобы вход теста совпадал с реальностью: заменяют одобренное фото.
    `moderated_at` заполняется — иначе состояние неотличимо от «не проверяли».
    """
    await session.execute(
        text(
            "UPDATE profile_photos SET status = 'approved', moderated_at = now() WHERE uuid = :uuid"
        ),
        {"uuid": photo_uuid},
    )
    await session.flush()


async def test_replace_keeps_position_and_resets_status(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 9.4 (AC-02): `PUT` — `200`, позиция и uuid прежние, статус `pending`.

    §18.1: без мутации одной строки (путь `DELETE`+`POST`) `position` изменилась
    бы на первую свободную, а `uuid` стал бы новым — тест краснеет по обоим
    полям. Именно это делал клиент до 2.5 (CR 2.4 патч 5).
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    first = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    second = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    target = second.json()
    await _make_photo_approved(session, target["uuid"])
    untouched = first.json()
    old_key = media.uploads[-1]["storage_key"]

    response = await client.put(
        f"/api/v1/profiles/me/photos/{target['uuid']}",
        files=_files(PNG),
        data={"consent": "true"},
        cookies=cookies,
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == EXPECTED_PHOTO_KEYS
    # AC-02, D-2: адрес и место в карточке не меняются.
    assert body["uuid"] == target["uuid"]
    assert body["position"] == target["position"]
    # AC-02: заменённое фото уходит на проверку, ссылки нет (D-2 стори 2.4).
    assert body["status"] == "pending"
    assert body["url"] is None
    assert body["rejection_reason"] is None
    assert body["moderated_at"] is None
    # Новый файл — новое расширение и новый ключ; старый объект удалён.
    assert media.uploads[-1]["storage_key"].endswith(".png")
    assert media.deletions == [old_key]
    # Остальные фото не тронуты: ни позиция, ни статус.
    listed = (await client.get("/api/v1/profiles/me/photos", cookies=cookies)).json()
    by_uuid = {item["uuid"]: item for item in listed["items"]}
    assert by_uuid[untouched["uuid"]]["position"] == untouched["position"]
    assert by_uuid[untouched["uuid"]]["status"] == "pending"


async def test_replace_without_consent_is_403(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 9.5 (FR64, D-6): без согласия — `403 photo_consent_required`.

    §18.1: при снятии `ensure_consent` замена прошла бы без согласия, и тест
    краснеет не только кодом: статус фото сменился бы на `pending` вопреки AC-02.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    photo_uuid = created.json()["uuid"]
    uploads_before = len(media.uploads)

    response = await client.put(
        f"/api/v1/profiles/me/photos/{photo_uuid}",
        files=_files(PNG),
        data={"consent": "false"},
        cookies=cookies,
    )

    assert response.status_code == 403
    assert response.json() == {"code": "photo_consent_required"}
    assert len(media.uploads) == uploads_before  # файл не ушёл в бакет


async def test_replace_non_image_keeps_photo_count(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 9.5 (AC-05): неверный файл — `422`, число фото и ключ прежние."""
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    photo_uuid = created.json()["uuid"]
    old_key = media.uploads[0]["storage_key"]

    response = await client.put(
        f"/api/v1/profiles/me/photos/{photo_uuid}",
        files=_files(b"not-an-image"),
        data={"consent": "true"},
        cookies=cookies,
    )

    assert response.status_code == 422
    assert response.json() == {"code": "photo_not_image"}
    listed = (await client.get("/api/v1/profiles/me/photos", cookies=cookies)).json()
    assert listed["total"] == 1
    # §18.2: число загрузок не выросло, ключ и тип прежние — файл не заменён.
    assert len(media.uploads) == 1
    assert media.uploads[0]["storage_key"] == old_key
    assert listed["items"][0]["content_type"] == "image/jpeg"
    assert media.deletions == []


async def test_replace_foreign_photo_is_404(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 9.5 (AC-09): чужое фото — `404 photo_not_found`, бакет не тронут.

    CR 2.5, патч P-4: у второго пользователя СВОЯ анкета с фото. Прежняя
    редакция теста этого не делала, и запрос отбивался первой ветвью
    (`profile is None`) — до проверки владельца фото исполнение не доходило.
    Тест был зелёным при полностью сломанном фильтре по `profile_id`: замена
    чужого фото прошла бы, и CI этого не заметил бы (§18.1, §18.2).
    """
    media = fake_media
    _, token = await _new_user(session)
    _, other_token = await _new_user(session)
    created = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )
    # Второй пользователь получает СВОЮ анкету и своё фото: иначе `replace_photo`
    # падает на `profile is None` и не доходит до сравнения `profile_id`.
    await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: other_token},
    )
    uploads_before = len(media.uploads)

    response = await client.put(
        f"/api/v1/profiles/me/photos/{created.json()['uuid']}",
        files=_files(PNG),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: other_token},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "photo_not_found"}
    # AC-09: файл не ушёл в бакет, чужое фото не тронуто.
    assert len(media.uploads) == uploads_before
    own = (
        await client.get("/api/v1/profiles/me/photos", cookies={ACCESS_COOKIE_NAME: token})
    ).json()
    assert own["total"] == 1
    assert own["items"][0]["content_type"] == "image/jpeg"


async def test_replace_garbage_uuid_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """Task 5.6: мусор в пути — `422 invalid_request`, а не `404`."""
    _, token = await _new_user(session)

    response = await client.put(
        "/api/v1/profiles/me/photos/not-a-uuid",
        files=_files(PNG),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


async def test_replace_storage_failure_rolls_back(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 9.5 (AC-05, D-3): сбой `put_object` — `503`, строка не изменена.

    §18.1: без `rollback()` в роутере строка осталась бы с метаданными нового
    файла, которого нет в бакете: `GET` показал бы `pending` с новым
    `content_type`, а ссылка никогда бы не собралась.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    photo_uuid = created.json()["uuid"]
    media.fail_put = True

    response = await client.put(
        f"/api/v1/profiles/me/photos/{photo_uuid}",
        files=_files(PNG),
        data={"consent": "true"},
        cookies=cookies,
    )

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    listed = (await client.get("/api/v1/profiles/me/photos", cookies=cookies)).json()
    assert listed["total"] == 1
    # AC-02: прежний файл остался прежним — тип не переписан.
    assert listed["items"][0]["content_type"] == "image/jpeg"
    assert media.deletions == []


async def test_upload_invalid_file_creates_no_profile(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 4.5 (AC-05): `422` на пустой БД не создаёт анкету-черновик.

    §18.1: доказано откатом — при возврате `_get_or_create_draft` ПЕРЕД
    валидацией (порядок 2.2-2.4) в `profiles` появляется строка, и тест
    краснеет на `count(*)==0`. Долг CR 2.4: после `422` фронт получал
    `isEdit=true` и заголовок «Ваша анкета».
    """
    _, token = await _new_user(session)

    response = await client.post(
        "/api/v1/profiles/me/photos",
        files=_files(b"not-an-image"),
        data={"consent": "true"},
        cookies={ACCESS_COOKIE_NAME: token},
    )

    assert response.status_code == 422
    assert response.json() == {"code": "photo_not_image"}
    count = await session.execute(text("SELECT count(*) FROM profiles"))
    assert count.scalar_one() == 0


async def test_edits_do_not_touch_users_or_create_rows(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 7.1, 7.2 (AC-04): правки не пишут в `users` и не создают сущностей.

    Домена заявок в коде нет (D-9), поэтому AC-04 доказывается отсутствием
    записи: снимок ВСЕХ колонок `users` до и после `PATCH`+`PUT` совпадает,
    а число строк в `profiles`/`profile_photos` меняется только на ожидаемое.

    §18.1: если правка анкеты начнёт писать в `users` (например, сбрасывать
    `status` или `likes_used_today`), либо создавать заявку — тест краснеет
    на первом же отличии снимка.
    """
    user, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    photo_uuid = created.json()["uuid"]

    users_before = (
        (await session.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user.id}))
        .mappings()
        .one()
    )
    profiles_before = (await session.execute(text("SELECT count(*) FROM profiles"))).scalar_one()

    # Маршрут правки — `PATCH /profiles/me` (у коллекции `PATCH` нет: 405).
    # Черновик, созданный загрузкой фото, дозаполняется до `complete`.
    patched = await client.patch(
        "/api/v1/profiles/me",
        json={"name": "Аня", "city": "Томск", "age": 32, "bio": "люблю горы"},
        cookies=cookies,
    )
    assert patched.status_code == 200
    replaced = await client.put(
        f"/api/v1/profiles/me/photos/{photo_uuid}",
        files=_files(PNG),
        data={"consent": "true"},
        cookies=cookies,
    )
    assert replaced.status_code == 200

    users_after = (
        (await session.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user.id}))
        .mappings()
        .one()
    )
    # AC-04: учётная запись не изменилась ни в одной колонке.
    assert dict(users_after) == dict(users_before)
    # AC-04: правки не создают строк вне `profiles`/`profile_photos`.
    profiles_after = (await session.execute(text("SELECT count(*) FROM profiles"))).scalar_one()
    assert profiles_after == profiles_before
    photos_count = (await session.execute(text("SELECT count(*) FROM profile_photos"))).scalar_one()
    assert photos_count == 1  # замена не добавила строку


async def test_replace_leaves_others_visible_with_links(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """Task 6.4 (AC-02): после замены список отдаёт `pending` без ссылки,
    а остальные фото — с прежними подписями.

    §18.1: если `replace_photo` забывает сбросить статус, заменённое фото
    остаётся `approved` и получит подписанный URL — тест краснеет на
    `url is None`. Если бы замена удаляла и вставляла строку заново,
    у второго фото изменился бы `uuid`/`position`, и словарь по uuid
    не нашёл бы его.
    """
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    first = (
        await client.post(
            "/api/v1/profiles/me/photos",
            files=_files(),
            data={"consent": "true"},
            cookies=cookies,
        )
    ).json()
    second = (
        await client.post(
            "/api/v1/profiles/me/photos",
            files=_files(),
            data={"consent": "true"},
            cookies=cookies,
        )
    ).json()
    # Оба одобрены: вход теста — анкета с выданными ссылками (AC-02).
    await _make_photo_approved(session, first["uuid"])
    await _make_photo_approved(session, second["uuid"])

    replaced = await client.put(
        f"/api/v1/profiles/me/photos/{second['uuid']}",
        files=_files(PNG),
        data={"consent": "true"},
        cookies=cookies,
    )
    assert replaced.status_code == 200

    listed = (await client.get("/api/v1/profiles/me/photos", cookies=cookies)).json()
    by_uuid = {item["uuid"]: item for item in listed["items"]}
    # Заменённое — на проверке, без подписи (D-2 стори 2.4).
    assert by_uuid[second["uuid"]]["status"] == "pending"
    assert by_uuid[second["uuid"]]["url"] is None
    # Второе фото не тронуто: `approved` и ссылка выдаётся.
    assert by_uuid[first["uuid"]]["status"] == "approved"
    assert by_uuid[first["uuid"]]["url"] == FAKE_URL
    assert by_uuid[first["uuid"]]["position"] == first["position"]


async def test_replace_oversize_file_is_413_and_keeps_row(
    client: AsyncClient,
    session: AsyncSession,
    fake_media: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 2.5, P-8 (AC-05): `413` на замене не меняет ни строку, ни бакет.

    Ветка «сверх лимита» на маршруте замены не была покрыта: единственный
    `413`-тест относился к загрузке (`POST`). Порядок «валидация → мутация»
    (D-3, D-7) — предмет этой стори, и регрессия в нём не ловилась.

    §18.1: доказано откатом — при переносе валидации ПОСЛЕ мутации строка
    получила бы новый `content_type`, и тест краснеет на нём.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    photo_uuid = created.json()["uuid"]
    uploads_before = len(media.uploads)
    # Лимит уменьшается ПОСЛЕ подготовки: иначе подготовительный `POST`
    # отбивался бы тем же `413`, и тест проверял бы не замену.
    monkeypatch.setattr(settings, "max_photo_bytes", 64)

    response = await client.put(
        f"/api/v1/profiles/me/photos/{photo_uuid}",
        files=_files(PNG + b"x" * 200),
        data={"consent": "true"},
        cookies=cookies,
    )

    assert response.status_code == 413
    assert response.json() == {"code": "request_too_large"}
    assert len(media.uploads) == uploads_before
    listed = (await client.get("/api/v1/profiles/me/photos", cookies=cookies)).json()
    assert listed["total"] == 1
    assert listed["items"][0]["status"] == "pending"
    assert media.deletions == []


async def test_replace_empty_file_is_422_photo_empty(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """CR 2.5, P-8 (AC-05): пустой файл на замене — `422 photo_empty`.

    Вторая размерная ветка `validate_photo`, тоже не покрытая для `PUT`.
    Проверяется, что код именно `photo_empty`, а не общий `photo_not_image`.
    """
    media = fake_media
    _, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    uploads_before = len(media.uploads)

    response = await client.put(
        f"/api/v1/profiles/me/photos/{created.json()['uuid']}",
        files=_files(b""),
        data={"consent": "true"},
        cookies=cookies,
    )

    assert response.status_code == 422
    assert response.json() == {"code": "photo_empty"}
    assert len(media.uploads) == uploads_before


async def test_replace_keeps_profile_row_unchanged(
    client: AsyncClient, session: AsyncSession, fake_media: Any
) -> None:
    """CR 2.5, P-9 (AC-02): замена не меняет строку `profiles` ни в колонке.

    AC-02 требует «анкета остаётся видимой (`status`, `is_hidden` прежние)»,
    но ни один тест не читал `profiles` до/после `PUT`: регрессия «замена
    сбрасывает `status` в `draft`» прошла бы CI. Снимок всех колонок —
    тот же приём, что в `test_rejected_photo_keeps_profile_unchanged` (2.4).

    §18.1: доказано откатом — добавление любого `UPDATE` строки `profiles`
    в `replace_photo` краснит тест на первом отличии снимка.
    """
    # Фикстура нужна как зависимость (подменяет хранилище), значение не используется.
    assert fake_media is not None
    user, token = await _new_user(session)
    cookies = {ACCESS_COOKIE_NAME: token}
    created = await client.post(
        "/api/v1/profiles/me/photos", files=_files(), data={"consent": "true"}, cookies=cookies
    )
    await client.patch(
        "/api/v1/profiles/me",
        json={"name": "Аня", "city": "Томск", "age": 32, "bio": "люблю горы"},
        cookies=cookies,
    )
    before = (
        (await session.execute(text("SELECT * FROM profiles WHERE user_id = :id"), {"id": user.id}))
        .mappings()
        .one()
    )
    assert before["status"] == "complete"

    await _make_photo_approved(session, created.json()["uuid"])
    response = await client.put(
        f"/api/v1/profiles/me/photos/{created.json()['uuid']}",
        files=_files(PNG),
        data={"consent": "true"},
        cookies=cookies,
    )
    assert response.status_code == 200

    after = (
        (await session.execute(text("SELECT * FROM profiles WHERE user_id = :id"), {"id": user.id}))
        .mappings()
        .one()
    )
    # AC-02: ни одна колонка анкеты не изменилась — включая `updated_at`.
    assert dict(after) == dict(before)
