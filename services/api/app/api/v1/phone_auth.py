"""Вход по номеру телефона с SMS-кодом (Task 7; AC-01…AC-05, AC-07).

Роут не содержит бизнес-логики: нормализация, коды, лимиты и поиск учётки —
в `services/`. Здесь только порядок шагов и HTTP-формы ответов.

D-9: все отказы — `JSONResponse` с `{"code": "..."}` из фиксированного набора.
`HTTPException` не используется: её тело — `{"detail": ...}`, и клиент получал бы
две разные формы ошибки в зависимости от ветки.
"""

from __future__ import annotations

import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.logging import log_oauth_event
from app.repositories.users import UserRepository
from app.services.auth_service import PhoneTakenError, UserBlockedError, resolve_phone_user
from app.services.phone import InvalidPhoneError, normalize_phone
from app.services.session_service import issue_session, set_session_cookies
from app.services.sms import SmsProviderError, get_sms_provider
from app.services.sms_code import (
    CooldownActiveError,
    SmsCodeConfigError,
    VerifyResult,
    hash_code,
    issue_code,
    verify_code,
)
from app.services.sms_store import (
    SmsStorageUnavailableError,
    get_sms_store,
)

router = APIRouter(prefix="/auth/phone", tags=["auth"])

PROVIDER = "phone"


class PhoneRequest(BaseModel):
    """Тело `/request`. Форма проверяется pydantic, содержимое — нормализатором."""

    phone: str = Field(min_length=1, max_length=32)


class PhoneVerify(BaseModel):
    """Тело `/verify`. Длина кода не ограничена жёстко: неверная длина —
    обычный неверный ввод (`code_invalid`), а не `422` от валидации."""

    phone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=32)


@router.post("/request", response_model=None)
async def request_code(
    payload: PhoneRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """AC-01, AC-04: отправляет код с учётом лимитов IP и телефона.

    Порядок: нормализация → бан/удаление → cooldown → лимит IP → лимит телефона
    → выдача кода → отправка. Лимиты проверяются **до** генерации кода: иначе
    при отказе в хранилище оставался бы живой код, которого пользователь не получил.
    """
    try:
        phone = normalize_phone(payload.phone)
    except InvalidPhoneError:
        # AC-01: невалидный номер — SMS не отправляется, учётка не создаётся.
        log_oauth_event(
            "phone_code_failed", provider=PROVIDER, stage="request", error_code="invalid_phone"
        )
        return JSONResponse({"code": "invalid_phone"}, status_code=400)

    # CR 1.4, патч 11: забаненный или удаляющийся не должен инициировать
    # платные отправки бесконечно. Раньше отказ возникал только на `/verify`,
    # то есть уже после списания денег за SMS.
    if await _is_blocked(session, phone):
        log_oauth_event(
            "phone_code_failed",
            provider=PROVIDER,
            stage="request",
            error_code="user_blocked",
            external_id=phone,
        )
        return JSONResponse({"code": "user_blocked"}, status_code=403)

    # Task 7.2: IP берётся из соединения, а не из `X-Forwarded-For`:
    # заголовок подделывается клиентом, и лимит обходился бы одним словом.
    # CR 1.4, патч 17: без `request.client` (unix-сокет, сломанный ASGI-сервер)
    # все такие запросы делили бы бакет `unknown` и выжигали общий лимит.
    # Отказ безопаснее: бакета, который нельзя посчитать, не существует.
    if request.client is None:
        log_oauth_event(
            "phone_code_failed",
            provider=PROVIDER,
            stage="request",
            error_code="client_ip_unknown",
            external_id=phone,
        )
        return JSONResponse({"code": "storage_unavailable"}, status_code=503)
    client_ip = request.client.host
    store = get_sms_store()

    try:
        # Cooldown проверяется ДО лимитов: пользователь, трижды нажавший
        # «отправить» за минуту, иначе сжигал бы всю квоту 3/10 мин и получал
        # блокировку на 10 минут при одном реально отправленном SMS.
        left = await store.cooldown_left(phone)
        if left is not None:
            log_oauth_event(
                "phone_code_failed",
                provider=PROVIDER,
                stage="request",
                error_code="cooldown",
                external_id=phone,
            )
            return _rate_limited(left)

        # AC-04: сначала IP — он защищает от рассылки по чужим номерам с одного
        # адреса; лимит телефона защищает конкретного получателя от спама.
        # Решение 2 CR 1.4: `scope="request"` — отдельный ключ от `/verify`.
        ip_retry = await store.hit_rate(
            "ip",
            client_ip,
            scope="request",
            window_seconds=settings.sms_rate_window_seconds,
            limit=settings.sms_rate_ip_limit,
        )
        if ip_retry is not None:
            log_oauth_event(
                "phone_rate_limited",
                provider=PROVIDER,
                stage="request",
                error_code="rate_limited_ip",
                external_id=phone,
            )
            return _rate_limited(ip_retry)

        phone_retry = await store.hit_rate(
            "phone",
            phone,
            scope="request",
            window_seconds=settings.sms_rate_window_seconds,
            limit=settings.sms_rate_phone_limit,
        )
        if phone_retry is not None:
            log_oauth_event(
                "phone_rate_limited",
                provider=PROVIDER,
                stage="request",
                error_code="rate_limited_phone",
                external_id=phone,
            )
            return _rate_limited(phone_retry)

        issued = await issue_code(phone)
    except CooldownActiveError as exc:
        # AC-01/D-9: cooldown отдаётся существующим кодом `rate_limited` —
        # закрытый набор кодов ошибок не расширяется, а клиент получает тот же
        # `retry_after` и тот же таймер, что и при лимите.
        log_oauth_event(
            "phone_code_failed",
            provider=PROVIDER,
            stage="request",
            error_code="cooldown",
            external_id=phone,
        )
        # Квота уже израсходована двумя `hit_rate`, а SMS не уйдёт: без откатa
        # три параллельных нажатия «отправить» запирали номер на 10 минут.
        await _refund_quota(client_ip, phone)
        return _rate_limited(exc.retry_after)
    except SmsCodeConfigError:
        # CR 1.4, патч 9: конфигурационная ошибка (пустой перц) — не 500 из
        # середины обработчика, а управляемый отказ с записью в лог.
        log_oauth_event(
            "phone_code_failed",
            provider=PROVIDER,
            stage="request",
            error_code="sms_config_error",
            external_id=phone,
        )
        await _refund_quota(client_ip, phone)
        return JSONResponse({"code": "sms_unavailable"}, status_code=503)
    except SmsStorageUnavailableError:
        # AC-04/D-8: без Redis лимит не работает, значит отправлять нельзя.
        log_oauth_event(
            "phone_code_failed",
            provider=PROVIDER,
            stage="request",
            error_code="storage_unavailable",
            external_id=phone,
        )
        return JSONResponse({"code": "storage_unavailable"}, status_code=503)

    log_oauth_event("phone_code_requested", provider=PROVIDER, stage="request", external_id=phone)

    try:
        await get_sms_provider().send_code(phone, _sms_text(issued.code))
    except (SmsProviderError, RuntimeError) as exc:
        # CR 1.4, патч 10: `get_sms_provider()` и stub бросают `RuntimeError`.
        # Роут ловил только `SmsProviderError`, поэтому опечатка в `SMS_PROVIDER`
        # или запрещённая заглушка давали 500 уже после `set_code` + cooldown.
        error_code = exc.error_code if isinstance(exc, SmsProviderError) else "sms_provider_error"
        # AC-07, D-9: наружу — только `sms_unavailable`, детали в лог.
        log_oauth_event(
            "phone_code_failed",
            provider=PROVIDER,
            stage="provider",
            error_code=error_code,
            external_id=phone,
        )
        # Откат состояния: код уже в Redis и cooldown взведён, но SMS не ушла.
        # Без откатa пользователь получает 503, затем 429 на повтор и не может
        # войти вовсе; код, которого он не видел, сгорает по TTL.
        try:
            await get_sms_store().delete_code(phone)
            await get_sms_store().delete_cooldown(phone)
        except SmsStorageUnavailableError:
            pass  # хранилище недоступно — откатывать нечего
        # CR 1.4, патч 1: квота лимитов тоже возвращается. `hit_rate` счётчики
        # уже инкрементированы, и три отказа провайдера выжигали 3/3 без единого
        # отправленного SMS.
        await _refund_quota(client_ip, phone)
        return JSONResponse({"code": "sms_unavailable"}, status_code=503)

    log_oauth_event("phone_code_sent", provider=PROVIDER, stage="provider", external_id=phone)
    # AC-01: `expires_in` — срок жизни кода для таймера, `resend_after` — когда
    # можно запросить новый. Самого кода в ответе нет ни при каких условиях.
    return JSONResponse(
        {
            "expires_in": issued.ttl_seconds,
            "resend_after": settings.sms_resend_cooldown_seconds,
        }
    )


@router.post("/verify", response_model=None)
async def verify(
    payload: PhoneVerify,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """AC-02, AC-03, AC-05, AC-06: проверяет код и выдаёт сессию.

    Порядок: нормализация → проверка кода → учётка → сессия → коммит → cookie.
    Проверка кода до обращения к БД: иначе перебор кодов нагружал бы базу
    вплоть до исчерпания попыток.
    """
    try:
        phone = normalize_phone(payload.phone)
    except InvalidPhoneError:
        log_oauth_event(
            "phone_verify_failed", provider=PROVIDER, stage="verify", error_code="invalid_phone"
        )
        return JSONResponse({"code": "invalid_phone"}, status_code=400)

    # Код из SMS приходит с пробелом или переводом строки при копировании
    # из мессенджера: без strip пользователь получает `code_invalid`
    # и теряет одну из пяти попыток на ровном месте.
    code = payload.code.strip()

    # Лимит на `/verify` обязателен: без него 5 POST-ов с мусорным кодом
    # на чужой номер блокируют вход жертвы, а атакующий повторяет цикл
    # каждые 60 секунд. Лимит шире, чем на `/request`: проверять код
    # законно можно чаще, чем запрашивать SMS.
    # CR 1.4, патч 17: без `request.client` бакет `unknown` смешивал бы всех.
    if request.client is None:
        return JSONResponse({"code": "storage_unavailable"}, status_code=503)
    client_ip = request.client.host
    store = get_sms_store()
    try:
        # Решение 2 CR 1.4: `scope="verify"` — свой ключ, не общий с `/request`.
        verify_retry = await store.hit_rate(
            "ip",
            client_ip,
            scope="verify",
            window_seconds=settings.sms_verify_window_seconds,
            limit=settings.sms_verify_ip_limit,
        )
        if verify_retry is not None:
            log_oauth_event(
                "phone_verify_failed",
                provider=PROVIDER,
                stage="verify",
                error_code="rate_limited_verify",
            )
            return _rate_limited(verify_retry)

        # Решение 1 CR 1.4: лимит по телефону. Атакующий с нескольких адресов
        # иначе перебирает код одного номера, пока не упрётся только в IP-лимит.
        phone_verify_retry = await store.hit_rate(
            "phone",
            phone,
            scope="verify",
            window_seconds=settings.sms_verify_window_seconds,
            limit=settings.sms_verify_phone_limit,
        )
        if phone_verify_retry is not None:
            log_oauth_event(
                "phone_verify_failed",
                provider=PROVIDER,
                stage="verify",
                error_code="rate_limited_verify",
                external_id=phone,
            )
            return _rate_limited(phone_verify_retry)

        result = await verify_code(phone, code)
    except SmsCodeConfigError:
        # CR 1.4, патч 9: конфигурационная ошибка не должна давать 500.
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="sms_config_error",
            external_id=phone,
        )
        return JSONResponse({"code": "sms_unavailable"}, status_code=503)
    except SmsStorageUnavailableError:
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="storage_unavailable",
            external_id=phone,
        )
        return JSONResponse({"code": "storage_unavailable"}, status_code=503)

    # AC-03: ни в одной ветке отказа нет записи в users и cookie сессии.
    if result is VerifyResult.EXPIRED:
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="code_expired",
            external_id=phone,
        )
        return JSONResponse({"code": "code_expired"}, status_code=410)

    if result is VerifyResult.ATTEMPTS_EXCEEDED:
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="code_attempts_exceeded",
            external_id=phone,
        )
        return JSONResponse({"code": "code_attempts_exceeded"}, status_code=401)

    if result is VerifyResult.INVALID:
        remaining = await _attempts_left(phone)
        if remaining is None:
            # CR 1.4, патч 14: счётчик не читается — это 503 storage_unavailable,
            # а не 401 с ложным `attempts_left=0` (D-8).
            log_oauth_event(
                "phone_verify_failed",
                provider=PROVIDER,
                stage="verify",
                error_code="storage_unavailable",
                external_id=phone,
            )
            return JSONResponse({"code": "storage_unavailable"}, status_code=503)
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="code_invalid",
            external_id=phone,
        )
        return JSONResponse({"code": "code_invalid", "attempts_left": remaining}, status_code=401)

    try:
        user, is_new = await resolve_phone_user(session, phone)
    except PhoneTakenError:
        # AC-05/D-6: номер занят OAuth-учёткой — автосклейки нет, сессия не выдаётся.
        # CR 1.4, патч 12: код не должен сгорать. Пользователь узнаёт про занятый
        # номер только здесь; без возврата кода повтор требует нового SMS.
        await _restore_code(phone, code)
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="phone_taken",
            external_id=phone,
        )
        return JSONResponse({"code": "phone_taken"}, status_code=409)
    except UserBlockedError:
        # AC-05: бан и запрошенное удаление блокируют вход.
        # CR 1.4, патч 12: код возвращается — иначе бан навсегда съедал SMS.
        await _restore_code(phone, code)
        log_oauth_event(
            "phone_verify_failed",
            provider=PROVIDER,
            stage="verify",
            error_code="user_blocked",
            external_id=phone,
        )
        return JSONResponse({"code": "user_blocked"}, status_code=403)

    # AC-06: та же сессия, что и при OAuth-входе — второй путь выдачи запрещён.
    issued = await issue_session(session, user)
    # Task 7.3: коммит в роуте — учётка и хеш refresh-токена одной транзакцией.
    await session.commit()

    log_oauth_event(
        "phone_verify_success",
        provider=PROVIDER,
        stage="verify",
        user_uuid=str(user.uuid),
        external_id=phone,
    )
    response = JSONResponse({"user_uuid": str(user.uuid), "is_new": is_new})
    set_session_cookies(response, issued)
    return response


def _sms_text(code: str) -> str:
    """Task 5.7: текст без ссылок и домена — ссылки в SMS повышают цену и спам-скор."""
    return f"Карамель: код {code}. Никому не сообщайте."


async def _attempts_left(phone: str) -> int | None:
    """AC-03: сколько попыток осталось — клиенту нужен счётчик, не только отказ.

    При недоступном счётчике возвращается `None`, а не 0: ложный ноль
    показывал бы «попыток нет» и уводил пользователя на новый запрос SMS,
    хотя код и попытки целы.
    """
    try:
        used = await get_sms_store().get_attempts(phone)
    except SmsStorageUnavailableError:
        return None
    return max(0, settings.sms_code_max_attempts - used)


async def _is_blocked(session: AsyncSession, phone: str) -> bool:
    """AC-05 (CR 1.4, патч 11): бан или запрошенное удаление до отправки SMS.

    Проверка только по номеру: учётки с таким `provider_user_id` ещё может не
    быть, и это нормально — тогда отправка разрешена.
    """
    user = await UserRepository(session).get_by_provider_user_id(PROVIDER, phone)
    if user is None:
        return False
    return user.status == "banned" or user.deletion_requested_at is not None


async def _restore_code(phone: str, code: str) -> None:
    """CR 1.4, патч 12: возвращает погашенный код в хранилище до конца TTL.

    Код гасится атомарно при сверке, но отказ на этапе учётки (`phone_taken`,
    `user_blocked`) — не вина пользователя: без возврата кода ему пришлось бы
    запрашивать новое SMS, чтобы увидеть тот же отказ.
    """
    with contextlib.suppress(SmsStorageUnavailableError, SmsCodeConfigError):
        await get_sms_store().set_code(
            phone, hash_code(phone, code), ttl_seconds=settings.sms_code_ttl_seconds
        )


async def _refund_quota(client_ip: str, phone: str) -> None:
    """CR 1.4, патч 1: возвращает израсходованную квоту `/request`.

    Вызывается только после отказа провайдера (SMS не ушла) и при
    конфигурационной ошибке. Недоступность хранилища здесь глотается: откат —
    вспомогательная операция, и её сбой не должен подменять основной ответ.
    """
    store = get_sms_store()
    try:
        await store.decr_rate("phone", phone, scope="request")
        await store.decr_rate("ip", client_ip, scope="request")
    except SmsStorageUnavailableError:
        pass


def _rate_limited(retry_after: int) -> JSONResponse:
    """AC-04: остаток окна уходит в тело и в `Retry-After`.

    Заголовок нужен прокси и клиентам, читающим только HTTP-метаданные;
    тело — фронту, который показывает таймер.
    """
    # CR 1.4, патч 16: `Retry-After: 0` заставлял клиента повторить немедленно
    # и получить второй 429. Нулевой остаток означает «окно неизвестно» —
    # подставляем длину окна, чтобы клиент точно подождал.
    if retry_after <= 0:
        retry_after = settings.sms_rate_window_seconds
    return JSONResponse(
        {"code": "rate_limited", "retry_after": retry_after},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )
