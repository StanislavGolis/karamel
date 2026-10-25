"""Сервис хранилища: подпись, отсутствие ACL, маппинг ошибок (Task 9.2).

§18.1: каждый тест падает без реализации. Если убрать `ACL`-контроль нельзя
(его нет), но `test_put_object_does_not_set_acl` краснеет ровно тогда, когда
кто-то добавит `ACL='public-read'` — это и есть защита FR58.

S3 в тестах НЕ поднимается (Task 9.8): подменяется клиент, сеть не используется.
`reset_client()` (Task 3.7) вызывается в фикстуре — без него подмена
сработала бы только в первом тесте, а следующие получили бы закэшированный
настоящий клиент и ушли бы в сеть.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import settings
from app.services import media_service

# Реквизиты, при которых клиент вообще создаётся. Значения фиктивные:
# ни один тест не делает сетевой запрос.
FAKE_CREDENTIALS = {
    "s3_endpoint": "https://s3.example.test",
    "s3_region": "ru-1",
    "s3_bucket": "test-bucket",
    "s3_access_key": "test-access-key",
    "s3_secret_key": "test-secret-key",
}


class _FakeS3Client:
    """Минимальный дубль boto3-клиента: запоминает вызовы, возвращает заданное."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._error = error

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(("put_object", kwargs))
        if self._error is not None:
            raise self._error

    def delete_object(self, **kwargs: Any) -> None:
        self.calls.append(("delete_object", kwargs))
        if self._error is not None:
            raise self._error

    def generate_presigned_url(
        self, operation: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        self.calls.append(
            (
                "generate_presigned_url",
                {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn},
            )
        )
        if self._error is not None:
            raise self._error
        return (
            "https://s3.example.test/photos/x/y.jpg"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=deadbeef"
        )


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реквизиты и сброс кэша клиента на каждый тест (Task 3.7)."""
    for name, value in FAKE_CREDENTIALS.items():
        monkeypatch.setattr(settings, name, value)
    media_service.reset_client()
    yield
    media_service.reset_client()


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeS3Client) -> _FakeS3Client:
    """Подменяет кэш клиента: `_get_client` вернёт дубль."""
    monkeypatch.setattr(media_service, "_client", client)
    return client


# --- подпись --------------------------------------------------------------------


def test_signed_url_has_expires_and_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-04, D-11: ссылка содержит `X-Amz-Expires=900` и `X-Amz-Signature`.

    Это единственное место, где проверяется сама подпись: в API-тесте
    `fake_media` возвращает фиктивный URL, и `X-Amz-Expires` там нет (Task 9.6).
    """
    monkeypatch.setattr(settings, "s3_signed_url_ttl", 900)
    client = _install(monkeypatch, _FakeS3Client())

    url = media_service.get_signed_url("photos/p/f.jpg")

    assert "X-Amz-Expires=900" in url
    assert "X-Amz-Signature=" in url
    operation, kwargs = client.calls[-1]
    assert operation == "generate_presigned_url"
    assert kwargs["ExpiresIn"] == 900
    assert kwargs["Params"] == {"Bucket": "test-bucket", "Key": "photos/p/f.jpg"}


def test_signed_url_uses_custom_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-11: явный `ttl` перекрывает конфиг — API не обязан хардкодить значение."""
    client = _install(monkeypatch, _FakeS3Client())

    url = media_service.get_signed_url("photos/p/f.jpg", ttl=60)

    assert "X-Amz-Expires=60" in url
    assert client.calls[-1][1]["ExpiresIn"] == 60


# --- загрузка -------------------------------------------------------------------


def test_put_object_does_not_set_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-02, FR58, D-2: `ACL` и `ServerSideEncryption` не выставляются.

    Появление `ACL='public-read'` делает тест красным — это и есть защита
    «фото не утекает в открытый доступ» на уровне вызова boto3.
    """
    client = _install(monkeypatch, _FakeS3Client())

    import anyio

    anyio.run(
        lambda: media_service.put_object(
            storage_key="photos/p/f.jpg",
            fileobj=__import__("io").BytesIO(b"payload"),
            content_type="image/jpeg",
            size_bytes=7,
        )
    )

    operation, kwargs = client.calls[-1]
    assert operation == "put_object"
    assert "ACL" not in kwargs
    assert "ServerSideEncryption" not in kwargs
    assert kwargs["Key"] == "photos/p/f.jpg"
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["ContentType"] == "image/jpeg"
    assert kwargs["ContentLength"] == 7


# --- маппинг ошибок -------------------------------------------------------------


def test_client_error_maps_to_media_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3.5: `ClientError` → `MediaStorageError`, код S3 в сообщении.

    §18.4: маппинг внешних ошибок обязан иметь тест на каждую ветку. Без
    него обе ветки `ClientError`/`BotoCoreError` могли бы вернуть одно и то же.
    """
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
    _install(monkeypatch, _FakeS3Client(error=error))

    import anyio

    with pytest.raises(media_service.MediaStorageError) as exc:
        anyio.run(
            lambda: media_service.put_object(
                storage_key="photos/p/f.jpg",
                fileobj=__import__("io").BytesIO(b"payload"),
                content_type="image/jpeg",
                size_bytes=7,
            )
        )

    assert "AccessDenied" in str(exc.value)


def test_botocore_error_maps_to_media_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3.5: `BotoCoreError` (сеть) тоже даёт `MediaStorageError`.

    Без этой ветки сетевая ошибка вылетала бы как `EndpointConnectionError`,
    и роутер отдал бы `500` вместо `503 storage_unavailable`.
    """
    _install(monkeypatch, _FakeS3Client(error=EndpointConnectionError(endpoint_url="https://x")))

    import anyio

    with pytest.raises(media_service.MediaStorageError) as exc:
        anyio.run(lambda: media_service.delete_object("photos/p/f.jpg"))

    assert "EndpointConnectionError" in str(exc.value)


# --- конфигурация ---------------------------------------------------------------


def test_missing_credentials_raise_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3.6: пустые реквизиты → `MediaUnavailableError`, а не сбой S3.

    В dev реквизиты пусты штатно (Task 2.2): `500` объявил бы нормальное
    состояние разработки внутренней ошибкой сервера.
    """
    for name in FAKE_CREDENTIALS:
        monkeypatch.setattr(settings, name, "")
    media_service.reset_client()

    with pytest.raises(media_service.MediaUnavailableError):
        media_service.get_signed_url("photos/p/f.jpg")


def test_reset_client_drops_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3.7: `reset_client` обязателен для изоляции тестов.

    Проверяется наблюдаемо: после сброса кэш пуст, и следующий вызов создаёт
    клиент заново (здесь — падает на пустых реквизитах, доказывая, что старый
    подменённый клиент не вернулся).
    """
    _install(monkeypatch, _FakeS3Client())
    media_service.reset_client()
    for name in FAKE_CREDENTIALS:
        monkeypatch.setattr(settings, name, "")

    with pytest.raises(media_service.MediaUnavailableError):
        media_service.get_signed_url("photos/p/f.jpg")
