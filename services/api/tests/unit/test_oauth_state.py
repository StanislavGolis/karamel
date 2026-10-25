"""Unit-тесты `oauth:state` и PKCE (Task 10.1; AC-05).

Хранилище подменяется на in-memory реализацию: Redis в unit-тестах не нужен,
а `fakeredis` — лишняя зависимость ради двух методов (`set`/`getdel`).
"""

import pytest

from app.services.oauth_state import (
    InMemoryStateStore,
    code_challenge,
    consume_state,
    issue_state,
    new_code_verifier,
    set_state_store,
)


@pytest.fixture(autouse=True)
def _store() -> None:
    """Каждый тест получает чистое хранилище: состояние не протекает между тестами."""
    set_state_store(InMemoryStateStore())


async def test_issue_then_consume_returns_record() -> None:
    """AC-05: выпущенный `state` погашается один раз и отдаёт `code_verifier`."""
    issued = await issue_state("yandex")

    record = await consume_state(issued.state, "yandex")

    assert record is not None
    assert record.provider == "yandex"
    assert record.code_verifier


async def test_second_consume_returns_none() -> None:
    """AC-05: повторное погашение отклоняется — одноразовость через `GETDEL`."""
    issued = await issue_state("yandex")
    await consume_state(issued.state, "yandex")

    second = await consume_state(issued.state, "yandex")

    assert second is None


async def test_unknown_state_returns_none() -> None:
    """AC-05: неизвестный (подделанный) `state` отклоняется."""
    assert await consume_state("подделка", "yandex") is None


async def test_wrong_provider_returns_none_and_burns_state() -> None:
    """AC-05: чужой провайдер отклоняется, и запись всё равно погашена."""
    issued = await issue_state("yandex")

    wrong = await consume_state(issued.state, "vk")
    retry = await consume_state(issued.state, "yandex")

    assert wrong is None
    # Повтор с правильным провайдером уже невозможен: запись сгорела.
    assert retry is None


async def test_expired_state_returns_none() -> None:
    """AC-05: истёкший `state` отклоняется.

    Реализация Redis удаляет ключ по TTL сама; здесь проверяется, что отсутствие
    ключа трактуется как «нет записи», а не как исключение.
    """
    issued = await issue_state("vk")
    # Имитация истечения: погашаем запись первой попыткой.
    await consume_state(issued.state, "vk")

    assert await consume_state(issued.state, "vk") is None


def test_code_challenge_matches_rfc7636_vector() -> None:
    """Task 10.1 (AC-02): вектор из RFC 7636 Appendix B.

    `verifier` и ожидаемый `challenge` взяты из RFC дословно: если реализация
    разойдётся с ними, VK ID отклонит обмен кода с невнятной ошибкой провайдера.
    """
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_code_verifier_length_within_rfc_bounds() -> None:
    """Task 2.3: длина verifier в границах RFC 7636 (43..128)."""
    verifier = new_code_verifier()

    assert 43 <= len(verifier) <= 128
    # Повторы не совпадают — это не константа.
    assert verifier != new_code_verifier()
