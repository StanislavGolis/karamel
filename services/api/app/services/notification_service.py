"""Уведомления о жизненном цикле заявки (стори 9.2; FR43, FR44, FR45, FR81).

**Почему отдельный модуль, а не три вызова в трёх сервисах.** Точек вызова
четыре (`submit_join_request`, `select_join_request`, `card_join_service`,
воркер + ленивое закрытие), и каждая собирает один и тот же payload.
Разошедшаяся форма (`card_uuid` строкой в одном месте, объектом в другом)
не заметна ни компилятору, ни клиенту — NFR60 требует от клиента
ИГНОРИРОВАТЬ неизвестное, то есть ошибку в форме не увидит никто.

**Порядок `notifications` → `WS` → `delivered_at` (D-9.2-4).** Отклонение от
§6.6 архитектуры («WS → notifications → ответ») обосновано в стори: `uuid`
строки нужен, чтобы отметить доставку ИМЕННО ей. Обратный порядок потребовал
бы ad-hoc ключ в конверте и второй `SELECT` по
`(type, card_uuid)` — предикат, который не различает два уведомления одного
типа по одной карточке. Инвариант ADR-11 («сетевых вызовов внутри
транзакции нет») сохранён: и запись, и публикация идут ПОСЛЕ `commit`
доменной операции.

**Почему `delivered_at` ставится только при `PUBLISH` (D-9.2-5, долг L-2).**
При деградации (`_deliver_locally_user`) событие дошло только до соединений
ЭТОГО процесса. Заполненный `delivered_at` означал бы «доставлено» для всех
инстансов, и подписчик другого процесса никогда бы его не получил — повтора
нет, шина не переотправляет. Пустой `delivered_at` — честный сигнал «в шину
не ушло», и 9.3 покажет такие строки.

**Почему ошибка уведомления не влияет на ответ (D-9.2-12, ADR-11).** Домен
уже закоммичен: заявка подана, выбор сделан. `500` из-за недоступного Redis
или сбоя вставки уведомления был бы ложью о доменной операции.
"""

from __future__ import annotations

import json
import uuid as uuid_lib
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationType
from app.repositories.notifications import NotificationsRepository
from app.schemas.ws import WsEnvelope, utc_timestamp
from app.services.ws_publisher import publish_to_user

__all__ = [
    "notify_company_assembled",
    "notify_request_received",
    "notify_request_selected",
    "notify_support_replied",
    "notify_window_expiring",
]

logger = structlog.get_logger(__name__)


def _payload(**values: Any) -> str:
    """Готовая JSON-строка payload'а — одна точка сериализации.

    Поля-значения приводятся к строке заранее: `str(uuid)` даёт каноническую
    форму, а `json.dumps` без приведения сериализовал бы `UUID` в
    нестандартном для клиента виде.

    **Личности в payload НЕТ** (D-9.2-9, FR81): только `card_uuid` и
    `chat_uuid`/`request_uuid`. Payload попадает в логи, выгрузки и отладку, и
    `applicant_id`/`author_id` в нём дали бы ровно тот оракул авторства,
    который FR81 запрещает.
    """
    body = {key: str(value) for key, value in values.items()}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


async def _create_publish_mark(
    session: AsyncSession,
    *,
    user_uuid: uuid_lib.UUID,
    user_id: int,
    notification_type: str,
    payload: str,
) -> bool:
    """Общий конвейер: `INSERT` → `commit` → `PUBLISH` → `delivered_at`.

    Возвращает `True`, только если событие реально ушло в шину И отметка
    доставки записана. Ошибка НЕ пробрасывается (D-9.2-12): доменная
    операция уже зафиксирована выше по стеку.

    **Отдельная короткая транзакция, а не часть доменной.** К моменту вызова
    основная транзакция закоммичена (ADR-11): откатывать её нельзя, а
    оставить в ней незавершённую работу — значит сорвать ответ клиенту.
    Та же граница, что у `notification_delivery` (D-30).
    """
    repo = NotificationsRepository(session)
    try:
        notification_uuid = await repo.create_one(
            user_id=user_id, type=notification_type, payload=payload
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — домен уже закоммичен, ответ успешен
        await session.rollback()
        logger.error(
            "notification.create.failed",
            notification_type=notification_type,
            error=type(exc).__name__,
        )
        return False

    # AC-01, AC-03, D-9.3-7: тип отключён пользователем — строки нет, WS НЕ
    # публикуется, ошибки НЕТ. Это нормальный исход настройки, а не сбой:
    # `notification.create.failed` остаётся за настоящими отказами (6 мест),
    # а отключение пишется в `notification.muted` уровнем `info`.
    # `delivered_at` не выставляется (ставить нечего) и семантика D-9.2-5
    # не меняется: пустое значение по-прежнему означает «в шину не ушло».
    if notification_uuid is None:
        logger.info("notification.muted", notification_type=notification_type)
        return False

    # Без `user_id`/`applicant_id`: тип и uuid строки достаточны для
    # корреляции, а личность в журнале — тот же оракул, что в payload (FR81).
    logger.info(
        "notification.created",
        notification_type=notification_type,
        notification_uuid=str(notification_uuid),
    )

    frame_payload = json.loads(payload)
    envelope = WsEnvelope.build(notification_type, frame_payload)
    published = await publish_to_user(user_uuid=user_uuid, envelope=envelope)
    if not published:
        # L-2/D-9.2-5: строка остаётся с `delivered_at IS NULL`. Повторной
        # публикации не будет — клиент получит событие при следующем его
        # возникновении либо увидит уведомление в списке (9.3).
        return False

    try:
        await repo.mark_delivered_by_uuid(notification_uuid=notification_uuid)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — уведомление уже создано и отправлено
        await session.rollback()
        logger.error(
            "notification.delivery.mark.failed",
            notification_type=notification_type,
            error=type(exc).__name__,
        )
        return False
    return True


async def notify_request_received(
    session: AsyncSession,
    *,
    author_uuid: uuid_lib.UUID,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    request_uuid: uuid_lib.UUID,
) -> bool:
    """AC-1, FR43: автору — «заявка пришла». Payload `{card_uuid, request_uuid}`.

    `request_uuid` в payload, а не `applicant_id`: автору нужно открыть
    заявку в списке (5а.4), а личность заявителя он и так видит там по правам.
    """
    return await _create_publish_mark(
        session,
        user_uuid=author_uuid,
        user_id=author_id,
        notification_type=NotificationType.REQUEST_RECEIVED.value,
        payload=_payload(card_uuid=card_uuid, request_uuid=request_uuid),
    )


async def notify_request_selected(
    session: AsyncSession,
    *,
    applicant_uuid: uuid_lib.UUID,
    applicant_id: int,
    card_uuid: uuid_lib.UUID,
    chat_uuid: uuid_lib.UUID,
) -> bool:
    """AC-2, FR44: заявителю — «ваша заявка выбрана». Payload `{card_uuid, chat_uuid}`.

    Состав payload'а совпадает с `card.full` намеренно (стори, Dev Notes):
    клиенту нужен ровно один идентификатор, чтобы открыть переписку, и
    два разных имени одного поля заставили бы его ветвиться по типу события.
    """
    return await _create_publish_mark(
        session,
        user_uuid=applicant_uuid,
        user_id=applicant_id,
        notification_type=NotificationType.REQUEST_SELECTED.value,
        payload=_payload(card_uuid=card_uuid, chat_uuid=chat_uuid),
    )


async def notify_window_expiring(
    session: AsyncSession,
    *,
    author_uuid: uuid_lib.UUID,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    expires_at: datetime,
) -> bool:
    """AC-01, AC-03 стори 9.4 (FR110): автору — «окно решения истекает».

    **Адресат — ТОЛЬКО автор** (D-9.4-3). PRD дословно: «Пользователь может
    получать уведомление о приближении истечения окна решения (автор)»
    (`prd-v2-action-first.md:665`). Участникам не шлётся: уведомление должно
    вести к действию, а выбор заявки участнику недоступен — он получил бы
    `403` по ссылке из уведомления. Это не экономия, а корректность.

    **Дедупликация — маркером карточки, а не строк** (AC-03, ADR-15).
    Вызывается РОВНО из ветки `REMINDED` после `commit` CAS-маркера
    `decision_reminded_at` (D-9.4-2, D-9.4-13). Второй ключ («кому уже
    отправили») запрещён ADR-15: два источника истины переживают падение
    процесса по-разному. Поэтому повторный прогон просто не доходит сюда.

    `payload` = `{card_uuid, expires_at}` — без личности (FR81, D-9.2-9):
    ни `author_id`, ни `user_id`; `expires_at` нужен клиенту, чтобы показать
    «осталось N минут» без второго запроса к карточке.

    Возвращает `True` только при успешной публикации и записанном
    `delivered_at` (конвейер 9.2). `False` — исход настройки (тип отключён)
    или сбой доставки; вызывающий НЕ откатывает маркер (D-9.4-4).
    """
    return await _create_publish_mark(
        session,
        user_uuid=author_uuid,
        user_id=author_id,
        notification_type=NotificationType.WINDOW_EXPIRING.value,
        payload=_payload(card_uuid=card_uuid, expires_at=utc_timestamp(expires_at)),
    )


async def notify_company_assembled(
    session: AsyncSession,
    *,
    author_uuid: uuid_lib.UUID,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    chat_uuid: uuid_lib.UUID,
) -> bool:
    """AC-3, FR45 вариант A: автору — «компания собрана». ТОЛЬКО автору.

    **Почему не выбранному заявителю** (D-9.2-8). FR45 в варианте A адресует
    уведомление автору: выбранный узнаёт о сборке из `request.selected`
    (AC-2) и из открывшегося чата. Второе уведомление тому же человеку о том
    же факте — шум, а `WS card.full` в комнату чата уведомлением не является
    (AC-3 стори).
    """
    return await _create_publish_mark(
        session,
        user_uuid=author_uuid,
        user_id=author_id,
        notification_type=NotificationType.COMPANY_ASSEMBLED.value,
        payload=_payload(card_uuid=card_uuid, chat_uuid=chat_uuid),
    )


async def notify_support_replied(
    session: AsyncSession,
    *,
    user_uuid: uuid_lib.UUID,
    user_id: int,
    ticket_uuid: uuid_lib.UUID,
) -> bool:
    """AC-02 стори 9.6 (FR57, FR81): автору обращения — «поддержка ответила».

    **Payload — РОВНО `{ticket_uuid}`** (У-8, ADR-13, D-9.6-8). Ни текста
    ответа, ни тела обращения, ни `user_id`, ни `answered_by_id`. Payload
    попадает в логи, выгрузки и отладку; копия ответа в нём была бы вторым
    местом хранения ПД без срока и без контроля доступа, а `answered_by_id`
    раскрыл бы личность модератора. Ответ читается в `GET /support/tickets`:
    `ticket_uuid` достаточно, чтобы клиент перечитал ту же строку списка.

    **Gate 9.3 применяется** (AC-01, D-9.3-6): `_create_publish_mark` вызывает
    `create_one`, внутри которого стоит `mute_gate_sql`. Тип
    `support.replied` — ОТКЛЮЧАЕМЫЙ: он НЕ в `REQUIRED_NOTIFICATION_TYPES`
    (там только `report.resolved` и `sanction.issued`), поэтому отключение
    канала не отменяет пункт PRD: ответ остаётся доступен в списке обращений.

    **Ответ не влияет на исход** (D-9.2-12): возвращает `False` при отключённом
    типе или сбое доставки, исключение не пробрасывает — домен уже закоммичен
    выше по стеку, и `500` из-за недоступного Redis был бы ложью об ответе.
    """
    return await _create_publish_mark(
        session,
        user_uuid=user_uuid,
        user_id=user_id,
        notification_type=NotificationType.SUPPORT_REPLIED.value,
        payload=_payload(ticket_uuid=ticket_uuid),
    )
