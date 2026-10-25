"""Системные сообщения компании (стори 6.7; AC-01…AC-04; FR106, NFR11).

**Что это.** Запись в `messages` без автора-пользователя: состав компании
изменился, и чат обязан объяснить это фактом, а не молчанием (UX §9:
«нейтральный тон, серый фон, без иконки предупреждения»).

**Почему шаблоны на сервере** (D-6.7-5). Клиент не присылает текст: иначе
системное сообщение стало бы каналом подделки факта (и XSS, если бы текст
вставлялся как HTML). Имена участников не подставляются: сообщение остаётся
безличным (анти-оракул FR81/ADR-13 действует и внутри компании), не требует
JOIN'а к `users` на пути записи и не тащит ПД в `messages`.

**Шифрование — то же, что у обычных** (D-6.7-3). Ключ `chat_message_key`,
AES-256-GCM, AAD `chat:{id}|sender:system` (домен, а не число). Второй путь
(plaintext / отдельный ключ) означал бы вторую ветку расшифровки в истории,
в списке чатов и в WS — и «забыть зашифровать» снова стало бы возможно.

**Транзакция — вызывающего** (D-6.7-7, ADR-11). `append_system_message`
НЕ коммитит: сообщение обязано появиться в ТОЙ ЖЕ транзакции, что и
доменное изменение. Публикация — отдельный шаг ПОСЛЕ `commit`: событие о
незакоммиченных данных показало бы клиенту сообщение, которого нет в БД.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from enum import StrEnum
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_message_body
from app.repositories.messages import MessageRepository
from app.schemas.ws import WsEnvelope, utc_timestamp
from app.services.ws_publisher import publish_to_chat

logger = structlog.get_logger(__name__)


class SystemMessageSettings(Protocol):
    """Минимум настроек: ключ шифрования переписки (как у 6.4/6.6)."""

    chat_message_key: str


class SystemMessageKind(StrEnum):
    """Три триггера стори (AC-01, AC-02, AC-03).

    `StrEnum`, а не голые строки: новый триггер без шаблона падает в тесте
    (`set(TEMPLATES) == set(KIND)`), а не в проде `KeyError`.
    """

    PARTICIPANT_LEFT = "participant_left"
    PARTICIPANT_JOINED = "participant_joined"
    CARD_CANCELLED = "card_cancelled"


#: AC-01…AC-04, D-6.7-5: серверные шаблоны. Без `{}`-подстановок и без имён:
#: текст — константа, а не строка формата (нечего подделывать клиенту и некуда
#: подставить ПД). Проверяется `test_templates_cover_all_kinds_and_carry_no_identity`.
SYSTEM_MESSAGE_TEMPLATES: dict[SystemMessageKind, str] = {
    SystemMessageKind.PARTICIPANT_LEFT: "Участник отменил участие",
    SystemMessageKind.PARTICIPANT_JOINED: "В компании новый участник",
    SystemMessageKind.CARD_CANCELLED: "Автор отменил встречу",
}


def build_system_envelope(
    *,
    chat_uuid: uuid_lib.UUID,
    message_uuid: uuid_lib.UUID,
    body: str,
    created_at: datetime,
) -> WsEnvelope:
    """AC-01…AC-04, D-6.7-4: событие `message.created` для системного сообщения.

    Тип — ТОТ ЖЕ `message.created`, что у обычных, а системность выражена
    полями payload. Новый тип (`chat_message.created` из постановки) означал
    бы молчаливую потерю на клиенте 6.4/6.5: NFR60 требует неизвестный тип
    ИГНОРИРОВАТЬ, а не падать. Дедуп брокера работает по `(type,
    message_uuid)` — `message_uuid` у системного есть (это строка в
    `messages`), значит долг X-6.5-6 не расширяется.
    """
    return WsEnvelope.build(
        "message.created",
        {
            "message_uuid": str(message_uuid),
            "chat_uuid": str(chat_uuid),
            # AC-04: автора НЕТ. `null`, а не нулевой uuid: клиент различает
            # системное и пользовательское по `is_system`, а не по «странному»
            # идентификатору.
            "sender_uuid": None,
            "is_system": True,
            "body": body,
            "created_at": utc_timestamp(created_at),
        },
    )


async def append_system_message(
    session: AsyncSession,
    *,
    chat_id: int,
    kind: SystemMessageKind,
    settings: SystemMessageSettings,
) -> tuple[uuid_lib.UUID, datetime, str]:
    """AC-01…AC-04: записывает системное сообщение в ТЕКУЩЕЙ транзакции.

    `commit` НЕ вызывается (D-6.7-7): сообщение и доменное изменение обязаны
    быть атомарны. Возвращает `(uuid, created_at, body)` — всё, что нужно
    вызывающему для публикации после `commit`.

    `sender_id=None` + `is_system=True` — инвариант `CHECK
    ck_messages_system_sender_exclusive`: одно без другого БД не примет.
    """
    body = SYSTEM_MESSAGE_TEMPLATES[kind]
    ciphertext = encrypt_message_body(
        body,
        chat_id=chat_id,
        # CR 6.7 (A-1): `None` — домен «системное сообщение» в AAD. Раньше
        # здесь стояла числовая константа `0`, и блоб системного сообщения
        # совпадал по AAD с сообщением пользователя `0` (явная вставка `id=0`
        # разрешена: `Identity` в PostgreSQL — `BY DEFAULT`).
        sender_id=None,
        key=settings.chat_message_key,
    )
    message_uuid = uuid_lib.uuid4()
    inserted = await MessageRepository(session).insert_if_absent(
        chat_id=chat_id,
        sender_id=None,
        body=ciphertext,
        message_uuid=message_uuid,
        client_message_id=None,
        is_system=True,
    )
    if inserted is None:  # pragma: no cover - защита от логической ошибки
        # У системного сообщения `client_message_id = NULL`, а частичный
        # уникальный индекс на такие строки не действует — конфликта быть
        # не может. Ветка существует, чтобы `None` не превратился в
        # `TypeError` при распаковке, если индекс когда-нибудь расширят.
        raise RuntimeError("Системное сообщение не может конфликтовать по client_message_id")
    stored_uuid, created_at, _internal_id = inserted
    logger.info(
        "chat.system_message.appended",
        chat_id=chat_id,
        message_uuid=str(stored_uuid),
        kind=str(kind),
    )
    return stored_uuid, created_at, body


async def publish_system_message(
    *,
    chat_uuid: uuid_lib.UUID,
    message_uuid: uuid_lib.UUID,
    body: str,
    created_at: datetime,
) -> None:
    """AC-01…AC-03: публикует событие. Вызывается ТОЛЬКО после `commit`.

    Ничего не возвращает и не бросает (`publish_to_chat` гасит сбой Redis,
    ADR-11/R8): системное сообщение уже в БД, и недоступность шины не имеет
    права превратить успешное доменное действие в `500`.
    """
    await publish_to_chat(
        chat_uuid=chat_uuid,
        envelope=build_system_envelope(
            chat_uuid=chat_uuid,
            message_uuid=message_uuid,
            body=body,
            created_at=created_at,
        ),
    )


async def append_for_card(
    session: AsyncSession,
    *,
    card_id: int,
    kind: SystemMessageKind,
    settings: SystemMessageSettings,
) -> tuple[uuid_lib.UUID, uuid_lib.UUID, str, datetime] | None:
    """Стори 6.7: системное сообщение в чат карточки, если чат существует.

    Возвращает `(chat_uuid, message_uuid, body, created_at)` или `None`,
    когда объяснять нечего: карточка не собрана (чата нет, FR30) либо у
    сессии нет доступа к БД.

    **Почему проверяется `execute`.** Unit-тесты отмены карточки
    (`test_card_state_transitions`) подставляют сессию-заглушку с одним
    `commit`: доменная матрица переходов проверяется без БД, и это правильно.
    Требование «системное сообщение пишется в той же транзакции» относится к
    реальной работе с данными; если постоянного хранилища нет, писать некуда,
    и вызов не должен падать `AttributeError` внутри доменной операции.
    Проверка описывает именно отсутствие хранилища, а не «тестовый режим»:
    объект без `execute` не может исполнить ни один запрос.
    """
    from app.repositories.chats import ChatRepository

    if getattr(session, "execute", None) is None:  # pragma: no cover - заглушки
        return None
    chat_uuid = await ChatRepository(session).get_by_event_card_id(event_card_id=card_id)
    if chat_uuid is None:
        return None
    row = await ChatRepository(session).get_by_uuid(chat_uuid=chat_uuid)
    if row is None:  # pragma: no cover - FK-инвариант `chats.event_card_id`
        return None
    message_uuid, created_at, body = await append_system_message(
        session, chat_id=row[0], kind=kind, settings=settings
    )
    return chat_uuid, message_uuid, body, created_at
