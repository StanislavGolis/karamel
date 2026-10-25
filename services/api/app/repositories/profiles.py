"""Репозиторий анкет (Task 3.1–3.4).

D-4: сессия передаётся аргументом — репозиторий не создаёт её сам, иначе
составная операция («создать пользователя + анкету») перестаёт быть одной
транзакцией.

Доступ снаружи — только `get_by_uuid` (AC-04): перебор `/1..N` по внутреннему
bigint невозможен, а `id` не покидает сервер.

Здесь нет `commit()`: фиксацию делает вызывающий слой (единица работы).
Создание анкеты через HTTP — стори 2.3; метод `create` существует для тестов
и для 2.3 (D-10).
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile

# CR 2.6, патч 2 (AC-05, D-4): верхняя граница выдачи. Значение — только
# защита от «выгрузить всю таблицу»; продуктовый лимит (20) задаст 4.2.
MAX_VISIBLE_LIMIT = 100


class ProfileRepository:
    """Запросы к `profiles` в терминах домена."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user_id(self, user_id: int) -> Profile | None:
        """Task 3.2 (AC-03): анкета текущего пользователя.

        `user_id` приходит из строки `users`, полученной зависимостью
        авторизации, а не из запроса — значение подделать нельзя.
        `scalar_one_or_none` (не `scalars().first()`): второй анкеты быть не
        может из-за `uq_profiles_user_id`, и если она появится — это дефект,
        который должен упасть, а не тихо вернуть первую строку.

        CR 2.1, патч 11: `populate_existing=True` обязателен. Без него объект,
        уже лежащий в identity map, возвращается снимком: обновили анкету
        в этой же сессии — чтение покажет старые поля. Тот же класс дефекта,
        что чинили в 1.6 (`FOR UPDATE` + `populate_existing`).
        """
        result = await self._session.execute(
            select(Profile)
            .where(Profile.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_by_uuid(self, profile_uuid: uuid_lib.UUID) -> Profile | None:
        """Task 3.3 (AC-04): единственный способ адресовать анкету снаружи.

        В 2.1 не используется HTTP-слоем (адресация по `uuid` появится
        в эпике 4), но нужен тестам схемы и понадобится там.

        CR 2.1, патч 11: `populate_existing` по той же причине, что
        в `get_by_user_id` — чтение должно отдавать состояние БД, а не
        снимок identity map.
        """
        result = await self._session.execute(
            select(Profile)
            .where(Profile.uuid == profile_uuid)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _build_for_update_statement(user_id: int) -> Any:
        """D-8, §18.3: `SELECT ... FOR UPDATE` по анкете пользователя.

        Вынесено отдельным статическим методом, чтобы текст запроса можно было
        проверить без БД: гонку двух `PATCH` в одной сессии доказать нельзя
        (вторая транзакция в том же соединении не начнётся), поэтому unit-тест
        фиксирует сам факт блокировки и `populate_existing`.

        `populate_existing` обязателен вместе с `FOR UPDATE`: без него объект,
        уже лежащий в identity map, вернётся снимком — сервис пересчитает
        `status` по старым значениям, и правка «потеряется», хотя блокировка
        создаст иллюзию защиты.
        """
        return (
            select(Profile)
            .where(Profile.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_by_user_id_for_update(self, user_id: int) -> Profile | None:
        """Task 5.1 (AC-03, D-8): анкета для правки, с блокировкой строки.

        Отличие от `get_by_user_id` — только `FOR UPDATE`. Блокировка нужна,
        потому что `status` вычисляется из итогового состояния: два
        параллельных `PATCH` без неё читают один снимок, каждый считает
        полноту по своему набору полей, и второй пишет `status='draft'`
        при фактически заполненной анкете.

        Блокировка снимается коммитом/откатом транзакции на уровне роутера:
        сервис `commit` не делает (D-4 стори 1.2).
        """
        result = await self._session.execute(self._build_for_update_statement(user_id))
        return result.scalar_one_or_none()

    @staticmethod
    def _build_visible_in_city_statement(city: str) -> Select[Profile]:
        """Task 2.1–2.3 (AC-01, AC-03, AC-04, AC-05): предикат видимости по городу.

        Единственное место в приложении, где описано правило «какие анкеты
        показывает выборка». Второй потребитель (4.2, воркер, модерация)
        обязан звать этот билдер, а не писать `WHERE city = ...` заново
        (AC-04).

        Почему `status='complete'` не избыточен вместе с заполненным городом:
        CHECK `completeness` разрешает `draft` с заполненным `city` и пустыми
        `name`/`age`/`bio` — такая строка в выдаче показала бы пустую карточку.

        Почему `is_hidden` проверяется явно: владелец скрывает анкету сам,
        и мужская анкета скрыта до одобрения заявки (поведение 2.1). Без
        условия скрытые попадали бы в чужую выдачу.

        Почему `city` сравнивается точным `=`: нормализация 2.3 приводит
        написание к канону из `PILOT_CITY` дословно (регистр и края сведены),
        поэтому `lower()`/`ILIKE` не нужны, а `lower()` вдобавок убивает
        индекс `idx_profiles_city_gender` (префикс `city`).

        `gender` в условие не входит: пол не влияет на попадание в выдачу
        (FR63, AC-05). Статуса фото предикат **не** проверяет: правило
        «видна при ≥1 `approved` фото» — продуктовое решение, вынесенное
        из 2.6 в 4.2/4.5 (см. Dev Notes → «Вне скоупа»). Не читать эту
        строку как «фото уже учтено».

        Город — параметр запроса, не литерал (AC-03): смена `PILOT_CITY`
        не требует правки кода.
        """
        return select(Profile).where(
            Profile.city == city,
            Profile.status == "complete",
            Profile.is_hidden.is_(False),
        )

    @staticmethod
    def _build_visible_in_city_page_statement(city: str, *, limit: int) -> Select[Profile]:
        """Task 2.4 (AC-01): предикат + `ORDER BY` + `LIMIT`, текст без БД.

        CR 2.6, патч 3 (§18.2): `ORDER BY`/`LIMIT` вынесены в отдельный билдер,
        потому что раньше их нельзя было проверить: unit-тест собирал свой
        запрос (`_build_visible_in_city_statement(city).limit(limit)`) и не видел
        ни `.order_by`, ни `limit` производственного пути, а интеграционные
        сравнения шли через одноэлементные списки и `set()`. Удаление
        `.order_by(Profile.uuid)` оставляло весь набор зелёным.

        Порядок по `uuid` — технический (детерминизм `LIMIT`), не продуктовый:
        90/10 и курсор появятся в 4.2.
        """
        return (
            ProfileRepository._build_visible_in_city_statement(city)
            .order_by(Profile.uuid)
            .limit(limit)
        )

    async def list_visible_in_city(self, city: str, *, limit: int) -> list[Profile]:
        """Task 2.4 (AC-01): видимые анкеты города пилота.

        Тонкая обёртка над билдером страницы: без `ORDER BY` `LIMIT` даёт
        недетерминированный набор между прогонами.

        CR 2.6, патч 2: `limit` валидируется здесь, а не только в роутере 4.2.
        `LIMIT -1` в PostgreSQL — ошибка выполнения (`500` на пользовательском
        вводе), `LIMIT 0` — молча пустая выдача. `None` в SQLAlchemy вообще
        означает «без ограничения» и выгружает таблицу целиком.

        `populate_existing` не ставится: выдача только читает, объекты
        в identity map не правятся. Ограничение осознанное — если 4.2 начнёт
        отдавать эти объекты в ответ после записи в той же сессии, его
        придётся вернуть (долг зафиксирован в deferred-work).
        """
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        bounded = min(limit, MAX_VISIBLE_LIMIT)
        statement = self._build_visible_in_city_page_statement(city, limit=bounded)
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def create(
        self,
        *,
        user_id: int,
        gender: str,
        **fields: Any,
    ) -> Profile:
        """CR 2.1, патч 10 (D-10): создаёт анкету, не фиксируя транзакцию.

        `flush` без `commit`: фиксацию делает вызывающий слой (D-4). `flush`
        нужен, чтобы `id`, `uuid` и server-дефолты (`status`, `is_hidden`,
        `created_at`) были заполнены до возврата и объект можно было читать.

        `gender` — отдельный аргумент, а не часть `**fields`: Решение 1 требует,
        чтобы пол совпадал с `users.gender`, и вызывающий обязан указать его
        осознанно, а не «забыть» в словаре полей.

        HTTP-метода создания в 2.1 нет (D-10): здесь только внутренний API
        для тестов и для сервиса 2.3.
        """
        profile = Profile(user_id=user_id, gender=gender, **fields)
        self._session.add(profile)
        await self._session.flush()
        return profile
