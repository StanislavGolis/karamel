"""История явок пользователя для модератора (стори 7.5; AC-01…AC-03, AC-05, AC-06, AC-09).

Роут не содержит бизнес-логики: проверки живут в
`services/attendance_history_service.py`, запросы — в репозитории
(project-context §6). Здесь только HTTP-форма: статусы, конверт ошибки
`{"code": ...}` и разбор курсора.

**Гард — `ModeratorUser` на маршруте** (R-01, R-06, D-7.5-2): та же
зависимость, что на всех маршрутах `/moderation/*`, поэтому второго правила
доступа не появляется, а `grep` по `ModeratorUser` показывает полный список
модераторских входов. Обычный пользователь получает `403
{"code": "forbidden"}` ДО резолва `user_uuid`, поэтому код ответа не зависит
от существования пользователя и оракула не даёт (AC-09 O-1).

**`404 user_not_found`, а не `403`** (AC-03): модератору ресурс разрешён, а
несуществующий адрес — это отсутствие ресурса. Для не-модератора `404` не
наступает никогда: гард стоит раньше (AC-09 O-3).

**Почему `_error` продублирован, а не импортирован из `moderation.py`**
(Task 4.3): `_error` там приватный и привязан к префиксу `/moderation/cards`;
импорт приватного имени связал бы два независимых ресурса, и правка конверта
очереди меняла бы историю явок. Четыре строки с общей ссылкой на
project-context §4 дешевле связанности.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditedModeratorUser, ModeratorUser
from app.core.db import get_session
from app.schemas.attendance_history import AttendanceHistoryPage
from app.schemas.moderation_user import ModerationUserCard
from app.services.attendance_history_cursor import (
    InvalidCursorError,
    decode_history_cursor,
)
from app.services.attendance_history_service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    AttendanceHistoryForbiddenError,
    list_history,
)
from app.services.moderation_user_card_service import (
    UserNotFoundError,
    load_user_card,
)

router = APIRouter(prefix="/moderation/users", tags=["moderation"])

# AC-09 O-5, project-context §4: единственный конверт ошибки `{"code": ...}`.
#
# CR 7.5 L-2: `_NO_STORE` передаётся в `_error` ЯВНО, хотя `NoStoreMiddleware`
# (QD A6) и так ставит заголовок на каждый ответ. Это не дублирование: строки
# `moderation.py`/`profiles.py` передают его тем же способом, и здесь сохранён
# ОДИН способ установки заголовка на ошибке в пределах проекта. Middleware
# видит готовый заголовок и второй не добавляет (`if not any(name.lower() ==
# b"cache-control")`), то есть второй путь заголовка не создаётся. Убрать
# аргумент значило бы, что конверт ошибки истории держится на middleware —
# единственном месте, которое можно отключить в тесте и не заметить.
_NO_STORE = {"cache-control": "no-store"}

#: AC-03: код отсутствующего пользователя. Отдельный от `card_not_found`:
#: адресат другой, и склейка кодов связала бы два ресурса.
USER_NOT_FOUND_CODE = "user_not_found"

#: NFR19: явный код отказа при недоступной БД — как у остальных приватных
#: ответов (образец `reports.py`, `moderation_reports.py`).
SERVICE_UNAVAILABLE_CODE = "service_unavailable"


#: AC-03: резолв публичного адреса во внутренний PK. Отдельный `SELECT id`, а
#: не `SELECT *`: колонки пользователя истории явок не нужны, и лишнее чтение
#: ПД (почта, телефон) в модераторском кабинете было бы расширением доступа
#: без потребителя (NFR39).
_RESOLVE_USER_SQL = "SELECT id FROM users WHERE uuid = :uuid"


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": ...}` — как в `moderation.py` и `profiles.py`.

    Клиент ветвится по `code`; `cache-control` ставит middleware.
    """
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


@router.get("/{user_uuid}/attendance", response_model=AttendanceHistoryPage)
async def read_user_attendance(
    user_uuid: uuid_lib.UUID,
    moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> AttendanceHistoryPage | JSONResponse:
    """AC-01, AC-03, AC-05, AC-06: история явок пользователя для модератора.

    Порядок — КОНТРАКТ (три SQL максимум, не зависит от `limit`):
    (1) гард `ModeratorUser` — `403` до любого SQL (AC-09 O-3);
    (2) резолв `user_uuid → id` — `404 user_not_found` (ровно 1 SQL);
    (3) чтение истории — 1 SQL (без курсора) или 1 SQL (с курсором).

    `moderator` не используется в теле намеренно: значение имеет ФАКТ проверки,
    а не объект. Имя `moderator`, а не `_`, потому что FastAPI показывает его
    в OpenAPI, и по сигнатуре должно быть видно, какой именно гард стоит.

    Пользователь без записей — `200` с пустым `items`, НЕ `404` (AC-01): нет
    истории ≠ нет пользователя, и склейка этих случаев заставила бы клиент
    различать их по коду ошибки.

    Битый `cursor` — `422 invalid_cursor` (AC-06), а не `500`: значение пришло
    от клиента.
    """
    resolved = await session.execute(text(_RESOLVE_USER_SQL), {"uuid": user_uuid})
    participant_id = resolved.scalar_one_or_none()
    if participant_id is None:
        # AC-03: модератор уже прошёл гард, поэтому `404` не раскрывает
        # существования пользователя никому, кому ресурс запрещён.
        return _error(USER_NOT_FOUND_CODE, 404)

    if cursor is not None:
        try:
            position = decode_history_cursor(cursor)
        except InvalidCursorError:
            # AC-06: `422`, не `500` — курсор пришёл от клиента.
            return _error(InvalidCursorError.code, 422)
    else:
        position = None

    try:
        return await list_history(
            session,
            participant_id=participant_id,
            viewer_id=moderator.id,
            is_moderator=True,
            limit=limit,
            cursor=position,
        )
    except AttendanceHistoryForbiddenError:
        # Task 3.1: недостижимо на этом маршруте (`is_moderator=True`), но
        # ветка есть — иначе смена гарда тихо превратила бы отказ в `500`.
        return _error(AttendanceHistoryForbiddenError.code, 403)


@router.get("/{user_uuid}", response_model=ModerationUserCard)
async def read_user_card(
    user_uuid: uuid_lib.UUID,
    request: Request,
    moderator: AuditedModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ModerationUserCard | JSONResponse:
    """AC-01…AC-11 (8.5): сводка пользователя для модератора (FR54).

    **Гард — `ModeratorUser` на маршруте** (D-65): обычный пользователь
    получает `403 forbidden` ДО любого SQL, поэтому код ответа не зависит от
    существования адресата и оракула не даёт (то же правило, что в 7.5).

    **`404 user_not_found`, а не `403`** — тот же код, что у
    `/{user_uuid}/attendance`: адресат один и тот же пользователь, и разные
    коды на двух маршрутах заставили бы клиент ветвиться по маршруту.

    **Число SQL — константа на обеих ветвях** (AC-11, D-65): «пользователя
    нет» и «есть без данных» стоят одинаково, иначе существование читается
    по числу обращений. Выравнивание живёт в сервисе (`_MISSING_USER_ID`),
    не в роуте: второй вход в это правило разошёлся бы с первым.

    **Пути не конфликтуют.** `/{user_uuid}` и `/{user_uuid}/attendance`
    различаются числом сегментов; регресс 7.5 проверяется тестом, а не
    порядком объявления.
    """
    try:
        card = await load_user_card(
            session,
            user_uuid=user_uuid,
            moderator_uuid=moderator.uuid,
            route=request.url.path,
        )
        # Стори 8.6 (AC-08, D-90): `commit` — в роуте, внутри `try`. 8.5 только
        # читал; запись аудита обязана зафиксироваться, и при ошибке записи
        # ответ не уходит, а строки не остаётся.
        await session.commit()
    except UserNotFoundError as error:
        # Стори 12.10, CR H-1 (AC-03, D-96): аудит пишется в сервисе ДО
        # `raise`, поэтому на ветви `404` в сессии уже лежит строка. Без
        # коммита `get_session` закрывает сессию откатом, и строка теряется:
        # модератор мог бы перебирать адреса без следа — ровно то, что D-96
        # объявляет предотвращённым.
        #
        # Коммит отдельный, а не в общем `finally`: ветвь `OperationalError`
        # коммитить не может (БД недоступна), и общий коммит превратил бы
        # `503` в исключение из обработчика. Вложенный `try` держит правило
        # 8.6/AC-08 и здесь: если запись аудита не зафиксировалась, ответ
        # не уходит — иначе модератор получил бы `404` без следа в журнале.
        try:
            await session.commit()
        except OperationalError:
            return _error(SERVICE_UNAVAILABLE_CODE, 503)
        return _error(error.code, 404)
    except OperationalError:
        # NFR19: БД недоступна — `503` с явным кодом, а не `500` без тела.
        return _error(SERVICE_UNAVAILABLE_CODE, 503)

    return card
