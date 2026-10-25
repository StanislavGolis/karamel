"""Валидация фото по сигнатуре (Task 9.1; AC-05, AC-06).

§18.1: каждый тест падает без реализации. Если убрать проверку сигнатуры,
`test_text_with_image_mime_is_rejected` становится красным: текст с
заявленным `image/jpeg` прошёл бы как изображение (D-5). Если убрать
проверку размера — красным становится тест границы.

§18.2: проверяется конкретный `code` и конкретный `status_code`, а не факт
исключения: `photo_not_image` и `photo_empty` — разные тексты в UI.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.photo_validation import (
    ALLOWED_CONTENT_TYPES,
    PhotoConsentRequiredError,
    PhotoValidationError,
    detect_content_type,
    ensure_consent,
    extension_for,
    validate_photo,
)

# Настоящие сигнатуры форматов: проверяется то, что реально приходит с телефона,
# а не выдуманная строка.
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP_HEAD = b"RIFF\x24\x00\x00\x00WEBPVP8 "
# HEIC из iPhone: `ftypheic` по смещению 4. На пилоте не принимается (D-5).
HEIC_HEAD = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"


@pytest.mark.parametrize(
    ("head", "expected"),
    [(JPEG_HEAD, "image/jpeg"), (PNG_HEAD, "image/png"), (WEBP_HEAD, "image/webp")],
)
def test_signature_detected(head: bytes, expected: str) -> None:
    """AC-05, D-5: JPEG/PNG/WebP распознаются по первым байтам."""
    assert detect_content_type(head) == expected


@pytest.mark.parametrize("head", [HEIC_HEAD, b"%PDF-1.7\n", b"GIF89a", b"", b"not-an-image"])
def test_non_image_rejected(head: bytes) -> None:
    """AC-05, D-5: PDF, GIF и HEIC не проходят — сигнатуры нет.

    HEIC проверяется отдельно от «прочего мусора»: его шлёт iPhone по
    умолчанию, и он должен отбиваться осознанно, а не случайно.
    """
    assert detect_content_type(head) is None

    with pytest.raises(PhotoValidationError) as exc:
        validate_photo(head=head, size_bytes=len(head) + 100, declared_content_type="image/jpeg")

    assert exc.value.code == "photo_not_image"
    assert exc.value.status_code == 422


def test_text_with_image_mime_is_rejected() -> None:
    """AC-05, D-5: заявленному `content_type` верить нельзя.

    Если проверку сигнатуры убрать и довериться заголовку multipart, этот
    тест станет красным: текст уйдёт в бакет с `image/jpeg`.
    """
    with pytest.raises(PhotoValidationError) as exc:
        validate_photo(
            head=b"<?php echo 1; ?>",
            size_bytes=1024,
            declared_content_type="image/jpeg",
        )

    assert exc.value.code == "photo_not_image"


def test_zero_bytes_is_photo_empty() -> None:
    """AC-05: нулевой файл — `422 photo_empty`, не `photo_not_image`.

    Пустой файл отбивается ДО проверки сигнатуры: иначе пользователь получил
    бы «это не изображение» на файле, который не загрузился вообще.
    """
    with pytest.raises(PhotoValidationError) as exc:
        validate_photo(head=b"", size_bytes=0, declared_content_type="image/jpeg")

    assert exc.value.code == "photo_empty"
    assert exc.value.status_code == 422


def test_size_over_limit_is_413(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-05, AC-08: `max_photo_bytes + 1` — `413 request_too_large`.

    Код тот же, что у гарда тела: клиенту не важно, какой рубеж его отбил.
    """
    monkeypatch.setattr(settings, "max_photo_bytes", 1024)

    with pytest.raises(PhotoValidationError) as exc:
        validate_photo(head=JPEG_HEAD, size_bytes=1025, declared_content_type="image/jpeg")

    assert exc.value.code == "request_too_large"
    assert exc.value.status_code == 413


def test_size_exactly_limit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-05: файл ровно в лимит проходит — иначе легальный файл отбивался бы.

    Без этого теста проверка `>=` вместо `>` не ловится: она отбивала бы
    файл ровно в `MAX_PHOTO_BYTES`, а такой файл легален.
    """
    monkeypatch.setattr(settings, "max_photo_bytes", 1024)

    result = validate_photo(head=JPEG_HEAD, size_bytes=1024, declared_content_type="image/jpeg")

    assert result.content_type == "image/jpeg"
    assert result.size_bytes == 1024
    assert result.extension == "jpg"


def test_detected_type_wins_over_declared() -> None:
    """AC-05, D-5: типом считается найденная сигнатура, а не заявление клиента.

    Клиент прислал PNG с `content_type=image/jpeg` — сохраняется `image/png`,
    иначе бакет и БД разошлись бы с содержимым файла.
    """
    result = validate_photo(
        head=PNG_HEAD, size_bytes=len(PNG_HEAD) + 10, declared_content_type="image/jpeg"
    )

    assert result.content_type == "image/png"
    assert result.extension == "png"


def test_allowed_content_types_match_extensions() -> None:
    """AC-05, D-3: у каждого разрешённого MIME есть расширение для ключа.

    Без этой проверки новый MIME в allowlist дал бы `KeyError` при построении
    `storage_key` — то есть `500` на легальной загрузке.
    """
    assert set(ALLOWED_CONTENT_TYPES) == {"image/jpeg", "image/png", "image/webp"}
    for content_type in ALLOWED_CONTENT_TYPES:
        assert extension_for(content_type) in {"jpg", "png", "webp"}


# --- AC-06: согласие ------------------------------------------------------------


def test_consent_false_raises_photo_consent_required() -> None:
    """AC-06: без согласия — `PhotoConsentRequiredError` до любого S3.

    Без этой проверки файл уже лежал бы в бакете к моменту отказа, и AC-06
    («факт согласия не фиксируется») нарушался бы сиротским объектом.
    """
    with pytest.raises(PhotoConsentRequiredError):
        ensure_consent(False)


def test_consent_true_passes() -> None:
    """AC-06: отмеченный чекбокс пропускает дальше."""
    ensure_consent(True)
