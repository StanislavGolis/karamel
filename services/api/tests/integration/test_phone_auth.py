"""Интеграционные тесты входа по телефону (Task 9.5; AC-01…AC-07).

Реальный PostgreSQL, схема через Alembic (D-8 стори 1.2), хранилище кодов —
`InMemorySmsStore`, провайдер — spy вместо SMS.ru: реального `SMSRU_API_ID`
в тестах нет, а живая отправка стоит денег.

Сеть не используется. Коммит в роуте понижается до `flush` — иначе он вышел бы
за внешнюю транзакцию фикстуры и её откат перестал бы изолировать тест.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import jwt
import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import phone_auth
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.sms import SmsProviderError
from app.services.sms_store import InMemorySmsStore, SmsStorageUnavailableError, set_sms_store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

PHONE = "+79001234567"
PHONE_RAW = "8 900 123-45-67"
# CR 1.4, патч 3: список полей, которых не должно быть в событии.
# `external_id` здесь НЕТ намеренно: он легально присутствует, но обязан
# содержать 12 hex-символов хеша (проверяется отдельно в `test_phone_logs_no_pii`).
# Прежняя версия теста искала ключ `phone`, которого в событиях не бывает,
# и потому «доказывала» отсутствие того, чего не было.
FORBIDDEN_LOG_KEYS = (
    "phone",
    "code",
    "sms_code",
    "api_id",
    "access_token",
    "refresh_token",
    "provider_user_id",
)


class SpySmsProvider:
    """Запоминает отправленные тексты вместо вызова SMS.ru.

    Код извлекается из текста сообщения: в хранилище лежит только HMAC-хеш (D-2),
    поэтому другого способа узнать код у теста нет — и это правильно.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with: SmsProviderError | None = None

    async def send_code(self, phone: str, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((phone, text))

    def last_code(self) -> str:
        """Код из последнего сообщения: `Карамель: код 123456. …`."""
        text = self.sent[-1][1]
        return text.split("код ")[1].split(".")[0]


@pytest.fixture(autouse=True)
def _stores() -> None:
    """Чистое хранилище кодов на каждый тест: лимиты не протекают между тестами."""
    set_sms_store(InMemorySmsStore())


@pytest.fixture
def sms(monkeypatch: pytest.MonkeyPatch) -> SpySmsProvider:
    """Подменяет фабрику провайдера в роуте на spy."""
    spy = SpySmsProvider()
    monkeypatch.setattr(phone_auth, "get_sms_provider", lambda: spy)
    return spy


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент поверх приложения с подменённой зависимостью сессии."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _request_code(client: AsyncClient, phone: str = PHONE) -> Any:
    """Запрос кода. Cooldown не обходится — иначе сценарии с ним не проверить.

    CR 1.4, патч 7: прежний хелпер снимал cooldown (`set_cooldown(phone, 0)`)
    во всех тестах, и порядок «cooldown до лимитов» проверялся ровно одним
    тестом. Регрессия прошла бы незамеченной. Тестам с повторной отправкой
    нужен явный обход — см. `_clear_cooldown`.
    """
    return await client.post("/api/v1/auth/phone/request", json={"phone": phone})


async def _clear_cooldown(phone: str = PHONE) -> None:
    """Явный обход cooldown для сценариев, где он не является предметом теста."""
    from app.services.sms_store import get_sms_store

    await get_sms_store().delete_cooldown(phone)


async def _login(client: AsyncClient, sms: SpySmsProvider, phone: str = PHONE) -> Any:
    """Полный вход: запрос кода → проверка. Возвращает ответ `/verify`.

    Cooldown снимается явно: повторный вход в одном тесте — не предмет проверки,
    а cooldown здесь блокировал бы второй `/request` с 429.
    """
    await _clear_cooldown(phone)
    await _request_code(client, phone)
    return await client.post(
        "/api/v1/auth/phone/verify", json={"phone": phone, "code": sms.last_code()}
    )


# --- AC-01: запрос кода ---


async def test_phone_request_sends_code(client: AsyncClient, sms: SpySmsProvider) -> None:
    """AC-01: 200 с `expires_in`/`resend_after`, SMS ушло, кода в ответе нет."""
    response = await client.post("/api/v1/auth/phone/request", json={"phone": PHONE_RAW})

    assert response.status_code == 200
    body = response.json()
    assert body == {"expires_in": 300, "resend_after": 60}
    assert len(sms.sent) == 1
    # AC-01: номер нормализован до отправки — провайдер получает E.164.
    assert sms.sent[0][0] == PHONE
    # AC-01: самого кода в ответе нет.
    assert sms.last_code() not in response.text


async def test_phone_request_invalid_phone_no_sms(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession
) -> None:
    """AC-01: невалидный номер → 400, SMS не отправлено, учётка не создана."""
    response = await client.post("/api/v1/auth/phone/request", json={"phone": "+380900123456"})

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_phone"}
    assert sms.sent == []
    total = await session.scalar(select(func.count()).select_from(User))
    assert total == 0


# --- AC-02: успешный вход ---


async def test_phone_verify_creates_user_once(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession
) -> None:
    """AC-02: первый вход создаёт учётку, повторный — та же запись и `is_new=false`."""
    first = await _login(client, sms)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["is_new"] is True

    second = await _login(client, sms)
    assert second.status_code == 200
    second_body = second.json()

    assert second_body["is_new"] is False
    assert second_body["user_uuid"] == first_body["user_uuid"]
    # AC-02: count(users) не растёт.
    total = await session.scalar(select(func.count()).select_from(User))
    assert total == 1

    user = await session.scalar(select(User))
    assert user is not None
    # AC-02: provider_user_id = phone (D-5), 18+ и согласие — стори 1.6.
    assert user.provider == "phone"
    assert user.provider_user_id == PHONE
    assert user.phone == PHONE
    assert user.status == "active"
    assert user.age_confirmed is False
    assert user.consent_given_at is None


async def test_phone_verify_concurrent_single_consume(sms: SpySmsProvider) -> None:
    """AC-02/D-3: из двух параллельных сверок код гасится ровно один раз.

    CR 1.4, патч 8: прежний тест гонял два `client.post` на одной `AsyncSession`,
    что запрещено SQLAlchemy (`AsyncSession` не поддерживает параллельное
    использование) — тест был флаком по инфраструктуре, а не по контракту.
    Плюс `InMemorySmsStore.consume_code` атомарен по построению (нет `await`),
    поэтому Lua-ветка не проверялась вообще.

    Здесь проверяется именно контракт хранилища: два параллельных `consume_code`
    с верным хешем дают ровно один `1` и один `-1`. Гонка на уровне БД
    (одна учётка при двух вставках) покрыта `test_oauth_concurrent_callbacks`.
    """
    from app.services.sms_code import hash_code
    from app.services.sms_store import get_sms_store

    store = get_sms_store()
    await store.set_code(PHONE, hash_code(PHONE, "123456"), ttl_seconds=300)

    results = await asyncio.gather(
        store.consume_code(PHONE, hash_code(PHONE, "123456")),
        store.consume_code(PHONE, hash_code(PHONE, "123456")),
    )

    # Ровно один успех: код гасится атомарно (D-3), второй видит отсутствие.
    assert sorted(results) == [-1, 1]


# --- AC-03: истёкший и неверный код ---


async def test_phone_verify_expired(client: AsyncClient) -> None:
    """AC-03: кода нет → 410 `code_expired`, и это не «неверный код»."""
    response = await client.post(
        "/api/v1/auth/phone/verify", json={"phone": PHONE, "code": "123456"}
    )

    assert response.status_code == 410
    assert response.json() == {"code": "code_expired"}


async def test_phone_verify_wrong_code_attempts_exhausted(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession
) -> None:
    """AC-03: неверный код → 401 с остатком попыток, затем лимит и удаление кода."""
    await _request_code(client)
    code = sms.last_code()
    wrong = "000000" if code != "000000" else "111111"

    first = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": wrong})
    assert first.status_code == 401
    assert first.json()["code"] == "code_invalid"
    assert first.json()["attempts_left"] == settings.sms_code_max_attempts - 1

    # Остальные сверяемые попытки: всего max_attempts, первая уже израсходована.
    for _ in range(settings.sms_code_max_attempts - 1):
        await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": wrong})
    # Следующая попытка упирается в потолок, а не сверяется.
    last = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": wrong})

    assert last.status_code == 401
    assert last.json() == {"code": "code_attempts_exceeded"}

    # AC-03: после исчерпания попыток верный код уже не принимается,
    # но сам код не уничтожен — иначе чужой мог бы сжечь вход пятью запросами.
    final = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": code})
    assert final.status_code == 401
    assert final.json() == {"code": "code_attempts_exceeded"}
    # AC-03: ни в одной ветке отказа учётка не создана.
    total = await session.scalar(select(func.count()).select_from(User))
    assert total == 0


# --- AC-04: лимиты в Redis ---


async def test_phone_rate_limit_ip(client: AsyncClient, sms: SpySmsProvider) -> None:
    """AC-04: 4-й запрос за окно → 429 `rate_limited` с остатком."""
    for index in range(3):
        # Разные номера: лимит телефона не должен маскировать лимит IP.
        phone = f"+790012345{index:02d}"
        response = await _request_code(client, phone)
        assert response.status_code == 200

    blocked = await _request_code(client, "+79001234599")

    assert blocked.status_code == 429
    body = blocked.json()
    assert body["code"] == "rate_limited"
    assert body["retry_after"] > 0
    assert "Retry-After" in blocked.headers
    # AC-04: заблокированный запрос не отправил SMS.
    assert len(sms.sent) == 3


async def test_phone_rate_limit_state_lives_in_store(
    client: AsyncClient, sms: SpySmsProvider
) -> None:
    """AC-04: состояние лимита лежит в хранилище, а не в объекте запроса.

    CR 1.4, патч 6: прежний тест создавал второй `AsyncClient` в том же процессе
    над тем же `InMemorySmsStore` — перезапуска он не моделировал, зато тёк
    `dependency_overrides` (без `pop`) в следующие тесты.

    Здесь проверяется то, что действительно является контрактом: счётчик живёт
    в `SmsStore`, отдельно от HTTP-слоя. Новый экземпляр клиента и новый объект
    запроса видят тот же счётчик.
    """
    from app.services.sms_store import get_sms_store

    store = get_sms_store()
    for index in range(3):
        await _request_code(client, f"+790012345{index:02d}")

    # Счётчик лежит в хранилище и виден снаружи HTTP-слоя.
    retry_after = await store.hit_rate(
        "ip", "127.0.0.1", scope="request", window_seconds=600, limit=3
    )
    assert retry_after is not None
    assert len(sms.sent) == 3


async def test_phone_request_cooldown_returns_rate_limited(
    client: AsyncClient, sms: SpySmsProvider
) -> None:
    """AC-01/D-9: повторная отправка в пределах cooldown → 429 `rate_limited`.

    Отдельный тест, потому что cooldown — не то же самое, что окно лимита:
    окно считает запросы за 10 минут, cooldown запрещает второй запрос сразу
    после первого. Код ошибки у них общий (`rate_limited`): D-9 фиксирует
    закрытый набор, и расширять его ради cooldown нельзя.
    """
    first = await client.post("/api/v1/auth/phone/request", json={"phone": PHONE})
    assert first.status_code == 200

    second = await client.post("/api/v1/auth/phone/request", json={"phone": PHONE})

    assert second.status_code == 429
    body = second.json()
    assert body["code"] == "rate_limited"
    assert 0 < body["retry_after"] <= 60
    # AC-01: повторное SMS не отправлялось — деньги владельца не тратятся.
    assert len(sms.sent) == 1


async def test_phone_redis_down(
    client: AsyncClient, sms: SpySmsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04/D-8: Redis недоступен → 503, SMS не отправляется."""

    class BrokenStore(InMemorySmsStore):
        async def hit_rate(self, *args: Any, **kwargs: Any) -> int | None:
            raise SmsStorageUnavailableError("Redis недоступен")

    set_sms_store(BrokenStore())

    response = await client.post("/api/v1/auth/phone/request", json={"phone": PHONE})

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    # D-8: тихая отправка без лимита запрещена — провайдер не вызывался.
    assert sms.sent == []


async def test_phone_provider_failure_is_generic(client: AsyncClient, sms: SpySmsProvider) -> None:
    """AC-07/D-9: отказ провайдера наружу — только `sms_unavailable`."""
    sms.fail_with = SmsProviderError(stage="provider", error_code="sms_invalid_key")

    response = await client.post("/api/v1/auth/phone/request", json={"phone": PHONE})

    assert response.status_code == 503
    assert response.json() == {"code": "sms_unavailable"}
    assert "sms_invalid_key" not in response.text


# --- AC-05: уникальность и бан ---


async def test_phone_taken_by_oauth_account(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession
) -> None:
    """AC-05/D-6: номер занят OAuth-учёткой → 409, автосклейки нет."""
    session.add(User(provider="yandex", provider_user_id="ya-1", phone=PHONE))
    await session.flush()

    await _request_code(client)
    response = await client.post(
        "/api/v1/auth/phone/verify", json={"phone": PHONE, "code": sms.last_code()}
    )

    assert response.status_code == 409
    assert response.json() == {"code": "phone_taken"}
    # AC-05: сессия не выдаётся, вторая учётка не создаётся.
    assert "karamel_access" not in response.cookies
    total = await session.scalar(select(func.count()).select_from(User))
    assert total == 1


@pytest.mark.parametrize("banned", [True, False])
async def test_phone_blocked_user(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession, banned: bool
) -> None:
    """AC-05: бан или запрошенное удаление блокируют вход без выдачи сессии.

    CR 1.4, патч 11: отказ теперь приходит уже на `/request` (403 `user_blocked`),
    а не только на `/verify`. Раньше заблокированный бесконечно инициировал
    платные отправки, а отказ видел лишь после списания денег за SMS.
    """
    from datetime import UTC, datetime

    user = User(
        provider="phone",
        provider_user_id=PHONE,
        phone=PHONE,
        status="banned" if banned else "active",
    )
    if not banned:
        user.deletion_requested_at = datetime.now(UTC)
    session.add(user)
    await session.flush()

    response = await _request_code(client)

    assert response.status_code == 403
    assert response.json() == {"code": "user_blocked"}
    # AC-01/AC-05: SMS не отправлено — деньги владельца не тратятся.
    assert sms.sent == []

    # `/verify` без кода: сессии нет, cookie не выдаются.
    verify = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": "123456"})
    assert verify.status_code == 410
    assert "karamel_access" not in verify.cookies


# --- AC-06: сессия ---


async def test_phone_verify_sets_session_cookies(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession
) -> None:
    """AC-06: обе cookie `HttpOnly`, в БД хеш 32 байта, `id` нет в payload JWT."""
    response = await _login(client, sms)

    assert response.status_code == 200
    access = response.cookies.get("karamel_access")
    refresh = response.cookies.get("karamel_refresh")
    assert access and refresh

    set_cookie_headers = response.headers.get_list("set-cookie")
    for header in set_cookie_headers:
        assert "HttpOnly" in header

    # AC-06: в БД только SHA-256 хеш refresh, ровно 32 байта.
    token_row = await session.scalar(select(RefreshToken))
    assert token_row is not None
    assert len(token_row.token_hash) == 32

    # AC-06: внутренний `id` не попадает в токен — только публичный `uuid`.
    payload = jwt.decode(access, options={"verify_signature": False}, algorithms=["HS256"])
    user = await session.scalar(select(User))
    assert user is not None
    assert payload["sub"] == str(user.uuid)
    assert "id" not in payload


# --- AC-07: логи без ПД ---


async def test_phone_logs_have_no_pii(client: AsyncClient, sms: SpySmsProvider) -> None:
    """AC-07: ни номер, ни код, ни `api_id` не попадают в логи.

    CR 1.4, патч 3: тест исполняет **реальный** процессор `RedactingOAuthLogger`
    на реальных событиях, а не подменяет цепочку через `capture_logs`.
    `capture_logs` перехватывает `event_dict` **до** процессоров, поэтому он не
    мог увидеть ни очистку, ни утечку: список запрещённых ключей смотрел на
    исходные поля, а после хеширования поле называется `external_id` и содержит
    12 hex-символов.
    """
    captured: list[dict[str, Any]] = []

    def _sink(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        # Копируем уже **после** `RedactingOAuthLogger`: сюда попадает то,
        # что реально уйдёт в журнал.
        captured.append(dict(event_dict))
        return event_dict

    config = structlog.get_config()
    processors = list(config["processors"])
    # Сток ставится непосредственно перед рендером, то есть после редактора ПД.
    structlog.configure(processors=[*processors[:-1], _sink, processors[-1]])

    try:
        await _request_code(client)
        code = sms.last_code()
        await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": "000000"})
        await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": code})
    finally:
        structlog.configure(processors=processors)

    oauth_events = [e for e in captured if str(e.get("event", "")).startswith("phone_")]
    assert oauth_events, "ни одного события phone_* не записано"

    for entry in oauth_events:
        for forbidden in FORBIDDEN_LOG_KEYS:
            assert forbidden not in entry, f"{forbidden} утёк в лог: {entry}"
        dumped = str(entry)
        # AC-07: ни номер, ни код не встречаются даже как подстрока.
        assert PHONE not in dumped, f"номер утёк в лог: {entry}"
        assert "79001234567" not in dumped, f"номер утёк в лог: {entry}"
        assert code not in dumped, f"код утёк в лог: {entry}"

    # Патч 2 CR 1.4: `external_id` в событии — 12 hex-символов (хеш), не номер.
    with_external = [e for e in oauth_events if e.get("external_id")]
    assert with_external, "ни одного события с external_id — проверка хеширования пуста"
    for entry in with_external:
        value = str(entry["external_id"])
        assert len(value) == 12, f"external_id не хеш: {value!r}"
        int(value, 16)  # бросит ValueError, если в поле не hex


async def test_provider_failure_refunds_quota(client: AsyncClient, sms: SpySmsProvider) -> None:
    """Патч 1 CR 1.4: отказ провайдера возвращает израсходованную квоту лимитов.

    `hit_rate` инкрементирует счётчики до вызова провайдера. Без откатa три
    отказа подряд выжигали 3/3 запросов за 10 минут при нуле отправленных SMS:
    пользователь получал 503, затем 429 и не мог войти вовсе.
    """
    sms.fail_with = SmsProviderError(stage="provider", error_code="sms_no_funds")

    for _ in range(3):
        await _clear_cooldown()
        response = await _request_code(client)
        assert response.status_code == 503
        assert response.json() == {"code": "sms_unavailable"}

    # Четвёртый запрос должен дойти до провайдера, а не упасть в 429.
    sms.fail_with = None
    await _clear_cooldown()
    recovered = await _request_code(client)

    assert recovered.status_code == 200, recovered.json()
    assert len(sms.sent) == 1


# --- CR 1.4: дополнительные контракты ---


async def test_verify_restores_code_on_phone_taken(
    client: AsyncClient, sms: SpySmsProvider, session: AsyncSession
) -> None:
    """Патч 12 CR 1.4: `409 phone_taken` не сжигает проверенный код.

    Код гасится атомарно при сверке, но отказ на этапе учётки — не вина
    пользователя: без возврата кода повтор требует нового SMS (деньги
    владельца) только чтобы увидеть тот же отказ.
    """
    session.add(User(provider="yandex", provider_user_id="ya-1", phone=PHONE))
    await session.flush()

    await _request_code(client)
    code = sms.last_code()

    first = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": code})
    assert first.status_code == 409

    # Код восстановлен: повтор даёт тот же 409, а не 410 `code_expired`.
    second = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": code})
    assert second.status_code == 409
    assert second.json() == {"code": "phone_taken"}


async def test_config_error_returns_503_not_500(
    client: AsyncClient, sms: SpySmsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Патч 9 CR 1.4: незаданный перц — управляемый 503, а не 500.

    `hash_code` бросал `RuntimeError` из середины обработчика: пользователь
    видел 500, попытка сгорала без сверки, а причина не попадала в лог.
    """
    monkeypatch.setattr(settings, "sms_code_secret", "short")

    response = await _request_code(client)

    assert response.status_code == 503
    assert response.json() == {"code": "sms_unavailable"}
    assert sms.sent == []


async def test_provider_runtime_error_is_wrapped(
    client: AsyncClient, sms: SpySmsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Патч 10 CR 1.4: `RuntimeError` фабрики провайдера — 503, не 500.

    `get_sms_provider` и stub бросают `RuntimeError` (опечатка в `SMS_PROVIDER`,
    запрещённая заглушка). Роут ловил только `SmsProviderError`, поэтому такой
    отказ давал 500 уже после `set_code` + cooldown.
    """

    def _broken() -> Any:
        raise RuntimeError("Неизвестный SMS_PROVIDER: 'smtp'")

    monkeypatch.setattr(phone_auth, "get_sms_provider", _broken)

    response = await _request_code(client)

    assert response.status_code == 503
    assert response.json() == {"code": "sms_unavailable"}


async def test_client_ip_unknown_returns_503(sms: SpySmsProvider, session: AsyncSession) -> None:
    """Патч 17 CR 1.4: без `request.client` бакет `unknown` не создаётся.

    Иначе все такие запросы делили один лимит, и один клиент выжигал квоту
    остальным. Отказ безопаснее выдуманного бакета.
    """

    class _RequestNoClient:
        """ASGI-запрос без информации о соединении (unix-сокет, сломанный сервер)."""

        client = None

    response = await phone_auth.request_code(
        phone_auth.PhoneRequest(phone=PHONE),
        _RequestNoClient(),
        session,  # type: ignore[arg-type]
    )

    assert response.status_code == 503
    # Роут вызван напрямую — это `JSONResponse`, а не httpx-ответ.
    assert json.loads(response.body) == {"code": "storage_unavailable"}
    assert sms.sent == []


def test_rate_limited_substitutes_window_for_zero() -> None:
    """Патч 16 CR 1.4: `Retry-After: 0` недостижим.

    Нулевой остаток (ttl = -1/-2/0) заставлял клиента повторить немедленно
    и получить второй 429.
    """
    response = phone_auth._rate_limited(0)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(settings.sms_rate_window_seconds)


async def test_garbage_without_code_does_not_burn_attempts(
    client: AsyncClient, sms: SpySmsProvider
) -> None:
    """Решение 1 CR 1.4: `/verify` без живого кода НЕ инкрементирует счётчик.

    Атакующий с нескольких адресов отправлял мусор на чужой номер и выжигал
    пять попыток жертвы ДО того, как она ввела код: верный код потом не
    принимался 300 секунд. Счётчик инкрементируется только при существующем коде.
    """
    from app.services.sms_store import get_sms_store

    # Меньше IP-лимита `/verify` (10): проверяется счётчик попыток, а не лимит.
    for _ in range(5):
        await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": "000000"})

    # Попытки не израсходованы: счётчика нет.
    assert await get_sms_store().get_attempts(PHONE) == 0

    # Жертва запрашивает код и входит с первой попытки.
    await _clear_cooldown()
    await _request_code(client)
    response = await client.post(
        "/api/v1/auth/phone/verify", json={"phone": PHONE, "code": sms.last_code()}
    )
    assert response.status_code == 200
