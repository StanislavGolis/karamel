"""Единственная точка доступа к приватному S3-хранилищу (Task 3.1–3.7).

project-context §6: «Хранилище: только через `media_service` подписанным URL».
Другого пути к бакету в коде нет: ни `boto3` в сервисах, ни прямых ссылок
в ответах API.

D-2: `boto3` (sync) + `anyio.to_thread`, а не `aiobotocore`. `aiobotocore`
намертво пинит версию `botocore`, и обновление любого из них ломает сборку
с точными пинами; поток на одну загрузку дешевле конфликта зависимостей,
а `run_sync` не блокирует event loop. Подпись presigned URL — чистая
CPU-работа, в поток не уходит.

D-11: presigned URL никогда не логируется и не сохраняется в БД. В лог
идёт только код ошибки S3 (`NoSuchKey`, `AccessDenied`) — без ключей,
без подписи.

Приватность (FR58, AC-02): `ACL` не выставляется вообще, политика бакета
публичного доступа не даёт. `ServerSideEncryption` тоже не задаётся:
шифрование — на стороне провайдера/диска ДЦ РФ (NFR11), а SSE-заголовок
в запросе ничего не добавляет к приватности.
"""

from __future__ import annotations

from typing import Any, BinaryIO

import anyio
import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

# CR 2.2, патч 3: structlog, а не stdlib `logging`. Два дефекта stdlib-пути:
#  1) `basicConfig(format="%(message)s")` выбрасывает `extra`, поэтому код S3
#     (`AccessDenied`) в лог не попадал — Task 3.5 не выполнялся фактически;
#  2) `RedactingSecretsProcessor` подключён к цепочке structlog, и stdlib-запись
#     проходит мимо редакции: `repr(settings)` с `S3_SECRET_KEY` утёк бы целиком
#     (проверено: секрет присутствовал в выводе).
logger = structlog.get_logger(__name__)

# Task 3.1, D-2: клиент создаётся один раз и кэшируется. `None` — «ещё не
# создавался»; отдельный флаг не нужен, потому что сам клиент правдив.
_client: Any | None = None

# D-2: `s3v4` — обязательная версия подписи для приватного доступа,
# `path`-адресация совместима с S3-провайдерами ДЦ РФ (Timeweb), которые
# не всегда поддерживают virtual-hosted стиль для бакетов с точками в имени.
_S3_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "path"})


class MediaStorageError(Exception):
    """Task 3.5: сбой хранилища при рабочей конфигурации — `500`/проброс выше.

    Отдельный тип от `MediaUnavailableError`: «хранилище недоступно из-за
    конфигурации» — это `503` и зона ответственности деплоя, а сбой операции
    при корректных реквизитах — дефект/инфраструктурный инцидент.
    """


class MediaUnavailableError(Exception):
    """Task 3.6: реквизиты S3 не заданы — `503 storage_unavailable`, не `500`.

    В dev реквизиты пусты штатно (Task 2.2): отдавать `500` значило бы
    объявлять нормальное состояние разработки внутренней ошибкой сервера,
    и клиент не смог бы отличить «мы не настроены» от «мы сломались».
    """


def _credentials_configured() -> bool:
    """Task 3.6: все пять реквизитов заполнены (задача 2.2 гарантирует «все или ни одного»)."""
    return all(
        value.strip()
        for value in (
            settings.s3_endpoint,
            settings.s3_region,
            settings.s3_bucket,
            settings.s3_access_key,
            settings.s3_secret_key,
        )
    )


def _get_client() -> Any:
    """Task 3.1, D-2: ленивый кэшированный клиент.

    Создаётся при первом обращении, а не на импорте: импорт `media_service`
    в тестах без реквизитов не должен ни ходить в сеть, ни падать.

    `MediaUnavailableError` бросается до создания клиента: boto3 с пустыми
    ключами не падает сам, а получает `403` от провайдера — то есть выглядит
    как сбой хранилища вместо ошибки конфигурации.
    """
    global _client
    if not _credentials_configured():
        raise MediaUnavailableError("S3 credentials are not configured")
    if _client is None:
        try:
            _client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint,
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                config=_S3_CONFIG,
            )
        except ValueError as exc:
            # CR 2.4 патч 5: boto3 бросает `ValueError` (не `BotoCoreError`) на
            # некорректный endpoint — без схемы, с опечаткой в хосте. Ни
            # `put_object`, ни `get_signed_url` его не оборачивали, и роутер
            # отдавал `500` вместо обещанного `503 storage_unavailable`: это
            # ровно состояние деплоя, для которого `503` и заведён.
            raise MediaUnavailableError(f"S3 client is misconfigured: {exc}") from exc
    return _client


def reset_client() -> None:
    """Task 3.7: сброс кэша клиента — обязателен для изоляции тестов.

    Фикстура `fake_media` подменяет объект клиента. Без этой функции подмена
    сработала бы только в первом тесте модуля: кэш вернул бы настоящий
    boto3-клиент, и следующие тесты ушли бы в реальную сеть (в CI она запрещена).
    Вызывается и в `fake_media`, и в тестах, меняющих реквизиты.
    """
    global _client
    _client = None


def _storage_error(operation: str, exc: Exception) -> MediaStorageError:
    """Task 3.5: маппинг ошибок boto3 в доменную, с кодом S3 вместо текста.

    В лог идёт только `Error.Code` (`NoSuchKey`, `AccessDenied`): ключ объекта
    и presigned URL — не логируются никогда (D-11), а сообщение boto3 может
    содержать и то, и другое.
    """
    code = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
    logger.warning("media_storage_error", operation=operation, s3_code=code)
    return MediaStorageError(f"S3 {operation} failed: {code or type(exc).__name__}")


async def put_object(
    *,
    storage_key: str,
    fileobj: BinaryIO,
    content_type: str,
    size_bytes: int,
) -> None:
    """Task 3.2 (AC-02): загрузка файла в приватный бакет.

    `ACL` НЕ выставляется: приватность обеспечивается отсутствием публичной
    политики бакета, а не атрибутом объекта — `public-read` здесь был бы
    прямым нарушением FR58. `ServerSideEncryption` не задаётся: шифрование
    на стороне провайдера/диска ДЦ РФ (NFR11).

    `ContentLength` передаётся явно: boto3 иначе вычислил бы размер, прочитав
    поток, и файл читался бы дважды.

    Вызов в рабочем потоке (`anyio.to_thread.run_sync`): синхронный boto3
    заблокировал бы event loop на всё время передачи 5 МиБ.
    """
    client = _get_client()

    def _upload() -> None:
        client.put_object(
            Bucket=settings.s3_bucket,
            Key=storage_key,
            Body=fileobj,
            ContentType=content_type,
            ContentLength=size_bytes,
        )

    try:
        await anyio.to_thread.run_sync(_upload)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_error("put_object", exc) from exc


async def delete_object(storage_key: str) -> None:
    """Task 3.3 (D-10): удаление объекта, идемпотентное по контракту S3.

    S3 не отвечает ошибкой на отсутствующий ключ: повторное удаление после
    успешного — норма, а не сбой. Поэтому `404`/`NoSuchKey` не различаются
    и не пробрасываются.

    Вызывается уже после `commit` в роутере (D-10): строка в БД удалена,
    и ошибка хранилища логируется, но клиенту не возвращается.
    """
    client = _get_client()

    def _delete() -> None:
        client.delete_object(Bucket=settings.s3_bucket, Key=storage_key)

    try:
        await anyio.to_thread.run_sync(_delete)
    except (ClientError, BotoCoreError) as exc:
        raise _storage_error("delete_object", exc) from exc


def get_signed_url(storage_key: str, *, ttl: int | None = None) -> str:
    """Task 3.4 (AC-04, D-11): подписанная ссылка с ограниченным сроком.

    Подпись считается локально (`generate_presigned_url` не делает запроса
    к провайдеру), поэтому в поток функция не уходит: это CPU-работа на
    микросекунды, а `to_thread` добавил бы переключение контекста дороже
    самой операции.

    `ExpiresIn` берётся из `S3_SIGNED_URL_TTL` (900 с по умолчанию): клиент
    получает `expires_in` в ответе и обновляет ссылку до истечения.
    """
    client = _get_client()
    expires_in = settings.s3_signed_url_ttl if ttl is None else ttl
    try:
        url: str = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": storage_key},
            ExpiresIn=expires_in,
        )
    except (ClientError, BotoCoreError) as exc:
        raise _storage_error("generate_presigned_url", exc) from exc
    return url
