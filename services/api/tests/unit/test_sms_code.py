"""Unit-тесты кода подтверждения (Task 9.2; AC-01, AC-02, AC-03).

D-2: в хранилище лежит HMAC-хеш, не код. D-3: одноразовость через `DEL` после
сверки — неверный ввод не должен сжигать код.
"""

import pytest

from app.core.config import settings
from app.services.sms_code import (
    CooldownActiveError,
    VerifyResult,
    generate_code,
    hash_code,
    issue_code,
    verify_code,
)
from app.services.sms_store import InMemorySmsStore, get_sms_store, set_sms_store

PHONE = "+79001234567"
OTHER_PHONE = "+79007654321"


@pytest.fixture(autouse=True)
def _store() -> None:
    """Чистое хранилище на каждый тест."""
    set_sms_store(InMemorySmsStore())


def test_generate_code_is_six_digits() -> None:
    """AC-01: код — 6 цифр; ведущие нули сохраняются (`zfill`, не `int`)."""
    for _ in range(50):
        code = generate_code()
        assert len(code) == 6
        assert code.isdigit()


def test_hash_is_deterministic_and_code_specific() -> None:
    """D-2: хеш детерминирован, но зависит и от телефона, и от кода.

    Зависимость от телефона не даёт переиспользовать хеш чужого кода:
    один и тот же `123456` для разных номеров даёт разные значения.
    """
    first = hash_code(PHONE, "123456")

    assert first == hash_code(PHONE, "123456")
    assert first != hash_code(PHONE, "123457")
    assert first != hash_code(OTHER_PHONE, "123456")


def test_hash_reveals_no_plain_code() -> None:
    """D-2: в значении хеша нет открытого кода и длина фиксирована."""
    hashed = hash_code(PHONE, "123456")

    assert "123456" not in hashed
    assert len(hashed) == 64  # SHA-256 hex


async def test_issue_code_stores_hash_not_plain_code() -> None:
    """AC-01: в хранилище хеш; открытый код возвращается только вызывающему."""
    issued = await issue_code(PHONE)

    stored = await get_store_code(PHONE)
    assert stored is not None
    assert stored != issued.code
    assert stored == hash_code(PHONE, issued.code)
    assert issued.ttl_seconds == settings.sms_code_ttl_seconds


async def test_verify_ok_consumes_code() -> None:
    """AC-02: верный код → OK, и код удалён (повтор невозможен)."""
    issued = await issue_code(PHONE)

    result = await verify_code(PHONE, issued.code)

    assert result is VerifyResult.OK
    assert await get_store_code(PHONE) is None


async def test_verify_twice_second_time_expired() -> None:
    """AC-02, D-3: повторная проверка того же кода — код уже погашен."""
    issued = await issue_code(PHONE)
    assert await verify_code(PHONE, issued.code) is VerifyResult.OK

    assert await verify_code(PHONE, issued.code) is VerifyResult.EXPIRED


async def test_verify_without_code_is_expired() -> None:
    """AC-03: кода нет (истёк или не запрашивался) → EXPIRED, а не INVALID."""
    assert await verify_code(PHONE, "123456") is VerifyResult.EXPIRED


async def test_verify_wrong_code_keeps_code_alive() -> None:
    """D-3: неверный ввод не сжигает код — иначе опечатка = новый запрос."""
    issued = await issue_code(PHONE)

    first = await verify_code(PHONE, "000000" if issued.code != "000000" else "111111")
    second = await verify_code(PHONE, issued.code)

    assert first is VerifyResult.INVALID
    assert second is VerifyResult.OK


async def test_verify_attempts_exhausted_keeps_code() -> None:
    """AC-03: после исчерпания попыток код НЕ удаляется.

    Удаление давало внешнему вызывающему возможность пятью запросами с мусорным
    кодом сжечь живой код жертвы и заблокировать ей вход навсегда.
    Исчерпание блокирует проверку, но не уничтожает выданный код.
    """
    issued = await issue_code(PHONE)
    wrong = "000000" if issued.code != "000000" else "111111"
    max_attempts = settings.sms_code_max_attempts

    results = [await verify_code(PHONE, wrong) for _ in range(max_attempts)]
    final = await verify_code(PHONE, issued.code)

    # Первые max_attempts попыток сверяются (последняя ещё INVALID, не EXCEEDED).
    assert all(r is VerifyResult.INVALID for r in results)
    # Следующая попытка упирается в потолок — и с верным кодом тоже.
    assert final is VerifyResult.ATTEMPTS_EXCEEDED
    # Код остался в хранилище: сжечь его снаружи нельзя.
    assert await get_store_code(PHONE) is not None


async def test_verify_other_phone_code_rejected() -> None:
    """D-2: хеш привязан к телефону — код одного номера не подходит другому."""
    issued = await issue_code(PHONE)

    assert await verify_code(OTHER_PHONE, issued.code) is VerifyResult.EXPIRED


async def test_issue_code_respects_cooldown() -> None:
    """AC-01: повторная выдача раньше cooldown отклоняется с остатком."""
    await issue_code(PHONE)

    with pytest.raises(CooldownActiveError) as exc_info:
        await issue_code(PHONE)

    assert exc_info.value.retry_after > 0


async def get_store_code(phone: str) -> str | None:
    """Читает сырое значение из подменённого хранилища."""
    return await get_sms_store().get_code(phone)
