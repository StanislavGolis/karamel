"""AC-05: примитивы refresh-токена — хеш и генерация."""

from app.core.security import REFRESH_TOKEN_HASH_LEN, hash_refresh_token, new_refresh_token


def test_hash_is_deterministic() -> None:
    """AC-05: один токен → один и тот же хеш (поиск по индексу возможен)."""
    raw = "token-value"

    assert hash_refresh_token(raw) == hash_refresh_token(raw)


def test_hash_length_is_32_bytes() -> None:
    """AC-05: длина дайджеста SHA-256 — 32 байта, как в `token_hash bytea(32)`."""
    digest = hash_refresh_token(new_refresh_token())

    assert isinstance(digest, bytes)
    assert len(digest) == REFRESH_TOKEN_HASH_LEN == 32


def test_different_tokens_give_different_hashes() -> None:
    """AC-05: коллизий на разных значениях нет."""
    assert hash_refresh_token("a") != hash_refresh_token("b")


def test_new_token_is_long_enough() -> None:
    """AC-05: 48 байт энтропии → не менее 48 символов в base64url."""
    token = new_refresh_token()

    assert len(token) >= 48


def test_new_token_is_unique() -> None:
    """AC-05: 100 вызовов не дают повторов."""
    tokens = {new_refresh_token() for _ in range(100)}

    assert len(tokens) == 100


def test_hash_is_not_reversible_to_token() -> None:
    """AC-05: хеш не содержит открытого токена.

    Проверка через равенство, а не через `in`: 64-байтовый токен не может быть
    подстрокой 32-байтового дайджеста ни при каком хеше — такая проверка тавтологична.
    """
    token = new_refresh_token()
    digest = hash_refresh_token(token)

    assert digest != token.encode("utf-8")
    assert hash_refresh_token(token) == digest


def test_hash_differs_from_hex_representation() -> None:
    """AC-05: в БД лежит сырой дайджест, а не hex-строка (32 байта, не 64)."""
    digest = hash_refresh_token(new_refresh_token())

    assert len(digest) == 32
    assert digest != digest.hex().encode("ascii")
