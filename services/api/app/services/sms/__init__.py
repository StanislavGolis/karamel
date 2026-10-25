"""Клиенты SMS-провайдеров (Task 5; AC-01, AC-07).

Пакет повторяет устройство `services/oauth/`: протокол, реализация провайдера
и общий HTTP-пул. Роут знает только `SmsProvider` и `SmsProviderError`, поэтому
смена провайдера = новый файл рядом, без правок роутов и сервисов.

D-1: провайдер — SMS.ru. Причина выбора: российский сервис (данные в РФ,
152-ФЗ), работает у физлица без ОКУП и постоплаты, простой HTTP API.
"""

from __future__ import annotations

from typing import Literal, Protocol

from app.core.config import settings

# D-4: `stage` — машинный контракт лога. `provider` отличает отказ провайдера
# от ошибок нашего кода (`request`/`verify`).
ProviderStage = Literal["request", "verify", "provider"]


class SmsProviderError(Exception):
    """Отказ отправки SMS.

    Наружу уходит только `503 sms_unavailable`: код провайдера (`sms_invalid_key`,
    `sms_no_funds`) — для лога и для владельца, но не для пользователя. Иначе
    клиент узнаёт про состояние нашего счёта и ключей.

    `status_text` провайдера **не** сохраняется: он может содержать `api_id`
    или часть номера, а исключение попадает в трейсбек и в лог ошибок.
    """

    def __init__(self, *, stage: ProviderStage, error_code: str) -> None:
        super().__init__(f"sms provider error at {stage}: {error_code}")
        self.stage = stage
        self.error_code = error_code


class SmsProvider(Protocol):
    """Контракт провайдера: отправить текст на номер.

    Принимает нормализованный E.164 (`+7…`), а приведение к формату провайдера —
    забота реализации: у SMS.ru `to` без `+`, и это не должно протекать в сервис.
    """

    async def send_code(self, phone: str, text: str) -> None: ...


def get_sms_provider() -> SmsProvider:
    """Фабрика по настройке `SMS_PROVIDER`.

    Импорт внутри функции: модули провайдеров тянут этот пакет за типами,
    и импорт на уровне модуля дал бы цикл.
    """
    if settings.sms_provider == "stub":
        from app.services.sms.stub import StubSmsProvider

        return StubSmsProvider()
    if settings.sms_provider != "smsru":
        # Молчаливый фолбэк на платный SMS.ru означал бы, что опечатка в env
        # (`stub `, `Stub`, `test`) тратит реальные деньги и шлёт SMS
        # на произвольные номера с dev-машины.
        raise RuntimeError(f"Неизвестный SMS_PROVIDER: {settings.sms_provider!r}")

    from app.services.sms.smsru import SmsRuProvider

    return SmsRuProvider()
