"""Правила сервиса мест (Task 7.7, 8.3; AC-02, AC-03, AC-04).

§18.1: каждый тест краснеет от удаления конкретной защиты. Проверяется не
«функция вернула объект», а «нелегальное состояние не создано»: место
в неизвестном сообществе, дубль имени и строка с ZWSP по краям не должны
дожить до `commit`.

§18.3: гонку двух вставок в одной сессии доказать нельзя. Поэтому
`IntegrityError` подменяется исключением драйвера с именем ограничения —
проверяется ветка обработки, а сама уникальность держится
`uq_places_city_name` (интеграционные тесты).
"""

from __future__ import annotations

import uuid as uuid_lib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.enums import PlaceCategory
from app.services.place_service import (
    PLACE_DUPLICATE_CODE,
    PlaceDuplicateError,
    PlaceUnknownCommunityError,
    create_place,
)


class _FakeNested:
    """SAVEPOINT-заглушка: считает входы и умеет «сломаться» при выходе."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.entered = 0

    async def __aenter__(self) -> _FakeNested:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self.error is not None:
            raise self.error
        return False


class _FakeSession:
    """Сессия-заглушка: `flush`, `commit`, `begin_nested` без PostgreSQL."""

    def __init__(self, *, flush_error: BaseException | None = None) -> None:
        self.flush_error = flush_error
        self.flushes = 0
        self.commits = 0
        self.nested = _FakeNested()
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        if self.flush_error is not None:
            raise self.flush_error

    async def commit(self) -> None:
        self.commits += 1

    def begin_nested(self) -> _FakeNested:
        return self.nested

    async def delete(self, obj: Any) -> None:
        """Task 7.2 (AC-04): физического удаления в коде быть не должно.

        Метод существует только чтобы тест мог утверждать «его не вызвали»:
        без него `session.delete` упал бы `AttributeError` и проверка
        «мягкое удаление всегда» была бы тавтологией на отсутствии метода.
        """
        self.deleted.append(obj)


class _FakeRepository:
    """Репозиторий-заглушка: результаты проверок дубля управляются тестом."""

    exists_result = False
    # Стори 3.2 (AC-03): что вернёт поиск существующего места по имени.
    found_place: Any = None
    # Стори 3.2 (AC-05): что вернёт поиск дубля С ИСКЛЮЧЕНИЕМ себя.
    excluding_result: Any = None
    checked: list[tuple[str, str]] = []
    excluding_calls: list[tuple[str, str, Any]] = []
    # Стори 3.3 (Task 7.3, AC-01…AC-04): страница и `total`, которые вернёт
    # репозиторий. Счётчик `page_calls` нужен, чтобы доказать «курсор разобран
    # ДО запроса» и «категория проверена ДО запроса»: при ошибке репозиторий
    # обязан остаться нетронутым.
    page_items: list[Any] = []
    page_total: int = 0
    page_calls: list[dict[str, Any]] = []

    def __init__(self, session: Any) -> None:
        self._session = session

    def add(self, place: Any) -> None:
        self._session.add(place)

    async def exists_in_city(self, city_slug: str, name: str) -> bool:
        type(self).checked.append((city_slug, name))
        return type(self).exists_result

    async def find_by_city_and_name(self, city_slug: str, name: str) -> Any:
        """Стори 3.2 (AC-03, D-6): существующее место с этим именем.

        Возвращается строка, а не `bool`: сервису нужен `is_active`, чтобы
        отличить активный дубль (`PLACE_DUPLICATE`) от скрытого
        (`PLACE_ARCHIVED` + `existing_uuid`).
        """
        type(self).checked.append((city_slug, name))
        return type(self).found_place

    async def exists_in_city_excluding(
        self, city_slug: str, name: str, exclude_uuid: Any
    ) -> Any:
        """Task 3.3 (AC-05): поиск дубля, не считая самого места."""
        type(self).excluding_calls.append((city_slug, name, exclude_uuid))
        return type(self).excluding_result

    async def list_page(self, **kwargs: Any) -> tuple[list[Any], int]:
        """Task 7.3 (AC-01…AC-04): страница и `total` без PostgreSQL.

        Принимает те же именованные аргументы, что производственный метод,
        и записывает их: тест проверяет, что сервис передал именно тот слаг,
        курсор и `include_inactive`, а не «вызвал хоть что-то».
        """
        type(self).page_calls.append(kwargs)
        return list(type(self).page_items), type(self).page_total

    @staticmethod
    def is_duplicate_name_error(error: Any) -> bool:
        """CR 3.1, патч 10: делегирует НАСТОЯЩЕЙ реализации, а не повторяет её.

        Раньше фейк копировал тело продакшн-метода. Копия повторяла и дефект:
        `getattr(error.orig, "constraint_name")` не работает под asyncpg, но
        и реализация, и фейк «соглашались» друг с другом — тест был зелёным
        при полностью мёртвой ветке. Теперь проверяется реальный код, и форма
        ошибки берётся из живого драйвера (см. `_asyncpg_unique_violation`).
        """
        from app.repositories.places import PlaceRepository as RealRepository

        return RealRepository.is_duplicate_name_error(error)


@pytest.fixture(autouse=True)
def _reset_repository() -> None:
    """Сбрасывает состояние заглушки между тестами (§18.8)."""
    _FakeRepository.exists_result = False
    _FakeRepository.found_place = None
    _FakeRepository.excluding_result = None
    _FakeRepository.checked = []
    _FakeRepository.excluding_calls = []
    _FakeRepository.page_items = []
    _FakeRepository.page_total = 0
    _FakeRepository.page_calls = []


@pytest.fixture(autouse=True)
def _patch_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет репозиторий и `flush`, чтобы не поднимать БД.

    `flush` у фейковой сессии свой, но сервис создаёт объект через модель,
    и без подмены репозитория `exists_in_city` пошёл бы в PostgreSQL.
    """
    from app.services import place_service

    monkeypatch.setattr(place_service, "PlaceRepository", _FakeRepository)


def _session_with_settings(**overrides: Any) -> tuple[_FakeSession, Any]:
    """Собирает фейковую сессию и настройки сообщества.

    `city_slugs` подменяются явно: тест не должен зависеть от `.env`
    разработчика (там стендовый набор сообществ).
    """
    from app.core.config import settings as real_settings

    values = {
        "city_slugs": ("tomsk", "kemerovo"),
        "default_city_slug": "tomsk",
        **overrides,
    }
    return _FakeSession(), SimpleNamespace(**{**vars(real_settings), **values})


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Кафе „Прага“",
        "category": PlaceCategory.CAFE,
        "address_text": "пр. Ленина, 1",
        "city_slug": "tomsk",
    }
    base.update(overrides)
    return base


# --- AC-03: сообщество из конфига ------------------------------------------------


async def test_unknown_city_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03, ADR-01 «отвергнуто (б)»: слаг вне конфига — доменная ошибка.

    Краснеет, если убрать сверку с `settings.city_slugs`: место легло бы
    в «невидимое сообщество» (опечатка `tomsk` → `tomsk1`), и понять это по
    API невозможно — лента просто пустая.
    """
    session, settings = _session_with_settings()

    with pytest.raises(PlaceUnknownCommunityError):
        await create_place(
            session,
            settings=settings,
            **_kwargs(city_slug="tomsk1"),
        )

    assert session.added == []


async def test_known_city_accepted() -> None:
    """AC-03: слаг из конфига проходит — сверка не блокирует легальный ввод.

    Краснеет, если сверять с `default_city_slug` вместо списка: второе
    сообщество (после расширения пилота) стало бы недоступным.
    """
    session, settings = _session_with_settings()

    place = await create_place(session, settings=settings, **_kwargs(city_slug="kemerovo"))

    assert place.city_slug == "kemerovo"


# --- AC-01: нормализация до вставки ---------------------------------------------


async def test_name_and_address_normalized_before_insert() -> None:
    """AC-01, §18.1: нормализация идёт ДО вставки, а не «в БД как-нибудь».

    Краснеет, если убрать нормализацию: `CHECK ck_places_name` отверг бы имя
    с ZWSP по краям, и пользователь получил бы `500` вместо сохранения.
    """
    session, settings = _session_with_settings()

    place = await create_place(
        session,
        settings=settings,
        **_kwargs(name="  Кафе\u200b „Прага“\u00a0", address_text=" пр. Ленина,\t1 "),
    )

    assert place.name == "Кафе „Прага“"
    assert place.address_text == "пр. Ленина, 1"


async def test_blank_name_rejected_before_insert() -> None:
    """AC-01: имя из невидимых символов — доменная ошибка, не `IntegrityError`.

    Краснеет, если проверять только длину: строка из одного ZWSP имеет длину 1
    и падала бы `CHECK`-ом в БД — `500` вместо `422` с понятным кодом.
    """
    from app.services.place_service import PlaceFieldError

    session, settings = _session_with_settings()

    with pytest.raises(PlaceFieldError):
        await create_place(session, settings=settings, **_kwargs(name="\u200b"))

    assert session.added == []


# --- AC-04: дубль ---------------------------------------------------------------


async def test_duplicate_detected_on_cheap_path() -> None:
    """AC-04, D-4: найденный дубль — `PlaceDuplicateError` с кодом `PLACE_DUPLICATE`.

    Краснеет, если убрать проверку `exists_in_city`: пользователь получил
    бы исключение драйвера (`500`) вместо понятного `409` в 3.2.
    """
    _FakeRepository.found_place = _place()
    session, settings = _session_with_settings()

    with pytest.raises(PlaceDuplicateError) as excinfo:
        await create_place(session, settings=settings, **_kwargs())

    assert excinfo.value.code == PLACE_DUPLICATE_CODE
    assert PLACE_DUPLICATE_CODE == "PLACE_DUPLICATE"
    assert session.added == []


def _asyncpg_integrity_error(constraint: str, *, sqlstate: str = "23505") -> IntegrityError:
    """CR 3.1, патч 10: `IntegrityError` В ТОЙ ФОРМЕ, что даёт живой asyncpg.

    Форма снята с реального драйвера (asyncpg 0.30 / PostgreSQL 18), а не
    придумана: `error.orig` — обёртка SQLAlchemy
    (`sqlalchemy.dialects.postgresql.asyncpg.UniqueViolationError`), у которой
    НЕТ `constraint_name`; имя ограничения лежит во внутреннем объекте
    `error.orig.orig` (`asyncpg.exceptions.UniqueViolationError`).

    Старая версия тестов клала `constraint_name` прямо на `error.orig`, то есть
    проверяла форму ошибки, которой в природе не существует. Из-за этого
    `is_duplicate_name_error` мог быть мёртвым, а тест — зелёным.
    """

    class _InnerDriverError(Exception):
        def __init__(self, name: str, state: str) -> None:
            super().__init__("driver error")
            self.constraint_name = name
            self.sqlstate = state
            self.pgcode = state

    class _SQLAlchemyWrapper(Exception):
        """Копия обёртки: атрибута `constraint_name` у неё нет намеренно."""

        def __init__(self, inner: BaseException, state: str) -> None:
            super().__init__("wrapped driver error")
            self.orig = inner
            self.sqlstate = state
            self.pgcode = state
            self.detail = "Key (city_slug, name) already exists."

    inner = _InnerDriverError(constraint, sqlstate)

    return IntegrityError("INSERT", {}, _SQLAlchemyWrapper(inner, sqlstate))


async def test_duplicate_detected_from_integrity_error() -> None:
    """AC-04, D-4: гонка двух вставок даёт ТУ ЖЕ доменную ошибку.

    Краснеет, если не обрабатывать `IntegrityError` ИЛИ если имя ограничения
    искать не там, где его кладёт asyncpg: вторая параллельная вставка
    отбивается `uq_places_city_name` уже после того, как
    `exists_in_city` вернул `False`, и админ получил бы `500`.
    """
    session, settings = _session_with_settings()
    session.flush_error = _asyncpg_integrity_error("uq_places_city_name")

    with pytest.raises(PlaceDuplicateError) as excinfo:
        await create_place(session, settings=settings, **_kwargs())

    assert excinfo.value.code == PLACE_DUPLICATE_CODE


async def test_foreign_integrity_error_is_not_masked_as_duplicate() -> None:
    """AC-04, Task 6.4: чужое нарушение целостности не превращается в дубль.

    Краснеет, если ловить любой `IntegrityError`: нарушение `ck_places_category`
    или `ck_places_city_slug` показывалось бы админу как «такое место
    уже есть», и он искал бы несуществующий дубль вместо кривого слага.
    """
    session, settings = _session_with_settings()
    session.flush_error = _asyncpg_integrity_error("ck_places_category", sqlstate="23514")

    with pytest.raises(IntegrityError):
        await create_place(session, settings=settings, **_kwargs())


# --- D-4, Task 7.5: SAVEPOINT ---------------------------------------------------


async def test_insert_runs_inside_savepoint() -> None:
    """Task 7.5, §18.8: вставка идёт в `begin_nested()`.

    Краснеет, если убрать SAVEPOINT: после `IntegrityError` транзакция
    в состоянии aborted, и следующий запрос в той же сессии падает
    `current transaction is aborted`. Тестовый `create_savepoint` спасает
    изоляцию теста, но не отменяет требования.
    """
    session, settings = _session_with_settings()

    await create_place(session, settings=settings, **_kwargs())

    assert session.nested.entered == 1


async def test_service_does_not_commit() -> None:
    """D-4 (стори 2.2): сервис не коммитит — единица работы у роутера.

    Краснеет, если добавить `commit()`: транзакцией начнёт владеть сервис,
    и откат на ошибке S3 в 3.2 станет невозможен.
    """
    session, settings = _session_with_settings()

    await create_place(session, settings=settings, **_kwargs())

    assert session.commits == 0


# --- CR 3.1: защиты, добавленные код-ревью ---------------------------------------


@pytest.mark.parametrize(
    ("field", "limit"),
    [("name", 120), ("address_text", 255)],
)
async def test_oversize_field_raises_domain_error(field: str, limit: int) -> None:
    """CR 3.1, патч 4: переполнение `varchar` — доменная ошибка, а не `500`.

    §18.1: КРАСНЕЕТ без проверки длины в сервисе. Переполнение `varchar`
    даёт `DBAPIError`, а не `IntegrityError` (проверено на живом asyncpg),
    поэтому `except IntegrityError` его не видит: воркер и импорт — прямо
    заявленные потребители сервиса — получали `500` на пользовательском вводе.
    """
    from app.services.place_service import PlaceFieldError

    session, settings = _session_with_settings()

    with pytest.raises(PlaceFieldError) as excinfo:
        await create_place(session, settings=settings, **_kwargs(**{field: "Ж" * (limit + 1)}))

    assert excinfo.value.code == f"{field}_too_long"
    assert excinfo.value.details == {"max_length": limit, "actual_length": limit + 1}
    assert session.added == []


async def test_oversize_after_normalization_is_rejected() -> None:
    """CR 3.1, патч 4: длина считается ПОСЛЕ NFC-нормализации.

    §18.1: краснеет, если проверять длину до нормализации. NFC удлиняет
    строку (`\ufb2c` → 3 символа, проверено), поэтому имя, валидное до
    канонизации, может превысить ширину колонки после неё.
    """
    from app.services.place_service import PlaceFieldError

    session, settings = _session_with_settings()
    # 41 символ `\ufb2c` → 123 символа после NFC > 120.
    payload = "\ufb2c" * 41

    with pytest.raises(PlaceFieldError) as excinfo:
        await create_place(session, settings=settings, **_kwargs(name=payload))

    assert excinfo.value.code == "name_too_long"
    assert session.added == []


async def test_invalid_category_code_is_constant_and_carries_allowed_list() -> None:
    """CR 3.1, патч 3 (AC-02): код категории — константа, перечень — в `details`.

    §18.1: краснеет, если склеить код и перечень в одну строку (как было:
    `category_invalid: допустимые категории: ...`). Клиент 3.2 ветвится по
    `code`, и неконстантная строка не совпадёт ни с одной веткой. Перечень
    обязан быть полным: AC-02 требует «перечень допустимых категорий».
    """
    from app.services.place_service import PLACE_CATEGORY_INVALID_CODE, PlaceFieldError

    session, settings = _session_with_settings()

    with pytest.raises(PlaceFieldError) as excinfo:
        await create_place(session, settings=settings, **_kwargs(category="bar"))

    assert excinfo.value.code == PLACE_CATEGORY_INVALID_CODE == "category_invalid"
    assert excinfo.value.details == {"allowed": [str(item) for item in PlaceCategory]}
    assert session.added == []


async def test_string_city_slugs_rejected() -> None:
    """CR 3.1, патч 5: строка вместо последовательности — `TypeError`, не подстрока.

    §18.1: краснеет без рантайм-гарда. `"toms" in "tomsk"` истинно (проверено),
    поэтому место создавалось бы в несуществующем сообществе `toms`.
    """
    session, settings = _session_with_settings(city_slugs="tomsk")

    with pytest.raises(TypeError):
        await create_place(session, settings=settings, **_kwargs(city_slug="toms"))

    assert session.added == []


# --- Стори 3.2: правка места (Task 3; AC-05, AC-07) ------------------------------


def _place(name: str = "Кафе „Прага“", *, is_active: bool = True) -> Any:
    """Место в памяти: правка не требует БД, проверяются решения сервиса."""
    from app.models.place import Place

    place = Place(
        name=name,
        category="cafe",
        address_text="пр. Ленина, 1",
        city_slug="tomsk",
    )
    place.is_active = is_active
    return place


async def test_update_touches_only_passed_fields() -> None:
    """AC-05: правка одного поля не перезаписывает остальные.

    §18.1: краснеет, если применять `changes` через `setattr` по всем ключам
    схемы (включая `None`): правка категории стёрла бы название и адрес,
    и место стало бы невалидным по `NOT NULL`.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    place = _place()

    updated = await update_place(
        session, place=place, changes={"category": PlaceCategory.CINEMA}, settings=settings
    )

    assert updated.category == "cinema"
    assert updated.name == "Кафе „Прага“"
    assert updated.address_text == "пр. Ленина, 1"


async def test_update_own_name_does_not_conflict() -> None:
    """AC-05: переименование «в себя» (нормализация) не даёт ложный `409`.

    §18.1: краснеет, если убрать `exclude_uuid` из поиска дубля. Правка только
    категории ищет место по собственному имени, находит СЕБЯ и отказывает
    `PLACE_DUPLICATE` на легальном вводе. `'Кафе  „Прага“'` → `'Кафе „Прага“'`
    нормализуется в то же имя, но конфликтом не является.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    place = _place(name="Кафе „Прага“")

    updated = await update_place(
        session, place=place, changes={"name": "  Кафе  „Прага“ "}, settings=settings
    )

    assert updated.name == "Кафе „Прага“"
    assert _FakeRepository.excluding_calls, "поиск дубля с исключением себя не вызван"


async def test_update_foreign_name_is_duplicate() -> None:
    """AC-05, AC-03: чужое имя в сообществе — `PlaceDuplicateError`.

    §18.1: краснеет, если не проверять дубль при правке: `uq` отбил бы
    вставку `IntegrityError`-ом, и админ получил бы `500` вместо `409`.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    _FakeRepository.excluding_result = _place(name="Кино „Победа“")
    place = _place()

    with pytest.raises(PlaceDuplicateError):
        await update_place(
            session, place=place, changes={"name": "Кино „Победа“"}, settings=settings
        )

    assert place.name == "Кафе „Прага“"


async def test_update_hidden_duplicate_is_archived_error() -> None:
    """AC-03, D-6: скрытый дубль — `PLACE_ARCHIVED` с `existing_uuid`.

    §18.1: краснеет, если скрытый дубль отдавать как `PLACE_DUPLICATE`: админ
    увидел бы «такое место уже есть» про место, которого нет в выдаче, и не
    получил бы единственного действия — «восстановить».
    """
    from app.services.place_service import PLACE_ARCHIVED_CODE, PlaceArchivedError, update_place

    session, settings = _session_with_settings()
    hidden = _place(name="Кино „Победа“", is_active=False)
    hidden.uuid = uuid_lib.uuid4()
    _FakeRepository.excluding_result = hidden
    place = _place()

    with pytest.raises(PlaceArchivedError) as excinfo:
        await update_place(
            session, place=place, changes={"name": "Кино „Победа“"}, settings=settings
        )

    assert excinfo.value.code == PLACE_ARCHIVED_CODE == "PLACE_ARCHIVED"
    assert excinfo.value.existing_uuid == hidden.uuid


async def test_update_can_restore_hidden_place() -> None:
    """AC-05, D-7: `is_active=true` возвращает место в выдачу.

    §18.1: краснеет, если `is_active` не входит в набор изменяемых полей:
    единственный маршрут восстановления (D-7) перестал бы работать, а
    `PLACE_ARCHIVED` подсказывал бы действие, которого нет.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    place = _place(is_active=False)

    updated = await update_place(
        session, place=place, changes={"is_active": True}, settings=settings
    )

    assert updated.is_active is True


async def test_update_blank_name_rejected() -> None:
    """AC-07: имя из ZWSP после нормализации — доменный `422 name_required`.

    §18.1: краснеет, если не переиспользовать проверки `create_place`: пустое
    имя дошло бы до БД и упало `CHECK`-ом — `500` на пользовательском вводе.
    """
    from app.services.place_service import PlaceFieldError, update_place

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"name": "\u200b"}, settings=settings)

    assert excinfo.value.code == "name_required"
    assert place.name == "Кафе „Прага“"


async def test_update_oversize_after_normalization_rejected() -> None:
    """AC-07, CR 3.1 патч 4: длина считается ПОСЛЕ нормализации и на правке.

    §18.1: краснеет, если проверять длину до NFC-канонизации: 41 × `\ufb2c`
    удлиняется до 123 символов и переполняет `varchar(120)` — это `DBAPIError`
    (не `IntegrityError`), то есть `500` из сервиса.
    """
    from app.services.place_service import PlaceFieldError, update_place

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"name": "\ufb2c" * 41}, settings=settings)

    assert excinfo.value.code == "name_too_long"
    assert excinfo.value.details == {"max_length": 120, "actual_length": 123}


async def test_update_invalid_category_carries_allowed() -> None:
    """AC-07: неизвестная категория на правке — `category_invalid` + `details`.

    §18.1: краснеет, если проверять категорию только в Pydantic: воркер/импорт
    идут мимо схем, и `ck_places_category` дал бы `500`.
    """
    from app.services.place_service import (
        PLACE_CATEGORY_INVALID_CODE,
        PlaceFieldError,
        update_place,
    )

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"category": "bar"}, settings=settings)

    assert excinfo.value.code == PLACE_CATEGORY_INVALID_CODE
    assert excinfo.value.details == {"allowed": [str(item) for item in PlaceCategory]}


async def test_update_duplicate_race_is_domain_error() -> None:
    """AC-03: гонка двух `UPDATE` даёт тот же `PlaceDuplicateError`.

    §18.3: гонку в одной сессии доказать нельзя, поэтому подменяется
    `IntegrityError` с РЕАЛЬНОЙ формой asyncpg. Краснеет, если не обрабатывать
    `IntegrityError` на правке: админ получил бы `500` вместо `409`.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    session.flush_error = _asyncpg_integrity_error("uq_places_city_name")
    place = _place()

    with pytest.raises(PlaceDuplicateError):
        await update_place(
            session, place=place, changes={"name": "Кино „Победа“"}, settings=settings
        )


async def test_update_foreign_integrity_error_propagates() -> None:
    """AC-03: чужое нарушение целостности не маскируется под дубль.

    §18.1: краснеет, если ловить любой `IntegrityError`: нарушение CHECK
    показывалось бы как «такое место уже есть».
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    session.flush_error = _asyncpg_integrity_error("ck_places_category", sqlstate="23514")
    place = _place()

    with pytest.raises(IntegrityError):
        await update_place(session, place=place, changes={"category": "cinema"}, settings=settings)


async def test_update_runs_inside_savepoint_and_does_not_commit() -> None:
    """Task 3.5, D-4: правка в `begin_nested()`, `commit()` — у роутера.

    §18.8: краснеет, если убрать SAVEPOINT (после `IntegrityError` сессия
    непригодна) или добавить `commit()` в сервис (единица работы переедет
    в сервис, и откат на ошибке в роутере станет невозможен).
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    place = _place()

    await update_place(
        session, place=place, changes={"category": PlaceCategory.CINEMA}, settings=settings
    )

    assert session.nested.entered == 1
    assert session.commits == 0


async def test_update_without_changes_is_noop() -> None:
    """AC-05, RISK-2: пустой набор изменений ничего не пишет.

    Сервис не решает, что считать пустым запросом: `PATCH` без полей отбивает
    роутер кодом `place_nothing_to_update`. §18.1: краснеет, если сервис начнёт
    падать на пустом словаре — это превратило бы защиту роутера в `500`.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    place = _place()

    updated = await update_place(session, place=place, changes={}, settings=settings)

    assert updated is place
    assert session.nested.entered == 0


# --- Стори 3.2: мягкое скрытие (Task 4; AC-04) -----------------------------------


async def test_deactivate_sets_flag_and_keeps_row() -> None:
    """AC-04, R-04: `DELETE` — это `is_active=false`, строка жива.

    §18.1: краснеет, если заменить на `session.delete`: карточки эпика 4
    потеряли бы место (`event_cards.place_id` — FK в `0008`), а FR68 говорит
    «скрыть», а не «удалить».
    """
    from app.services.place_service import deactivate_place

    session, settings = _session_with_settings()
    place = _place()

    deactivated = await deactivate_place(session, place=place)

    assert deactivated.is_active is False
    assert session.deleted == []
    assert session.commits == 0


async def test_deactivate_is_idempotent() -> None:
    """AC-04: повторный `DELETE` — `204`, без падения.

    §18.1: краснеет, если на уже скрытом месте бросать ошибку: ретрай клиента
    (обрыв сети после записи) получал бы `409`/`404` на успешной операции.
    """
    from app.services.place_service import deactivate_place

    session, _ = _session_with_settings()
    place = _place(is_active=False)

    deactivated = await deactivate_place(session, place=place)

    assert deactivated.is_active is False
    assert session.deleted == []


async def test_create_hidden_duplicate_is_archived() -> None:
    """AC-03, D-6: скрытый дубль при СОЗДАНИИ — `PLACE_ARCHIVED` с `uuid`.

    §18.1: краснеет, если на создании отвечать `PLACE_DUPLICATE`. Найдено
    интеграционным тестом `test_create_hidden_duplicate_is_archived`: сервис
    проверял только факт существования имени и не смотрел на `is_active`,
    поэтому админ получал «такое место уже есть» про место, которого нет
    в выдаче, и не мог выйти из тупика.
    """
    from app.services.place_service import PLACE_ARCHIVED_CODE, PlaceArchivedError

    hidden = _place(name="Кафе „Прага“", is_active=False)
    hidden.uuid = uuid_lib.uuid4()
    _FakeRepository.found_place = hidden
    session, settings = _session_with_settings()

    with pytest.raises(PlaceArchivedError) as excinfo:
        await create_place(session, settings=settings, **_kwargs())

    assert excinfo.value.code == PLACE_ARCHIVED_CODE
    assert excinfo.value.existing_uuid == hidden.uuid
    assert session.added == []


async def test_create_active_duplicate_stays_plain_duplicate() -> None:
    """AC-03: активный дубль — `PLACE_DUPLICATE`, без `existing_uuid`.

    §18.1: краснеет, если ветку `PLACE_ARCHIVED` сделать безусловной: админ
    получал бы «восстановите» для места, которое и так видно в выдаче.
    """
    _FakeRepository.found_place = _place()
    session, settings = _session_with_settings()

    with pytest.raises(PlaceDuplicateError) as excinfo:
        await create_place(session, settings=settings, **_kwargs())

    assert excinfo.value.code == PLACE_DUPLICATE_CODE
    assert not hasattr(excinfo.value, "existing_uuid")


# --- CR 3.2: дефекты, найденные код-ревью (патчи 2–6) ----------------------------


async def test_update_null_name_is_field_error_not_500() -> None:
    """CR 3.2, патч 2 (AC-05, AC-07): явный `null` в `name` — `422 name_required`.

    §18.1: КРАСНЕЕТ от отката патча 2 — до него `None` уходил в
    `normalize_text(None)` → `TypeError` → `500` без конверта `{code}`.
    Проверено исполнением: `PATCH {"name": null}` давал `500 Internal Server Error`.
    """
    from app.services.place_service import PlaceFieldError, update_place

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"name": None}, settings=settings)

    assert excinfo.value.code == "name_required"
    assert session.nested.entered == 0, "ошибка поля не должна открывать SAVEPOINT"


async def test_update_null_address_is_field_error_not_500() -> None:
    """CR 3.2, патч 2: то же для `address_text`.

    §18.1: краснеет от отката патча 2 (`TypeError` в `normalize_text`).
    """
    from app.services.place_service import PlaceFieldError, update_place

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"address_text": None}, settings=settings)

    assert excinfo.value.code == "address_text_required"


async def test_update_null_category_is_domain_error() -> None:
    """CR 3.2, патч 2 (AC-07): `null` в категории — `category_invalid` + `allowed`.

    §18.1: краснеет от отката патча 2 — `PlaceCategory(None)` давал `ValueError`
    из хелпера, не доменный код с перечнем.
    """
    from app.services.place_service import PlaceFieldError, update_place

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"category": None}, settings=settings)

    assert excinfo.value.code == "category_invalid"
    assert excinfo.value.details["allowed"], "перечень категорий обязателен (AC-02)"


async def test_update_null_is_active_is_nothing_to_update() -> None:
    """CR 3.2, патч 2 (RISK-2): `{"is_active": null}` — тот же пустой PATCH.

    §18.1: краснеет от отката патча 2 — раньше такой запрос давал `200`
    «сохранено» без единой правки и обходил гард `place_nothing_to_update`.
    """
    from app.services.place_service import (
        PLACE_NOTHING_TO_UPDATE_CODE,
        PlaceFieldError,
        update_place,
    )

    session, settings = _session_with_settings()
    place = _place()

    with pytest.raises(PlaceFieldError) as excinfo:
        await update_place(session, place=place, changes={"is_active": None}, settings=settings)

    assert excinfo.value.code == PLACE_NOTHING_TO_UPDATE_CODE


async def test_update_assigns_fields_inside_savepoint() -> None:
    """CR 3.2, патч 3 (AC-03): присваивание полей — ВНУТРИ `begin_nested()`.

    §18.1: краснеет от отката патча 3. До него `place.name = ...` выполнялось
    до входа в SAVEPOINT, первый `flush`/autoflush отправлял `UPDATE` вне него,
    и `IntegrityError` в гонке откатывал всю транзакцию: вместо `409` сессия
    отдавала `PendingRollbackError` → `500` (доказано на живом PostgreSQL 18).

    Проверяется порядок: к моменту выхода из SAVEPOINT имя уже новое, а
    `entered == 1` подтверждает, что запись шла под защитой.
    """
    from app.services.place_service import update_place

    session, settings = _session_with_settings()
    place = _place()

    updated = await update_place(
        session, place=place, changes={"name": "Новое имя"}, settings=settings
    )

    assert session.nested.entered == 1, "запись обязана идти внутри SAVEPOINT"
    assert updated.name == "Новое имя"


async def test_update_duplicate_race_reads_city_before_savepoint() -> None:
    """CR 3.2, патч 6 (AC-03): лог гонки не читает объект после отката SAVEPOINT.

    §18.1: краснеет от отката патча 6. После отката SAVEPOINT объект
    экспирится, и `place.city_slug` в `except` давал `MissingGreenlet`
    (ленивая подгрузка в async вне greenlet) — `409 PLACE_DUPLICATE`
    превращался в `500` (проверено исполнением на живом PostgreSQL).
    """
    from app.services.place_service import PlaceDuplicateError, update_place

    session, settings = _session_with_settings()
    place = _place()
    # Сессия-заглушка: `flush` внутри SAVEPOINT падает нарушением уникальности.
    session.flush_error = _asyncpg_integrity_error("uq_places_city_name")

    with pytest.raises(PlaceDuplicateError):
        await update_place(session, place=place, changes={"name": "Занятое имя"}, settings=settings)


# --- Task 7.3: выдача списка (AC-01…AC-04) ---------------------------------------


def _row(*, minutes: int, place_uuid: Any | None = None) -> Any:
    """Строка места с заданным `created_at`: порядок и курсор проверяются по нему."""
    from datetime import UTC, datetime, timedelta

    return SimpleNamespace(
        uuid=place_uuid or uuid_lib.uuid4(),
        name=f"Место {minutes}",
        category=str(PlaceCategory.CAFE),
        address_text="пр. Ленина, 1",
        is_active=True,
        created_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes),
    )


async def test_list_uses_default_city_slug_from_settings() -> None:
    """AC-01: слаг берётся из конфига, а не из запроса.

    §18.1: краснеет, если заменить `settings.default_city_slug` на
    литерал или на аргумент функции — клиент смог бы запросить чужое
    сообщество, а в пилоте их несколько (`tomsk`, `kemerovo`).
    """
    from app.services.place_service import list_places

    session, settings = _session_with_settings(default_city_slug="kemerovo")
    _FakeRepository.page_items = [_row(minutes=0)]
    _FakeRepository.page_total = 1

    await list_places(session, settings=settings)

    assert _FakeRepository.page_calls[0]["city_slug"] == "kemerovo"


async def test_list_rejects_slug_outside_config() -> None:
    """AC-01: слаг из конфига обязан входить в `city_slugs`.

    §18.1: краснеет, если убрать сверку. Опечатка в `DEFAULT_CITY_SLUG`
    дала бы молча пустую ленту вместо отказа, и «в сообществе нет мест»
    стало бы неотличимо от ошибки конфигурации.
    """
    from app.services.place_service import PlaceUnknownCommunityError, list_places

    session, settings = _session_with_settings(
        city_slugs=("tomsk",), default_city_slug="kemerovo"
    )

    with pytest.raises(PlaceUnknownCommunityError):
        await list_places(session, settings=settings)

    assert _FakeRepository.page_calls == [], "до БД дело доходить не должно"


async def test_list_unknown_category_raises_field_error_with_allowed() -> None:
    """AC-02: категория вне белого списка — `category_invalid` + перечень.

    §18.1: краснеет, если пропустить значение в репозиторий. Тогда выдача
    была бы пустой, и клиент не узнал бы, что категория неверна.
    Перечень в `details.allowed` — требование AC-02.
    """
    from app.services.place_service import (
        PLACE_CATEGORY_INVALID_CODE,
        PlaceFieldError,
        list_places,
    )

    session, settings = _session_with_settings()

    with pytest.raises(PlaceFieldError) as exc_info:
        await list_places(session, category="zoo", settings=settings)

    assert exc_info.value.code == PLACE_CATEGORY_INVALID_CODE
    assert exc_info.value.details == {"allowed": [str(item) for item in PlaceCategory]}
    assert _FakeRepository.page_calls == []


def _cursor_raw(minutes: int = 0) -> tuple[str, uuid_lib.UUID]:
    """Готовый курсор и `uuid` строки, к которой он указывает."""
    from datetime import UTC, datetime, timedelta

    from app.services.place_cursor import encode_place_cursor

    place_uuid = uuid_lib.uuid4()
    moment = datetime(2026, 10, 23, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return encode_place_cursor(moment, place_uuid), place_uuid


async def test_list_decodes_cursor_before_query() -> None:
    """AC-04: валидный курсор доходит до репозитория парой `(created_at, uuid)`.

    §18.1: краснеет, если передавать сырую строку в `after` — сравнение
    `tuple_ > tuple_` с `str` дало бы `DataError` → `500` на легальном курсоре.
    """
    from app.services.place_service import list_places

    session, settings = _session_with_settings()
    raw, place_uuid = _cursor_raw()

    await list_places(session, cursor=raw, settings=settings)

    after = _FakeRepository.page_calls[0]["after"]
    assert after is not None
    assert after[1] == place_uuid


async def test_list_broken_cursor_is_domain_error() -> None:
    """AC-04, AC-07: битый курсор — `invalid_cursor`, а не `500`.

    §18.1: краснеет, если пробросить `InvalidCursorError` наверх. HTTP-слой
    отображает ТОЛЬКО доменные ошибки (`_domain_error`), и ошибка кодека
    дала бы `500` на пользовательском вводе.
    """
    from app.services.place_service import PlaceCursorError, list_places

    session, settings = _session_with_settings()

    with pytest.raises(PlaceCursorError) as exc_info:
        await list_places(session, cursor="zzz", settings=settings)

    assert exc_info.value.code == "invalid_cursor"
    assert _FakeRepository.page_calls == []


async def test_list_no_extra_row_means_no_cursor() -> None:
    """AC-04: страница кончилась — `next_cursor is None`.

    §18.1: краснеет, если собирать курсор всегда. Клиент делал бы лишний
    запрос и получал пустой `items` при непустом `total`.
    """
    from app.services.place_service import list_places

    session, settings = _session_with_settings()
    _FakeRepository.page_items = [_row(minutes=0), _row(minutes=1)]
    _FakeRepository.page_total = 2

    page = await list_places(session, limit=2, settings=settings)

    assert page.next_cursor is None
    assert len(page.items) == 2


async def test_list_extra_row_produces_cursor_from_last_kept_item() -> None:
    """AC-04: курсор — от ПОСЛЕДНЕГО элемента усечённой страницы.

    §18.1: краснеет, если собрать курсор от лишней строки: следующая страница
    началась бы ПОСЛЕ строки, которую клиент не видел, то есть строка
    потерялась бы навсегда.
    """
    from app.services.place_cursor import decode_place_cursor
    from app.services.place_service import list_places

    session, settings = _session_with_settings()
    first, second, extra = _row(minutes=0), _row(minutes=1), _row(minutes=2)
    _FakeRepository.page_items = [first, second, extra]
    _FakeRepository.page_total = 10

    page = await list_places(session, limit=2, settings=settings)

    assert len(page.items) == 2
    assert page.next_cursor is not None
    moment, place_uuid = decode_place_cursor(page.next_cursor)
    assert place_uuid == second.uuid
    assert moment == second.created_at


async def test_list_total_does_not_depend_on_cursor() -> None:
    """AC-04, D-4: `total` — «сколько всего под фильтром», а не «сколько осталось».

    §18.1: краснеет, если считать `total` после наложения курсора: на второй
    странице счётчик уменьшался бы, и пагинация показывала бы разное число
    страниц на каждом шаге.
    """
    from app.services.place_service import list_places

    session, settings = _session_with_settings()
    _FakeRepository.page_items = [_row(minutes=0)]
    _FakeRepository.page_total = 7
    raw, _ = _cursor_raw(minutes=5)

    page = await list_places(session, cursor=raw, settings=settings)

    assert page.total == 7


async def test_list_does_not_commit() -> None:
    """D-4 стори 2.2: чтение не фиксирует транзакцию.

    §18.1: краснеет, если добавить `session.commit()`. Выдача ничего не пишет,
    а коммит закрыл бы чужую незавершённую работу в той же сессии.
    """
    from app.services.place_service import list_places

    session, settings = _session_with_settings()

    await list_places(session, settings=settings)

    assert session.commits == 0


async def test_list_string_city_slugs_rejected() -> None:
    """CR 3.1, патч 5b: строка вместо последовательности слагов — `TypeError`.

    §18.1: краснеет, если убрать рантайм-гард. `in` по строке — поиск
    ПОДСТРОКИ, и слаг `toms` прошёл бы проверку `city_slugs = "tomsk"`.
    """
    from app.services.place_service import list_places

    session, settings = _session_with_settings(city_slugs="tomsk")

    with pytest.raises(TypeError):
        await list_places(session, settings=settings)


# --- CR 3.3, патч 4: эффективный предел страницы -----------------------------


async def test_list_limit_above_max_uses_effective_limit_for_cursor() -> None:
    """AC-04 (CR 3.3, патч 4): курсор считается по ЭФФЕКТИВНОМУ пределу.

    §18.1: краснеет, если сравнивать `len(items)` с исходным `limit`.
    Репозиторий урезает предел до `MAX_PLACES_PAGE_SIZE` и берёт
    `LIMIT min(limit, MAX) + 1`, поэтому при `limit=150` он вернёт 101 строку.
    Сравнение `101 > 150` ложно → лишняя строка НЕ отбрасывается,
    `next_cursor` остаётся `None`, и клиент молча теряет остаток выдачи
    при `total > 101` (доказано исполнением до патча).

    Через HTTP не воспроизводится (там фиксированные `PLACES_PAGE_SIZE`),
    но именно ради внутренних вызовов воркера/импорта заведена константа.
    """
    from app.schemas.place import MAX_PLACES_PAGE_SIZE
    from app.services.place_cursor import decode_place_cursor
    from app.services.place_service import list_places

    session, settings = _session_with_settings()
    # Репозиторий вернул `min(limit, MAX) + 1` = 101 строку.
    _FakeRepository.page_items = [_row(minutes=index) for index in range(MAX_PLACES_PAGE_SIZE + 1)]
    _FakeRepository.page_total = 500

    page = await list_places(session, limit=150, settings=settings)

    assert len(page.items) == MAX_PLACES_PAGE_SIZE
    assert page.next_cursor is not None
    moment, place_uuid = decode_place_cursor(page.next_cursor)
    assert place_uuid == page.items[-1].uuid
    assert moment == page.items[-1].created_at
