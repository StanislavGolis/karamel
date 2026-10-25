"""Гард доступности места для карточки события (стори 4.1, Task 6.3–6.4).

AC-04 (FR98, ADR-12, ADR-13): карточка может ссылаться ТОЛЬКО на место,
которое (1) существует, (2) активно и (3) принадлежит сообществу карточки.

**Почему отдельный модуль, а не метод `PlaceService`.** Правило принадлежит
карточке, а не справочнику: справочник не знает, что такое карточка и зачем
ей сообщество. Кроме того `PlaceService` закрыт стори 3.2/3.3 — правка
«по месту» сломала бы тесты записи мест ради одной функции из трёх строк.

**Почему отказ один на все три случая (ADR-13).** Скрытое место и место
чужого сообщества дают ОДИН И ТОТ ЖЕ `422 PLACE_NOT_AVAILABLE`, без
различия в коде и в тексте. Разные коды (`PLACE_ARCHIVED`,
`PLACE_FOREIGN_COMMUNITY`) позволили бы перебором `uuid` узнать, какие
места существуют в чужих сообществах, — это раскрытие существования,
запрещённое ADR-13 (там же, где «404 вместо 403» для карточек).

**Почему `422`, а не `404`.** `404` говорит «ресурса по адресу нет», и это
про саму карточку. Здесь адрес верен (`POST /api/v1/cards`), а неверны
данные в теле — семантика домена, project-context §4.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.places import PlaceRepository

# AC-04: машинный код ошибки. ВЕРХНИЙ_РЕГИСТР_ЧЕРЕЗ_ПОДЧЁРКИВАНИЯ —
# project-context §4, как `PLACE_DUPLICATE`. Клиент ветвится по константе,
# а не по тексту сообщения.
PLACE_NOT_AVAILABLE_CODE = "PLACE_NOT_AVAILABLE"


class PlaceNotAvailableError(Exception):
    """AC-04: место нельзя использовать в карточке.

    `details.place_uuid` — не раскрытие существования, а адрес того, что
    клиент сам прислал: он уже знает этот `uuid`, иначе не передал бы его.
    Поле нужно, чтобы форма подсветила конкретный выпадающий список при
    нескольких полях-местах в будущем; сейчас место одно, но контракт
    ошибки не должен меняться при их добавлении.

    Отдельный класс, а не `PlaceFieldError`: у того `code` приходит
    аргументом конструктора, потому что кодов у него много. Здесь код
    ровно один и зафиксирован классом — как `PlaceDuplicateError`.
    """

    code = PLACE_NOT_AVAILABLE_CODE

    def __init__(self, place_uuid: uuid_lib.UUID) -> None:
        super().__init__(PLACE_NOT_AVAILABLE_CODE)
        self.place_uuid = place_uuid

    @property
    def details(self) -> dict[str, str]:
        """Тело ошибки: только `place_uuid`, без причины отказа.

        Причина (скрыто / чужое сообщество / не существует) НЕ выводится:
        см. docstring модуля про ADR-13.
        """
        return {"place_uuid": str(self.place_uuid)}


async def ensure_place_available(
    session: AsyncSession,
    *,
    place_uuid: uuid_lib.UUID,
    community_slug: str,
    repository: PlaceRepository | None = None,
) -> None:
    """AC-04: единственная точка правила «место доступно для карточки».

    Вызывается из 4.2 до вставки карточки, внутри той же транзакции: если
    место скроют между проверкой и вставкой, карточка всё равно будет
    создана на существующее место (FK цел), а «скрытость» — обратимое
    состояние (FR68: скрытое место можно восстановить). Гонка здесь не
    разрушает данные, поэтому отдельная блокировка не берётся.

    `repository` — только для тестов: подмена репозитория позволяет проверить
    обе ветки без БД (§18.2). В рабочем коде аргумент не передаётся.

    Возвращает `None` при успехе; при отказе бросает `PlaceNotAvailableError`.
    Возврат значения не нужен: место уже проверено, а его поля придут
    из ответа `GET /places` (клиент выбирает место из списка — FR98).
    """
    repo = repository if repository is not None else PlaceRepository(session)
    available = await repo.is_available_for_card(
        place_uuid=place_uuid, community_slug=community_slug
    )
    if not available:
        raise PlaceNotAvailableError(place_uuid)
