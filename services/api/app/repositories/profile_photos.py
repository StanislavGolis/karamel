"""Репозиторий фото анкеты (Task 5.1–5.6).

D-4 стори 1.2: сессия передаётся аргументом, `commit()` здесь не вызывается —
фиксацию делает роутер (единица работы). `flush` внутри `create` нужен, чтобы
`id`, `uuid` и server-дефолты были заполнены до возврата.

D-7, Task 5.1: блокировка анкеты НЕ реализуется здесь повторно. Второй
`SELECT ... FOR UPDATE` по `profiles` в этом модуле означал бы два разных
места, блокирующих одну строку по-разному, и взаимные блокировки между
загрузкой фото и правкой анкеты. Используется существующий
`ProfileRepository.get_by_user_id_for_update` (`repositories/profiles.py:91`).
Всё, что ниже, вызывается уже под этой блокировкой.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PhotoStatus
from app.models.profile import Profile
from app.models.profile_photo import ProfilePhoto

# Task 4.3 (AC-08): границы `limit` очереди модерации. `LIMIT 0` и отрицательный
# `limit` PostgreSQL молча превращает в пустой список, поэтому значения
# проверяются до запроса. 100 — верхняя граница страницы кабинета (3.6).
PENDING_LIMIT_MIN = 1
PENDING_LIMIT_MAX = 100

# AC-03, D-12: лимит фото на анкету и допустимый диапазон позиций.
# Позиция — порядок показа, а не плотный индекс: дырки после удаления
# допустимы, но значения вне 1..3 запрещены CHECK-ом `ck_profile_photos_position`.
MAX_PHOTOS_PER_PROFILE = 3
POSITIONS: tuple[int, ...] = tuple(range(1, MAX_PHOTOS_PER_PROFILE + 1))


class ProfilePhotoRepository:
    """Запросы к `profile_photos` в терминах домена."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_by_profile(self, profile_id: int) -> int:
        """AC-03, D-7: число фото анкеты.

        `count(*)` вместо загрузки строк: лимит проверяется на каждой загрузке,
        и тянуть ради него три объекта с текстовыми полями незачем.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(ProfilePhoto)
            .where(ProfilePhoto.profile_id == profile_id)
        )
        return int(result.scalar_one())

    async def list_positions(self, profile_id: int) -> set[int]:
        """D-12: занятые позиции анкеты — вход для `next_position`.

        Отдельный метод, потому что выбор свободной позиции — правило домена,
        а не запрос: тест проверяет его без БД.
        """
        result = await self._session.execute(
            select(ProfilePhoto.position).where(ProfilePhoto.profile_id == profile_id)
        )
        return {int(row) for row in result.scalars().all()}

    @staticmethod
    def pick_next_position(occupied: set[int]) -> int | None:
        """Task 5.2, D-12: ПЕРВАЯ свободная позиция из {1, 2, 3}, а не `max+1`.

        После удаления среднего фото `max(position)=3`, и `max+1=4` нарушил бы
        `ck_profile_photos_position` (`1..3`). Вставка падала бы с `IntegrityError`,
        который Task 6.3 трактует как гонку и повторяет — то есть бесконечный
        отказ на легальной операции.

        `None` — свободных мест нет; сервис превращает это в `409 photo_limit_reached`.
        """
        for position in POSITIONS:
            if position not in occupied:
                return position
        return None

    async def create(
        self,
        session: AsyncSession,
        profile: Profile,
        *,
        photo_uuid: uuid_lib.UUID,
        storage_key: str,
        position: int,
        content_type: str,
        size_bytes: int,
        consent_given_at: datetime,
    ) -> ProfilePhoto:
        """Task 5.3, D-4: создаёт строку фото, не фиксируя транзакцию.

        `flush` без `commit`: фиксацию делает роутер. `flush` нужен, чтобы
        server-дефолты (`status`, `created_at`) были заполнены до возврата
        и объект можно было сериализовать без `MissingGreenlet`.

        `photo_uuid` — обязательный аргумент: `storage_key` строится из него
        (`photos/{profile_uuid}/{photo_uuid}.{ext}`, D-3), и генерировать uuid
        внутри репозитория значило бы возвращать ключ, не связанный со строкой.

        `consent_given_at` — обязательный аргумент, а не дефолт: AC-07 требует
        сохранить время согласия вместе с фото, и «забыть» его нельзя.
        """
        photo = ProfilePhoto(
            uuid=photo_uuid,
            profile_id=profile.id,
            storage_key=storage_key,
            position=position,
            content_type=content_type,
            size_bytes=size_bytes,
            consent_given_at=consent_given_at,
        )
        session.add(photo)
        await session.flush()
        return photo

    async def list_by_profile(self, profile_id: int) -> list[ProfilePhoto]:
        """Task 5.4: все фото анкеты, упорядоченные по позиции показа.

        Порядок задаётся в SQL, а не в Python: клиент показывает фото
        в порядке `position`, и сортировка на стороне сервера — единственная.
        """
        result = await self._session.execute(
            select(ProfilePhoto)
            .where(ProfilePhoto.profile_id == profile_id)
            .order_by(ProfilePhoto.position)
        )
        return list(result.scalars().all())

    async def get_owned(self, profile_id: int, photo_uuid: uuid_lib.UUID) -> ProfilePhoto | None:
        """Task 5.5 (AC-09): фото анкеты по публичному uuid или `None`.

        Фильтр по `profile_id` в SQL, а не в Python: иначе чужое фото
        сначала загружалось бы в память, и любой забытый `if` ниже отдавал бы
        его владельцу. Ответ на чужое и на несуществующее одинаков — `404`
        (AC-09): существование чужого фото не подтверждается.
        """
        result = await self._session.execute(
            select(ProfilePhoto).where(
                ProfilePhoto.profile_id == profile_id,
                ProfilePhoto.uuid == photo_uuid,
            )
        )
        return result.scalar_one_or_none()

    async def delete(self, photo: ProfilePhoto) -> None:
        """Task 5.6, D-10: удаляет строку фото (без `commit`).

        Объект удаляется через сессию, а не `DELETE ... WHERE`: строка уже
        получена `get_owned` под блокировкой, и повторный поиск по условию
        мог бы удалить не то, что проверено.
        """
        await self._session.delete(photo)
        await self._session.flush()

    async def list_approved_by_profile(self, profile_id: int) -> list[ProfilePhoto]:
        """Task 4.1 (AC-04, D-7): только одобренные фото анкеты, по позиции.

        Единственный вход для ЧУЖИХ выборок (эпик 4): `pending` и `rejected`
        не покидают сервер физически, а не фильтруются на клиенте
        (project-context §5: фильтрация в репозитории). Поэтому причину
        отказа (`rejection_reason`) чужому увидеть невозможно — её просто
        нет в результате.
        """
        result = await self._session.execute(
            select(ProfilePhoto)
            .where(
                ProfilePhoto.profile_id == profile_id,
                ProfilePhoto.status == str(PhotoStatus.APPROVED),
            )
            .order_by(ProfilePhoto.position)
        )
        return list(result.scalars().all())

    async def list_primary_approved_by_user_ids(self, user_ids: list[int]) -> dict[int, str]:
        """Стори 5а.3, Task 6.2 (AC-07, NFR34): главное фото каждого автора — ПАКЕТОМ.

        Возвращает `user_id -> storage_key` для авторов из `user_ids`. Один
        запрос на страницу, а не `N` запросов: страница «Моих заявок» до 50
        элементов, и построчная подпись фото дала бы 50 запросов в БД на
        один экран (N+1 — класс дефекта, который не виден на тесте с одной
        заявкой).

        «Главное» — фотография с МИНИМАЛЬНОЙ `position`: `1` показывается
        первой (D-12), и именно её клиент рисует в `PersonMini`. Брать
        любую одобренную нельзя — порядок кадра на экране стал бы
        недетерминированным при трёх фото.

        Только `approved`: `pending`/`rejected` не покидают сервер физически
        (та же причина, что в `list_approved_by_profile`).

        Пустой `user_ids` → пустой словарь БЕЗ запроса: `IN ()` — синтаксическая
        ошибка, а вызывать БД ради заведомо пустого результата незачем.
        """
        if not user_ids:
            return {}
        result = await self._session.execute(
            select(Profile.user_id, ProfilePhoto.storage_key, ProfilePhoto.position)
            .join(Profile, Profile.id == ProfilePhoto.profile_id)
            .where(
                Profile.user_id.in_(user_ids),
                ProfilePhoto.status == str(PhotoStatus.APPROVED),
            )
            .order_by(Profile.user_id, ProfilePhoto.position)
        )
        # Первая строка на `user_id` — фото с минимальной `position`: строки
        # уже отсортированы, поэтому `setdefault` оставляет именно её. Сортировка
        # в SQL, а не в питоне: порядок нужен до схлопывания, иначе выбиралось
        # бы произвольное фото.
        primary: dict[int, str] = {}
        for user_id, storage_key, _position in result.all():
            primary.setdefault(int(user_id), str(storage_key))
        return primary

    async def list_pending(self, limit: int) -> list[ProfilePhoto]:
        """Task 4.2, 4.3 (AC-08): очередь модерации, FIFO по `created_at, id`.

        Порядок `created_at, id` — не косметика: `created_at` может совпасть
        (две загрузки в одну микросекунду), и без второго ключа порядок
        между ними был бы неопределён, а пагинация очереди — невоспроизводимой.

        AC-08, D-8: параметра `gender` НЕТ и join на `profiles` НЕТ.
        Приоритета по полу не существует: очередь общая, и тест проверяет
        фактический порядок двух разнополых анкет плюс текст SQL (Task 7.11,
        7.12). Добавление `gender` в этот метод сломает оба теста.

        Запрос опирается на частичный индекс `idx_profile_photos_status_pending`
        (project-context §7): `WHERE status = 'pending'` — его точное условие.

        `limit` валидируется здесь, а не у вызывающего: `LIMIT 0` и
        отрицательный `limit` PostgreSQL молча превращает в пустой список,
        и «очередь пуста» стало бы неотличимо от «передали ноль». Границы
        `1..100` — Task 4.3.
        """
        # CR 2.4 патч 2: тип проверяется до сравнения. `None`/`"50"` давали
        # `TypeError` вместо обещанного `ValueError`, а `True`/`50.5` проходили
        # проверку насквозь (`True == 1`, `50.5 > 1`). `bool` — наследник `int`,
        # поэтому исключается отдельно: кабинет 3.6 получит `limit` из query.
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"limit must be int, got {type(limit).__name__}")
        if not PENDING_LIMIT_MIN <= limit <= PENDING_LIMIT_MAX:
            raise ValueError(
                f"limit must be in {PENDING_LIMIT_MIN}..{PENDING_LIMIT_MAX}, got {limit}"
            )
        result = await self._session.execute(
            select(ProfilePhoto)
            .where(ProfilePhoto.status == str(PhotoStatus.PENDING))
            .order_by(ProfilePhoto.created_at, ProfilePhoto.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_uuid_for_update(self, photo_uuid: uuid_lib.UUID) -> ProfilePhoto | None:
        """Task 4.4, 4.6 (AC-06): фото по uuid под `FOR UPDATE` или `None`.

        Порядок блокировок в проекте: `profiles → profile_photos`
        (как в `add_photo`: сначала строка анкеты, затем фото). Этот метод
        блокирует ТОЛЬКО фото и не берёт анкету — цикла блокировок не
        возникает: никто не держит фото и не ждёт анкету.

        Фильтра по анкете здесь нет намеренно: вызывающий — модерация
        (3.7), она работает с фото по uuid, а не от лица владельца.
        """
        result = await self._session.execute(
            select(ProfilePhoto).where(ProfilePhoto.uuid == photo_uuid).with_for_update()
        )
        return result.scalar_one_or_none()

    async def set_moderation_result(
        self,
        photo: ProfilePhoto,
        *,
        status: PhotoStatus,
        rejection_reason: str | None,
        moderated_at: datetime,
    ) -> None:
        """Task 4.5, D-6 (AC-06, AC-10): записывает решение модератора.

        Мутация ORM-объекта под `FOR UPDATE`, а НЕ `session.execute(update(...))`.
        Причина: `update()` обходит ORM-события и оставляет объект в сессии
        со старыми значениями — последующее чтение `photo.status` вернуло бы
        `pending`, и AC-10 («значения читаются без `refresh`») нарушился бы.
        Тест 6.4 падает при переходе на `update()`.

        `flush` без `commit`: фиксацию делает вызывающий (D-4 стори 1.2).
        """
        photo.status = str(status)
        photo.rejection_reason = rejection_reason
        photo.moderated_at = moderated_at
        # AC-10, Task 6.3: `updated_at` ставится здесь явно, а не через
        # `onupdate=func.now()` из `TimestampMixin`. Причина техническая:
        # server-side `onupdate` вычисляется БД, SQLAlchemy для UPDATE не
        # запрашивает RETURNING, и атрибут помечается expired. Чтение
        # `photo.updated_at` в async тогда требует IO в неположенном месте —
        # `MissingGreenlet` (проверено тестом 6.3 до этой правки). Модель
        # не меняется (запрет стори); значение остаётся фактическим временем
        # мутации, тем же, что записала бы БД.
        photo.updated_at = datetime.now(UTC)
        await self._session.flush()
