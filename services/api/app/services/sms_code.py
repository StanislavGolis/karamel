"""Код подтверждения телефона: генерация, хранение хешем, проверка (Task 4).

D-2: в хранилище лежит HMAC-SHA256(`SMS_CODE_SECRET`, `phone:code`), а не код.
Открытый код в дампе Redis — мгновенная компрометация любого входа; bcrypt/argon2
на 6-значном коде выигрыша не дают (перц 256 бит уже делает офлайн-перебор
бесполезным), зато добавляют 100+ мс CPU на каждую проверку под rate-limit.

D-3: одноразовость через `DEL` **после** сверки, а не `GETDEL`. `GETDEL` погасил бы
код на любом неверном вводе, и опечатка превращалась бы в новый запрос к
провайдеру — то есть в реальные деньги владельца.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import Enum

import structlog

from app.core.config import settings
from app.services.phone import InvalidPhoneError, normalize_phone
from app.services.sms_store import SmsStorageUnavailableError, get_sms_store


class VerifyResult(Enum):
    """Исход проверки кода. Значения — машинные, тексты формирует роут."""

    OK = "ok"
    EXPIRED = "expired"
    INVALID = "invalid"
    ATTEMPTS_EXCEEDED = "attempts_exceeded"


@dataclass(frozen=True, slots=True)
class IssuedCode:
    """Выданный код и его TTL. Открытый код живёт только до отправки в SMS."""

    code: str
    ttl_seconds: int


class SmsCodeConfigError(Exception):
    """Перц HMAC не настроен (CR 1.4, патч 9).

    Конфигурационная ошибка, а не отказ пользователя: раньше `hash_code` бросал
    `RuntimeError` из середины `/verify` — роут отдавал 500, попытка сгорала
    без сверки, а причина (пустой `SMS_CODE_SECRET`) не была видна в логе.
    Роут ловит это исключение и отдаёт управляемый ответ.
    """


class CooldownActiveError(Exception):
    """Повторная отправка раньше cooldown (AC-01).

    Несёт остаток в секундах: клиенту нужно показать «повторите через N», а не
    общее «слишком часто». Без остатка фронт либо врёт про срок, либо не
    показывает таймер вовсе.
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"cooldown active: {retry_after}s")
        self.retry_after = retry_after


def generate_code() -> str:
    """AC-01: `secrets`, не `random`.

    `random` предсказуем по состоянию генератора: зная несколько предыдущих
    кодов, перебор следующего становится тривиальным. `zfill` сохраняет
    ведущие нули — `str(randbelow(10**6))` терял бы их и давал коды короче 6.
    """
    length = settings.sms_code_length
    return str(secrets.randbelow(10**length)).zfill(length)


def hash_code(phone: str, code: str) -> str:
    """D-2: HMAC-SHA256 с перцем, сообщение `phone:code`.

    Телефон в сообщении привязывает хеш к номеру: код, выданный на один номер,
    не подойдёт другому даже при совпадении цифр. Соль не нужна — номер уже
    часть сообщения, а перец (`SMS_CODE_SECRET`) закрывает офлайн-перебор.
    """
    secret = settings.sms_code_secret
    if not secret or len(secret) < 32:
        # HMAC с пустым ключом — это обычный SHA-256: хеш перебирается
        # из дампа Redis за миллисекунды, а номер известен атакующему.
        # CR 1.4, патч 9: доменное исключение вместо `RuntimeError` — роут
        # обязан отличить конфигурационную ошибку от бага в коде.
        raise SmsCodeConfigError("SMS_CODE_SECRET не задан или короче 32 символов")
    message = f"{phone}:{code}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _stub_fixed_code(phone: str) -> str | None:
    """Dev-заглушка: фиксированный код вместо `secrets` для выбранных номеров.

    Возвращает `None`, когда заглушка не настроена — тогда вызывающий идёт
    обычным путём (`generate_code`). Возвращает код, только если он включён
    в настройках **и** номер попадает в область действия.

    Почему нормализация, а не сравнение строк как есть: `SMS_STUB_FIXED_PHONES`
    заполняет человек, и он напишет `89234490800`, `+7 923 449-08-00` или
    `+79234490800` — всё это один номер. Прямое сравнение строк дало бы
    «заглушка включена, но не сработала», и причину искали бы в генераторе
    кода, а не в формате записи. `issue_code` уже получает нормализованный
    номер из роута, но защита от повторной нормализации дешевле отладки
    расхождения: `normalize_phone` идемпотентна.

    Что НЕ делает: не влияет на хеширование, TTL, cooldown и счётчик попыток.
    Код по-прежнему уходит в Redis только как HMAC-хеш (D-2) — фиксируется
    исключительно источник значения, и путь проверки остаётся тем же.
    """
    fixed_code = (settings.sms_stub_fixed_code or "").strip()
    if not fixed_code:
        return None

    try:
        normalized = normalize_phone(phone)
    except InvalidPhoneError:
        # Ненормализуемый номер до сюда доходит только при вызове в обход роута.
        # Заглушка не должна маскировать ошибку формата: пусть работает обычная
        # генерация, а `hash_code`/хранилище получат тот же номер, что и раньше.
        return None

    raw_list = (settings.sms_stub_fixed_phones or "").strip()
    if raw_list:
        allowed: list[str] = []
        for entry in raw_list.split(","):
            candidate = entry.strip()
            if not candidate:
                continue  # «+7…, ,+7…» и хвостовая запятая — не ошибка
            try:
                allowed.append(normalize_phone(candidate))
            except InvalidPhoneError:
                # Запись мусора в списке не должна валить выдачу кода: иначе
                # опечатка в `.env` ломает вход целиком. Валидатор конфигурации
                # проверяет только код, а не записи списка — здесь мягко.
                continue
        if normalized not in allowed:
            return None

    # `sms_stub`, а не `oauth`: фильтр `RedactingOAuthLogger` вырезал бы `msg`,
    # и в логе не осталось бы следа заглушки (та же причина, что в `stub.py`).
    # Телефон маскирован: полные номера в логах не норма (AC-07).
    masked = f"{normalized[:2]}***{normalized[-2:]}" if len(normalized) > 4 else "***"
    structlog.get_logger("sms_stub").warning("SMS_STUB_FIXED_CODE", phone=masked)
    return fixed_code


async def issue_code(phone: str) -> IssuedCode:
    """Task 4.3 (AC-01): генерация → хеш → атомарный cooldown → запись кода.

    CR 1.4, патч 14: cooldown **захватывается** (`SET … NX`), а не проверяется
    отдельным чтением. Проверка и запись двумя round-trip давали TOCTOU:
    два параллельных `/request` оба видели «cooldown нет», оба слали SMS,
    и `set_code` второго убивал код первого — пользователь получал два платных
    сообщения, из которых работало второе.

    CR 1.4, патч 15: запись кода и сброс попыток — единица работы. Частичный
    сбой откатывает уже записанное состояние, иначе в хранилище оставался бы
    живой код, которого пользователь не получил.
    """
    store = get_sms_store()
    # Dev-заглушка: если задан `SMS_STUB_FIXED_CODE` и номер попадает в область
    # действия — берём фиксированный код вместо `secrets`. Ниже путь не меняется:
    # хеш, cooldown, TTL, попытки и проверка остаются прежними, потому что
    # фиксируется только источник значения, а не механика хранения (D-2, D-3).
    code = _stub_fixed_code(phone) or generate_code()
    ttl = settings.sms_code_ttl_seconds
    # Хеш считается до захвата cooldown: конфигурационная ошибка не должна
    # запирать номер на 60 секунд без единой отправки.
    code_hash = hash_code(phone, code)

    left = await store.acquire_cooldown(phone, settings.sms_resend_cooldown_seconds)
    if left is not None:
        raise CooldownActiveError(left)

    try:
        await store.set_code(phone, code_hash, ttl_seconds=ttl)
        # AC-03: новый код начинает счёт попыток заново. Иначе после исчерпания
        # попыток на прошлом коде новый код не удалось бы проверить ни разу.
        await store.delete_attempts(phone)
    except Exception:
        try:
            await store.delete_code(phone)
            await store.delete_cooldown(phone)
        except SmsStorageUnavailableError:
            pass  # хранилище недоступно — откатывать нечего
        raise
    return IssuedCode(code=code, ttl_seconds=ttl)


async def verify_code(phone: str, code: str) -> VerifyResult:
    """Task 4.4 (AC-02, AC-03): счётчик попыток → атомарная сверка с гашением.

    Порядок: инкремент, затем атомарная проверка через `consume_code`.
    `incr_attempts` до сверки обязателен: иначе подсчёт шёл бы только по
    неверным вводам, и параллельные запросы с верным кодом обходили бы лимит.

    `consume_code` — один Lua-вызов (GET+compare+DEL). Три отдельных await давали
    TOCTOU: два параллельных `/verify` с верным кодом оба проходили сверку.
    """
    store = get_sms_store()
    max_attempts = settings.sms_code_max_attempts

    # CR 1.4, Решение 1: счётчик попыток инкрементируется только если код есть.
    # Раньше `incr_attempts` шёл до проверки существования кода: атакующий
    # с нескольких адресов отправлял мусор на чужой номер и выжигал пять
    # попыток жертвы, после чего верный код не принимался 300 секунд.
    # Защита от перебора несуществующих кодов не нужна — перебирать нечего,
    # а стоимость запросов ограничивает лимит `/verify` по IP и по телефону.
    if await store.get_code(phone) is None:
        return VerifyResult.EXPIRED

    # Хеш до инкремента: конфигурационная ошибка не должна съедать попытку.
    code_hash = hash_code(phone, code)

    attempts = await store.incr_attempts(phone, ttl_seconds=settings.sms_code_ttl_seconds)
    if attempts > max_attempts:
        # `>` вместо `>=`: при `>=` пятая попытка отбрасывалась НЕ сверив код,
        # то есть реально доступно 4 попытки вместо 5.
        #
        # Код НЕ удаляется: иначе любой внешний вызывающий мог бы пятью запросами
        # сжечь живой код жертвы и заблокировать ей вход навсегда.
        return VerifyResult.ATTEMPTS_EXCEEDED

    result = await store.consume_code(phone, code_hash)
    if result == -1:
        # AC-03: истёкший и уже погашенный код неразличимы снаружи — оба EXPIRED.
        return VerifyResult.EXPIRED
    if result == 0:
        return VerifyResult.INVALID

    # result == 1: код совпал и уже погашен атомарно внутри Lua.
    await store.delete_attempts(phone)
    return VerifyResult.OK
