"""История явок для модератора и для себя (стори 7.5; AC-01…AC-11).

**Решение о гарде принимает ВЫЗЫВАЮЩИЙ роут** (Task 3.1): у модераторского
маршрута гард `ModeratorUser`, у своего — `CurrentUser`, и это два разных
вопроса с разными кодами отказа. Сервис ДУБЛИРУЕТ инвариант «смотреть можно,
если `is_moderator` ИЛИ `participant_id == viewer_id`» как последнюю линию:
без него обход через прямой вызов сервиса (или будущий третий роут) отдал бы
чужую историю, а тест поймал бы это только по счастливой случайности (P-13).

**Чего в сервисе НЕТ и почему.**

- Нет чтения флагов (`author_confirmed`, `participant_confirmed`,
  `confirmed_at`) — даже для вычисления `my_answer` (AC-02, P-8, D-7.5-4):
  непрочитанное нельзя раскрыть, а у модератора нет своей стороны в чужой
  паре. Своя история уже отдаётся `GET /cards/{uuid}/attendance` (D-7.2-4).
- Нет `total`/`has_more` (D-7.5-9): `total` требует `COUNT`, а признак «есть
  ещё» выражается отсутствием `next_cursor` — второй флаг был бы вторым
  источником правды для одного вопроса.
- Нет аудит-лога (D-7.5-8): 8.6 логирует ПЕРЕПИСКУ (FR60, NFR32), а история
  явок — не переписка; отдельное правило аудита вне 8.6 размножило бы аудит.
- Нет ретенции/обезличивания (D-7.5-7): ADR-07 реализуется в 10.6 удалением.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.attendance import AttendanceRepository
from app.schemas.attendance_history import AttendanceHistoryItem, AttendanceHistoryPage
from app.services.attendance_history_cursor import encode_history_cursor

__all__ = [
    "AttendanceHistoryForbiddenError",
    "list_history",
]

#: AC-05: предел страницы по умолчанию и границы входа.
#: `le=50` — не про производительность страницы (она ограничена `LIMIT`), а
#: про устойчивость анти-оракула O-4: `limit` не влияет на число SQL, но
#: неограниченное значение позволило бы вытянуть историю целиком одним
#: запросом и обойти смысл пагинации.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

#: AC-03: код отказа сервиса. Совпадает с конвертом `403 {"code": "forbidden"}`
#: из `deps.MODERATOR_FORBIDDEN`: клиент ветвится по коду, и вторая форма
#: одного отказа дала бы два вида одной ошибки.
FORBIDDEN_CODE = "forbidden"


class AttendanceHistoryForbiddenError(Exception):
    """AC-03 (NFR39): смотреть чужую историю нельзя — `403 forbidden`.

    Поднимается только при нарушении инварианта внутри сервиса: на маршрутах
    отказ выдаёт гард (`ModeratorUser` / `CurrentUser`) ДО вызова. Класс
    существует, чтобы обход через сервис не отдавал данные молча.
    """

    code = FORBIDDEN_CODE


async def list_history(
    session: AsyncSession,
    *,
    participant_id: int,
    viewer_id: int,
    is_moderator: bool,
    limit: int,
    cursor: tuple[datetime, uuid_lib.UUID] | None,
) -> AttendanceHistoryPage:
    """AC-01…AC-11: страница истории явок пользователя `participant_id`.

    `now` не принимается: сервис не вычисляет время вовсе. Время в ответе —
    `starts_at` карточки (из БД), а ключ сортировки — `attendance.created_at`
    (тоже из БД). Инъекция `now` нужна там, где сервер ПИШЕТ метку (7.2/7.4);
    чтение истории от часов процесса не зависит, и добавлять параметр «на
    всякий случай» значило бы дать тестам возможность подменить то, чего код
    не использует (тавтология, §18.2).

    Порядок — КОНТРАКТ: (1) инвариант доступа, (2) чтение, (3) сборка
    `next_cursor`. Обратный порядок прочитал бы данные, которые отдавать
    нельзя, и отказ стал бы дороже по числу запросов.
    """
    # (1) AC-03/AC-04, Task 3.1, P-13: последняя линия защиты. Модератор
    # смотрит любого, пользователь — только себя. Проверка стоит ДО чтения:
    # отказ не должен стоить запроса к `attendance`.
    if not is_moderator and participant_id != viewer_id:
        raise AttendanceHistoryForbiddenError()

    # (2) AC-01, AC-11: `participant_id` — единственный предикат адресности.
    # Компания из трёх человек даёт по строке на участника, и история каждого
    # содержит ровно свою: смешения нет по построению запроса.
    repo = AttendanceRepository(session)
    rows = await repo.list_participant_history(
        participant_id=participant_id, limit=limit, cursor=cursor
    )

    # (3) AC-02, D-7.5-4: РОВНО четыре поля. `HistoryRow.card_id`/`created_at`
    # остаются внутри: первый — внутренний PK карточки (D-2), второй — ключ
    # сортировки. `my_answer` не вычисляется вовсе (см. докстринг модуля).
    page_rows = rows[:limit]
    items = [
        AttendanceHistoryItem(
            card_uuid=row.card_uuid,
            card_title=row.card_title,
            starts_at=row.starts_at,
            status=row.status,
        )
        for row in page_rows
    ]

    # AC-05, D-7.5-9: `limit + 1` строк — признак «есть ещё». Курсор строится
    # по последнему элементу ВЫДАННОЙ страницы и только если лишняя строка
    # пришла: на последней странице `next_cursor = null`, и клиент не делает
    # лишний запрос «а есть ли ещё». Пустая страница курсора не даёт: строить
    # его не из чего, а `null` для неё означает верное «дальше ничего нет».
    next_cursor: str | None = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = encode_history_cursor(last.created_at, last.card_uuid)

    return AttendanceHistoryPage(items=items, next_cursor=next_cursor)
