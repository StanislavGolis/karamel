"""Выдача сессии на входе (Task 7.3, 7.4; AC-06).

D5: граница со стори 1.5. Здесь создаётся **одна** сессия на момент входа:
BFF-прокси, автообновление access, ротация refresh, выход и бан — стори 1.5.
Если бы ротация появилась здесь, 1.5 переделывала бы этот код.

Единственная точка выдачи токенов: второй путь (например, в роуте SMS-входа
стори 1.4) породил бы расхождение в TTL и в составе cookie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    encode_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from app.models.user import User
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.account_deletion_service import deletion_window_expired
from app.services.auth_service import UserBlockedError


class SessionExpiredError(Exception):
    """Task 3.2–3.4 (AC-02, AC-06): обновлять нечего.

    Наружу у всех причин один код `session_expired`: клиенту различать нечего,
    а разные коды позволили бы перебором отличать «токен существовал» от
    «токена не было».
    """


class SessionUnknownError(SessionExpiredError):
    """CR 1.5, патч 16: строки с таким хешем нет — неизвестный или уже погашенный токен.

    Наследник `SessionExpiredError`: существующие обработчики продолжают ловить
    один тип, но роут различает случай и **не** очищает cookie. Проигравший
    в гонке refresh получает этот отказ; если бы его ответ ставил `Max-Age=0`
    и приходил после победителя, браузер стёр бы только что выданную живую пару.
    """


# AC-06: NFR14 — access 1 час, refresh 30 дней.
ACCESS_COOKIE_MAX_AGE = 3600
REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30

ACCESS_COOKIE_NAME = "karamel_access"
REFRESH_COOKIE_NAME = "karamel_refresh"


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Открытые значения токенов. Живут только до установки cookie."""

    access_token: str
    refresh_token: str


async def issue_session(session: AsyncSession, user: User) -> IssuedSession:
    """Task 7.3 (AC-06): access-токен + refresh-токен, в БД только хеш.

    `create_token` возвращает запись, но открытое значение в неё не попадает:
    хеш считается здесь, поэтому открытый токен не проходит через репозиторий.
    """
    access_token = encode_access_token(user)
    refresh_token = new_refresh_token()
    repository = RefreshTokenRepository(session)
    await repository.create_token(user.id, hash_refresh_token(refresh_token))
    return IssuedSession(access_token=access_token, refresh_token=refresh_token)


def set_session_cookies(response: Response, issued: IssuedSession) -> None:
    """Task 7.4 (AC-06): cookie `HttpOnly`, `SameSite=Lax`, `Path=/`.

    `HttpOnly` — токен недоступен из JS, значит XSS не крадёт сессию.
    `SameSite=Lax` — форма входа возвращает пользователя кросс-сайтовым GET,
    `Strict` сломал бы возврат с oauth.yandex.ru; при этом POST-запросы
    с чужих сайтов cookie не приложат.
    `Secure` включается вне development: по http флаг просто отбросит cookie.
    """
    secure = settings.cookie_secure
    # Task 6.3 (AC-01, D-11): `domain` передаётся только когда задан.
    # `domain=None` в Starlette означает «не ставить атрибут» — это host-only
    # cookie, единственно верный вариант по умолчанию: поддомены её не получат.
    domain = settings.session_cookie_domain
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        issued.access_token,
        max_age=ACCESS_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
        domain=domain,
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        issued.refresh_token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
        domain=domain,
    )


def clear_session_cookies(response: Response) -> None:
    """Task 5.1 (AC-04, AC-05, AC-06): удаляет обе session-cookie.

    D-8: атрибуты удаления повторяют атрибуты выдачи **точно**. Cookie
    идентифицируется тройкой (имя, домен, путь): `delete_cookie` с другим
    доменом — это другая cookie, и старая осталась бы в браузере, продолжая
    отправляться на API.
    """
    domain = settings.session_cookie_domain
    secure = settings.cookie_secure
    for name in (ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME):
        response.delete_cookie(
            name,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
            domain=domain,
        )


async def rotate_session(session: AsyncSession, raw_refresh_token: str) -> IssuedSession:
    """Task 3.1 (AC-02, AC-05, AC-06): атомарная ротация пары токенов.

    D-4: порядок consume → issue → `commit()` (коммит в роуте) обязателен.
    Если сначала выпустить новую пару, а гашение старой упадёт с ошибкой,
    в браузере и в БД останутся **два** живых refresh-токена — ровно то,
    что ротация должна исключать. Обратный порядок безопасен: падение
    выпуска откатывает транзакцию, и старый токен остаётся живым.

    D-5: `consume_for_rotation` вызывается до проверки срока. Строка нужна
    ради `user_id` — без неё нельзя отозвать все токены забаненного (AC-05).
    Гашение просроченного токена безвредно: он и так не пройдёт проверку срока.
    """
    repository = RefreshTokenRepository(session)
    token_hash = hash_refresh_token(raw_refresh_token)

    # Task 3.3: `None` = токена нет или он уже погашен (в том числе конкурентом).
    stored = await repository.consume_for_rotation(token_hash)
    if stored is None:
        # CR 1.5, патч 16: отличаем «токена не было» от «просрочен/забанен»:
        # очищать cookie в этом случае нельзя, иначе ответ проигравшего в гонке
        # гасит пару, которую только что выдал победитель.
        raise SessionUnknownError

    # CR 1.5, патч 2: гашение фиксируется ДО любой дальнейшей проверки.
    # Иначе `raise` ниже откатывает `consume_for_rotation`, украденный токен
    # остаётся живым и replay повторяется — reuse-detection не работает.
    # Отдельный коммит безопасен: он гасит старый токен и ничего не выпускает.
    await session.commit()

    # Task 3.4: срок проверяется после гашения — погашение просроченного
    # безвредно, а `user_id` нужен даже для него.
    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise SessionExpiredError

    # Task 3.5 (AC-05): бан между запросами. Проверка в БД, а не в JWT:
    # access живёт час, и «немедленный» бан иначе невыполним.
    user = await UserRepository(session).get_by_id(stored.user_id)
    if user is None:
        # CR 1.5, патч 24: удалённая учётка — это истёкшая сессия (401),
        # а не бан (403). «Заблокирован» для несуществующего пользователя
        # вводит клиента в заблуждение и не даёт выйти на экран входа.
        raise SessionExpiredError
    if user.status == "banned" or deletion_window_expired(user, now=datetime.now(UTC)):
        await repository.revoke_all_for_user(stored.user_id)
        # CR 1.5, патч 1: отзыв токенов и метка `sessions_revoked_at`
        # должны попасть в БД — иначе бан не действует до конца сессии.
        await session.commit()
        raise UserBlockedError

    # Task 3.6: выпуск в той же транзакции; коммит делает роут —
    # consume и create должны попасть в БД вместе или не попасть вовсе.
    return await issue_session(session, user)


async def revoke_session(session: AsyncSession, raw_refresh_token: str | None) -> None:
    """Task 4.1 (AC-04, D-6): выход завершает **все** сессии пользователя.

    Токена нет или он мусорный — не ошибка: выход должен быть идемпотентным.
    Ошибка на повторном нажатии «Выйти» выкидывала бы `500` вместо `204`.

    CR 1.5, патч 17: поиск идёт `get_by_hash`, без фильтров срока и бана.
    `get_active` возвращал `None` для просроченного токена и для забаненного
    пользователя — и выход молча ничего не отзывал именно в тех случаях,
    когда «выйти со всех устройств» нужнее всего.
    """
    if not raw_refresh_token:
        return
    repository = RefreshTokenRepository(session)
    stored = await repository.get_by_hash(hash_refresh_token(raw_refresh_token))
    if stored is None:
        return
    await repository.revoke_all_for_user(stored.user_id)


def oauth_cookie_name() -> str:
    """Имя cookie, привязывающей `state` к браузеру (защита от login CSRF)."""
    return settings.oauth_cookie_name


def access_cookie_name() -> str:
    """Имя access-cookie: используется зависимостью авторизации."""
    return ACCESS_COOKIE_NAME


def set_oauth_cookie(response: Response, state: str) -> None:
    """Кладёт nonce-копию `state` в cookie на время OAuth-флоу.

    Сверка cookie со `state` на callback доказывает, что вход начат **этим**
    браузером. Без неё атакующий передаёт жертве свой `code`+`state`, и жертва
    получает сессию чужого аккаунта (login CSRF / фиксация сессии).

    `SameSite=Lax` обязателен: провайдер возвращает пользователя кросс-сайтовым
    GET, и `Strict` отбросил бы cookie ровно в этот момент.
    """
    response.set_cookie(
        key=oauth_cookie_name(),
        value=state,
        max_age=settings.oauth_state_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_oauth_cookie(response: Response) -> None:
    """CR 1.5, патч 19: удаляет `karamel_oauth` после использования.

    Cookie жила до истечения `OAUTH_STATE_TTL_SECONDS` (10 минут) в любом
    исходе: успешный вход, отказ провайдера, ошибка обмена. В браузере
    оставался `state`, пригодный для повторной попытки login CSRF в пределах
    TTL, хотя серверная запись уже погашена. Атрибуты повторяют `set_oauth_cookie`:
    cookie идентифицируется тройкой (имя, домен, путь).
    """
    response.delete_cookie(
        oauth_cookie_name(),
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
