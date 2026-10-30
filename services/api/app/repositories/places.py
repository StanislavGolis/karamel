"""Репозиторий справочника мест (стори 3.1, Task 6.1–6.5; AC-04).

project-context §6: цепочка «роутер → сервис → репозиторий». Здесь только
запросы — ни правил домена, ни HTTP-кодов. Правило «дубль запрещён» живёт
в сервисе (`PlaceService`) и в БД (`uq_places_city_name`), а не тут.

D-4: сессия передаётся аргументом — репозиторий не создаёт её сам, иначе
составная операция перестаёт быть одной транзакцией.

`commit()` здесь не вызывается: единица работы у вызывающего слоя (как
в `profile_photos.py` и `profiles.py`).

Строители запросов (`build_*`) — статические: текст условия проверяется
unit-тестом без БД (§18.2). Гонку двух вставок в одной сессии доказать
нельзя (§18.3), поэтому корректность запрета дубля держит уникальное
ограничение, а не `exists_in_city`.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.place import Place
from app.repositories.errors import constraint_name_of
from app.schemas.place import MAX_PLACES_PAGE_SIZE


class PlaceRepository:
    """Запросы к `places` в терминах домена."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def build_exists_statement(city_slug: str, name: str) -> Select[Place]:
        """AC-04: проверка «место с таким названием есть в ЭТОМ сообществе».

        Оба условия обязательны: AC-04 явно разрешает места с одинаковым
        названием в разных сообществах, и проверка только по имени
        отказывала бы на легальном вводе.

        Значения передаются параметрами, а не склейкой в текст: название
        с кавычкой (`O'Brien's`) сломало бы SQL или дало ложное совпадение.

        Вынесено отдельным статическим методом, чтобы текст условия
        проверялся без БД: гонку двух вставок в одной сессии доказать
        нельзя (§18.3), а «убрать `city_slug` из `WHERE`» — реальный
        дефект, который тест обязан ловить.
        """
        return select(Place).where(
            Place.city_slug == city_slug,
            Place.name == name,
        )

    @staticmethod
    def build_exists_excluding_statement(
        city_slug: str, name: str, exclude_uuid: uuid_lib.UUID
    ) -> Select[Place]:
        """Task 3.3 (AC-05): «такое имя занято, но не мной».

        `Place.uuid != exclude_uuid` — не оптимизация, а условие корректности:
        `PATCH` ищет дубль по имени, которое у самого места уже есть, находит
        СЕБЯ и отказывал бы `409 PLACE_DUPLICATE` на любой правке категории
        или адреса. Проверено откатом: без исключения себя краснеет
        `test_update_own_name_does_not_conflict`.

        Сравнение по `uuid`, а не по `id`: `uuid` уже есть на руках у роутера
        (он пришёл в пути), а внутренний bigint наружу не выдаётся (D-2).
        """
        return select(Place).where(
            Place.city_slug == city_slug,
            Place.name == name,
            Place.uuid != exclude_uuid,
        )

    @staticmethod
    def build_get_by_uuid_statement(place_uuid: uuid_lib.UUID) -> Select[Place]:
        """AC-01, D-2: адресация снаружи — только по `uuid`.

        `id` в условие не попадает никогда: внутренний bigint не должен
        становиться публичным адресом (перебор `/1..N` обошёл бы лимиты).
        """
        return select(Place).where(Place.uuid == place_uuid)

    # --- Выдача списка (стори 3.3, Task 3.1–3.5; AC-01, AC-02, AC-04) ---
    #
    # Предикат выдачи живёт в ОДНОЙ приватной функции: страница и `total`
    # обязаны считаться по одному набору условий. Две копии разошлись бы
    # при первом же добавлении фильтра, и `total` перестал бы совпадать
    # с суммой страниц (мутация 10.3 «total без фильтра категории»).

    @staticmethod
    def _list_conditions(
        *, city_slug: str, category: str | None, include_inactive: bool
    ) -> list[Any]:
        """AC-01, AC-02, AC-03: условия выдачи БЕЗ курсора — общий предикат.

        Срез сообщества обязателен всегда: слаг приходит из конфига (D-3),
        параметра в запросе нет, и место чужого сообщества не должно попадать
        в ленту ни при каком наборе query-параметров.

        `is_active` добавляется только когда скрытые НЕ запрошены: инверсия
        (`include_inactive is not True`) — то есть фильтр снимается ТОЛЬКО
        явным `True`. Прежняя форма (`not include_inactive`) была fail-open:
        небулево значение из нетипизированного вызова (`"false"` из очереди,
        `.env`-парсинга или JSON-поля воркера — истинная строка) молча
        ПОКАЗЫВАЛО скрытые места в публичной ленте, хотя докстринг обещал
        обратное (CR 3.3, патч 5; доказано исполнением: `'false'` → фильтр
        отсутствует). Отказ обязан быть закрытым: сомнительное значение =
        публичная выдача, а не админская.

        Категория накладывается поверх: фильтр сужает выдачу, но не отменяет
        ни срез сообщества, ни `is_active`.
        """
        conditions: list[Any] = [Place.city_slug == city_slug]
        # CR 3.3, патч 5: строгое `is not True` вместо инверсии. Скрытые
        # места видны только при ЯВНОМ `True`, который уже прошёл
        # `require_moderator` в зависимости.
        if include_inactive is not True:
            conditions.append(Place.is_active.is_(True))
        if category is not None:
            conditions.append(Place.category == category)
        return conditions

    @staticmethod
    def build_list_statement(
        *,
        city_slug: str,
        category: str | None,
        include_inactive: bool,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> Select[Place]:
        """AC-01, AC-02, AC-04: страница выдачи — предикат + порядок + `LIMIT n+1`.

        Порядок `created_at, uuid` совпадает с ключом курсора: ключ сортировки
        и ключ перехода обязаны быть одним и тем же, иначе `(created_at, uuid) >`
        пропускал бы строки, упорядоченные иначе.

        `uuid` в `ORDER BY` — как тип `uuid`, не `text`: приведение изменило бы
        порядок и убило индекс.

        Курсор — row-value сравнение `(created_at, uuid) > (:ts, :uuid)`, а не
        `created_at >= :ts`: `>=` возвращает последнюю строку предыдущей
        страницы на каждой границе, то есть дубли. Разложенное на `OR`
        сравнение дало бы тот же результат, но потеряло бы row-value-план;
        текст запроса проверяется unit-тестом (§18.2).

        `LIMIT limit + 1`: лишняя строка — единственный признак «есть следующая
        страница». Без неё клиент делает лишний запрос и получает пустой
        `items` при непустом `total`.
        """
        statement = select(Place).where(
            *PlaceRepository._list_conditions(
                city_slug=city_slug,
                category=category,
                include_inactive=include_inactive,
            )
        )
        if after is not None:
            after_created_at, after_uuid = after
            statement = statement.where(
                tuple_(Place.created_at, Place.uuid) > tuple_(after_created_at, after_uuid)
            )
        return statement.order_by(Place.created_at.asc(), Place.uuid.asc()).limit(limit + 1)

    @staticmethod
    def build_count_statement(
        *, city_slug: str, category: str | None, include_inactive: bool
    ) -> Select[int]:
        """AC-02, AC-04, D-4: `total` — «сколько всего под фильтром».

        Считается по тому же предикату БЕЗ курсора и БЕЗ `LIMIT`: `total`
        не зависит от того, на какой странице клиент находится. Иначе
        `COUNT(*) OVER()` заставил бы СУБД материализовать весь отфильтрованный
        набор на каждой странице — ровно то, что курсор убирает.

        `ORDER BY` в `COUNT` нет: порядок не влияет на число, а лишняя
        сортировка — работа без результата.
        """
        return (
            select(func.count())
            .select_from(Place)
            .where(
                *PlaceRepository._list_conditions(
                    city_slug=city_slug,
                    category=category,
                    include_inactive=include_inactive,
                )
            )
        )

    async def list_page(
        self,
        *,
        city_slug: str,
        category: str | None,
        include_inactive: bool,
        after: tuple[datetime, uuid_lib.UUID] | None,
        limit: int,
    ) -> tuple[list[Place], int]:
        """AC-01…AC-04: страница и `total` одним вызовом.

        `limit < 1` — `ValueError`, как в `ProfileRepository.list_visible_in_city`:
        `LIMIT 0` даёт молча пустую выдачу, `LIMIT -1` — ошибку драйвера (`500`
        на внутренней ошибке вызывающего). Верхняя граница `MAX_PLACES_PAGE_SIZE`
        защищает от вызова с `limit` больше контрактного из воркера/скрипта.

        `populate_existing` не ставится: выдача только читает и объекты
        в identity map не правит (то же решение, что в `list_visible_in_city`).

        Порядок двух запросов не важен для корректности, но стабилен: страница
        первой, `total` второй — при отладке в логе видно, какой запрос упал.
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        bounded = min(limit, MAX_PLACES_PAGE_SIZE)
        page_statement = self.build_list_statement(
            city_slug=city_slug,
            category=category,
            include_inactive=include_inactive,
            after=after,
            limit=bounded,
        )
        page_result = await self._session.execute(page_statement)
        items = list(page_result.scalars().all())

        count_statement = self.build_count_statement(
            city_slug=city_slug,
            category=category,
            include_inactive=include_inactive,
        )
        count_result = await self._session.execute(count_statement)
        # `func.count()` типизирован как `int`; курсор-результат `scalar_one`
        # отдаёт `Any`, поэтому приведение к `int` только для mypy-strict.
        total: int = count_result.scalar_one()
        return items, total

    def add(self, place: Place) -> None:
        """Ставит объект в сессию. `flush` делает сервис — он же ловит `IntegrityError`."""
        self._session.add(place)

    async def get_by_uuid(self, place_uuid: uuid_lib.UUID) -> Place | None:
        """Task 6.1: место по публичному идентификатору.

        `populate_existing=True` обязателен (CR 2.1, патч 11): без него объект,
        уже лежащий в identity map, вернулся бы снимком, и после правки
        в этой же сессии чтение показало бы старые поля.
        """
        result = await self._session.execute(
            self.build_get_by_uuid_statement(place_uuid).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def find_by_city_and_name(self, city_slug: str, name: str) -> Place | None:
        """Task 6.1: место по паре `(city_slug, name)`.

        Адресация парой, а не только именем: одинаковые названия в разных
        сообществах — легальное состояние (AC-04).

        CR 3.1, патч 16: `limit(1)`, а не полный набор строк. Уникальность
        пары держит `uq_places_city_name`, но строки с краевыми
        пробельными раньше проходили CHECK (см. патч 8), поэтому в старых
        данных дубль возможен — и `scalar_one_or_none()` на двух строках
        даёт `MultipleResultsFound`, то есть падение вместо `None`. `limit(1)`
        сохраняет контракт «первое совпадение или ничего» на любых данных.
        """
        # QD B1 (CR 3.2): `ORDER BY` делает выбор детерминированным. Без него
        # `limit(1)` на двух совпадениях возвращал произвольную строку, и
        # `details.existing_uuid` в `PLACE_ARCHIVED` указывал на случайное
        # место — админ «восстанавливал» не то. Порядок «сначала самое старое»:
        # первое созданное место и есть то, которое держит имя исторически.
        # `id` добавлен как тай-брейкер: `created_at` у двух строк в одной
        # транзакции совпадает до микросекунды (`now()` = время начала транзакции).
        statement = (
            self.build_exists_statement(city_slug, name)
            .order_by(Place.created_at, Place.id)
            .limit(1)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def exists_in_city(self, city_slug: str, name: str) -> bool:
        """AC-04, D-4: дешёвый путь для понятной ошибки, НЕ гарантия уникальности.

        Проверка перед вставкой пропускает две параллельные вставки одного
        имени (§18.3): между `SELECT` и `INSERT` второй запрос успевает
        вставить свою строку. Корректность обеспечивает
        `uq_places_city_name`; этот метод существует, чтобы в обычном
        (неконкурентном) случае пользователь получил `PLACE_DUPLICATE`
        без исключения из драйвера.
        """
        statement = self.build_exists_statement(city_slug, name)
        result = await self._session.execute(statement.limit(1))
        return result.first() is not None

    async def exists_in_city_excluding(
        self, city_slug: str, name: str, exclude_uuid: uuid_lib.UUID
    ) -> Place | None:
        """Task 3.3–3.4 (AC-05, AC-03): занято ли имя ДРУГИМ местом.

        Возвращается сама строка, а не `bool`: сервису нужен `is_active` и
        `uuid` найденного места, чтобы отличить активный дубль (`409
        PLACE_DUPLICATE`) от скрытого (`409 PLACE_ARCHIVED` +
        `details.existing_uuid`, D-6). Второй запрос за той же строкой дал бы
        окно гонки между двумя чтениями.

        `limit(1)` по той же причине, что в `find_by_city_and_name`:
        `scalar_one_or_none()` на двух строках даёт `MultipleResultsFound`,
        то есть падение вместо ответа.
        """
        # QD B1 (CR 3.2): тот же детерминизм, что в `find_by_city_and_name`.
        # Порядок обязан совпадать: `PLACE_ARCHIVED.existing_uuid` на `PATCH`
        # и на `POST` должен указывать на ОДНО и то же место, иначе админ
        # видит два разных адреса для одной и той же ситуации.
        statement = (
            self.build_exists_excluding_statement(city_slug, name, exclude_uuid)
            .order_by(Place.created_at, Place.id)
            .limit(1)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    # --- Доступность места для карточки (стори 4.1, Task 6.1–6.2; AC-04) ---

    @staticmethod
    def build_availability_statement(*, place_uuid: uuid_lib.UUID, city_slug: str) -> Select[Place]:
        """AC-04 (FR98, ADR-12): место, на которое можно сослаться в карточке.

        Три условия обязательны вместе:

        1. `uuid = :uuid` — место вообще существует;
        2. `is_active IS TRUE` — место не скрыто (FR68: «скрыть» ≠ «удалить»);
           скрытое место остаётся в старых карточках, но новую на него
           завести нельзя;
        3. `city_slug = :slug` — место принадлежит сообществу карточки.
           Без этого карточка в Томске могла бы сослаться на кемеровское
           кафе, а лента её сообщества показала бы адрес, которого участники
           не знают.

        Отдельный билдер, а не `build_list_statement`: у выдачи другой
        порядок, `LIMIT n+1` и курсор. Переиспользование связало бы
        публичную ленту с проверкой записи — правка одной ломала бы другую.

        Никаких `ORDER BY`/`LIMIT`: это проверка существования, а не выборка;
        порядок без сортировки не нужен, а `LIMIT 1` заставил бы СУБД
        материализовать строку там, где достаточно `EXISTS`.
        """
        return select(Place).where(
            Place.uuid == place_uuid,
            Place.is_active.is_(True),
            Place.city_slug == city_slug,
        )

    async def is_available_for_card(self, *, place_uuid: uuid_lib.UUID, city_slug: str) -> bool:
        """AC-04: `EXISTS`-проверка доступности места перед созданием карточки.

        `SELECT EXISTS(...)`, а не выборка строки: сервису нужен только факт,
        а не поля места (они придут из ответа на `GET /places`). `EXISTS`
        останавливается на первом совпадении, тогда как `scalars().first()`
        материализует объект `Place` в identity map — лишняя работа
        и лишняя строка в кеше сессии.

        `uuid` приходит уже типом `UUID`: приведение из строки живёт в схеме
        `EventCardCreate` (Pydantic), а не здесь. Битый UUID туда не дойдёт,
        поэтому `DBAPIError` на этой ветке невозможен.
        """
        statement = self.build_availability_statement(place_uuid=place_uuid, city_slug=city_slug)
        result = await self._session.execute(select(statement.exists()))
        available: bool = result.scalar_one()
        return available

    async def find_available_for_card(
        self, *, place_uuid: uuid_lib.UUID, city_slug: str
    ) -> Place | None:
        """Стори 4.2, AC-02: доступное место для карточки — сама строка, не факт.

        Отличие от `is_available_for_card` (4.1): там нужен только факт («можно
        ли сослаться»), здесь — ещё и внутренний `place_id` для FK. Поэтому
        возвращается объект, а не `bool`.

        Предикат НЕ дублируется: используется тот же `build_availability_statement`,
        что и в гарде. Иначе проверка и заполнение FK разошлись бы, и место
        могло бы пройти гард и не найтись при резолве.

        `populate_existing=True` — тот же приём, что в `get_by_uuid`: если место
        уже в identity map и было изменено в этой сессии, чтение обязано вернуть
        актуальное значение.
        """
        result = await self._session.execute(
            self.build_availability_statement(
                place_uuid=place_uuid, city_slug=city_slug
            ).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def constraint_name_of(error: Any) -> str | None:
        """Имя нарушенного ограничения — общий разбор, QD A2 / CR 3.1 патч 1.

        До QD A2 тело жило здесь, а вторая КОПИЯ — в `photo_service._is_position_race`.
        Копия разошлась: она читала `error.orig.constraint_name`, которого под
        asyncpg нет, поэтому ветка «гонка за позицию» была мёртвой и вместо
        `409 photo_limit_reached` пользователь получал `500`. Единственный
        источник — `app/repositories/errors.constraint_name_of`.
        """
        return constraint_name_of(error)

    @staticmethod
    def is_duplicate_name_error(error: Any) -> bool:
        """AC-04, Task 6.4: отличает нарушение `uq_places_city_name` от прочих.

        Без этой проверки любой `IntegrityError` (нарушенный CHECK, чужой
        уникальный ключ) превращался бы в `PLACE_DUPLICATE`, и админ видел бы
        «такое место уже есть» на ошибке, к дублю не относящейся.
        """
        return PlaceRepository.constraint_name_of(error) == "uq_places_city_name"
