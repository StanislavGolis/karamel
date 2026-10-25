"""Сервис фото анкеты: добавление, список, удаление (Task 6.1–6.6b).

project-context §6: цепочка «роутер → сервис → репозиторий». Роутер не
обращается к БД и к хранилищу напрямую: правило «сколько фото и в каком
порядке» живёт здесь, и второй потребитель (воркер модерации эпика 3)
не начнёт его дублировать с расхождениями.

D-4 стори 1.2: `commit()` здесь НЕ вызывается — единица работы у роутера.
Сервис делает `flush` через репозиторий и возвращает объект; откат при
ошибке S3 делает роутер или вызывающий код.

D-7: лимит трёх фото проверяется ПОД блокировкой строки анкеты. Без неё
две параллельные загрузки видят `count=2`, обе вставляют `position=3`,
и одну отбивает `uq(profile_id, position)` — пользователь получает `500`.

D-9: порядок «INSERT (flush) → S3 → commit». Обратный порядок оставлял бы
сиротские объекты в бакете при любом `IntegrityError`. Остаточный риск
«объект загружен, commit упал» принимается долгом пилота: ключ
детерминирован и виден в логе, уборка — воркер retention (эпик 8).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime
from typing import BinaryIO

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Gender, PhotoStatus
from app.models.profile import Profile
from app.models.profile_photo import ProfilePhoto
from app.models.user import User
from app.repositories.errors import constraint_name_of
from app.repositories.profile_photos import MAX_PHOTOS_PER_PROFILE, ProfilePhotoRepository
from app.repositories.profiles import ProfileRepository
from app.services import media_service
from app.services.consent_service import GenderRequiredError
from app.services.photo_validation import (
    ValidatedPhoto,
    ensure_consent,
    validate_photo,
)

# CR 2.2, Решение 2 + CR 2.4 патч 4: structlog, а не stdlib `logging`.
# stdlib-логгер не принимает именованные поля (`logger.warning("x", k=v)` →
# `TypeError` в рантайме, поймано mypy) и обходит `RedactingSecretsProcessor`:
# запись через него не проходит редакцию секретов.
logger = structlog.get_logger(__name__)

# AC-05, D-5: сколько байт достаточно для проверки сигнатуры. WebP требует
# смотреть до смещения 12 (`RIFF....WEBP`), поэтому 12 — минимум, а не 8.
_SIGNATURE_BYTES = 12


def _storage_key(profile_uuid: uuid_lib.UUID, photo_uuid: uuid_lib.UUID, extension: str) -> str:
    """D-3: ключ объекта в бакете. Имя исходного файла не участвует.

    Разделение по анкете (`photos/{profile_uuid}/...`) нужно, чтобы удаление
    аккаунта или выгрузка архива фото (эпик 8) шли одним префиксом, без
    перебора всех объектов бакета.
    """
    return f"photos/{profile_uuid}/{photo_uuid}.{extension}"


def _replace_storage_key(
    profile_uuid: uuid_lib.UUID, photo_uuid: uuid_lib.UUID, extension: str
) -> str:
    """CR 2.5, Решение R-1: ключ замены ВСЕГДА уникален.

    `_storage_key` детерминирован, и при замене файла тем же расширением
    новый объект писался бы ПОВЕРХ старого. Это давало два дефекта:

      * ранее выданный presigned URL (`approved`-фото, TTL 900 с) продолжал
        отдавать уже НЕПРОВЕРЕННЫЙ файл — обход модерации на всё время TTL;
      * откат после `put_object` (сбой `commit`) возвращал в БД метаданные
        старого файла при новом содержимом в бакете.

    Уникальный суффикс снимает оба: старый ключ и его подпись остаются
    старыми (ссылка умирает вместе с удалением объекта), а совпадения ключей
    не бывает вовсе — прежняя ветка «ключ не изменился, удалять нечего»
    (D-4 стори 2.5) переформулирована этим решением.

    Публичный `uuid` фото НЕ меняется (AC-02, D-2): суффикс живёт только
    во внутреннем `storage_key`.
    """
    return f"photos/{profile_uuid}/{photo_uuid}-{uuid_lib.uuid4().hex[:16]}.{extension}"


class PhotoLimitReachedError(Exception):
    """AC-03: на анкете уже три фото — `409 photo_limit_reached`.

    `409`, а не `422`: запрос синтаксически верен, конфликт в состоянии
    ресурса (лимит исчерпан). Клиент по этому коду показывает «сначала
    удалите фото», а не ошибку формы.
    """


class PhotoNotFoundError(Exception):
    """AC-09: фото не принадлежит анкете или не существует — `404 photo_not_found`.

    Один тип на оба случая намеренно: различать их в ответе значило бы
    подтверждать существование чужого фото.
    """


async def _get_or_create_draft(session: AsyncSession, user: User) -> Profile:
    """Task 6.6, 6.6a, 6.6b: анкета для фото, создаётся как черновик.

    D-8 стори 2.1: фото приезжают раньше текста анкеты, а
    `profile_photos.profile_id` NOT NULL — значит строка `profiles` должна
    существовать уже на загрузке первого фото. Создаётся `status='draft'`,
    текстовые поля остаются NULL.

    CR 2.3 стори 2.1, Решение 3: `is_hidden=(gender == Gender.MALE)` —
    без этого черновик, созданный загрузкой фото, стал бы видимым для
    мужской анкеты в выборке эпика 4. Существующая анкета НЕ трогается:
    её `is_hidden` — решение пользователя (скрытие) или 2.3.

    Task 6.6a: `user.gender is None` → `GenderRequiredError`. Без этой ветки
    составной FK `(user_id, gender) -> users(id, gender)` дал бы
    `IntegrityError` → `500`, ровно как в `create_profile`
    (`profile_service.py:269`).

    Task 6.6b: пока строки нет, блокировать нечего. Два параллельных `POST`
    дают два `INSERT` → `IntegrityError` на `uq_profiles_user_id`. Паттерн
    `create_profile` (`profile_service.py:300-313`): откат → перечитывание
    под `FOR UPDATE` → продолжить. `500` не отдаётся.
    """
    # Значения снимаются с `user` ДО возможного rollback: после отката объект
    # истекает, а синхронное чтение атрибута в async падает MissingGreenlet.
    user_id = user.id
    gender = user.gender
    if gender is None:
        # Task 6.6a: без пола составной FK не примет строку.
        raise GenderRequiredError

    repository = ProfileRepository(session)
    existing = await repository.get_by_user_id_for_update(user_id)
    if existing is not None:
        return existing

    try:
        profile = await repository.create(
            user_id=user_id,
            gender=str(gender),
            status="draft",
            is_hidden=(gender == Gender.MALE),
        )
    except IntegrityError:
        # Task 6.6b: гонка двух первых загрузок. Второй INSERT нарушает
        # `uq_profiles_user_id`; откат и перечитывание дают готовую строку.
        await session.rollback()
        raced = await repository.get_by_user_id_for_update(user_id)
        if raced is None:
            # Ошибка не была гонкой (например, нарушен CHECK) — не глотаем.
            raise
        return raced
    return profile


def _read_signature(fileobj: BinaryIO) -> bytes:
    """AC-05, D-5: первые байты файла для проверки сигнатуры.

    Позиция восстанавливается: тот же поток уходит в `put_object`, и
    оставленное смещение отправило бы в бакет файл без начала — то есть
    битое изображение при формально успешной загрузке.
    """
    head = fileobj.read(_SIGNATURE_BYTES)
    fileobj.seek(0)
    return head


def _is_position_race(exc: IntegrityError) -> bool:
    """CR 2.4 патч 3 + QD A2: `IntegrityError` — гонка за позицию, а не дефект.

    QD A2 (CR 3.2): здесь была КОПИЯ разбора, читавшая `error.orig.constraint_name`.
    Под asyncpg этого атрибута нет — имя лежит на `error.orig.orig`, поэтому
    проверка возвращала `False` ВСЕГДА, и гонка за позицию давала `500` вместо
    `409 photo_limit_reached` (проверено формой ошибки из живого драйвера).
    Теперь используется единственный общий разбор; на неизвестной форме
    по-прежнему `False`: лучше честный `500` с диагностикой, чем ложный `409`
    на нарушении CHECK.
    """
    constraint = constraint_name_of(exc)
    return constraint in {
        "uq_profile_photos_profile_id_position",
        "uq_profile_photos_storage_key",
    }


async def add_photo(
    session: AsyncSession,
    user: User,
    *,
    fileobj: BinaryIO,
    size_bytes: int,
    declared_content_type: str | None,
    consent: bool,
) -> ProfilePhoto:
    """Task 6.1 (AC-02, AC-03, AC-05..AC-07): загрузка фото анкеты.

    Порядок операций обязателен:
      1. согласие (AC-06) — ДО любого обращения к S3 и до создания строки;
      2. валидация файла (AC-05) — ДО создания черновика (D-7 стори 2.5);
      3. черновик анкеты и её блокировка (D-7);
      4. лимит трёх фото под блокировкой (AC-03);
      5. `storage_key` и INSERT с `flush`;
      6. upload в S3;
      7. возврат объекта — `commit()` делает роутер.

    Стори 2.5, Task 4.1 (D-7): шаги 1-2 идут ДО `_get_or_create_draft`. Раньше
    черновик создавался первым, и любой `422 photo_not_image`/`photo_empty`
    оставлял пользователю анкету, которой он не заполнял: на фронте `isEdit`
    становился `true`, заголовок менялся с «Заполните анкету» на «Ваша
    анкета» после НЕУДАЧНОЙ загрузки (долг CR 2.4). Валидация не требует
    строки в БД — она работает с сигнатурой и размером файла.

    Ошибка S3 пробрасывается как `MediaStorageError`, транзакция откатывается
    вызывающим кодом, строки в БД не остаётся.
    """
    # Task 4.4 (AC-06): согласие проверяется первым. Иначе файл уже лежал бы
    # в бакете, а строки не было — сирота, которую никто не уберёт до эпика 8.
    ensure_consent(consent)

    # AC-05, D-7 стори 2.5: валидация ПЕРЕД созданием черновика. Негодный файл
    # не должен оставлять следов в БД: иначе `422`/`413` создаёт пользователю
    # анкету, которую он не заполнял (долг CR 2.4). Строка анкеты валидации
    # не нужна — проверяются сигнатура, размер и заявленный MIME.
    head = _read_signature(fileobj)
    validated: ValidatedPhoto = validate_photo(
        head=head, size_bytes=size_bytes, declared_content_type=declared_content_type
    )

    profile = await _get_or_create_draft(session, user)

    repository = ProfilePhotoRepository(session)
    # AC-03, D-7: проверка под блокировкой строки анкеты, полученной выше.
    if await repository.count_by_profile(profile.id) >= MAX_PHOTOS_PER_PROFILE:
        raise PhotoLimitReachedError

    photo_uuid = uuid_lib.uuid4()
    storage_key = _storage_key(profile.uuid, photo_uuid, validated.extension)
    position = repository.pick_next_position(await repository.list_positions(profile.id))
    if position is None:
        # Дубль проверки выше — страховка: `count` и свободные позиции могут
        # разойтись только при испорченной строке (позиция вне 1..3), и тогда
        # честнее `409`, чем `IntegrityError` от CHECK.
        raise PhotoLimitReachedError

    try:
        photo = await repository.create(
            session,
            profile,
            photo_uuid=photo_uuid,
            storage_key=storage_key,
            position=position,
            content_type=validated.content_type,
            size_bytes=validated.size_bytes,
            consent_given_at=datetime.now(UTC),
        )
    except IntegrityError as exc:
        # Task 6.3 (D-7): страховка от гонки, а не основной механизм. Второй
        # повтор `next_position` не нужен: вызывающий получит `409` через
        # проверку `count` при следующем запросе, а здесь важно не отдать `500`.
        #
        # CR 2.4 патч 3: `409` отдаётся только за нарушение уникальности
        # позиции (`uq_profile_photos_profile_id_position`) или ключа хранилища.
        # Раньше ЛЮБОЙ `IntegrityError` (нарушение `ck_profile_photos_content_type`,
        # `..._size`, `..._storage_key` после будущей правки валидатора) выглядел
        # для клиента как «лимит исчерпан» с советом удалить фото. Прочие
        # нарушения пробрасываются и дают `500` — это дефект, а не лимит.
        await session.rollback()
        if not _is_position_race(exc):
            raise
        raise PhotoLimitReachedError from None

    await media_service.put_object(
        storage_key=storage_key,
        fileobj=fileobj,
        content_type=validated.content_type,
        size_bytes=validated.size_bytes,
    )
    return photo


async def replace_photo(
    session: AsyncSession,
    user: User,
    photo_uuid: uuid_lib.UUID,
    *,
    fileobj: BinaryIO,
    size_bytes: int,
    declared_content_type: str | None,
    consent: bool,
) -> tuple[ProfilePhoto, str | None]:
    """Task 3.1 (AC-02, AC-05): атомарная замена файла существующего фото.

    Замена — мутация ОДНОЙ строки, а не `DELETE`+`POST` (D-1). Между двумя
    запросами фото физически отсутствовало бы: анкета на мгновение теряла
    место в карточке, а при `409` клиент удалял старое фото ДО загрузки
    нового и при отказе второго шага оставался без фото вовсе. Здесь окна
    нет: строка не исчезает ни на одном шаге.

    Порядок операций обязателен (D-3, D-7):
      1. согласие (FR64) — ДО любого обращения к S3 и до записи (D-6);
      2. валидация файла (AC-05) — ДО мутации: негодный файл не должен
         оставлять следов в БД;
      3. блокировка строки анкеты (`FOR UPDATE`) — тот же порядок, что
         в `add_photo`: `profiles` → `profile_photos`, цикла блокировок нет;
      4. поиск фото владельца (AC-09: чужое и несуществующее неразличимы);
      5. мутация полей и `flush`;
      6. upload в S3 — уже после `flush`, но до `commit`: сбой `put_object`
         откатывает строку, и метаданные не разъезжаются с бакетом.

    Возвращает `(photo, old_storage_key)`. Ключ нужен роутеру: объект
    в S3 удаляется ПОСЛЕ `commit` (D-10 стори 2.2) — после фиксации строки
    прежний ключ взять неоткуда. `None` означает, что ключ не изменился
    (то же расширение, D-4) и удалять ничего нельзя: `delete_object` снёс бы
    только что загруженный файл.

    `PhotoNotFoundError` — анкеты нет либо фото чужое/несуществует (`404`),
    как в `remove_photo`: отдельный код подтверждал бы существование чужого фото.
    """
    # AC-05: валидация и согласие — до любой записи. Обратный порядок
    # (мутация → проверка) оставил бы анкету с обнулённым `storage_key`
    # после `422`, то есть фото, которого нет ни в БД, ни в бакете.
    ensure_consent(consent)

    head = _read_signature(fileobj)
    validated: ValidatedPhoto = validate_photo(
        head=head, size_bytes=size_bytes, declared_content_type=declared_content_type
    )

    profile = await ProfileRepository(session).get_by_user_id_for_update(user.id)
    if profile is None:
        raise PhotoNotFoundError

    # CR 2.5, R-2: строка фото берётся ПОД БЛОКИРОВКОЙ (`FOR UPDATE`), а не
    # простым чтением. `apply_moderation_result` блокирует `profile_photos`,
    # а замена до этой правки — только `profiles`: пересечения не было, и
    # модератор мог записать `approved` поверх заменённого файла (или замена
    # молча затирала решение модератора). Порядок блокировок остаётся
    # `profiles` → `profile_photos`, цикла нет.
    #
    # Фильтр владельца проверяется здесь: `get_by_uuid_for_update` ищет по
    # одному `uuid`, поэтому чужое фото отсекается сравнением `profile_id`,
    # а не отдельным запросом (AC-09: чужое и несуществующее неразличимы).
    photo = await ProfilePhotoRepository(session).get_by_uuid_for_update(photo_uuid)
    if photo is None or photo.profile_id != profile.id:
        raise PhotoNotFoundError

    # AC-02: `uuid` и `position` НЕ меняются (D-2). `uuid` — адрес ресурса,
    # его смена сломала бы идемпотентность и открытые вкладки; `position` —
    # место в карточке, пользователь заменяет фото, а не переставляет его.
    old_storage_key = photo.storage_key
    # AC-02: тот же `uuid` в ключе + уникальный суффикс (CR 2.5, R-1);
    # расширение — из проверенного MIME (D-3). Ключ не может совпасть
    # со старым, поэтому прежний объект всегда удаляется после `commit`.
    new_storage_key = _replace_storage_key(profile.uuid, photo.uuid, validated.extension)

    # AC-02, D-6: замена = новый файл, поэтому согласие фиксируется заново,
    # а прежнее решение модератора сбрасывается целиком: `rejection_reason`
    # и `moderated_at` относятся к СТАРОМУ файлу и после замены ложны.
    # Мутация ORM-полями под `FOR UPDATE`, не `update()` (D-6 стори 2.4):
    # `update()` оставил бы в сессии старые значения, и ответ клиенту
    # содержал бы прежний статус.
    photo.storage_key = new_storage_key
    photo.content_type = validated.content_type
    photo.size_bytes = validated.size_bytes
    photo.status = str(PhotoStatus.PENDING)
    photo.rejection_reason = None
    photo.moderated_at = None
    photo.consent_given_at = datetime.now(UTC)
    try:
        await session.flush()
    except IntegrityError:
        # CR 2.5, патч P-2: `flush` здесь может нарушить любое ограничение
        # `profile_photos` (CHECK типа/размера, уникальность ключа). Без
        # отката сессия осталась бы в failed-транзакции, и роутер отдал бы
        # `500` на ЛЮБОЙ последующий запрос в этом соединении. Откат
        # обязателен; само исключение не глотаем — это дефект данных,
        # а не лимит (в отличие от `add_photo`, где есть ветка 409).
        await session.rollback()
        raise

    await media_service.put_object(
        storage_key=new_storage_key,
        fileobj=fileobj,
        content_type=validated.content_type,
        size_bytes=validated.size_bytes,
    )

    # AC-02, CR 2.5 R-1: ключ замены уникален ВСЕГДА ($uuid-$суффикс),
    # поэтому прежний объект удаляется после `commit` без ветвления.
    # Ветка «ключ совпал — удалять нельзя» (D-4 стори 2.5) больше не нужна:
    # совпадения не бывает, а возврат `None` здесь означал бы утечку объекта.
    return photo, old_storage_key


async def list_photos(session: AsyncSession, user: User) -> list[tuple[ProfilePhoto, str | None]]:
    """Task 3.1, 3.2 (AC-03, AC-04): фото анкеты; подпись — только у выданных.

    Подпись считается при каждом чтении, а не хранится: TTL 900 с, и
    сохранённая ссылка стала бы недействительной, а в БД попал бы временный
    секрет (D-11).

    AC-03, D-2: для `pending` возвращается `None`, и `get_signed_url`
    НЕ ВЫЗЫВАЕТСЯ ВООБЩЕ. Разница принципиальна: «посчитать и не отдать»
    оставило бы валидную ссылку на непроверенное фото в памяти на 900 с
    и в любой будущей правке ответа она могла бы уехать клиенту. Счётчик
    вызовов в тестах проверяет именно отсутствие вызова (Task 7.3).

    `approved` и `rejected` подписываются одинаково (AC-04): владелец видит
    и одобренное фото, и отклонённое — вместе с причиной отказа (AC-05).

    Анкеты нет — пустой список, а не `404`: экран анкеты рисует «фото нет»,
    и отдельный код ошибки заставлял бы клиент различать два пустых состояния.
    """
    profile = await ProfileRepository(session).get_by_user_id(user.id)
    if profile is None:
        return []
    photos = await ProfilePhotoRepository(session).list_by_profile(profile.id)
    result: list[tuple[ProfilePhoto, str | None]] = []
    for photo in photos:
        # AC-03: ветка `pending` не обращается к хранилищу вообще.
        if PhotoStatus(photo.status) is PhotoStatus.PENDING:
            result.append((photo, None))
            continue
        # CR 2.4 патч 4 (AC-03): сбой подписи ОДНОГО решённого фото не должен
        # делать ошибкой весь список. Раньше `MediaStorageError` из середины
        # цикла превращал `GET /me/photos` в `503`, и владелец терял из виду
        # и `approved`, и `pending` — включая те, для которых ссылка не нужна
        # вовсе. Теперь битое фото отдаётся с `url = None` и попадает
        # в лог; `MediaUnavailableError` (реквизиты S3 не заданы) по-прежнему
        # валит запрос целиком: это состояние деплоя, а не судьба одного объекта.
        try:
            url = media_service.get_signed_url(photo.storage_key)
        except media_service.MediaStorageError:
            logger.warning("photo_signed_url_failed", photo_uuid=str(photo.uuid))
            url = None
        result.append((photo, url))
    return result


# AC-05, D-10: предел причины отказа. CHECK в БД страхует только от пустой
# строки, длину держит сервис — иначе модератор записал бы в карточку
# пользователя килобайты текста.
MAX_REJECTION_REASON_LEN = 500

# Task 5.2 (AC-06), CR 2.4 патч 4: `str.strip()` без набора символов.
# Явный набор `\t\r\n\f\v` не содержал юникод-пробелов (NBSP `\u00a0`,
# тонкий `\u2009`, идеографический `\u3000`), и причина, вставленная из Word,
# считалась непустой: пользователь видел карточку отказа без текста.
# Голый `strip()` режет всё, что `str.isspace()` считает пробельным.


class ModerationTransitionError(Exception):
    """AC-06: доменный отказ записи решения модератора.

    Два РАЗНЫХ кода, а не один (VS 2.4, Enhancement): кабинет модератора
    (3.7) по коду отличает «в этот статус переводить нельзя» от «причина
    отказа не принята». С одним кодом клиент показывал бы одно и то же
    сообщение на два разных действия.

    Доменная ошибка, а не `IntegrityError`: CHECK БД отдал бы `500`
    на штатную ошибку модератора, и транзакция была бы уже испорчена.
    """

    CODE_TRANSITION_INVALID = "moderation_transition_invalid"
    CODE_REASON_INVALID = "moderation_reason_invalid"

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


async def apply_moderation_result(
    session: AsyncSession,
    photo_uuid: uuid_lib.UUID,
    *,
    status: PhotoStatus,
    rejection_reason: str | None = None,
) -> ProfilePhoto:
    """Task 5.2–5.6 (AC-05, AC-06): единственная точка записи решения.

    HTTP-маршрута у этой функции НЕТ и в 2.4 не будет: кабинет модератора —
    3.6/3.7, а до тех пор фото остаются `pending` (D-5, `deferred-work.md`).
    Потребители — сервис модерации 3.7 и тесты 2.4.

    Правила (AC-06):

      * цель `pending` запрещена — `moderation_transition_invalid`.
        Вернуть фото в очередь нельзя: CHECK БД требует для `pending`
        `moderated_at IS NULL`, то есть пришлось бы стирать историю решения;
      * `approved` — причина принудительно `None`;
      * `rejected` — причина обязательна, после `strip` непустая и
        ≤ `MAX_REJECTION_REASON_LEN`, иначе `moderation_reason_invalid`.
        Пустая причина нарушила бы `ck_profile_photos_rejection_reason`
        и дала бы `IntegrityError` → `500`;
      * консистентность проверяется ДО `flush`, поэтому строка не меняется
        при любом отказе (тест 8.3 проверяет именно это);
      * `moderated_at` ставится здесь, а не дефолтом модели: время решения —
        факт домена, а не свойство строки при вставке;
      * переходы между решёнными статусами разрешены (модератор передумал,
        Task 8.6) — новый `moderated_at` перезаписывает прежний.

    `commit()` не вызывается (D-4 стори 1.2): фиксацию делает вызывающий.
    `FOR UPDATE` берётся в репозитории — два модератора на одно фото
    выстроятся в очередь, а не перезапишут решение друг друга.
    """
    # CR 2.4 патч 1: нормализация ДО сравнений. Сравнения ниже — `is`, потому
    # что `PhotoStatus` — `StrEnum` с каноническими членами; но если вызывающий
    # (кабинет модератора 3.7) передал обычную строку `"pending"`, то
    # `"pending" is PhotoStatus.PENDING` — False, запрет молча не срабатывал,
    # и в БД уезжала строка, нарушающая CHECK → IntegrityError → 500 вместо
    # доменного кода. Приведение делает контракт устойчивым к типу аргумента.
    try:
        status = PhotoStatus(status)
    except ValueError:
        raise ModerationTransitionError(
            ModerationTransitionError.CODE_TRANSITION_INVALID,
            f"unknown moderation target: {status!r}",
        ) from None

    if status is PhotoStatus.PENDING:
        raise ModerationTransitionError(
            ModerationTransitionError.CODE_TRANSITION_INVALID,
            "moderation target pending is not allowed",
        )

    normalized_reason: str | None
    if status is PhotoStatus.APPROVED:
        normalized_reason = None
    else:
        # CR 2.4 патч 4: голый `strip()` — юникод-пробелы тоже пустыня.
        normalized_reason = (rejection_reason or "").strip()
        if not normalized_reason or len(normalized_reason) > MAX_REJECTION_REASON_LEN:
            raise ModerationTransitionError(
                ModerationTransitionError.CODE_REASON_INVALID,
                "rejection_reason must be non-empty and not longer than 500 chars",
            )

    repository = ProfilePhotoRepository(session)
    photo = await repository.get_by_uuid_for_update(photo_uuid)
    if photo is None:
        # Тот же тип, что и у владельца (AC-09 стори 2.2): несуществующее фото
        # не подтверждается отдельным кодом.
        raise PhotoNotFoundError

    await repository.set_moderation_result(
        photo,
        status=status,
        rejection_reason=normalized_reason,
        moderated_at=datetime.now(UTC),
    )
    return photo


async def remove_photo(session: AsyncSession, user: User, photo_uuid: uuid_lib.UUID) -> str:
    """Task 6.5 (AC-09, D-10): удаляет строку и возвращает `storage_key`.

    Возвращает ключ, потому что объект в S3 удаляется уже ПОСЛЕ `commit`
    в роутере: после фиксации строка недоступна, и ключ взять неоткуда.
    Порядок «commit → S3 delete» выбран сознательно: обратный порядок
    удалял бы объект, который при откате транзакции остался бы в БД
    с битой ссылкой.

    Чужое и несуществующее фото неразличимы — `PhotoNotFoundError` (AC-09).
    """
    profile = await ProfileRepository(session).get_by_user_id_for_update(user.id)
    if profile is None:
        raise PhotoNotFoundError

    photo = await ProfilePhotoRepository(session).get_owned(profile.id, photo_uuid)
    if photo is None:
        raise PhotoNotFoundError

    storage_key = photo.storage_key
    await ProfilePhotoRepository(session).delete(photo)
    return storage_key
