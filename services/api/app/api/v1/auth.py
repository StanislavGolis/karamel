"""OAuth-вход через Яндекс ID и VK ID (Task 6, AC-01, AC-02, AC-03, AC-05, AC-07).

Роут не содержит бизнес-логики: выпуск `state`, обмен кода, поиск учётки и выдача
сессии живут в `services/`. Здесь только порядок шагов и HTTP-формы ответов.

Порядок в callback зафиксирован Task 6.3 и он же — защита:
`state` → `code`/`device_id` → обмен → профиль → пользователь → проверка бана → сессия.
Обмен кода до проверки `state` превратил бы защиту в декорацию (AC-05).
"""

from __future__ import annotations

import contextlib
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.db import get_session
from app.core.logging import log_oauth_event
from app.schemas.consent import ConsentRequest, ConsentResponse, SessionUserResponse
from app.services.auth_service import UserBlockedError, resolve_user
from app.services.consent_service import (
    ConsentRaceError,
    ConsentRequiredError,
    GenderLockedError,
    GenderRequiredError,
    record_consent,
)
from app.services.oauth import OAuthProviderError, get_client
from app.services.oauth_state import RedisUnavailableError, consume_state, issue_state
from app.services.session_service import (
    ACCESS_COOKIE_MAX_AGE,
    REFRESH_COOKIE_NAME,
    SessionExpiredError,
    SessionUnknownError,
    clear_oauth_cookie,
    clear_session_cookies,
    issue_session,
    oauth_cookie_name,
    revoke_session,
    rotate_session,
    set_oauth_cookie,
    set_session_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"])

ProviderName = Literal["yandex", "vk"]

# AC-04/D8: наружу уходит только известный код. Значение `?error=` управляется
# клиентом и провайдером, поэтому в лог попадает лишь по allowlist:
# иначе `/callback?error=<500 КБ>` засоряет логи без единого `state`.
_ALLOWED_PROVIDER_ERRORS = frozenset(
    {"access_denied", "invalid_request", "invalid_scope", "server_error", "temporarily_unavailable"}
)
_ERROR_PATTERN = re.compile(r"^[a-z_]{1,64}$")


def _normalize_provider_error(raw: str) -> str:
    """Allowlist для `?error=`: неизвестное значение -> `unknown`.

    CR 1.5, патч 21 и CR 1.6, патч 5: этот дефект пережил две правки, потому
    что выглядел исправленным. Разбор по шагам прежней версии:

    1. `value in _ALLOWED_PROVIDER_ERRORS` → вернуть `value`;
    2. иначе вернуть `value`, если оно подходит под `^[a-z_]{1,64}$`;
    3. иначе (мусор, пробелы, спецсимволы) → `unknown`.

    Шаг 2 делал шаг 1 бессмысленным: `access_denied` (в allowlist) и
    `some_random_thing` (не в allowlist) давали **одинаковый** результат — оба
    проходили regex и оба сохранялись в лог. То есть allowlist не влиял ни на
    что, и произвольная строка от клиента (`?error=любой_текст`) попадала
    в журнал. Теперь allowlist — единственное условие: всё, что вне его,
    сводится к `unknown`.

    Значение никогда не уходит наружу (наружу — `oauth_denied`), поэтому
    точность здесь нужна журналу, а не ответу. `_ERROR_PATTERN` сохранён
    для отсечения нестроковых/пустых значений до сравнения.
    """
    value = raw.strip().lower()
    if _ERROR_PATTERN.match(value) and value in _ALLOWED_PROVIDER_ERRORS:
        return value
    return "unknown"


@router.get("/{provider}/start")
async def start(provider: ProviderName) -> RedirectResponse:
    """Task 6.2 (AC-01, AC-02): выпускает `state` и уводит на страницу провайдера.

    `provider` объявлен `Literal`, а не `str`: неизвестный провайдер даёт `422`
    от валидации, а не `500` из глубины фабрики клиентов.

    На ответ ставится `karamel_oauth` cookie с nonce — второй рубеж против
    login CSRF: `state` подтверждает, что вход начат, cookie — что начат **этим**
    браузером. Без неё атакующий подсовывает жертве свой `code`+`state`.
    """
    try:
        issued = await issue_state(provider)
    except RedisUnavailableError:
        # Хранилище недоступно: без этой ветки пользователь получал 500,
        # а в access-логе uvicorn оставался URL с кодом и state.
        log_oauth_event(
            "oauth_start_failed", provider=provider, stage="start", error_code="storage_unavailable"
        )
        return _redirect_login_error("oauth_failed")
    client = get_client(provider)
    challenge = issued.code_challenge if provider == "vk" else None
    url = client.build_authorize_url(issued.state, challenge)
    log_oauth_event("oauth_start", provider=provider, stage="start")
    response = RedirectResponse(url, status_code=302)
    set_oauth_cookie(response, issued.state)
    return response


@router.get("/{provider}/callback", response_model=None)
async def callback(
    provider: ProviderName,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    device_id: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse | JSONResponse:
    """Task 6.3 (AC-01–AC-05): обработка возврата от провайдера.

    Любой отказ ведёт наружу нашим кодом ошибки (`oauth_denied`, `oauth_failed`,
    `invalid_state`, `invalid_callback`, `user_blocked`), текст провайдера
    в ответ не попадает (D8). Все отказы — редирект на `/login?error=`: голый
    JSON в браузере оставлял пользователя без пути назад в приложение.
    """
    # AC-04: отказ провайдера приходит как `?error=access_denied` — до всякой работы.
    if error is not None:
        log_oauth_event(
            "oauth_callback_failed",
            provider=provider,
            stage="callback",
            error_code=_normalize_provider_error(error),
        )
        # CR 1.5, патч 20: ветка отказа тоже проходит защиту. Раньше внешняя
        # ссылка `…/callback?error=access_denied` давала управляемый редирект
        # без сверки cookie и без гашения записи `state` — одноразовый ключ
        # оставался живым до TTL. Теперь запись гасится, если cookie совпала
        # со `state`, а `karamel_oauth` удаляется в любом случае.
        if state:
            cookie_state = request.cookies.get(oauth_cookie_name())
            if cookie_state and cookie_state == state:
                # Отказ провайдера уже случился; недоступность хранилища
                # не должна превращать редирект в 500.
                with contextlib.suppress(RedisUnavailableError):
                    await consume_state(state, provider)
        response = _redirect_login_error("oauth_denied")
        clear_oauth_cookie(response)
        return response

    if not state:
        log_oauth_event(
            "oauth_callback_failed", provider=provider, stage="callback", error_code="missing_state"
        )
        return _redirect_login_error("invalid_state")

    # Login CSRF: сверка cookie с `state` до погашения записи. Без этой проверки
    # `state` подтверждал лишь «ключ существует», а не «этот браузер начал вход».
    cookie_state = request.cookies.get(oauth_cookie_name())
    if not cookie_state or cookie_state != state:
        log_oauth_event(
            "oauth_callback_failed",
            provider=provider,
            stage="callback",
            error_code="state_cookie_mismatch",
        )
        return _redirect_login_error("invalid_state")

    # Форма запроса проверяется ДО погашения `state`: иначе битый запрос сжигает
    # одноразовый state, и пользователю нужен новый `/start` из-за технической ошибки.
    if not code:
        log_oauth_event(
            "oauth_callback_failed", provider=provider, stage="callback", error_code="missing_code"
        )
        return _redirect_login_error("oauth_failed")

    # Task 4.4 (AC-02): без `device_id` обмен у VK невозможен — отсекаем до сети.
    if provider == "vk" and not device_id:
        log_oauth_event(
            "oauth_callback_failed",
            provider=provider,
            stage="callback",
            error_code="invalid_callback",
        )
        return _redirect_login_error("invalid_callback")

    # AC-05: `state` гасится атомарно (GETDEL) — повтор с тем же значением не пройдёт.
    try:
        record = await consume_state(state, provider)
    except RedisUnavailableError:
        log_oauth_event(
            "oauth_callback_failed",
            provider=provider,
            stage="callback",
            error_code="storage_unavailable",
        )
        return _redirect_login_error("oauth_failed")
    if record is None:
        log_oauth_event(
            "oauth_callback_failed", provider=provider, stage="callback", error_code="invalid_state"
        )
        return _redirect_login_error("invalid_state")

    client = get_client(provider)
    try:
        token = await client.exchange_code(
            code, device_id=device_id, code_verifier=record.code_verifier
        )
        profile = await client.fetch_profile(token.access_token)
    except OAuthProviderError as exc:
        # AC-04: `error_code` и `stage` — без `code`, `state`, `access_token` и тела ответа.
        log_oauth_event(
            "oauth_callback_failed",
            provider=provider,
            stage=exc.stage,
            error_code=exc.error_code,
            external_id=exc.external_id,
        )
        return _redirect_login_error("oauth_failed")

    # AC-03: поиск или создание учётки по паре (provider, provider_user_id).
    try:
        user = await resolve_user(session, profile)
    except UserBlockedError:
        log_oauth_event(
            "oauth_callback_failed",
            provider=provider,
            stage="profile",
            error_code="user_blocked",
            external_id=profile.provider_user_id,
        )
        return _redirect_login_error("user_blocked")

    # AC-06: одна сессия на вход; BFF/ротация — стори 1.5 (D5).
    issued = await issue_session(session, user)
    # Task 6.4: коммит в роуте — пользователь и хеш refresh-токена одной транзакцией.
    await session.commit()

    log_oauth_event(
        "oauth_callback_success",
        provider=provider,
        stage="session",
        user_uuid=str(user.uuid),
        # CR 1.5, патч 22: здесь всегда сырое значение — `log_oauth_event`
        # хеширует `external_id` сам (одна точка, `external_id_hash`). Для phone
        # это E.164, то есть ПД: в журнал попадает только 12 hex-символов.
        external_id=profile.provider_user_id,
    )
    response = RedirectResponse(f"{settings.web_origin.rstrip('/')}/app", status_code=302)
    set_session_cookies(response, issued)
    # CR 1.5, патч 19: вход завершён — одноразовый `state` больше не нужен.
    # Cookie жила до TTL (10 минут) и оставалась в браузере уже после того,
    # как серверная запись погашена.
    clear_oauth_cookie(response)
    return response


@router.get("/me", response_model=SessionUserResponse)
async def me(user: CurrentUser, response: Response) -> SessionUserResponse:
    """Task 1.6/1.7 (AC-05): первый потребитель зависимости авторизации.

    Эндпоинт существует не ради бизнес-функции, а как проверяемое
    доказательство, что зависимость подключена: без него «живой access
    забаненного → 403» нечем проверить снаружи (AC-05).

    В ответе только `uuid`, `role`, `status` — ни `id`, ни `phone`,
    ни `provider` (AC-02, project-context §4).

    CR 1.5, патч 23: `Cache-Control: no-store` обязателен. Ответ привязан
    к сессии, а промежуточный кэш (CDN, корпоративный прокси) сохранил бы
    его и отдал следующему пользователю той же машины.
    """
    # Task 1.1 (AC-05, AC-06): добавлены `age_confirmed`, `consent_given_at`
    # и `gender`. Гарда зоны `(app)/app` и экран `/consent` решают по этим
    # полям, а не по наличию cookie (D-8). Время согласия — ISO 8601 UTC с `Z`
    # (project-context §4), пустое значение — `null`.
    # Патч 11: формат даты приходит из `iso_utc` (поле сериализатора модели),
    # вторая копия функции удалена.
    response.headers["cache-control"] = "no-store"
    return SessionUserResponse(
        uuid=user.uuid,
        role=user.role,
        status=user.status,
        gender=user.gender,
        age_confirmed=user.age_confirmed,
        consent_given_at=user.consent_given_at,
        # Решение 3: серверный признак «мужчина ждёт открытия заявок».
        # Заявок (таблица `applications`) в этой стори нет, поэтому мужчина
        # всегда ждёт: правило продукта выражено в контракте, а не только в UI.
        awaiting_applications=user.gender == "male",
    )


@router.post("/consent", response_model=ConsentResponse)
async def consent(
    payload: ConsentRequest,
    response: Response,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConsentResponse | JSONResponse:
    """Task 3.1–3.5 (AC-01, AC-02, AC-03, AC-04): подтверждение 18+ и согласия.

    Один атомарный факт: возраст и согласие на обработку ПД фиксируются
    вместе (D-3), пол — здесь же. Отдельные `POST /auth/age` и
    `/auth/pd-consent` дали бы учётку с подтверждённым возрастом без согласия.

    Коды отказа (project-context §4): `422 consent_required`,
    `422 gender_required` — семантика домена; `409 gender_locked` — конфликт
    состояния.; `401`/`403` приходят из зависимости `CurrentUser`.
    """
    try:
        updated, is_new = await record_consent(session, user, payload)
    except ConsentRequiredError:
        log_oauth_event(
            "consent_failed",
            provider="consent",
            stage="consent",
            error_code="consent_required",
            user_uuid=str(user.uuid),
        )
        return JSONResponse({"code": "consent_required"}, status_code=422)
    except GenderRequiredError:
        log_oauth_event(
            "consent_failed",
            provider="consent",
            stage="consent",
            error_code="gender_required",
            user_uuid=str(user.uuid),
        )
        return JSONResponse({"code": "gender_required"}, status_code=422)
    except GenderLockedError:
        log_oauth_event(
            "consent_failed",
            provider="consent",
            stage="consent",
            error_code="gender_locked",
            user_uuid=str(user.uuid),
        )
        return JSONResponse({"code": "gender_locked"}, status_code=409)
    except ConsentRaceError:
        # Патч 3: строку не удалось заблокировать (учётка исчезла между
        # чтением зависимости и блокировкой). `409` честнее `500`: состояние
        # конфликтует с текущим, и повтор запроса после перезагрузки решает.
        log_oauth_event(
            "consent_failed",
            provider="consent",
            stage="consent",
            error_code="consent_race",
            user_uuid=str(user.uuid),
        )
        return JSONResponse({"code": "consent_race"}, status_code=409)

    # D-4 стори 1.2: единица работы у роута.
    await session.commit()
    log_oauth_event(
        "consent_recorded",
        provider="consent",
        stage="consent",
        user_uuid=str(updated.uuid),
    )
    # Патч 10: ответ описывается `ConsentResponse` (`response_model`),
    # поэтому модель собирается один раз и уходит в OpenAPI.
    response.headers["cache-control"] = "no-store"
    # Task 3.3: без `id`, без `phone`, без `provider_user_id` (AR21/AR22).
    return ConsentResponse(
        uuid=updated.uuid,
        status=updated.status,
        gender=updated.gender,
        age_confirmed=updated.age_confirmed,
        consent_given_at=updated.consent_given_at,
        is_new=is_new,
    )


@router.post("/refresh", response_model=None)
async def refresh(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Task 3.7 (AC-02, AC-05, AC-06): ротация пары по refresh-cookie.

    `response_model=None`: тело ответа — фиксированный `{"expires_in"}`, а не
    сериализация модели, и токены в него не попадают никогда (AC-08).
    """
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh:
        # Task 3.2: нет cookie — обновлять нечего.
        log_oauth_event(
            "session_refresh_failed",
            provider="session",
            stage="refresh",
            error_code="missing_cookie",
        )
        return _session_error("session_expired", 401)

    try:
        issued = await rotate_session(session, raw_refresh)
    except SessionUnknownError:
        # CR 1.5, патч 16: токен неизвестен (в том числе погашен конкурентом).
        # Ответ — без `delete_cookie`: проигравший в гонке refresh отвечает
        # позже победителя, и `Max-Age=0` стёр бы только что выданную пару.
        log_oauth_event(
            "session_refresh_failed",
            provider="session",
            stage="refresh",
            error_code="session_unknown",
        )
        response = JSONResponse({"code": "session_expired"}, status_code=401)
        response.headers["cache-control"] = "no-store"
        return response
    except SessionExpiredError:
        # Task 3.8 (AC-06): просроченный/чужой токен.
        log_oauth_event(
            "session_refresh_failed",
            provider="session",
            stage="refresh",
            error_code="session_expired",
        )
        return _session_error("session_expired", 401)
    except UserBlockedError:
        # Task 3.5/3.8 (AC-05): бан обнаружен — все токены отозваны внутри
        # `rotate_session`, cookie очищаются, повтор от клиента запрещён.
        log_oauth_event(
            "session_refresh_failed",
            provider="session",
            stage="refresh",
            error_code="user_blocked",
        )
        return _session_error("user_blocked", 403)

    # D-4: consume старого и create нового — одной транзакцией.
    # CR 1.5, патч 2: гашение уже зафиксировано внутри `rotate_session`,
    # поэтому сбой этого коммита оставляет старый токен мёртвым (безопасно),
    # а новый просто не выдаётся.
    await session.commit()
    log_oauth_event("session_refreshed", provider="session", stage="refresh")
    response = JSONResponse({"expires_in": ACCESS_COOKIE_MAX_AGE})
    response.headers["cache-control"] = "no-store"
    set_session_cookies(response, issued)
    return response


@router.post("/logout", response_model=None)
async def logout(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Task 4.2/4.3 (AC-04, D-6): выход идемпотентен, тело пустое (`204`).

    Без cookie, с мусорной cookie, с уже отозванной — всегда `204`.
    Ошибка на повторном нажатии «Выйти» бесполезна пользователю и мешает
    клиенту безопасно повторять запрос.

    CR 1.5, патч 18: отказ БД при отзыве тоже даёт `204` с очисткой cookie.
    Без этого `try/except` недоступность базы оставляла пользователя
    в залогиненном состоянии: cookie живы, выйти нельзя.
    """
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh:
        try:
            await revoke_session(session, raw_refresh)
            await session.commit()
            log_oauth_event("session_logout", provider="session", stage="logout")
        except Exception:  # noqa: BLE001 — выход не должен падать никогда
            # Гасим несостоявшуюся транзакцию, чтобы сессия соединения
            # осталась пригодной, и продолжаем: cookie очищаются ниже.
            await session.rollback()
            log_oauth_event(
                "session_logout_failed",
                provider="session",
                stage="logout",
                error_code="storage_unavailable",
            )

    # Task 4.2: `204` без тела. `delete_cookie` ставит `Max-Age=0` с теми же
    # `Path`/`Domain`/`Secure`, что и выдача (D-8).
    response = Response(status_code=204)
    clear_session_cookies(response)
    # CR 1.5, патч 19: незавершённый OAuth-флоу тоже не переживает выход.
    clear_oauth_cookie(response)
    return response


def _session_error(code: str, status_code: int) -> JSONResponse:
    """Task 3.8 (AC-02, AC-05, AC-06): отказ с обязательной очисткой cookie.

    Без очистки браузер продолжает отправлять мёртвый refresh на каждый
    запрос: пользователь видит отказы, а сессия не сбрасывается.
    """
    response = JSONResponse({"code": code}, status_code=status_code)
    clear_session_cookies(response)
    return response


def _redirect_login_error(code: str) -> RedirectResponse:
    """AC-04: пользователь возвращается на `/login` с нашим кодом, не с текстом провайдера.

    CR 1.5, патч 19: гасится `karamel_oauth`. Через этот хелпер проходят все
    отказы callback, и cookie со `state` не должна переживать ни один из них:
    серверная запись погашена или бесполезна, а браузер продолжал бы слать
    её в следующий вход.
    """
    response = RedirectResponse(
        f"{settings.web_origin.rstrip('/')}/login?error={code}", status_code=302
    )
    clear_oauth_cookie(response)
    return response
