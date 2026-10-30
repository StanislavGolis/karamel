"""Справочник мест: запись из кабинета администратора (стори 3.2, Task 5–7).

AC-01…AC-07. Роут не содержит бизнес-логики: нормализация, запрет дубля,
различение активного и скрытого дубля живут в `services/place_service.py`,
запросы — в репозитории (project-context §6, §9). Здесь только HTTP-форма:
статусы, конверт ошибки, `cache-control` и единица работы (`commit`).

Стори 3.3 (Task 6): `GET /places` читает справочник с фильтром по категории
и курсорной пагинацией. `include_inactive` (R-03, R-06) объявлен ТОЛЬКО
в зависимости `get_places_reader` — второй источник в сигнатуре роутера
позволил бы прочитать `False`, пока зависимость проверила роль для `True`.

R-04: `DELETE` — всегда мягкое удаление (`is_active=false`). Физического
удаления нет ни на одном пути: `session.delete` в коде отсутствует, и это
зафиксировано тестом чтения строки из БД.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ModeratorUser, PlacesReaderUser
from app.core.config import settings
from app.core.db import get_session
from app.models.enums import PlaceCategory
from app.schemas.place import (
    PLACES_PAGE_SIZE,
    PlaceCreate,
    PlaceListResponse,
    PlaceRead,
    PlaceUpdate,
)
from app.services.place_service import (
    PLACE_NOT_FOUND_CODE,
    PLACE_NOTHING_TO_UPDATE_CODE,
    PlaceArchivedError,
    PlaceCursorError,
    PlaceDuplicateError,
    PlaceFieldError,
    PlaceUnknownCommunityError,
    create_place,
    deactivate_place,
    list_places,
    update_place,
)

router = APIRouter(prefix="/places", tags=["places"])

# AC-07, project-context §4: единственный конверт ошибки `{"code": ...}`.
# Заголовок ставится на КАЖДЫЙ ответ, включая ошибки: `404`/`409` раскрывают
# существование места, а список мест — состояние справочника сообщества.
_NO_STORE = {"cache-control": "no-store"}


def _error(code: str, status_code: int, details: dict[str, Any] | None = None) -> JSONResponse:
    """Конверт `{"code": ...}` с необязательным `details`.

    `code` — машинный идентификатор в `snake_case`/`UPPER_SNAKE`; `details`
    несёт данные для действия (перечень категорий, `existing_uuid`), а не
    текст для человека. Клиент ветвится по `code`, поэтому склейка кода
    и данных в одну строку запрещена (урок CR 3.1).
    """
    body: dict[str, Any] = {"code": code}
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


def _domain_error(error: Exception) -> JSONResponse:
    """Task 5.3, 6.3: единое отображение доменных ошибок места в HTTP.

    Одна функция на `POST` и `PATCH`, чтобы один и тот же ввод давал один
    и тот же код в обоих маршрутах: две копии ветвления разошлись бы, и админ
    получал бы на правке другой ответ, чем на создании.

    Коды:
    - `409 PLACE_DUPLICATE` — имя занято активным местом;
    - `409 PLACE_ARCHIVED` + `details.existing_uuid` — имя занято СКРЫТЫМ
      местом (D-6: единственное действие — «восстановить»);
    - `422 <code>` — ошибка поля (`name_required`, `name_too_long`,
      `category_invalid` + `details.allowed`);
    - `422 PLACE_UNKNOWN_COMMUNITY` — слаг вне конфига.
    """
    if isinstance(error, PlaceDuplicateError):
        return _error(error.code, 409)
    if isinstance(error, PlaceArchivedError):
        # D-6: без `existing_uuid` админ не знает, какое место восстанавливать.
        return _error(error.code, 409, {"existing_uuid": str(error.existing_uuid)})
    if isinstance(error, PlaceUnknownCommunityError):
        return _error(error.code, 422)
    # Task 6.3 стори 3.3 (AC-04, AC-07): курсор разбирается до `PlaceFieldError`.
    # Ветка ОБЯЗАНА стоять выше `assert isinstance(PlaceFieldError)`: иначе
    # `invalid_cursor` попал бы в `assert` и дал `AssertionError` → `500`
    # вместо `422` (ловушка зафиксирована в Dev Notes стори).
    if isinstance(error, PlaceCursorError):
        return _error(error.code, 422)
    # `PlaceFieldError` — последняя ветка: у неё код не константа класса,
    # а атрибут, и она не должна перехватывать остальные типы.
    assert isinstance(error, PlaceFieldError)
    return _error(error.code, 422, error.details)


@router.get("", response_model=PlaceListResponse)
async def list_places_endpoint(
    reader: PlacesReaderUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    category: PlaceCategory | None = None,
    cursor: Annotated[str | None, Query()] = None,
) -> PlaceListResponse | JSONResponse:
    """Task 6 (AC-01…AC-07): выдача активных мест сообщества пилота.

    `reader` даёт сразу два ответа: кто читает и видит ли он скрытые места.
    `include_inactive` объявлен в зависимости и НЕ дублируется в сигнатуре
    (Task 6.2, ловушка «два источника»): иначе роутер мог бы передать в сервис
    `False`, пока зависимость уже проверила роль для `True`.

    `category` типизирован `PlaceCategory`: мусор отбивает Pydantic, и
    существующая ветка `validation_error_handler` отдаёт `422 category_invalid`
    + `details.allowed` (AC-02, AC-07). Второго кода не вводится.

    `cursor` — непрозрачная строка (Task 6.2/2.2). Разбор в сервисе: битое
    значение даёт `422 invalid_cursor`, а не `500` (AC-04).

    `cache-control: no-store` ставит `NoStoreMiddleware` (QD A6): ручная
    установка создала бы второй, независимо поддерживаемый путь заголовка.

    `commit()` не вызывается: чтение (D-4 стори 2.2). `populate_existing`
    в репозитории тоже нет — объекты из выдачи не правятся.
    """
    try:
        page = await list_places(
            session,
            category=category,
            include_inactive=reader.include_inactive,
            cursor=cursor,
            limit=PLACES_PAGE_SIZE,
            settings=settings,
        )
    except (PlaceCursorError, PlaceFieldError, PlaceUnknownCommunityError) as error:
        return _domain_error(error)

    return PlaceListResponse(
        items=[PlaceRead.model_validate(place) for place in page.items],
        total=page.total,
        cursor=page.next_cursor,
    )


@router.post("", response_model=PlaceRead, status_code=201)
async def create_place_endpoint(
    payload: PlaceCreate,
    user: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> PlaceRead | JSONResponse:
    """Task 5 (AC-01, AC-02, AC-03, AC-07): создание места администратором.

    `ModeratorUser` даёт `401 unauthenticated` без сессии и `403 forbidden`
    обычному пользователю (AC-01). Согласие на ПД не требуется: у модератора
    может не быть анкеты, а справочник вести он обязан (D-3).

    `201` + `PlaceRead`: наружу уходят только `uuid`, `name`, `category`,
    `address_text`, `is_active` — внутренний `id` и координаты не отдаются
    (D-2 стори 3.1, FR67/NFR34). Место сразу активно: `is_active` не входит
    в `PlaceCreate` и берётся из `server_default`/Python-дефолта модели.

    Второй проверки дубля здесь нет (D-1 стори 3.1): правило живёт в сервисе
    и в `uq_places_city_name`, роутер только отображает ошибку.
    """
    try:
        place = await create_place(
            session,
            name=payload.name,
            category=payload.category,
            address_text=payload.address_text,
            city_slug=payload.city_slug,
            settings=settings,
        )
    except (
        PlaceDuplicateError,
        PlaceArchivedError,
        PlaceFieldError,
        PlaceUnknownCommunityError,
    ) as error:
        return _domain_error(error)

    # D-4 стори 2.2: единица работы у роутера, сервис делает только `flush`.
    await session.commit()
    response.headers["cache-control"] = "no-store"
    return PlaceRead.model_validate(place)


@router.patch("/{place_uuid}", response_model=PlaceRead)
async def update_place_endpoint(
    place_uuid: uuid_lib.UUID,
    payload: PlaceUpdate,
    user: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> PlaceRead | JSONResponse:
    """Task 6 (AC-05, AC-06, AC-07): частичная правка и восстановление.

    `place_uuid` типизирован `uuid.UUID`: мусор в пути даёт `422` от общего
    обработчика, а не `404` — клиент различает опечатку и отсутствие места
    (`/places/1` не должен выглядеть как «место удалено»).

    Пустое тело — `422 place_nothing_to_update` (RISK-2): молчаливый `200`
    сообщил бы «сохранено» без единой правки, и клиент считал бы изменение
    применённым.

    `city_slug` в теле отбивается схемой (`extra="forbid"`, R-05) —
    перенос между сообществами запрещён.
    """
    place = await _get_place(session, place_uuid)
    if place is None:
        return _error(PLACE_NOT_FOUND_CODE, 404)

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return _error(PLACE_NOTHING_TO_UPDATE_CODE, 422)

    try:
        place = await update_place(session, place=place, changes=changes, settings=settings)
    except (PlaceDuplicateError, PlaceArchivedError, PlaceFieldError) as error:
        return _domain_error(error)

    await session.commit()
    response.headers["cache-control"] = "no-store"
    return PlaceRead.model_validate(place)


@router.delete("/{place_uuid}", status_code=204)
async def delete_place_endpoint(
    place_uuid: uuid_lib.UUID,
    user: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Task 7 (AC-04, R-04): скрытие места — всегда мягкое, идемпотентное.

    `204` независимо от того, было место активным или уже скрытым: повторный
    `DELETE` после обрыва сети не должен выглядеть сбоем операции, которая
    прошла. Неизвестный `uuid` — `404 PLACE_NOT_FOUND`.

    Строка остаётся живой с `is_active=false`: карточки эпика 4 ссылаются на
    место (FR68 «скрыть» ≠ «удалить»), а восстановление идёт
    `PATCH is_active=true` (D-7).

    `204` не имеет тела, поэтому заголовок ставится на возвращаемый объект
    ответа напрямую — общий `_error`/`response.headers` здесь не подходит
    для случая «нет тела».
    """
    place = await _get_place(session, place_uuid)
    if place is None:
        return _error(PLACE_NOT_FOUND_CODE, 404)

    await deactivate_place(session, place=place)
    await session.commit()
    return Response(status_code=204, headers=_NO_STORE)


async def _get_place(session: AsyncSession, place_uuid: uuid_lib.UUID) -> Any:
    """Task 6.1: место по публичному `uuid` или `None`.

    Отдельная функция, чтобы `PATCH` и `DELETE` не расходились в способе
    адресации: оба обязаны искать по `uuid` через репозиторий (`populate_existing`
    внутри), а не по `id` и не сырым SQL.
    """
    from app.repositories.places import PlaceRepository

    return await PlaceRepository(session).get_by_uuid(place_uuid)
