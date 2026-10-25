"""Анкета: чтение, создание, правка, определение города (AC-01..AC-07).

Роут не содержит бизнес-логики: нормализация и доменные коды живут
в `services/profile_service.py`, геолокация — в `services/geo_service.py`,
запросы к БД — в репозитории (project-context §9). Здесь только HTTP-форма:
статусы, конверт ошибки и `cache-control`.

С 2.3 есть `POST /profiles` (`201`/`200`/`409`), `PATCH /profiles/me`
и `POST /profiles/locate-city`. Маршрута `/profiles/{uuid}` по-прежнему нет
(Task 6.7): он сломал бы существующий тест `test_routes_no_numeric_ids` —
`/api/v1/profiles/1` дал бы `422` вместо `404`. Адресация по `uuid` — эпик 4.

CR 2.1, Решение 2: зависимость — `ConsentUser` (18+ и согласие на ПД), а не
`CurrentUser`. Иначе прямой вызов API с cookie отдавал бы ПД пользователя,
не давшего согласия (152-ФЗ); фронтовая гарда этого не закрывает.

CR 2.1, Решение 3: `response_model=ProfileOwn` — владелец видит `is_hidden`
и `status`, публичная карточка (эпик 4) их не увидит.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ConsentUser, CurrentUser
from app.core.config import settings
from app.core.db import get_session
from app.schemas.attendance_history import AttendanceHistoryPage
from app.schemas.photo import PhotoListResponse, PhotoOwn
from app.schemas.profile import (
    LocateCityRequest,
    LocateCityResponse,
    ProfileCreate,
    ProfileOwn,
    ProfileUpdate,
)
from app.services import media_service
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
from app.services.consent_service import GenderRequiredError
from app.services.geo_service import locate_city
from app.services.media_service import MediaStorageError, MediaUnavailableError
from app.services.photo_service import (
    PhotoLimitReachedError,
    PhotoNotFoundError,
    add_photo,
    list_photos,
    remove_photo,
    replace_photo,
)
from app.services.photo_validation import PhotoConsentRequiredError, PhotoValidationError
from app.services.profile_service import (
    ProfileExistsError,
    ProfileFieldError,
    create_profile,
    get_profile_for_user,
    update_profile,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])

# AC-04 стори 2.1: ответ привязан к сессии. Без `no-store` прокси или браузер
# мог бы отдать чужой профиль из кэша после смены пользователя на том же
# устройстве. Заголовок ставится на КАЖДЫЙ ответ профиля, включая ошибки:
# `404` тоже раскрывает факт наличия/отсутствия анкеты.
_NO_STORE = {"cache-control": "no-store"}

# D-10: сбой удаления объекта в S3 после успешного `204` логируется, но
# клиенту не возвращается.
# CR 2.2, патч 3: structlog, а не stdlib `logging`. При `format="%(message)s"`
# stdlib-путь выбрасывает `extra`, а `RedactingSecretsProcessor` подключён
# к цепочке structlog — запись через stdlib шла мимо редакции секретов.
logger = structlog.get_logger(__name__)


def _error(code: str, status_code: int) -> JSONResponse:
    """Единый конверт ошибки `{"code": ...}` — как в `auth.py`.

    Клиент разбирает одно поле `code` и выбирает текст по нему; свободный
    `message` не парсится. `cache-control` ставится и на ошибку: иначе
    `404 profile_not_found` кэшировался бы и переживал создание анкеты.
    """
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


@router.get("/me", response_model=ProfileOwn)
async def read_my_profile(
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> ProfileOwn | JSONResponse:
    """AC-04, AC-05: анкета текущего пользователя.

    `ConsentUser` даёт `401` без сессии, `403 user_blocked` забаненному
    и `403 consent_required` не прошедшему онбординг (CR 2.1, Решение 2).
    Анкеты нет → `404 profile_not_found`. Внутренние `id`/`user_id`
    не покидают сервер: их нет в `ProfileOwn`.
    """
    profile = await get_profile_for_user(session, user)
    if profile is None:
        return _error("profile_not_found", 404)
    response.headers["cache-control"] = "no-store"
    return ProfileOwn.model_validate(profile)


@router.post("", response_model=ProfileOwn, status_code=201)
async def create_my_profile(
    payload: ProfileCreate,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> ProfileOwn | JSONResponse:
    """AC-02, D-1: создание анкеты — `201`, дозаполнение черновика — `200`.

    Код ответа различает два контракта: «создано» и «черновик заполнен».
    Клиент по нему понимает, показывать ли «анкета создана» или просто
    вернуться в кабинет.

    `gender` берётся из `users.gender` внутри сервиса (D-4): в теле его нет,
    и `extra="forbid"` в схеме отклонит попытку прислать его.

    Ошибки: `409 profile_exists` (анкета уже заполнена), `422 <code>`
    (доменная валидация), `422 gender_required` (пол не зафиксирован).
    """
    try:
        profile, created = await create_profile(session, user, payload, settings.pilot_city)
    except ProfileExistsError:
        # D-1: вторая анкета невозможна; прежние данные не перезаписываются.
        return _error("profile_exists", 409)
    except ProfileFieldError as exc:
        # AC-01, AC-05: `422` с доменным кодом (`city_required`, `bio_too_long`).
        return _error(exc.code, 422)
    except GenderRequiredError:
        # D-4: без пола составной FK не примет строку — это отказ ввода.
        return _error("gender_required", 422)

    # Task 6.5, D-4 стори 1.2: единица работы у роута, сервис делает только flush.
    await session.commit()
    # AC-02: `201` при создании, `200` при заполнении черновика (D-1).
    response.status_code = 201 if created else 200
    response.headers["cache-control"] = "no-store"
    return ProfileOwn.model_validate(profile)


@router.patch("/me", response_model=ProfileOwn)
async def update_my_profile(
    payload: ProfileUpdate,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> ProfileOwn | JSONResponse:
    """AC-03: частичная правка анкеты.

    Отсутствующий ключ — поле не меняется; явный `null` — `422 field_required`
    (D-11, D-3: стереть поле нельзя). `status` пересчитывается по факту
    заполнения всех четырёх полей.

    Анкеты нет → `404 profile_not_found`, а не `422`: иначе клиент без анкеты
    получал бы ошибку формы вместо «анкеты ещё нет».

    D-8: сервис читает строку с `FOR UPDATE` — два параллельных `PATCH`
    не пересчитывают `status` по разным снимкам.
    """
    try:
        profile = await update_profile(session, user, payload, settings.pilot_city)
    except ProfileFieldError as exc:
        return _error(exc.code, 422)

    if profile is None:
        return _error("profile_not_found", 404)

    await session.commit()
    response.headers["cache-control"] = "no-store"
    return ProfileOwn.model_validate(profile)


@router.get("/me/photos", response_model=PhotoListResponse)
async def list_my_photos(
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> PhotoListResponse | JSONResponse:
    """Task 7.3 (AC-04): фото анкеты с подписанными ссылками.

    Ссылка считается при каждом чтении (`media_service.get_signed_url`),
    а не хранится: TTL 900 с, и сохранённая ссылка стала бы недействительной,
    а в БД попал бы временный секрет (D-11).

    Анкеты нет или фото нет — `200` с пустым `items`: экран анкеты рисует
    «фото нет», и отдельный `404` заставлял бы клиент различать два пустых
    состояния. Ошибка хранилища — `503 storage_unavailable` (Task 3.6),
    а не `500`: это зона ответственности деплоя.
    """
    try:
        photos = await list_photos(session, user)
    except MediaUnavailableError:
        return _error("storage_unavailable", 503)
    except MediaStorageError:
        return _error("storage_unavailable", 503)

    expires_in = settings.s3_signed_url_ttl
    items = [PhotoOwn.from_photo(photo, url=url, expires_in=expires_in) for photo, url in photos]
    response.headers["cache-control"] = "no-store"
    return PhotoListResponse(items=items, total=len(items))


@router.post("/me/photos", response_model=PhotoOwn, status_code=201)
async def upload_my_photo(
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    file: Annotated[UploadFile, File()],
    # AC-06, Task 7.2: дефолт `False` ОБЯЗАТЕЛЕН. При `Form(...)` отсутствующее
    # поле отбивает Pydantic, и `validation_error_handler` (`main.py`) отдаёт
    # `422 invalid_request` вместо доменного `422 photo_consent_required`.
    consent: Annotated[bool, Form()] = False,
) -> PhotoOwn | JSONResponse:
    """Task 7.2 (AC-02, AC-03, AC-05, AC-06): загрузка фото.

    `multipart/form-data`: поле `file` — сам файл, поле `consent` — чекбокс
    «это моё фото, согласен на показ другим пользователям» (FR64). Согласие
    проверяется ДО обращения к S3 (Task 4.4): иначе файл уже лежал бы
    в бакете, а строки не было.

    Коды: `201` — создано; `409 photo_limit_reached` — три фото уже есть;
    `413 request_too_large` — файл больше `MAX_PHOTO_BYTES`;
    `422 photo_not_image|photo_empty|photo_consent_required|gender_required`;
    `503 storage_unavailable` — реквизиты S3 не заданы (Task 3.6).

    `MediaStorageError` → `503`, а не `500`: сбой хранилища при рабочей
    конфигурации — инфраструктурный инцидент, клиенту полезнее «попробуйте
    позже», чем «внутренняя ошибка». Транзакция откатывается, строки нет.
    """
    # Размер и сигнатура проверяются по содержимому. `size_bytes` берётся из
    # `UploadFile` (`SpooledTemporaryFile`): FastAPI уже прочитал тело, поэтому
    # размер известен без повторного чтения файла целиком в память.
    fileobj = file.file
    fileobj.seek(0, 2)
    size_bytes = fileobj.tell()
    fileobj.seek(0)

    try:
        photo = await add_photo(
            session,
            user,
            fileobj=fileobj,
            size_bytes=size_bytes,
            declared_content_type=file.content_type,
            consent=consent,
        )
    except PhotoConsentRequiredError:
        return _error("photo_consent_required", 422)
    except PhotoValidationError as exc:
        return _error(exc.code, exc.status_code)
    except PhotoLimitReachedError:
        return _error("photo_limit_reached", 409)
    except GenderRequiredError:
        # Task 6.6a: без пола составной FK не примет строку — отказ ввода.
        return _error("gender_required", 422)
    except (MediaUnavailableError, MediaStorageError):
        # D-9: откат обязателен — иначе строка осталась бы без объекта в бакете
        # и «фото, которого нет в хранилище» попало бы в список.
        # `MediaUnavailableError` (Task 3.6) — реквизиты не заданы, это `503`,
        # а не `500`: состояние деплоя, а не дефект запроса.
        await session.rollback()
        return _error("storage_unavailable", 503)

    # AC-03, D-2 стори 2.4: подписи здесь НЕТ. Только что созданное фото
    # всегда `pending`, а `pending` не отдаётся вовсе — подпись считалась бы
    # для невыданного изображения и жила бы в памяти 900 с.
    #
    # CR 2.2, патч 1 (подпись до `commit` + откат при её сбое) снят вместе
    # с кодом: сбой подписи больше не может случиться на этом маршруте.
    # Риск закрыт как неактуальный, снятие зафиксировано в `deferred-work.md`.
    #
    # D-4 стори 1.2: единица работы у роутера, сервис делает только flush.
    await session.commit()
    response.headers["cache-control"] = "no-store"
    return PhotoOwn.from_photo(photo, url=None, expires_in=settings.s3_signed_url_ttl)


@router.put("/me/photos/{photo_uuid}", response_model=PhotoOwn)
async def replace_my_photo(
    photo_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
    file: Annotated[UploadFile, File()],
    # AC-06, Task 7.2 стори 2.2: дефолт `False` ОБЯЗАТЕЛЕН — иначе отсутствующее
    # поле отбивает Pydantic и вместо доменного `422 photo_consent_required`
    # уходит общий `422 invalid_request`.
    consent: Annotated[bool, Form()] = False,
) -> PhotoOwn | JSONResponse:
    """Task 5.1 (AC-02, AC-05): замена файла существующего фото.

    Атомарная мутация одной строки: `uuid` и `position` сохраняются, содержимое
    и метаданные заменяются, статус уходит в `pending` (D-1, D-2). Прежний
    путь замены на клиенте — `DELETE`+`POST` (CR 2.4 патч 5) — оставлял окно,
    в котором фото отсутствовало; при `409` старое фото удалялось до загрузки
    нового.

    `photo_uuid` типизирован `uuid.UUID`: мусор в пути даёт `422 invalid_request`
    от общего обработчика, а не `404` — клиент различает опечатку и отсутствие.

    Коды: `200` — заменено; `403 photo_consent_required` (FR64: новый файл
    требует нового согласия); `404 photo_not_found` — чужое или несуществующее
    фото, а также отсутствие анкеты (AC-09); `413 request_too_large` — файл
    больше `MAX_PHOTO_BYTES`; `422 photo_not_image|photo_empty`;
    `503 storage_unavailable`. `409 photo_limit_reached` здесь НЕ возвращается:
    позиция занята той же строкой, лимит не может быть исчерпан заменой.

    # AC-05: порядок «согласие → валидация → мутация» обеспечивает сервис:
    негодный файл не оставляет следов ни в `profiles`, ни в `profile_photos`.
    """
    # Размер берётся из уже прочитанного `UploadFile` (SpooledTemporaryFile):
    # повторное чтение файла целиком в память не нужно.
    fileobj = file.file
    fileobj.seek(0, 2)
    size_bytes = fileobj.tell()
    fileobj.seek(0)

    try:
        photo, old_storage_key = await replace_photo(
            session,
            user,
            photo_uuid,
            fileobj=fileobj,
            size_bytes=size_bytes,
            declared_content_type=file.content_type,
            consent=consent,
        )
    except PhotoConsentRequiredError:
        return _error("photo_consent_required", 403)
    except PhotoValidationError as exc:
        return _error(exc.code, exc.status_code)
    except PhotoNotFoundError:
        return _error("photo_not_found", 404)
    except (MediaUnavailableError, MediaStorageError):
        # D-3: `put_object` идёт до `commit`, поэтому здесь строка ещё не
        # зафиксирована. Откат обязателен: иначе в БД остались бы метаданные
        # нового файла, которого нет в бакете. Это `503`, а не `500`: для
        # рабочей конфигурации сбой хранилища — инфраструктурный инцидент.
        await session.rollback()
        return _error("storage_unavailable", 503)

    # D-4 стори 1.2: единица работы у роутера, сервис делает только flush.
    await session.commit()

    if old_storage_key is not None:
        # D-3: объект удаляется ПОСЛЕ `commit` — после фиксации строки прежний
        # ключ взять неоткуда. `None` означает, что ключ не изменился (то же
        # расширение): удалять нельзя, иначе снесло бы только что загруженный
        # файл (D-4). Ошибка удаления строку уже не откатывает — сироту уберёт
        # воркер retention эпика 8, клиенту `200` уже обещан.
        try:
            await media_service.delete_object(old_storage_key)
        except Exception:  # noqa: BLE001 - как в DELETE: клиенту 200 уже обещан
            logger.warning("photo_object_orphaned", storage_key_len=len(old_storage_key))

    response.headers["cache-control"] = "no-store"
    # AC-02, D-2 стори 2.4: после замены фото всегда `pending`, поэтому ссылка
    # не подписывается вовсе — `url=null`, клиент рисует «На проверке».
    return PhotoOwn.from_photo(photo, url=None, expires_in=settings.s3_signed_url_ttl)


@router.delete(
    "/me/photos/{photo_uuid}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
async def delete_my_photo(
    photo_uuid: uuid_lib.UUID,
    user: ConsentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> Response | JSONResponse:
    """Task 7.4, 7.5 (AC-09, D-10): удаление фото владельцем.

    `photo_uuid` типизирован `uuid.UUID`: мусор в пути даёт `422 invalid_request`
    от общего обработчика, а не `404` — клиент различает «опечатка в пути»
    и «фото нет».

    D-10: порядок «DELETE + commit → S3 delete_object». Ошибка S3 после
    успешного `204` только логируется: строка уже удалена, и возврат ошибки
    заставил бы клиент повторять `DELETE`, получая `404` на втором шаге.
    Обратный порядок удалял бы объект, который при откате остался бы в БД
    с битой ссылкой.
    """
    try:
        storage_key = await remove_photo(session, user, photo_uuid)
    except PhotoNotFoundError:
        return _error("photo_not_found", 404)

    await session.commit()
    try:
        await media_service.delete_object(storage_key)
    except Exception:  # noqa: BLE001 - CR 2.4 патч 11
        # D-10: объект-сирота недостижим (ключ нигде не сохранён) и убирается
        # воркером retention эпика 8. Клиенту `204` уже обещан.
        #
        # CR 2.4 патч 11: ловится ЛЮБОЕ исключение, а не только доменные
        # `MediaStorageError`/`MediaUnavailableError`. Строка в БД уже удалена
        # (`commit` выше), поэтому непойманный `ValueError`/`TypeError` из
        # boto3 отдавал `500` на успешно выполненном удалении: пользователь
        # видел ошибку, повторял `DELETE` и получал `404` на втором шаге.
        # CR 2.2, патч 3: structlog — stdlib-путь терял поля и обходил редакцию.
        # Длина ключа, а не ключ: сам ключ в лог не идёт (D-11).
        logger.warning("photo_object_orphaned", storage_key_len=len(storage_key))

    response.headers["cache-control"] = "no-store"
    return Response(status_code=204, headers=_NO_STORE)


@router.get("/me/attendance", response_model=AttendanceHistoryPage)
async def read_my_attendance(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> AttendanceHistoryPage | JSONResponse:
    """AC-04, AC-05, AC-06 (D-7.5-3): СВОЯ история явок.

    Отдельный маршрут, а не параметр на модераторском: в пути НЕТ `user_uuid`,
    поэтому подстановка чужого адреса невозможна ПО КОНСТРУКЦИИ, а не по
    проверке (D-7.5-3). Набор полей тот же (AC-02) — второй контракт не
    заводится, чужие флаги не раскрываются, потому что не читаются.

    Гард — `CurrentUser`, а не `ConsentUser`: история явок — не анкета и не ПД
    профиля (имя, город, фото), а факт собственного участия. Требование
    согласия здесь отняло бы историю у участника, который её же и создал,
    а `get_current_user` уже проверил сессию, бан и отзыв.

    `viewer_id == participant_id` — инвариант сервиса (`is_moderator=False`),
    поэтому обход через `list_history` невозможен (P-13).
    """
    if cursor is not None:
        try:
            position = decode_history_cursor(cursor)
        except InvalidCursorError:
            return _error(InvalidCursorError.code, 422)
    else:
        position = None

    try:
        return await list_history(
            session,
            participant_id=user.id,
            viewer_id=user.id,
            is_moderator=False,
            limit=limit,
            cursor=position,
        )
    except AttendanceHistoryForbiddenError:
        # Недостижимо по построению (`participant_id == viewer_id`), но
        # превращать будущий обход в `500` нельзя — отказ обязан остаться 403.
        return _error(AttendanceHistoryForbiddenError.code, 403)


@router.post("/locate-city", response_model=LocateCityResponse)
async def locate_my_city(
    payload: LocateCityRequest,
    user: ConsentUser,
    response: Response,
) -> LocateCityResponse:
    """AC-06, D-9, D-10: город по координатам — без хранения координат.

    Координаты приходят в теле и живут только в этом запросе: диапазоны
    проверяет `LocateCityRequest` (`422 invalid_coordinates` через общий
    обработчик), затем сравнение с прямоугольником пилота в `geo_service`.
    Ни БД, ни лог, ни ответ координат не содержат (FR59).

    Вне пилота — `{"city": null, "reason": "outside_pilot_city"}`, а не
    ошибка: гость из другого города — норма, клиент предложит ввести город
    вручную. `ConsentUser` обязателен: без согласия не собираем даже
    приблизительное местоположение (152-ФЗ).

    D-10: живёт в `profiles`, а не отдельным `geo`-роутером; BFF-маршрут —
    существующий универсальный `/api/proxy/profiles/locate-city`.
    """
    city = locate_city(
        lat=payload.lat,
        lng=payload.lng,
        bbox=settings.pilot_city_bbox,
        pilot_city=settings.pilot_city,
    )
    # AC-06: ответ зависит от запроса и не должен кэшироваться — иначе
    # следующий пользователь на том же устройстве получил бы чужой город.
    response.headers["cache-control"] = "no-store"
    if city is None:
        return LocateCityResponse(city=None, reason="outside_pilot_city")
    return LocateCityResponse(city=city)
