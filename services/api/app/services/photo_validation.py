"""Валидация загружаемого фото (Task 4.1–4.5, AC-05, AC-06).

D-5: тип файла определяется по сигнатуре первых байт, а не по
`content_type` из multipart. Заявленный MIME — это строка, которую клиент
прислал сам: `curl -F 'file=@shell.php;type=image/jpeg'` проходит любую
проверку заголовка. `Pillow` не вводится сознательно: декодирование 5 МиБ
может дать декомпрессионную бомбу (PNG 100×100 КБ разворачивается в гигабайты)
и лишние 20+ МиБ в контейнере с `mem_limit: 384m`. Реальная проверка
содержимого — на модерации (2.4).

D-5: HEIC/HEIF из iPhone на пилоте НЕ принимаются. Это осознанное
ограничение, а не дефект: клиент конвертирует в JPEG перед загрузкой.
Расширение конвертера (`heif-convert`) в контейнер не тянем — он требует
libheif и добавляет десятки мегабайт ради одного формата.

D-3: имя исходного файла не используется ни здесь, ни в `storage_key`:
в имени бывают ФИО, телефоны и `../`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

# AC-05, D-5: allowlist типов. Совпадает с CHECK `ck_profile_photos_content_type`
# в БД — расхождение дало бы `IntegrityError` после успешной валидации.
ALLOWED_CONTENT_TYPES: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")

# D-3: расширение выбирается из проверенного MIME, а не из имени файла:
# `photo.exe` с сигнатурой JPEG получит `.jpg`, а не сохранит `.exe` в ключе.
_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# AC-05: сигнатуры форматов. Проверяются первые байты, поэтому достаточно
# префикса. WebP — RIFF-контейнер: на позиции 8 обязано быть `WEBP`, иначе
# любой WAV/AVI с меткой `RIFF` прошёл бы как изображение.
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RIFF_SIGNATURE = b"RIFF"
_WEBP_MARKER = b"WEBP"


def extension_for(content_type: str) -> str:
    """Расширение для `storage_key` из проверенного MIME (D-3).

    Имя файла не участвует: `IMG_0001.HEIC` с сигнатурой JPEG даёт `.jpg`.
    """
    return _EXTENSIONS[content_type]


def detect_content_type(head: bytes) -> str | None:
    """AC-05, D-5: тип по сигнатуре или `None`, если это не изображение.

    Порядок проверок не важен (сигнатуры не пересекаются), но WebP требует
    двух условий: `RIFF` в начале и `WEBP` по смещению 8. Одна проверка
    `RIFF` пропустила бы видео/аудио RIFF-контейнеры.
    """
    if head.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if head.startswith(_PNG_SIGNATURE):
        return "image/png"
    if head.startswith(_RIFF_SIGNATURE) and head[8:12] == _WEBP_MARKER:
        return "image/webp"
    return None


@dataclass(frozen=True, slots=True)
class ValidatedPhoto:
    """Результат валидации: проверенный MIME, размер и расширение для ключа."""

    content_type: str
    size_bytes: int
    extension: str


class PhotoValidationError(Exception):
    """AC-05: доменная ошибка файла — `422 <code>` или `413` в роутере.

    Код в атрибуте, а не в тексте: HTTP-слой отдаёт `{"code": ...}`,
    и клиент выбирает текст по коду.
    """

    def __init__(self, code: str, status_code: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class PhotoConsentRequiredError(Exception):
    """AC-06: согласие на показ фото не отмечено — `422 photo_consent_required`.

    Отдельный тип от `PhotoValidationError`: согласие проверяется ДО
    обращения к S3 (Task 4.4), и эта ошибка не про содержимое файла.
    """


def validate_photo(
    *, head: bytes, size_bytes: int, declared_content_type: str | None
) -> ValidatedPhoto:
    """Task 4.1–4.3, 4.5 (AC-05): сигнатура, размер, MIME.

    Порядок проверок важен для кода ошибки:
      1. размер — `413 request_too_large` (согласовано с гардом тела, AC-08)
         и `422 photo_empty` для нуля байт;
      2. сигнатура — `422 photo_not_image`.

    `declared_content_type` принимается только для диагностики и не влияет
    на результат: верить заявлению клиента нельзя (D-5). Он есть в сигнатуре,
    потому что вызывающий код его знает, и при расхождении полезно видеть
    в логе, что именно заявил клиент.
    """
    del declared_content_type  # D-5: на решение не влияет, см. docstring.

    if size_bytes <= 0:
        raise PhotoValidationError("photo_empty", status_code=422)
    if size_bytes > settings.max_photo_bytes:
        # AC-05, AC-08: тот же код, что у гарда тела — клиенту не важно,
        # какой из двух рубежей его отбил.
        raise PhotoValidationError("request_too_large", status_code=413)

    detected = detect_content_type(head)
    if detected is None:
        raise PhotoValidationError("photo_not_image", status_code=422)

    return ValidatedPhoto(
        content_type=detected,
        size_bytes=size_bytes,
        extension=extension_for(detected),
    )


def ensure_consent(consent: bool) -> None:
    """Task 4.4 (AC-06): согласие проверяется ДО обращения к S3.

    Без этой проверки файл уже лежал бы в бакете к моменту отказа, и
    AC-06 («загрузка недоступна, факт согласия не фиксируется») нарушался бы:
    объект загружен, строки нет — сирота, которую никто не уберёт до эпика 8.
    """
    if not consent:
        raise PhotoConsentRequiredError
