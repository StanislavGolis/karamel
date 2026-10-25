"""Зависимости авторизации (Task 1; AC-01, AC-05; D-1, D-2, D-3).

`deps.py` лежит рядом с `v1/`, а не внутри него: зависимость переиспользует
WebSocket-роут эпика 5, а он не принадлежит версии HTTP-API.

Здесь `HTTPException`, в отличие от роутов (`JSONResponse` с `{"code": ...}`):
зависимость не может вернуть ответ, она может только бросить исключение.
Тело ошибки приводится к тому же контракту `{"code": ...}` обработчиком
`auth_error_handler` в `main.py`.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import InvalidTokenError, decode_access_token
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.session_service import ACCESS_COOKIE_NAME

UNAUTHENTICATED = {"code": "unauthenticated"}
USER_BLOCKED = {"code": "user_blocked"}


def _is_revoked(user: User, payload: dict[str, object]) -> bool:
    """Решение 1 CR 1.5: access, выпущенный до отзыва сессий, недействителен.

    Выход и бан ставят `users.sessions_revoked_at = now()`. Access живёт час
    и не сверяется с БД по `jti`, поэтому без этой проверки после «Выйти»
    на чужом устройстве доступ к переписке сохранялся до истечения токена.

    Сравнивается `iat` (секунды), а `sessions_revoked_at` — микросекунды:
    `int()` усечения не допускает, поэтому метка берётся в тех же секундах.
    Токен без `iat` считается выпущенным до отзыва — безопасная сторона отказа.
    """
    revoked_at = user.sessions_revoked_at
    if revoked_at is None:
        return False
    issued_at = payload.get("iat")
    if not isinstance(issued_at, int | float):
        return True
    if revoked_at.tzinfo is None:
        revoked_at = revoked_at.replace(tzinfo=UTC)
    return issued_at < revoked_at.timestamp()


def _extract_token(request: Request) -> str | None:
    """Task 1.2 (AC-01, D-2): cookie первым, `Authorization: Bearer` вторым.

    Порядок не косметика: при обратном порядке любой, кто может поставить
    заголовок (прокси, расширение браузера, отладчик), перекрыл бы cookie
    и подменил пользователя. Cookie — единственный транспорт, которым
    владеет продукт, поэтому она выигрывает.

    Заголовок нужен для случаев, где cookie не приезжает: раздельные
    поддомены, мобильный клиент, внутренние вызовы.
    """
    cookie_token = request.cookies.get(ACCESS_COOKIE_NAME)
    if cookie_token:
        return cookie_token

    header = request.headers.get("authorization")
    if not header:
        return None
    # CR 1.5, патч 25: `split(None, 1)` режет по любому whitespace, а не только
    # по пробелу. RFC 7235 разрешает `Bearer\t<token>`, и `partition(" ")`
    # возвращал scheme="Bearer\t<token>" → 401 на валидном заголовке.
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts
    # Схема сравнивается без учёта регистра (RFC 7235), значение — как есть.
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Task 1.1 (AC-01, AC-05): текущий пользователь или отказ.

    D-1: нет токена и битый токен дают одинаковый `401 unauthenticated`.
    Различать клиенту нечего, а разница «токен был, но неверный» помогает
    перебору.

    D-3: статус читается из БД на **каждом** запросе. Access живёт час,
    поэтому бан, проверенный только при выдаче токена, вступил бы в силу
    через час — AC-05 требует «немедленно».
    """
    token = _extract_token(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED)

    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        # Task 1.3: истёкший или подделанный токен — 401, не 500.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED
        ) from None

    # CR 1.5, патч 6: приведение внутри `try/except`. `decode_access_token`
    # проверяет форму `sub`, но полагаться на это нельзя: подписанный токен
    # с не-UUID `sub` (или подменённый ключ подписи) давал 500 вместо 401
    # прямо в зависимости, минуя обработчик ошибок токена.
    try:
        user_uuid = uuid_lib.UUID(str(payload["sub"]))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED
        ) from None
    user = await UserRepository(session).get_by_uuid(user_uuid)
    if user is None:
        # Task 1.4: токен подписан верно, но учётки нет (удалена) — 401.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED)

    # Решение 1 CR 1.5: отзыв сессий (выход, бан) гасит и уже выданный access.
    # 401, а не 403: клиент должен обновить пару или уйти на вход; 403
    # заставил бы его показать «заблокирован» вышедшему пользователю.
    if _is_revoked(user, payload):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED)

    # Task 1.5 (AC-05): бан и запрошенное удаление — 403, не 401.
    # 401 заставил бы клиента идти обновлять сессию и создал шторм refresh.
    if user.status == "banned" or user.deletion_requested_at is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=USER_BLOCKED)

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

# CR 2.1, Решение 2: закрытый конверт ошибки «нет согласия на ПД».
# Не путать с `422 consent_required` у `POST /auth/consent` (стори 1.6):
# там это ошибка входных данных формы, здесь — запрет доступа к ресурсу.
CONSENT_REQUIRED = {"code": "consent_required"}


async def get_current_user_with_consent(user: CurrentUser) -> User:
    """CR 2.1, Решение 2 (FR9, 152-ФЗ): доступ к анкете только после 18+ и согласия.

    Проблема, которую закрывает зависимость: `get_current_user` проверяет
    сессию и бан, но не согласие. Правило «18+ и согласие» жило только
    в Next-гарде, и прямой вызов API с украденной cookie отдавал имя, город,
    возраст и био пользователя, не давшего согласия на обработку ПД.

    Проверяются оба факта, а не только `consent_given_at`: CHECK
    `ck_users_consent_pair` (миграция 0003) держит их парой, но зависимость
    не должна полагаться на инвариант, который проверяется в другой таблице.

    `403`, а не `401`: сессия валидна, пользователь опознан — ему запрещён
    ресурс, пока он не прошёл онбординг. `401` заставил бы клиента идти
    обновлять токен и создал бы шторм refresh.
    """
    if not user.age_confirmed or user.consent_given_at is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CONSENT_REQUIRED)
    return user


ConsentUser = Annotated[User, Depends(get_current_user_with_consent)]


# Стори 3.2, Task 1.1 (AC-01, R-01): закрытый конверт отказа администратора.
# Отдельная константа, а не `{"code": "forbidden"}` по месту: код сравнивается
# клиентом, и опечатка в одной из копий дала бы неотличимую ошибку «что-то
# пошло не так». Значение совпадает с `_STATUS_CODES[403]` в `main.py` — тот же
# статус всегда даёт тот же код (D-2: пустое тело ломало бы конверт §4).
logger = structlog.get_logger(__name__)

MODERATOR_FORBIDDEN = {"code": "forbidden"}


async def require_moderator(user: CurrentUser) -> User:
    """Task 1.1–1.3 (AC-01, R-01): запись в справочник — только модератору.

    Администратор справочника — `UserRole.MODERATOR` (AR30 PRD, AC 3.2 эпика 3):
    отдельной роли не заводим, миграции `users` нет. Модератор — единственная
    привилегированная роль продукта, и второй уровень прав («редактор
    справочника») без потребителя стал бы неотличим от модератора на практике.

    Зависимость стоит на `CurrentUser`, а НЕ на `ConsentUser` (D-3):
    у администратора может не быть анкеты и согласия на ПД, а справочник вести
    он обязан. Бан, запрошенное удаление и отзыв сессии уже проверены
    `get_current_user`, поэтому здесь остаётся только роль.

    `403`, а не `401`: сессия валидна, пользователь опознан — ему запрещён
    ресурс. `401` заставил бы клиента идти обновлять токен и создал бы шторм
    refresh на каждой попытке (то же решение, что в `ConsentUser`).

    Тело — `{"code": "forbidden"}` без `details` (D-2): отказ не должен
    превращаться в карту прав, подсказывая, чего именно не хватает.

    Роль читается из БД на каждом запросе (её приносит `get_current_user`),
    поэтому понижение прав действует немедленно — как и бан (D-3 стори 1.2).
    """
    # Task 1.2: сравнение с членом `StrEnum`, а не со строкой по месту.
    # Из БД роль приходит строкой, поэтому сравнение идёт по значению: `StrEnum`
    # сравнивается со своей строкой, и обе формы проходят. Обратное условие
    # (`!= UserRole.USER`) открывало бы запись любой незнакомой роли.
    if user.role != UserRole.MODERATOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MODERATOR_FORBIDDEN)
    return user


ModeratorUser = Annotated[User, Depends(require_moderator)]


async def require_audited_moderator(request: Request, user: CurrentUser) -> User:
    """AC-04 стори 8.6 (D-85): `403` пишется в лог, ответ не меняется.

    Зависимость стоит ПОСЛЕ `ModeratorUser`, то есть срабатывает только на
    отказе. Успешный доступ она не трогает: аудит доступа пишет сервис
    (в таблицу), а здесь — только факт ОТКАЗА, и только в `structlog`.

    **Почему отказ не идёт в `access_logs`.** Строка на отказ создала бы
    вектор усиления записи: любой пользователь с валидной сессией набивал бы
    таблицу запросами, которые никогда не раскроют данных. Плюс в записи нет
    `chat_uuid`/`card_uuid` для неудавшегося запроса — она описывает доступ,
    которого не было.

    **Почему `route`, а не `request.url`.** Путь без query-строки: параметры
    запроса — ввод клиента, и в аудите отказа им места нет. `path` уже
    нормализован роутером.

    **`raise` без `from`.** Исключение пере-бросается тем же объектом: тело
    `{"code": "forbidden"}` и `401/403`-семантика не меняются, иначе вторая
    копия ответа разошлась бы с первой при правке.

    **Подзависимость — `CurrentUser`, а НЕ `ModeratorUser`.** Это не стиль:
    FastAPI резолвит подзависимости ДО входа в тело функции, поэтому
    `ModeratorUser` поднял бы `403` мимо этого `try/except`, и событие
    `moderation.access_denied` не записалось бы никогда. Проверено прогоном:
    с `ModeratorUser` тест AC-04 получал пустой `capture_logs()`.
    """
    try:
        return await require_moderator(user)
    except HTTPException:
        logger.warning(
            "moderation.access_denied",
            user_uuid=str(user.uuid),
            route=request.url.path,
        )
        raise


AuditedModeratorUser = Annotated[User, Depends(require_audited_moderator)]


# Стори 3.3, Task 5.1 (AC-03, R-03, R-06): результат зависимости чтения.
# `include_inactive` живёт В ОБЪЕКТЕ, а не отдельным параметром роутера:
# параметр, объявленный и в зависимости, и в сигнатуре, дал бы два источника,
# и роутер мог бы прочитать `False`, пока зависимость проверила роль для `True`.
@dataclass(frozen=True)
class PlacesReader:
    """AC-01, AC-03: кто читает справочник и видит ли он скрытые места.

    `user` уже прошёл `get_current_user` (сессия, бан, отзыв). `include_inactive`
    здесь — результат ПРОВЕРКИ, а не запрос клиента: `True` в этом объекте
    означает, что роль модератора подтверждена `require_moderator`.
    """

    user: User
    include_inactive: bool


async def get_places_reader(
    user: CurrentUser,
    include_inactive: Annotated[bool, Query()] = False,
) -> PlacesReader:
    """AC-03, R-06: `include_inactive=true` — только для модератора.

    R-06: тот же гард, что и на записи, — `require_moderator`. Варианты
    «тихо игнорировать флаг» и «422 invalid_request» отклонены: разные формы
    отказа в записи и чтении дали бы клиенту два вида одной ошибки.

    Условный вызов внутри зависимости, а не второй роут с `ModeratorUser`:
    один источник сравнения роли. Роутер получает готовый `include_inactive`
    и не может «забыть» проверку — иначе список придётся раздавать скрытым
    местам всем, а `total` у админа и участника разойдётся без причины.

    `403` с телом `{"code": "forbidden"}` (D-2): отказ не превращается в карту
    прав. `ConsentUser` НЕ используется: справочник читает участник, у которого
    может не быть анкеты, а согласие на ПД — предмет других маршрутов.

    Роль читается из БД на каждом запросе (`get_current_user`), поэтому
    понижение прав действует немедленно.
    """
    if include_inactive:
        await require_moderator(user)
        return PlacesReader(user=user, include_inactive=True)
    return PlacesReader(user=user, include_inactive=False)


# Task 5.3: алиас для сигнатуры роутера.
PlacesReaderUser = Annotated[PlacesReader, Depends(get_places_reader)]
