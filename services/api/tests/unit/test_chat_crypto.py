"""Шифрование тела сообщения (стори 6.1; AC-04, NFR31).

§18.1: каждый тест краснеет от конкретной правки. AES-256-GCM выбран ради
АУТЕНТИФИКАЦИИ: без неё подмена байта шифртекста в дампе БД давала бы
валидный «другой текст», и получатель читал бы чужое сообщение как своё.

§18.11: булев признак (nonce уникален) проверяется в ОБОИХ значениях —
«два блоба одного текста различны» и «расшифровка обоих даёт исходный
текст». Без второго утверждения тест прошёл бы и при шифровании мусора.

CR 6.1: добавлены тесты AAD (перенос блоба между чатами и авторами),
верхней границы тела, длины ключа из параметра и не-UTF8 plaintext.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from app.core.crypto import (
    _NONCE_SIZE,
    _TAG_SIZE,
    MAX_BODY_BYTES,
    MIN_KEY_CHARS,
    decrypt_message_body,
    encrypt_message_body,
)

KEY = "unit_test_chat_message_key_at_least_32_chars"
OTHER_KEY = "another_unit_test_chat_key_at_least_32_chars"
CHAT_ID = 11
SENDER_ID = 22


def _encrypt(plaintext: str, *, chat_id: int = CHAT_ID, sender_id: int = SENDER_ID) -> bytes:
    return encrypt_message_body(plaintext, chat_id=chat_id, sender_id=sender_id, key=KEY)


def _decrypt(blob: bytes, *, chat_id: int = CHAT_ID, sender_id: int = SENDER_ID) -> str:
    return decrypt_message_body(blob, chat_id=chat_id, sender_id=sender_id, key=KEY)


@pytest.mark.parametrize(
    "plaintext",
    [
        "hello",
        "привет, как дела",
        "эмодзи 🔥🚀 и кириллица",
        "я",
        "д" * 4000,
    ],
)
def test_roundtrip_returns_original_text(plaintext: str) -> None:
    """AC-04: `decrypt(encrypt(x)) == x` для ASCII, кириллицы, эмодзи, длин.

    Краснеет, если ключ выводится недетерминированно (например, с случайной
    солью внутри `_derive_key`) или если nonce отрезается неверной длины.
    """
    assert _decrypt(_encrypt(plaintext)) == plaintext


def test_same_text_produces_different_blobs() -> None:
    """AC-04: nonce новый на КАЖДОЕ сообщение — блобы одного текста различны.

    Краснеет, если nonce станет константой или производной от текста: при GCM
    повтор nonce с одним ключом раскрывает XOR двух plaintext'ов.
    """
    first = _encrypt("одно и то же")
    second = _encrypt("одно и то же")

    assert first != second
    # §18.11: оба блоба обязаны РАСШИФРОВЫВАТЬСЯ в исходный текст.
    assert _decrypt(first) == "одно и то же"
    assert _decrypt(second) == "одно и то же"


def test_blob_layout_is_nonce_then_ciphertext_then_tag() -> None:
    """AC-04: длина блоба = 12 (nonce) + len(utf8) + 16 (тег GCM).

    Краснеет при смене формата (например, base64 поверх блоба или nonce в
    отдельном поле): формат — часть контракта с колонкой `messages.body`.
    """
    plaintext = "привет"

    assert len(_encrypt(plaintext)) == (_NONCE_SIZE + len(plaintext.encode("utf-8")) + _TAG_SIZE)


def test_plaintext_is_not_present_in_blob() -> None:
    """AC-04, NFR31: plaintext в блобе отсутствует.

    Краснеет, если шифрование подменено кодированием (`utf-8`/base64): тогда
    дамп БД содержал бы читаемый текст переписки.
    """
    plaintext = "секретное сообщение"

    assert plaintext.encode("utf-8") not in _encrypt(plaintext)


@pytest.mark.parametrize("position", [0, _NONCE_SIZE, -1])
def test_modified_byte_breaks_decryption(position: int) -> None:
    """AC-04: подмена байта nonce / шифртекста / тега → `InvalidTag`.

    Краснеет, если режим заменить на неаутентифицированный (CTR/CBC): тогда
    испорченный блоб расшифровался бы в мусор без ошибки.
    """
    blob = bytearray(_encrypt("текст для подмены"))
    blob[position] ^= 0xFF

    with pytest.raises(InvalidTag):
        _decrypt(bytes(blob))


@pytest.mark.parametrize(
    ("chat_id", "sender_id"),
    [
        (CHAT_ID + 1, SENDER_ID),  # другой чат — перенос в чужую переписку
        (CHAT_ID, SENDER_ID + 1),  # другой автор — приписать себе чужое слово
    ],
)
def test_blob_cannot_be_moved_between_chats_or_authors(chat_id: int, sender_id: int) -> None:
    """CR 6.1 (AAD): блоб, вставленный в другой чат/от другого автора, не читается.

    Краснеет, если AAD убрать (`None`): тег валиден, и получатель прочитал бы
    чужое сообщение как своё — ровно тот обход, ради которого выбран GCM.
    §18.11: та же пара значений проверяется и в «своём» чате (roundtrip-тест).
    """
    blob = _encrypt("только для своего чата")

    with pytest.raises(InvalidTag):
        _decrypt(blob, chat_id=chat_id, sender_id=sender_id)


def test_other_key_cannot_decrypt() -> None:
    """AC-04, NFR31: ключ из настроек — единственный, кто читает переписку.

    Краснеет, если ключ игнорируется и берётся константой модуля: смена
    `CHAT_MESSAGE_KEY` не влияла бы ни на что.
    """
    blob = _encrypt("только для своего ключа")

    with pytest.raises(InvalidTag):
        decrypt_message_body(blob, chat_id=CHAT_ID, sender_id=SENDER_ID, key=OTHER_KEY)


@pytest.mark.parametrize("key", ["", "   ", "x" * (MIN_KEY_CHARS - 1), "short"])
def test_short_key_is_rejected_by_crypto_itself(key: str) -> None:
    """CR 6.1: порог длины ключа проверяется В САМОМ модуле, не только в конфиге.

    Краснеет, если проверка живёт только в валидаторе настроек: `key="a"`
    дал бы полноценный AES-256-ключ из одного символа (перебор — секунды).
    """
    with pytest.raises(ValueError):
        encrypt_message_body("текст", chat_id=CHAT_ID, sender_id=SENDER_ID, key=key)


def test_key_with_surrounding_whitespace_is_normalized() -> None:
    """CR 6.1: `strip()` в `_derive_key` совпадает с нормализацией валидатора.

    Краснеет, если `_derive_key` хеширует исходную строку: `CHAT_MESSAGE_KEY`
    из `.env` с завершающим `\\n` давал бы ключ, отличный от того же секрета
    без перевода строки, и старые сообщения стали бы нечитаемыми.
    """
    blob = encrypt_message_body("текст", chat_id=CHAT_ID, sender_id=SENDER_ID, key=f"  {KEY}  ")

    assert _decrypt(blob) == "текст"


def test_empty_plaintext_is_rejected() -> None:
    """AC-04: пустое сообщение не шифруется.

    Краснеет, если убрать проверку: в БД появился бы блоб из nonce+tag,
    неотличимый по смыслу от «пустого сообщения».
    """
    with pytest.raises(ValueError):
        _encrypt("")


def test_body_over_upper_bound_is_rejected() -> None:
    """CR 6.1: тело длиннее `MAX_BODY_BYTES` отвергается ДО записи в БД.

    Краснеет, если проверки нет: сообщение на сотни мегабайт прошло бы схему
    и `BYTEA`, и страница истории тянула бы его целиком в память.
    """
    assert MAX_BODY_BYTES > 0
    with pytest.raises(ValueError):
        _encrypt("д" * (MAX_BODY_BYTES + 1))


def test_body_of_exactly_upper_bound_is_accepted() -> None:
    """CR 6.1, §18.11: ровно `MAX_BODY_BYTES` — предельное тело проходит.

    Краснеет при сдвиге границы (`>=` вместо `>`): максимальное законное
    сообщение отвергалось бы.
    """
    plaintext = "a" * MAX_BODY_BYTES

    assert _decrypt(_encrypt(plaintext)) == plaintext


@pytest.mark.parametrize("size", [0, _NONCE_SIZE + _TAG_SIZE - 1, _NONCE_SIZE + _TAG_SIZE])
def test_short_blob_is_value_error_not_invalid_tag(size: int) -> None:
    """AC-04: «битые данные» и «неверный ключ» — РАЗНЫЕ ошибки.

    Краснеет, если короткий блоб отдавать в AESGCM: библиотека бросила бы
    `InvalidTag`, и вызывающий код (6.4) принял бы обрезанную строку за
    подделку с чужим ключом.
    Вход — `b"\\x00" * size`, а не `b"" * size`: пустая строка при умножении
    остаётся пустой, и все три случая проверяли бы одну ветку (CR 6.1,
    тавтология найдена аудитом).
    """
    with pytest.raises(ValueError):
        _decrypt(b"\x00" * size)


def test_blob_of_one_byte_over_minimum_is_invalid_tag_not_value_error() -> None:
    """AC-04, §18.11: 29 байт — уже «шифртекст», но с неверным тегом.

    Краснеет, если границу сдвинуть (`<= 29`): испорченный блоб сообщения
    из одного байта отдавался бы как «битые данные» вместо подделки, и 6.4
    отвечал бы разными кодами на одно и то же состояние.
    """
    with pytest.raises(InvalidTag):
        _decrypt(b"\x00" * (_NONCE_SIZE + _TAG_SIZE + 1))


def test_non_utf8_plaintext_is_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR 6.1: не-UTF8 plaintext после валидного тега → `ValueError`, не `500`.

    Краснеет, если `decode` не обёрнут: `UnicodeDecodeError` — не доменная
    ошибка, и страница истории падала бы с 500 на каждом чтении такого блоба.
    """
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.core import crypto

    aesgcm = AESGCM(hashlib.sha256(KEY.encode()).digest())
    nonce = b"\x01" * _NONCE_SIZE
    sealed = aesgcm.encrypt(nonce, b"\xff\xfe", crypto._aad(chat_id=CHAT_ID, sender_id=SENDER_ID))

    with pytest.raises(ValueError):
        _decrypt(nonce + sealed)


def test_key_is_taken_from_settings_by_default() -> None:
    """AC-04, NFR31: без явного `key` берётся `Settings.chat_message_key`.

    Краснеет, если дефолт останется константой модуля: ключ из окружения
    перестал бы защищать переписку (NFR31 требует «ключ вне БД и вне кода»).
    """
    from app.core.config import get_settings

    blob = encrypt_message_body("ключ из настроек", chat_id=CHAT_ID, sender_id=SENDER_ID)

    assert decrypt_message_body(blob, chat_id=CHAT_ID, sender_id=SENDER_ID) == ("ключ из настроек")
    # Разные ключи: блоб, снятый ключом настроек, не читается тестовым ключом.
    with pytest.raises(InvalidTag):
        _decrypt(blob)
    assert get_settings().chat_message_key != KEY
