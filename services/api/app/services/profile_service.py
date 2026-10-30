"""Сервис анкеты: чтение, создание, правка (Task 4.1–4.6, Task 5.1–5.2).

project-context §9: цепочка «роутер → сервис → репозиторий». Роутер не
обращается к БД напрямую: иначе правило «какие данные видны» размазывается
по HTTP-слою, и второй потребитель (WebSocket, воркер) начинает его
дублировать с расхождениями.

D-4 стори 1.2: `commit()` здесь НЕ вызывается — единица работы у роутера.
Сервис делает `flush` через репозиторий и возвращает объект.

D-5: нормализация города живёт здесь (через `profile_normalize`), а не в БД:
CHECK умеет только края (`btrim`) и пустоту, а регистр в БД проверить нельзя —
`CHECK (lower(city) = city)` заставил бы хранить «томск».

D-1: `POST` при существующем `draft` заполняет его (`200`), при `complete` —
`409`. Без этого пользователь, загрузивший фото в 2.2 (строка-черновик уже
есть), не смог бы сохранить анкету вообще — тупик онбординга.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Gender
from app.models.profile import Profile
from app.models.user import User
from app.repositories.profiles import ProfileRepository
from app.schemas.profile import (
    BIO_MAX,
    CITY_MAX,
    NAME_MAX,
    ProfileCreate,
    ProfileUpdate,
)
from app.services.age_service import today_or_now, validate_birth_date
from app.services.consent_service import GenderRequiredError
from app.services.profile_normalize import (
    normalize_bio,
    normalize_city,
    normalize_name,
)


class ProfileFieldError(Exception):
    """AC-01, AC-03, AC-05: доменная ошибка поля — `422 <code>` в роутере.

    Код лежит в атрибуте, а не в тексте: HTTP-слой отдаёт его клиенту как
    `{"code": ...}`, и клиент выбирает текст ошибки по коду, а не по строке
    сообщения. Форма `422` (а не `400`) — project-context §4: это семантика
    домена, а не синтаксис запроса.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProfileExistsError(Exception):
    """AC-02, D-1: анкета уже заполнена — `409 profile_exists`.

    Отдельный тип, а не `ProfileFieldError`: это не ошибка поля, а конфликт
    состояния. Клиент по нему понимает «анкета уже есть, иди в правку».
    """


# Единственный источник правды по пределам — схема (`NAME_MAX` и др.).
# Дублирование литералами разошлось бы с Pydantic-лимитами: клиент получал бы
# `invalid_request` от схемы вместо доменного `*_too_long` из сервиса.
_TEXT_LIMITS: dict[str, tuple[int, str]] = {
    "name": (NAME_MAX, "name_too_long"),
    "city": (CITY_MAX, "city_too_long"),
    "bio": (BIO_MAX, "bio_too_long"),
}


# Фикс OOM (2026-10-22): во сколько раз длина ЗНАЧИМЫХ символов может
# превышать нормализованный предел, прежде чем отвергнуть без нормализации.
# Нормализация схлопывает пробельные последовательности, поэтому сырое
# значение законно бывает длиннее предела: `'  Томск  '` — 9 символов при
# итоговых 5. Запас ×20 (bio: 10 000 символов) покрывает любой разумный
# паддинг, но 5 млн символов из `bio` до `normalize_bio` уже не дойдут:
# посимвольный обход Unicode-категорий на них и был причиной OOM.
_RAW_LIMIT_FACTOR = 20

# Пробельные серии, которые нормализация всё равно схлопнет. Считаются
# в C-движке `re`, без Python-цикла по символам: `\s` покрывает NBSP
# и тонкий пробел, то есть весь «законный паддинг» из копипаста.
_RAW_WS_RUN = re.compile(r"\s+")


def _check_raw_length(field: str, value: str) -> None:
    """Фикс OOM (2026-10-22): отказ по длине ДО нормализации.

    Дефект: `bio` на 5 млн символов проходил в `normalize_bio`, и
    `_strip_invisible` обходил каждый символ (две проверки Unicode-категории)
    синхронно в async-обработчике. 10 МБ роняли контейнер по OOM, 5 МБ
    занимали event loop на ~1 с — все остальные запросы стояли.

    Считаются ЗНАЧИМЫЕ символы: пробельные серии схлопываются тем же
    правилом, что и в нормализации, но силами `re` (C-скорость). Иначе
    валидное значение с длинным паддингом (`'а' + ' ' * 10_000`) получало бы
    `bio_too_long`, хотя после нормализации занимает один символ.

    Код ошибки тот же, что у проверки после нормализации (`bio_too_long`
    и т.д.): клиенту не важно, какой из двух рубежей его отбил.
    """
    limit, code = _TEXT_LIMITS[field]
    if len(_RAW_WS_RUN.sub(" ", value)) > limit * _RAW_LIMIT_FACTOR:
        raise ProfileFieldError(code)


def _normalize_field(field: str, value: str, pilot_city: str) -> str:
    """Применяет нормализацию, соответствующую полю (D-5).

    Город канонизируется по эталону конфига (FR16), имя — без смены регистра
    (решает пользователь), текст — с сохранением абзацев.

    Фикс OOM (2026-10-22): перед нормализацией проверяется сырая длина —
    иначе мегабайты текста обходились посимвольно в event loop.
    """
    _check_raw_length(field, value)
    if field == "city":
        return normalize_city(value, pilot_city)
    if field == "bio":
        return normalize_bio(value)
    return normalize_name(value)


def _check_text(field: str, value: str, *, required_code: str) -> str:
    """Пустота и длина после нормализации — доменные коды, не `500` (AC-05).

    Порядок важен: сначала пустота. Иначе `'   '` (три пробела) сообщил бы
    `*_too_long` при пределе 50 — бессмысленный код для клиента.

    Длина проверяется ПОСЛЕ нормализации: `'  Томск  '` — пять символов,
    а не девять. Иначе пользователь получал бы отказ на тексте, который
    после схлопывания пробелов в предел укладывается.
    """
    if not value:
        raise ProfileFieldError(required_code)
    limit, code = _TEXT_LIMITS[field]
    if len(value) > limit:
        raise ProfileFieldError(code)
    return value


def _today_or_now(today: date | None) -> date:
    """`today` — параметр ради детерминированной границы 18 лет (AC-6).

    Значение по умолчанию берётся здесь, а не в сигнатурах вызывающих:
    HTTP-слой не должен знать про "сегодня", иначе его пришлось бы
    подменять в каждом тесте.

    Стори 12.3 (Task 10, долг `W-12.2-b`): сам источник «сегодня» переехал в
    `age_service.today_or_now()` — он же обслуживает остальные семь мест
    вызова. Второй хелпер не создаётся: дубль решения разошёлся бы на границе
    суток, и гард 18+ с сериализацией ответа посчитали бы разные числа.
    """
    return today_or_now() if today is None else today


def _check_birth_date(value: date, today: date | None = None) -> date:
    """AC-2, AC-4: дата рождения проверяется ДО БД.

    Иначе CHECK в БД дал бы `IntegrityError` → `500`, а несовершеннолетний
    получил бы доступ, потому что 18+ больше не декларируется (`age_confirmed`).

    Доменный код отказа (`underage` / `invalid_birth_date`) приходит из
    `age_service` — там же живёт формула, чтобы веб-форма и API считали
    возраст одинаково.
    """
    try:
        return validate_birth_date(value, _today_or_now(today))
    except ValueError as exc:
        raise ProfileFieldError(str(exc)) from exc


def _validate_and_normalize_create(
    payload: ProfileCreate, pilot_city: str, *, today: date | None = None
) -> dict[str, Any]:
    """Task 4.2 (AC-01, AC-04, AC-05): полное тело → нормализованные значения.

    Возвращает словарь ровно из четырёх полей: `gender`, `status`, `user_id`
    клиент не присылает (D-4), и в результат они не попадают.

    `city_required` вместо общего `field_required` — AC-01 называет город
    отдельно: клиент показывает ошибку у поля «город», а не общим баннером.
    """
    city = _normalize_field("city", payload.city, pilot_city)
    return {
        "name": _check_text(
            "name",
            _normalize_field("name", payload.name, pilot_city),
            required_code="field_required",
        ),
        # AC-01: город обязателен и получает собственный код.
        "city": _check_text("city", city, required_code="city_required"),
        "birth_date": _check_birth_date(payload.birth_date, today),
        "bio": _check_text(
            "bio", _normalize_field("bio", payload.bio, pilot_city), required_code="field_required"
        ),
    }


def _validate_and_normalize_update(
    payload: ProfileUpdate, pilot_city: str, *, today: date | None = None
) -> dict[str, Any]:
    """Task 4.2, D-11: частичное тело → только реально переданные поля.

    Различие «не передано» и «передано `null`» делается через
    `model_fields_set`:
      * ключа нет — поле не попадает в словарь, значение в БД не меняется;
      * ключ есть со значением `None` — `422 field_required` (D-3: стереть
        поле нельзя, иначе CHECK полноты даст `IntegrityError` → `500`).

    Проверки те же, что при создании: `PATCH` — не обходной путь для лимитов.
    """
    result: dict[str, Any] = {}
    for field in ("name", "city", "birth_date", "bio"):
        if field not in payload.model_fields_set:
            continue
        raw = getattr(payload, field)
        if raw is None:
            # D-11: явный `null` — отказ, а не обнуление поля.
            raise ProfileFieldError("field_required")
        if field == "birth_date":
            # Стори 12.2, VS-2: `birth_date` — единственное поле возраста.
            # `age` не пишется вовсе: у старой учётки он остаётся как был,
            # у новой — `NULL`. Записать вычисленное число значило бы вернуть
            # второй источник правды (ADR-17, «Отвергнуто» а).
            result[field] = _check_birth_date(raw, today)
            continue
        normalized = _normalize_field(field, raw, pilot_city)
        result[field] = _check_text(
            field,
            normalized,
            required_code="city_required" if field == "city" else "field_required",
        )
    return result


def _compute_status(
    *,
    name: str | None,
    city: str | None,
    age: int | None,
    bio: str | None,
    birth_date: date | None = None,
) -> str:
    """Task 4.3 (AC-02, AC-03, D-2): `complete` только при всех четырёх полях.

    `None` и пустая строка считаются незаполненным одинаково: CHECK полноты
    в БД смотрит на `NULL`, а пустая строка до БД не доходит — её отбивает
    валидация. Но полагаться на это нельзя: строка из воркера или миграции
    могла бы прийти с `''`, и `status='complete'` дал бы пустую карточку.
    """
    filled = [name, city, bio]
    # Стори 12.2, AC-5 (grandfathering): возраст есть из ЛЮБОГО источника.
    # 40 008 старых учёток имеют `age` и не имеют `birth_date` — для них
    # `complete` обязан сохраниться, иначе вход в продукт сломается у всех.
    # Требовать именно `birth_date` значило бы отбросить их в `draft`.
    if birth_date is None and age is None:
        return "draft"
    if any(not value for value in filled):
        return "draft"
    return "complete"


async def get_profile_for_user(session: AsyncSession, user: User) -> Profile | None:
    """Task 5.1 (AC-05): анкета пользователя или `None`.

    `None` — не ошибка: учётка без анкеты нормальна (онбординг не пройден,
    стори 2.2/2.3). Роутер превращает её в `404 profile_not_found`.
    """
    return await ProfileRepository(session).get_by_user_id(user.id)


async def create_profile(
    session: AsyncSession,
    user: User,
    payload: ProfileCreate,
    pilot_city: str,
    *,
    today: date | None = None,
) -> tuple[Profile, bool]:
    """Task 4.4–4.6 (AC-02, D-1, D-4): создание или дозаполнение черновика.

    Возвращает `(profile, created)`; роутер по `created` выбирает `201`/`200`.

    Три ветки:
      * анкеты нет — создаём (`created=True`, `201`);
      * есть `draft` — заполняем её (`created=False`, `200`). Строка могла
        появиться в 2.2 при загрузке фото: `profile_photos.profile_id NOT NULL`;
      * есть `complete` — `409 profile_exists` (вторая анкета невозможна,
        `uq_profiles_user_id`), прежние данные не перезаписываются.

    D-4: `gender` берётся из `users.gender`, а не из запроса. `None`
    теоретически невозможен после consent, но проверка дешёвая: составной FK
    `(user_id, gender)` не примет `NULL`, и без неё получился бы `500`.
    """
    values = _validate_and_normalize_create(payload, pilot_city, today=today)

    if user.gender is None:
        # D-4: без пола составной FK даст IntegrityError → 500.
        raise GenderRequiredError

    # Значения снимаются с `user` ДО возможного rollback: после отката объект
    # истекает, а синхронное чтение атрибута в async падает MissingGreenlet.
    user_id = user.id
    gender = str(user.gender)

    repository = ProfileRepository(session)
    # CR 2.3, патч 15: чтение под `FOR UPDATE` и здесь. Раньше `POST` читал
    # черновик без блокировки: `PATCH` фиксировал правку, `POST` перезаписывал
    # её своим снимком — потерянное обновление. `FOR UPDATE` ставит оба
    # запроса в очередь на одну строку.
    existing = await repository.get_by_user_id_for_update(user_id)

    if existing is not None:
        return await _fill_draft(session, existing, values, gender=gender, today=today)

    status = _compute_status(
        name=values["name"],
        city=values["city"],
        age=None,
        birth_date=values["birth_date"],
        bio=values["bio"],
    )
    try:
        profile = await repository.create(
            user_id=user_id,
            gender=gender,
            status=status,
            # CR 2.3, Решение 3: анкета мужчины создаётся скрытой. Пол в пилоте
            # один — женский: мужская анкета не должна попасть в выборку эпика 4,
            # когда та начнёт читать `profiles` по городу и полу. Запрещать
            # создание нельзя (онбординг общий), поэтому флаг, а не отказ.
            is_hidden=(gender == Gender.MALE),
            **values,
        )
    except IntegrityError:
        # CR 2.3, патч 1: гонка двух `POST` (двойной клик, retry BFF). Второй
        # `INSERT` нарушает `uq_profiles_user_id` и раньше давал необработанный
        # `500`. Паттерн `resolve_user` из 1.3: откат → повторное чтение →
        # решение по фактическому состоянию строки.
        await session.rollback()
        raced = await repository.get_by_user_id_for_update(user_id)
        if raced is None:
            # Ошибка не была гонкой (например, нарушен другой CHECK) — не глотаем.
            raise
        return await _fill_draft(session, raced, values, gender=gender, today=today)

    # Server-дефолты (`uuid`, `created_at`, `updated_at`) заполняются БД;
    # `refresh` делает их доступными для сериализации ответа без MissingGreenlet.
    await session.refresh(profile)
    return profile, True


async def _fill_draft(
    session: AsyncSession,
    profile: Profile,
    values: dict[str, Any],
    *,
    gender: str,
    today: date | None = None,
) -> tuple[Profile, bool]:
    """D-1: заполняет существующую строку — `200`, `created=False`.

    Вынесено отдельной функцией, потому что ветка нужна дважды: при обычном
    чтении черновика и после отката проигранной гонки (`IntegrityError`).
    Дублирование разошлось бы: одна ветка ставила бы `is_hidden`, другая нет.

    `status='complete'` — `409 profile_exists`: вторая анкета невозможна
    (`uq_profiles_user_id`), прежние данные не перезаписываются.
    """
    if profile.status == "complete":
        raise ProfileExistsError

    for field, value in values.items():
        setattr(profile, field, value)
    profile.status = _compute_status(
        name=values["name"],
        city=values["city"],
        # AC-5: legacy-`age` у существующей строки не сбрасывается —
        # учётка, заполнившая дату, сохраняет старый запасной источник.
        age=profile.age,
        birth_date=values["birth_date"],
        bio=values["bio"],
    )
    # CR 2.3, Решение 3: дозаполнение черновика мужчиной тоже даёт скрытую
    # анкету — иначе флаг зависел бы от того, кто создал строку (2.2 — фото).
    profile.is_hidden = gender == Gender.MALE
    await session.flush()
    # `updated_at` обновляется в БД (`onupdate=func.now()`): после UPDATE
    # SQLAlchemy помечает атрибут истёкшим, и сериализация в ответ падает
    # MissingGreenlet — синхронный доступ к незагруженному атрибуту в async.
    # `expire_on_commit=False` этого не лечит: истечение происходит на
    # UPDATE, а не на commit. Загрузка здесь, в сервисе, — одна точка,
    # вместо повторов в роутере и в каждом потребителе.
    await session.refresh(profile)
    return profile, False


async def update_profile(
    session: AsyncSession,
    user: User,
    payload: ProfileUpdate,
    pilot_city: str,
    *,
    today: date | None = None,
) -> Profile | None:
    """Task 4.5 (AC-03, D-6, D-8): частичная правка с блокировкой строки.

    `None` — анкеты нет; роутер превращает это в `404 profile_not_found`.
    Отсутствие анкеты проверяется ДО валидации полей: `PATCH` без анкеты
    должен давать `404`, а не `422 city_required` — иначе клиент без анкеты
    получал бы ошибку формы вместо «анкеты ещё нет».

    D-8: чтение с `FOR UPDATE`. `status` вычисляется из итогового состояния;
    два параллельных `PATCH` без блокировки читают один снимок, каждый считает
    полноту по своему набору, и второй пишет `status='draft'` при фактически
    заполненной анкете.
    """
    repository = ProfileRepository(session)
    profile = await repository.get_by_user_id_for_update(user.id)
    if profile is None:
        return None

    values = _validate_and_normalize_update(payload, pilot_city, today=today)
    for field, value in values.items():
        setattr(profile, field, value)

    # AC-03: `status` пересчитывается по факту заполнения всех четырёх полей.
    profile.status = _compute_status(
        name=profile.name,
        city=profile.city,
        age=profile.age,
        birth_date=profile.birth_date,
        bio=profile.bio,
    )
    await session.flush()
    # AC-03: `updated_at` пересчитан БД — подтягиваем до отдачи в ответ,
    # иначе сериализация падает MissingGreenlet (см. `create_profile`).
    await session.refresh(profile)
    return profile
