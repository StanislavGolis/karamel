"""Сводка пользователя для модератора (стори 8.5; AC-01, AC-03, AC-04, AC-11).

**Порядок — КОНТРАКТ** (D-65, AC-11): резолв адреса → профиль → жалобы →
санкции → явки. Резолв стоит ПЕРВЫМ, поэтому несуществующий адрес отдаёт
`404` до любых чтений ПД.

**Ось NFR39 — равенство числа SQL на двух ветвях.** «Пользователя нет»
(`404`) и «пользователь есть, данных пусто» (`200` с пустыми списками)
обязаны стоить ОДИНАКОВО обращений, иначе существование читается по числу
запросов. Поэтому негативная ветвь НЕ выходит после пустого резолва, а
выполняет те же восемь запросов с sentinel `_MISSING_USER_ID = 0`
(`users.id` — `IDENTITY`, начинается с 1, совпадений не будет).

Пара «нет прав / нет пользователя» этой оси НЕ подлежит: `403` отдаёт
`ModeratorUser` на маршруте ДО любого SQL (7.5, AC-09 O-3), и выравнивать
его значило бы читать БД ради того, чтобы отказать.

**Два РАЗНЫХ лимита** (DS 8.5, D-72): жалобы и санкции — `20` (история
нарушений), явки — `10` (FR40 задал это число для «сколько видно, не
переходя на экран списка»). Один общий лимит нарушил бы одно из требований.

**Гонок нет** (D-71): маршрут только читает. Ни `commit`, ни `FOR UPDATE`,
ни CAS — блокировать нечего, и §18.3 к стори не применяется.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.access_logs import AccessLogRepository
from app.repositories.reports import ReportsRepository
from app.repositories.sanctions import SanctionsRepository
from app.schemas.moderation_user import (
    AttendanceBlock,
    AttendanceBrief,
    ModerationUserCard,
    ModerationUserProfile,
    ReportBrief,
    ReportsBlock,
    SanctionBrief,
    SanctionsBlock,
)
from app.schemas.report import Resolution

__all__ = [
    "CARD_ATTENDANCE_LIMIT",
    "CARD_LIST_LIMIT",
    "UserNotFoundError",
    "load_user_card",
]

#: AC-10 (8.5), D-72: усечение истории нарушений. Число не из требования —
#: 20 покрывает «серия нарушений видна без перехода на экран списка», а
#: полные числа отдаются счётчиками.
CARD_LIST_LIMIT = 20

#: AC-01 (8.5), D-69: последние 10 явок. Число зафиксировано решением (по
#: образцу лимита фрагмента переписки 8.3, FR40).
CARD_ATTENDANCE_LIMIT = 10

#: AC-11, D-65: заведомо несуществующий `user_id` для выравнивания числа SQL
#: на негативной ветви. `users.id` — `IDENTITY`, начинается с 1, поэтому 0 не
#: совпадёт ни с одним пользователем. Тот же приём, что `_MISSING_CARD_ID`/
#: `_MISSING_CHAT_ID` в `report_service` (D-36, D-38).
_MISSING_USER_ID = 0


class UserNotFoundError(Exception):
    """AC-03 (8.5): пользователя нет — `404 user_not_found`.

    Код тот же, что у истории явок 7.5: адресат один и тот же пользователь,
    и разные коды на двух маршрутах заставили бы клиент ветвиться по
    маршруту вместо причины.
    """

    code = "user_not_found"


#: AC-03 (8.5): резолв адреса. `SELECT id, uuid` — ОБА нужны: `id` для
#: последующих выборок, `uuid` для профиля (он отдаётся наружу как есть).
#: Второй запрос за `uuid` разошёлся бы с первым при правке.
_RESOLVE_SQL = "SELECT id, uuid FROM users WHERE uuid = :uuid"

#: AC-05, AC-14 (8.5): профиль + статус учётной записи.
#:
#: **`LEFT JOIN`, не `JOIN`.** Пользователь мог зарегистрироваться и не
#: заполнить анкету; `JOIN` выбросил бы строку, и такой пользователь
#: получил бы `404` вместо `200` с пустыми полями (AC-14).
#:
#: Отдаются РОВНО шесть полей (D-66, NFR27): ни `phone`, ни `provider`,
#: ни `bio`, ни фото. `bio` — пользовательский контент с отдельной очередью
#: модерации (эпик 3), а идентификаторы входа — ключ к аккаунту, не основание
#: для решения.
_PROFILE_SQL = """
SELECT u.uuid, p.name, p.city, p.age, u.gender, u.status
  FROM users AS u
  LEFT JOIN profiles AS p ON p.user_id = u.id
 WHERE u.id = :user_id
"""

#: AC-01, D-69 (8.5): последние 10 фактов явки пользователя.
#:
#: `participant_id` — единственная ось: автор карточки флаги участника не
#: имеет (`attendance` хранит пару «участник + карточка», автор вычисляется
#: из `event_cards`). Модератору для сводки достаточно фактов, где человек
#: БЫЛ участником; его авторские встречи видны в карточках событий.
_ATTENDANCE_ITEMS_SQL = """
SELECT c.uuid, a.status, c.starts_at
  FROM attendance AS a
  JOIN event_cards AS c ON c.id = a.card_id
 WHERE a.participant_id = :user_id
 ORDER BY a.created_at DESC, a.card_id DESC
 LIMIT :limit
"""

#: AC-10 (8.5): ПОЛНОЕ число фактов явки.
_ATTENDANCE_COUNT_SQL = "SELECT count(*) FROM attendance WHERE participant_id = :user_id"


def _report_brief(
    row: tuple[uuid_lib.UUID, str, str, str | None, datetime, datetime | None],
) -> ReportBrief:
    """Строит `ReportBrief` из строки репозитория.

    `status`/`resolution` приведены к литералам схемы: рассинхрон домена
    (`CHECK` в БД разошёлся с `Literal`) — дефект сервера, и Pydantic бросит
    `ValidationError` → `500`, а не молча отдаст незнакомое значение клиенту
    (класс `A-ENUM-01` из 7.2).
    """
    # `cast` — домен приходит из БД строками: `CHECK` гарантирует набор, но
    # mypy видит `str`. Рассинхрон `CHECK`/`Literal` — дефект сервера, и он
    # проявится `ValidationError` → `500` на живом коде (класс `A-ENUM-01`),
    # а не молчаливой подменой типа.
    return ReportBrief(
        uuid=row[0],
        target_type=cast("Literal['no_show', 'message']", row[1]),
        status=cast("Literal['open', 'resolved_warning', 'resolved_ban', 'dismissed']", row[2]),
        resolution=cast("Resolution | None", row[3]),
        reported_at=row[4],
        resolved_at=row[5],
    )


async def load_user_card(
    session: AsyncSession,
    *,
    user_uuid: uuid_lib.UUID,
    moderator_uuid: uuid_lib.UUID | None = None,
    route: str | None = None,
) -> ModerationUserCard:
    """AC-01…AC-11: сводка пользователя для модератора.

    Число SQL — КОНСТАНТА, не зависящая от данных: резолв (1), профиль (1),
    жалобы (2), счётчики жалоб (1), санкции (1+1), явки (1+1) = **9** на
    обеих ветвях. На негативной ветви те же восемь запросов выполняются с
    sentinel-ом `_MISSING_USER_ID`, поэтому разницы в числе обращений нет.

    Гарда здесь нет намеренно: роль проверяет маршрут (`ModeratorUser`), и
    второй вход доступа разошёлся бы с первым при первой правке (урок
    `_load_card` CR 7.4 H-1).

    `now` не принимается: сервис не вычисляет время. Обе метки в ответе —
    `reported_at`/`resolved_at`/`created_at` из БД, часы процесса не участвуют,
    и параметр «на всякий случай» дал бы тестам возможность подменить то,
    чего код не использует (§18.2).
    """
    resolved = await session.execute(text(_RESOLVE_SQL), {"uuid": user_uuid})
    row = resolved.first()

    # AC-11, D-65: НЕ выходим здесь на `404`. Негативная ветвь обязана стоить
    # столько же запросов, сколько позитивная, иначе существование читается
    # по числу обращений. `found` запоминается и превращается в `404` ПОСЛЕ
    # всех чтений.
    found = row is not None
    # `cast` нужен, потому что mypy не сужает `Row | None` по флагу `found`:
    # `row is not None` — условие, а не тип. Значение читается только в
    # ветви `found`, поэтому приведение не ослабляет проверку.
    user_id = int(cast("tuple[int, uuid_lib.UUID]", row)[0]) if found else _MISSING_USER_ID

    # (2) AC-05, AC-14: профиль + статус. `LEFT JOIN profiles` — у пользователя
    # может не быть анкеты; такой профиль отдаётся с пустыми полями, не 404.
    profile_row = (await session.execute(text(_PROFILE_SQL), {"user_id": user_id})).first()

    reports = ReportsRepository(session)
    sanctions = SanctionsRepository(session)

    # (3)–(4) AC-06, AC-08: две оси жалоб. Списки усечены, счётчики полные.
    filed_rows = await reports.card_filed(user_id=user_id, limit=CARD_LIST_LIMIT)
    against_rows = await reports.card_against(user_id=user_id, limit=CARD_LIST_LIMIT)
    filed_total, against_total = await reports.card_counts(user_id=user_id)

    # (5)–(6) AC-07: санкции и их полное число.
    sanction_rows = await sanctions.card_items(user_id=user_id, limit=CARD_LIST_LIMIT)
    sanction_total = await sanctions.card_count(user_id=user_id)

    # (7)–(8) AC-01, D-69: явки — последние 10 и полное число.
    attendance_rows = (
        await session.execute(
            text(_ATTENDANCE_ITEMS_SQL), {"user_id": user_id, "limit": CARD_ATTENDANCE_LIMIT}
        )
    ).all()
    attendance_total = int(
        (await session.execute(text(_ATTENDANCE_COUNT_SQL), {"user_id": user_id})).scalar_one()
    )

    # Стори 8.6 (AC-08, D-86, D-96): аудит пишется ДО выхода на `404` — факт
    # попытки доступа тоже фиксируется, и число SQL становится равным на обеих
    # ветвях (NFR39, AC-11 стори 8.5). Иначе «нет пользователя» стоило бы 9
    # запросов, а «есть без данных» — 10, и существование читалось бы по числу
    # обращений. Проверено прогоном: без этого тест 8.5 краснел.
    if moderator_uuid is not None:
        await AccessLogRepository(session).log_card_read(
            moderator_uuid=moderator_uuid,
            subject_uuid=user_uuid,
            route=route,
        )

    if not found or profile_row is None:
        # `profile_row is None` при `found` невыразимо: `users` читается по
        # `id`, а `LEFT JOIN profiles` не может обнулить строку пользователя.
        # Проверка стоит как страховка от будущей правки `JOIN`-а на другой
        # источник — иначе такой пользователь получил бы `200` без профиля.
        #
        # **Строка аудита к этому моменту уже записана** (D-96): модератор
        # действительно открывал карточку, и факт попытки — часть аудита.
        raise UserNotFoundError()

    return ModerationUserCard(
        profile=ModerationUserProfile(
            uuid=profile_row[0],
            name=profile_row[1],
            city=profile_row[2],
            age=profile_row[3],
            gender=profile_row[4],
            status=profile_row[5],
        ),
        reports=ReportsBlock(
            filed=[_report_brief(r) for r in filed_rows],
            against=[_report_brief(r) for r in against_rows],
            filed_total=filed_total,
            against_total=against_total,
        ),
        sanctions=SanctionsBlock(
            items=[
                SanctionBrief(
                    uuid=r[0],
                    type=cast("Literal['warning', 'ban']", r[1]),
                    reason=r[2],
                    created_at=r[3],
                    moderator_uuid=r[4],
                )
                for r in sanction_rows
            ],
            total=sanction_total,
        ),
        attendance=AttendanceBlock(
            items=[
                AttendanceBrief(card_uuid=r[0], status=r[1], starts_at=r[2])
                for r in attendance_rows
            ],
            total=attendance_total,
        ),
    )
