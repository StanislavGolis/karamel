"""Клиент SMS.ru (Task 5.2–5.5; AC-01, AC-07).

Контракт провайдера проверен `curl` 2026-10-21:
`POST https://sms.ru/sms/send` с фиктивным `api_id` вернул HTTP **200** и тело
`{"status":"ERROR","status_code":200,"status_text":"Неправильный api_id"}`.
Отсюда два следствия, заложенные в код:

1. HTTP-код не говорит об успехе — смотреть надо `status` в теле;
2. `status_text` содержит текст провайдера и может раскрывать `api_id`, поэтому
   в исключение и в лог он не копируется: только машинный код.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.services.oauth import HTTP_TIMEOUT, _transport_error_code, get_http_client
from app.services.sms import SmsProviderError

SEND_URL = "https://sms.ru/sms/send"

# Коды SMS.ru, которые мы различаем. Всё остальное — общий `sms_provider_error`:
# список кодов провайдера меняется, и заводить по коду на каждый бессмысленно.
_STATUS_OK = 100
_STATUS_INVALID_KEY = 200
_STATUS_NO_FUNDS = 201

_ERROR_CODES: dict[int, str] = {
    _STATUS_INVALID_KEY: "sms_invalid_key",
    _STATUS_NO_FUNDS: "sms_no_funds",
}


def _to_provider_number(phone: str) -> str:
    """AC-01: `to` без `+`.

    SMS.ru принимает номер в национальной записи (`79001234567`); ведущий `+`
    даёт ошибку формата, которую легко спутать с отказом провайдера.
    """
    return phone.lstrip("+")


def _error_code_for(status_code: object) -> str:
    """Машинный код по `status_code` провайдера. Неизвестное — общий."""
    if isinstance(status_code, int):
        return _ERROR_CODES.get(status_code, "sms_provider_error")
    return "sms_provider_error"


class SmsRuProvider:
    """Отправка SMS через SMS.ru. Провайдер спрятан за протоколом `SmsProvider`."""

    async def send_code(self, phone: str, text: str) -> None:
        """Task 5.2 (AC-01): отправляет текст; при отказе бросает `SmsProviderError`.

        `api_id` уходит только в теле запроса: в лог и в исключение он не попадает
        (AC-07) — иначе ключ оказался бы в журнале контейнера.
        """
        try:
            response = await get_http_client().post(
                SEND_URL,
                data={
                    "api_id": settings.smsru_api_id,
                    "to": _to_provider_number(phone),
                    "msg": text,
                    "json": "1",
                },
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            # Task 5.4: единый маппинг сетевых отказов с OAuth — `provider_timeout`,
            # `provider_unreachable`, `provider_transport_error`.
            raise SmsProviderError(stage="provider", error_code=_transport_error_code(exc)) from exc

        # Task 5.3: тело может быть HTML-страницей шлюза — `response.json()`
        # бросил бы ValueError и превратил отказ провайдера в 500.
        # CR 1.4, патч 13: HTTP-код проверяется ДО разбора тела.
        # Прокси или балансировщик с 502 и унаследованным телом `{"status":"OK"}`
        # иначе считался бы успехом: код сгорел бы по TTL, пользователь ждал бы
        # несуществующее SMS, а инцидент не попал бы в лог.
        if not 200 <= response.status_code < 300:
            raise SmsProviderError(
                stage="provider", error_code=f"sms_http_status_{response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise SmsProviderError(stage="provider", error_code="sms_provider_error") from None

        if not isinstance(payload, dict):
            raise SmsProviderError(stage="provider", error_code="sms_provider_error")

        # SMS.ru возвращает HTTP 200 и при отказе доставки конкретному получателю:
        # {"status":"OK","status_code":100,"sms":{"79…":{"status":"ERROR",…}}}.
        # Без разбора карты `sms` отказ выглядел бы как успешная отправка:
        # код сгорел бы по TTL, а пользователь ждал бы несуществующее SMS.
        # (CR 1.4, патч 3 — разбор карты обязателен и идёт до проверки `status`.)
        sms_map = payload.get("sms")
        if isinstance(sms_map, dict) and sms_map:
            for recipient in sms_map.values():
                if not isinstance(recipient, dict):
                    continue
                if recipient.get("status") == "ERROR":
                    code = recipient.get("status_code")
                    raise SmsProviderError(
                        stage="provider",
                        error_code=f"recipient_error_{code}" if code else "recipient_error",
                    )

        status = payload.get("status")
        status_code = payload.get("status_code")
        if status == "OK" and status_code == _STATUS_OK:
            return
        raise SmsProviderError(stage="provider", error_code=_error_code_for(status_code))
