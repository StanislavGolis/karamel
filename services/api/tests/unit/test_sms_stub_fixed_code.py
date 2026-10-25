"""Dev-заглушка фиксированного SMS-кода (владелец, 2026-10-25).

Зачем: в пилоте нет настоящих SMS (`SMS_PROVIDER=stub`), и сгенерированный код
каждый раз приходится искать в логе контейнера. Фиксированный код убирает этот
шаг при ручном тестировании входа по телефону.

Что здесь проверяется и почему это не формальность:

* заглушка **включается** только явной настройкой и только для указанных
  номеров — иначе она бы молча ломала генерацию у всех;
* заглушка **не отключает** проверку кода: неверный код по-прежнему `INVALID`,
  код по-прежнему гасится после успеха и живёт по TTL. Фиксация источника
  значения не должна превращаться в «принимаем что угодно»;
* телефон в списке можно записать в любой форме (`8923…`, `+7 923 …`), потому
  что заполняет его человек, а сравнивается нормализованный номер.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.sms_code import VerifyResult, issue_code, verify_code
from app.services.sms_store import InMemorySmsStore, get_sms_store, set_sms_store

#: Номер владельца в нормализованном виде (вход: `89234490800`).
PINNED_PHONE = "+79234490800"
#: Номер вне списка заглушки.
OTHER_PHONE = "+79001234567"
FIXED_CODE = "111111"


@pytest.fixture(autouse=True)
def _store() -> None:
    """Чистое хранилище на каждый тест: cooldown и коды не перетекают."""
    set_sms_store(InMemorySmsStore())


@pytest.fixture
def fixed_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включает заглушку для одного номера, не трогая `.env` разработчика."""
    monkeypatch.setattr(settings, "sms_stub_fixed_code", FIXED_CODE)
    monkeypatch.setattr(settings, "sms_stub_fixed_phones", PINNED_PHONE)


async def test_pinned_phone_gets_fixed_code(fixed_stub: None) -> None:
    """AC: номер в списке получает ровно заданный код, а не генерацию."""
    issued = await issue_code(PINNED_PHONE)

    assert issued.code == FIXED_CODE


async def test_other_phone_gets_generated_code(fixed_stub: None) -> None:
    """AC: номер вне списка — обычная генерация, и это не фиксированный код.

    Краснеет, если заглушка начнёт срабатывать на всех подряд: тогда любой
    вход локально шёл бы по одному коду и тест на генерацию ничего не ловил.
    """
    issued = await issue_code(OTHER_PHONE)

    assert issued.code != FIXED_CODE
    assert len(issued.code) == 6
    assert issued.code.isdigit()


async def test_empty_phone_list_applies_to_any_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC: пустой список номеров — код действует для любого номера.

    Это осознанное удобство ручного теста: не нужно помнить нормализованную
    запись номера, чтобы получить фиксированный код.
    """
    monkeypatch.setattr(settings, "sms_stub_fixed_code", FIXED_CODE)
    monkeypatch.setattr(settings, "sms_stub_fixed_phones", "")

    first = await issue_code(PINNED_PHONE)
    set_sms_store(InMemorySmsStore())
    second = await issue_code(OTHER_PHONE)

    assert first.code == FIXED_CODE
    assert second.code == FIXED_CODE


async def test_phone_list_accepts_national_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC: список нормализуется — `8923…` и `+7 923 …` это один номер.

    Краснеет, если сравнивать строки как есть: человек заполняет `.env`
    в привычной записи, а `issue_code` получает `+7…`, и заглушка «не работает»
    без единой ошибки в логе.
    """
    monkeypatch.setattr(settings, "sms_stub_fixed_code", FIXED_CODE)
    monkeypatch.setattr(settings, "sms_stub_fixed_phones", "89234490800")

    issued = await issue_code(PINNED_PHONE)

    assert issued.code == FIXED_CODE


async def test_verify_with_fixed_code_ok_and_one_time(fixed_stub: None) -> None:
    """AC: полный цикл issue → verify даёт OK, и код гасится после успеха.

    Проверяются сразу два требования: фиксация не ломает вход и одноразовость
    (`consume_code`) остаётся в силе.
    """
    issued = await issue_code(PINNED_PHONE)

    assert await verify_code(PINNED_PHONE, issued.code) is VerifyResult.OK
    # Повтор тем же кодом — уже EXPIRED: код погашен, как и при генерации.
    assert await verify_code(PINNED_PHONE, issued.code) is VerifyResult.EXPIRED


async def test_verify_wrong_code_still_invalid(fixed_stub: None) -> None:
    """AC: фиксация не отключает сверку — неверный код остаётся INVALID.

    Это главный тест на безопасность заглушки: если бы `verify_code` принимал
    любой ввод при заданном фиксированном коде, локальная конфигурация
    маскировала бы дефект проверки, и он всплыл бы только в проде.
    """
    await issue_code(PINNED_PHONE)

    assert await verify_code(PINNED_PHONE, "000000") is VerifyResult.INVALID


async def test_fixed_code_respects_ttl(fixed_stub: None) -> None:
    """AC: TTL прежний — фиксированный код живёт столько же, сколько обычный."""
    issued = await issue_code(PINNED_PHONE)

    assert issued.ttl_seconds == settings.sms_code_ttl_seconds
    stored = await get_sms_store().get_code(PINNED_PHONE)
    assert stored is not None


async def test_disabled_stub_keeps_plain_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC: пустой `SMS_STUB_FIXED_CODE` — обычная генерация для любого номера."""
    monkeypatch.setattr(settings, "sms_stub_fixed_code", "")
    monkeypatch.setattr(settings, "sms_stub_fixed_phones", "")

    issued = await issue_code(PINNED_PHONE)

    assert issued.code != FIXED_CODE
    assert len(issued.code) == 6