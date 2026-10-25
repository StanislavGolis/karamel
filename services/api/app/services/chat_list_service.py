"""Список чатов и отметка прочитанного (стори 6.6; AC-01…AC-06).

**Гард — ТОТ ЖЕ, что в 6.4** (`_resolve_chat` из `chat_message_service`).
Второй предикат участия не заводится: два независимых определения «я в
компании» рано или поздно разойдутся, и тогда список покажет чат, в который
нельзя написать, или наоборот. Побочный эффект — анти-оракул (D-6.4-6)
наследуется бесплатно: число SQL-запросов на «посторонний» и «чата нет»
одинаково, потому что путь один.

**`delivered ≠ read`** (NFR12, project-context §9). Эта стори ставит
`read_at`, но НЕ трогает `delivered_at` (0017): «доставлено» — факт
транспорта, «прочитано» — факт внимания человека.

**Список — один запрос** (AC-05, NFR12). Последнее сообщение и счётчик
непрочитанных приходят `LATERAL`-подзапросами (см.
`ChatRepository.build_list_for_viewer_statement`); сервис только расшифровывает
тела и собирает страницу. Расшифровка — здесь, потому что репозиторий не
знает ключей (D-6.4-4).

**Скрытые жалобой чаты** (FR37) отсекает репозиторий единым предикатом с
маркером `FR37-EXTENSION-POINT`; сервис об этом не знает и не должен —
фильтрация на клиенте или в сервисе оставила бы данные в ответе API
(project-context §5, §12).
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import structlog
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_message_body
from app.repositories.chats import ChatRepository
from app.repositories.messages import MessageRepository
from app.services.chat_message_service import _resolve_chat
from app.services.message_cursor import decode_message_cursor, encode_message_cursor
from app.services.notification_delivery import mark_notifications_read
from app.services.place_cursor import InvalidCursorError

logger = structlog.get_logger(__name__)

# Размер страницы списка: 20 — «экран целиком» на телефоне без догрузки.
# Верхняя граница обязательна: без неё `?limit=100000` вернул бы все чаты
# одним ответом и обнулил бы смысл курсора (D-6.4-3, тот же довод).
CHATS_PAGE_SIZE = 20
MAX_CHATS_PAGE_SIZE = 100


class ChatListSettings(Protocol):
    """Минимум настроек: ключ шифрования переписки.

    Протокол, а не импорт `Settings`: unit-тест подставляет ключ без чтения
    `.env` (тот же приём, что `ChatSettings` в 6.4).
    """

    chat_message_key: str


@dataclass(frozen=True)
class ChatListLastMessage:
    """Последнее сообщение в строке списка.

    `body` — УЖЕ расшифрованный текст: ключ не покидает сервер, а клиенту
    нужен превью (NFR31). `None` вместо пустой строки: `null`, не `""` (§4).
    """

    uuid: uuid_lib.UUID
    # Стори 6.7 (AC-04): у системного сообщения автора нет — `None`.
    sender_uuid: uuid_lib.UUID | None
    body: str
    created_at: datetime
    #: Стори 6.7 (AC-04): признак системного сообщения в превью списка.
    is_system: bool = False


@dataclass(frozen=True)
class ChatListItem:
    """AC-01: строка списка чатов.

    `last_message = None` — сообщений нет. Такой чат ВСЁ РАВНО в списке
    (AC-04): компания собрана, но переписка ещё не началась (или в ней будут
    только системные сообщения 6.7). Отфильтровать его значило бы «чат
    исчез, пока никто не написал».

    `unread_count` не включает собственные сообщения (AC-06).
    """

    chat_uuid: uuid_lib.UUID
    card_uuid: uuid_lib.UUID
    card_title: str
    last_message: ChatListLastMessage | None
    unread_count: int
    last_activity_at: datetime


@dataclass(frozen=True)
class ChatListPage:
    """AC-01: страница списка — элементы, `total` и непрозрачный курсор.

    `total` приходит из `COUNT(*) OVER ()` того же запроса: отдельный
    `COUNT` был бы третьим round-trip и считал бы по тем же предикатам
    второй раз (AC-05). `cursor = None` — дальше ничего нет.
    """

    items: list[ChatListItem]
    total: int
    cursor: str | None


@dataclass(frozen=True)
class ChatReadResult:
    """AC-02: результат отметки прочтения.

    `unread_count` всегда `0` после успешного вызова: либо только что
    обнулили, либо там и было пусто (идемпотентность). Поле есть, чтобы
    клиент не делал второй `GET /chats` ради счётчика.
    """

    chat_uuid: uuid_lib.UUID
    unread_count: int


def _decrypt_last_message(
    raw: Any,
    *,
    chat_id: int,
    # Стори 6.7 (AC-04): `None` — системное сообщение (автора нет). Внутри
    # передаётся КАК ЕСТЬ: домен `system` выбирается внутри `_aad`;
    # тип обязан это разрешать, иначе `mypy --strict` отвергает вызов.
    sender_id: int | None,
    settings: ChatListSettings,
) -> str | None:
    """Расшифровывает тело последнего сообщения или отдаёт `None`.

    `chat_id`/`sender_id` — реальные внутренние ключи СТРОКИ: они входят в
    AAD (`chat:{id}|sender:{id}`), и подстановка нулей сделала бы расшифровку
    невозможной ВСЕГДА (`InvalidTag`) — дефект, пойманный собственным тестом
    до прогона (ключ верный, AAD — нет).

    Битая строка — `None`, а НЕ исключение: список чатов обзорный, и одно
    повреждённое сообщение не должно обнулять экран. Пишется
    `chat_list.corrupted`, строка показывается без превью. В истории (6.4)
    поведение ОБРАТНОЕ: там подмена текста недопустима, и ошибка явная.
    """
    try:
        return decrypt_message_body(
            bytes(raw),
            chat_id=chat_id,
            # Стори 6.7 (D-6.7-3): у системного сообщения `sender_id IS NULL`,
            # а AAD обязан быть детерминированным — иначе превью системного
            # сообщения ВСЕГДА падало бы `InvalidTag` и строка списка
            # показывалась бы без текста.
            # CR 6.7 (A-1): `None` передаётся КАК ЕСТЬ — домен «system» в AAD
            # выбирается внутри `_aad`, а не подстановкой числа 0 (число 0
            # совпадало бы с AAD пользователя `id=0`).
            sender_id=sender_id,
            key=settings.chat_message_key,
        )
    except (InvalidTag, ValueError):
        logger.error("chat_list.corrupted", chat_id=chat_id, sender_id=sender_id)
        return None


async def list_chats(
    session: AsyncSession,
    *,
    viewer_id: int,
    cursor: str | None = None,
    limit: int = CHATS_PAGE_SIZE,
    settings: ChatListSettings,
) -> ChatListPage:
    """AC-01, AC-04, AC-05: список чатов зрителя с последним сообщением.

    Курсор разбирается ДО запроса: битое значение обязано стоить ноль
    запросов (`422 invalid_cursor`, а не `500`).

    `limit + 1`: лишняя строка — признак «есть ещё», без второго запроса на
    существование следующей страницы (тот же приём, что в истории 6.4).

    Расшифровка — ПОСЛЕ выборки: репозиторий не знает ключей (D-6.4-4),
    и тело последнего сообщения приходит шифртекстом.
    """
    after = decode_message_cursor(cursor) if cursor is not None else None
    effective_limit = max(1, min(limit, MAX_CHATS_PAGE_SIZE))

    rows = await ChatRepository(session).list_for_viewer(
        viewer_id=viewer_id,
        after=after,
        limit=effective_limit + 1,
    )

    has_more = len(rows) > effective_limit
    page_rows = rows[:effective_limit]

    total = int(page_rows[0][11]) if page_rows else 0

    items: list[ChatListItem] = []
    for row in page_rows:
        (
            chat_id,
            chat_uuid,
            card_uuid,
            card_title,
            last_message_uuid,
            last_message_sender_id,
            last_message_sender_uuid,
            last_message_body,
            last_message_created_at,
            unread_value,
            last_activity_at,
            _total,
        ) = row

        resolved_chat_uuid = (
            chat_uuid if isinstance(chat_uuid, uuid_lib.UUID) else uuid_lib.UUID(str(chat_uuid))
        )
        resolved_card_uuid = (
            card_uuid if isinstance(card_uuid, uuid_lib.UUID) else uuid_lib.UUID(str(card_uuid))
        )

        last_message: ChatListLastMessage | None = None
        if last_message_uuid is not None:
            body = _decrypt_last_message(
                last_message_body,
                chat_id=int(chat_id),
                # `None` — системное сообщение: расшифровка идёт с AAD-константой.
                sender_id=None if last_message_sender_id is None else int(last_message_sender_id),
                settings=settings,
            )
            if body is not None:
                last_message = ChatListLastMessage(
                    uuid=last_message_uuid
                    if isinstance(last_message_uuid, uuid_lib.UUID)
                    else uuid_lib.UUID(str(last_message_uuid)),
                    # AC-04: `None` у системного — `null` в ответе, не нулевой uuid.
                    sender_uuid=None
                    if last_message_sender_uuid is None
                    else (
                        last_message_sender_uuid
                        if isinstance(last_message_sender_uuid, uuid_lib.UUID)
                        else uuid_lib.UUID(str(last_message_sender_uuid))
                    ),
                    body=body,
                    created_at=last_message_created_at,
                    is_system=last_message_sender_id is None,
                )

        items.append(
            ChatListItem(
                chat_uuid=resolved_chat_uuid,
                card_uuid=resolved_card_uuid,
                card_title=str(card_title),
                last_message=last_message,
                unread_count=int(unread_value or 0),
                last_activity_at=last_activity_at,
            )
        )

    next_cursor = None
    if has_more and items:
        tail = items[-1]
        next_cursor = encode_message_cursor(tail.last_activity_at, tail.chat_uuid)

    return ChatListPage(items=items, total=total, cursor=next_cursor)


async def mark_chat_read(
    session: AsyncSession,
    *,
    viewer_id: int,
    chat_uuid: uuid_lib.UUID,
) -> ChatReadResult:
    """AC-02: обнуляет непрочитанные в чате, идемпотентно.

    Порядок — контракт:

    1. гард (`_resolve_chat`, тот же, что 6.4): посторонний получает
       `404 chat_not_found` ДО любой записи — и с тем же числом запросов,
       что «чата нет» (анти-оракул, ADR-13);
    2. `UPDATE ... WHERE read_at IS NULL AND sender_id <> viewer`: повторный
       вызов возвращает 0 строк и НЕ переписывает время первого прочтения
       (`rowcount` 1 → 0 — это и есть идемпотентность, §18.11);
    3. `commit` ДО ответа: отметка обязана быть видна следующему `GET`.

    **Событие `message.read` клиенту НЕ отправляется** — это задел: 6.6
    ставит только серверное состояние, а WS-квитанция о прочтении появится
    вместе с системными сообщениями 6.7 и скрытием 8.2, где у неё есть
    получатель (собеседник). Отправлять сейчас значило бы придумать формат
    события, который эти стори переделывали бы.
    """
    access = await _resolve_chat(session, chat_uuid=chat_uuid, viewer_id=viewer_id)

    touched = await MessageRepository(session).mark_read(
        chat_id=access.chat_id, viewer_id=viewer_id
    )
    await session.commit()

    # AC-06 стори 8.2 (D-26): уведомления жалобы по этому диалогу считаются
    # прочитанными. Отдельная транзакция: отметка сообщений уже закоммичена,
    # и её откат невозможен. Бикондиционал `read_after_delivery` держится сам:
    # `delivered_at IS NOT NULL` входит в предикат.
    from app.services.report_service import REPORT_CREATED_TYPE

    await mark_notifications_read(session, chat_uuid=chat_uuid, type_=REPORT_CREATED_TYPE)

    logger.info(
        "chat_list.marked_read",
        chat_uuid=str(chat_uuid),
        messages_read=touched,
    )
    return ChatReadResult(chat_uuid=chat_uuid, unread_count=0)


__all__ = [
    "CHATS_PAGE_SIZE",
    "MAX_CHATS_PAGE_SIZE",
    "ChatListItem",
    "ChatListLastMessage",
    "ChatListPage",
    "ChatListSettings",
    "ChatReadResult",
    "InvalidCursorError",
    "list_chats",
    "mark_chat_read",
]
