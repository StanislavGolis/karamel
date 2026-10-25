"""Обмен сообщениями в чате компании (стори 6.4; AC-01…AC-06).

**Тело сообщения шифруется ДО вставки** (NFR31, D-6.4-4). Шифрование живёт
здесь, а не в репозитории: репозиторий не должен знать про ключи, и «забыть
зашифровать» на новом пути записи стало бы невозможно — он просто не умеет
писать открытый текст. Формат и ключ — `app/core/crypto.py` (AES-256-GCM,
AAD `chat:{id}|sender:{id}`), ключ из `Settings.chat_message_key`, в БД не
попадает никогда.

**Гард доступа ОДИН на «чата нет» и «не участник»** (D-6.4-1): один класс
ошибки, одно тело `chat_not_found`, `404`. Ветка `403` отсутствует намеренно:
она подтвердила бы существование чата (FR86, ADR-13).

**Тайминг-оракул** (D-6.4-6, урок F-1 стори 6.3): число SQL-запросов на
«посторонний» и «чата нет» обязано совпадать. Поэтому запрос участия
выполняется ВСЕГДА — на not-found пути с заведомо несуществующим `card_id`.
Иначе посторонний отличал бы существующий чат по времени ответа.

**Идемпотентность отправки** (AC-04, NFR18): `ON CONFLICT (chat_id,
client_message_id) DO NOTHING` + дочитывание существующего. Ретрай после
обрыва сети возвращает `201` с ТЕМ ЖЕ `uuid` и не создаёт дубль.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import (
    decrypt_message_body,
    encrypt_message_body,
)
from app.repositories.chats import ChatRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.messages import MessageRepository
from app.schemas.ws import WsEnvelope
from app.services.message_cursor import decode_message_cursor, encode_message_cursor
from app.services.ws_publisher import publish_to_chat

logger = structlog.get_logger(__name__)

# Размер страницы истории: 30 — «экран целиком» без догрузки, 100 — предел
# выгрузки. Верхняя граница обязательна: без неё `?limit=100000` вернул бы
# всю переписку одним ответом и обнулил бы смысл курсора (D-6.4-3).
MESSAGES_PAGE_SIZE = 30
MAX_MESSAGES_PAGE_SIZE = 100

# AC-06, D-6.4-5: верхняя граница тела в СИМВОЛАХ (схема), а байтовый предел
# шифра (`MAX_BODY_BYTES = 65535`) остаётся вторым рубежом: кириллица в UTF-8
# занимает два байта на символ, и 4000 символов — это до 8000 байт.
MESSAGE_MAX_LENGTH = 4000

# D-6.4-6: заведомо несуществующий `card_id` для выравнивания числа запросов
# на пути «чата нет» (`event_cards.id` — IDENTITY, начинается с 1).
_MISSING_CARD_ID = 0


class ChatSettings(Protocol):
    """Минимум настроек сервиса: ключ шифрования переписки.

    Протокол, а не импорт `Settings`: unit-тест подставляет ключ без чтения
    `.env`. Поле объявлено явно — `getattr` с дефолтом молча подменил бы
    опечатку в имени и оставил переписку без ключа.
    """

    chat_message_key: str


class ChatNotFoundError(Exception):
    """AC-03 (ADR-13, D-6.4-1): чата нет ИЛИ зритель не участник — ответ один."""

    code = "chat_not_found"


class MessageCorruptedError(Exception):
    """AC-06, D-6.4-5: шифртекст в БД не расшифровывается.

    Это дефект ДАННЫХ на сервере (подмена байта, чужой ключ после ротации),
    а не ввод клиента: `422` обвинил бы пользователя в сообщении, которое он
    не отправлял, и подсказал бы «исправьте текст». `500` с машинным кодом
    честнее и не маскируется под ошибку формы.
    """

    code = "message_corrupted"


@dataclass(frozen=True)
class ChatMessage:
    """AC-01, AC-02: одно сообщение — доменный объект, не схема.

    `sender_uuid` — публичный идентификатор (D-2): внутренний `sender_id`
    живёт только в репозитории и в AAD шифра (он там и нужен).
    """

    uuid: uuid_lib.UUID
    # Стори 6.7 (AC-04): у СИСТЕМНОГО сообщения автора нет — `None`.
    # `null`, а не нулевой uuid: клиент различает по `is_system`, а
    # «странный» идентификатор заставил бы его гадать.
    sender_uuid: uuid_lib.UUID | None
    body: str
    created_at: datetime
    #: Стори 6.7 (AC-04): признак системного сообщения.
    is_system: bool = False


@dataclass(frozen=True)
class ChatMessagePage:
    """AC-02: страница истории — элементы и непрозрачный курсор.

    `next_cursor = None` означает «дальше ничего нет». Курсор собирается
    ТОЛЬКО при наличии следующей страницы: cursor на последней странице
    заставлял бы клиента делать лишний запрос, который вернёт пустоту.
    """

    items: list[ChatMessage]
    next_cursor: str | None
    #: X-6.5-5 (chore P-7): курсор последнего элемента страницы, всегда.
    #: `next_cursor` не заменяет его: на последней странице он `None`, и клиент
    #: не смог бы попросить «всё после того, что вижу» при реконнекте WS.
    last_cursor: str | None = None


@dataclass(frozen=True)
class _ChatAccess:
    """Результат гарда: чат и карточка, к которой он относится."""

    chat_id: int
    event_card_id: int
    card_uuid: uuid_lib.UUID


async def _resolve_chat(
    session: AsyncSession, *, chat_uuid: uuid_lib.UUID, viewer_id: int
) -> _ChatAccess:
    """AC-03: гард участия — ОДИН путь для отправки и чтения истории.

    Порядок и число запросов фиксированы (D-6.4-6):

    1. чат + `author_id` карточки одним JOIN'ом (без второго round-trip);
    2. запрос участия — ВСЕГДА, даже когда чата нет: `card_ids` берётся из
       найденного чата либо из `_MISSING_CARD_ID`. Иначе «посторонний» стоил
       бы на один запрос больше «несуществующего чата», и существование
       утекало бы по времени ответа (урок F-1 стори 6.3).

    Автор карточки — участник по определению (ADR-04): у него нет строки в
    `join_requests`, и признак `selected` для него всегда ложен. Проверка
    авторства ОБЯЗАТЕЛЬНА, иначе автор не смог бы писать в свой же чат.
    """
    found = await ChatRepository(session).get_by_uuid(chat_uuid=chat_uuid)
    card_id_for_lookup = found[1] if found is not None else _MISSING_CARD_ID

    # Второй запрос — безусловный (D-6.4-6). Возвращает подмножество карточек,
    # где зритель `selected`; на not-found пути заведомо пустое.
    participating = await JoinRequestRepository(session).viewer_participation_card_ids(
        applicant_id=viewer_id, card_ids=[card_id_for_lookup]
    )

    if found is None:
        raise ChatNotFoundError()

    chat_id, event_card_id, card_uuid, author_id = found
    # Автор карточки приходит ТЕМ ЖЕ JOIN'ом: отдельный запрос авторства
    # стоил бы лишнего round-trip ТОЛЬКО на существующем чате — и это сам по
    # себе тайминг-оракул (урок F-1 стори 6.3).
    if author_id != viewer_id and event_card_id not in participating:
        raise ChatNotFoundError()

    return _ChatAccess(chat_id=chat_id, event_card_id=event_card_id, card_uuid=card_uuid)


async def send_message(
    session: AsyncSession,
    *,
    viewer_id: int,
    chat_uuid: uuid_lib.UUID,
    body: str,
    client_message_id: uuid_lib.UUID | None,
    settings: ChatSettings,
    now: datetime | None = None,
) -> ChatMessage:
    """AC-01, AC-04: отправка сообщения участником компании.

    Порядок шагов — контракт:

    1. гард участия (`_resolve_chat`): посторонний получает `404` до любой
       записи и до шифрования — нечего шифровать для того, кто не в компании;
    2. шифрование (`encrypt_message_body`, NFR31): только шифртекст идёт в БД;
    3. идемпотентная вставка (`ON CONFLICT ... DO NOTHING`, AC-04);
    4. при конфликте — дочитывание существующего по `client_message_id`:
       клиент получает `201` с ТЕМ ЖЕ `uuid`, второго сообщения нет (NFR18);
    5. `commit` ДО ответа: данные обязаны быть видны следующему соединению
       сразу после `201` (иначе отправка «теряется» до следующего запроса).

    `now` — только для тестов порядка: в рабочем коде время ставит БД
    (`server_default=now()`), и подмена здесь разошлась бы с `created_at`
    строки.
    """
    access = await _resolve_chat(session, chat_uuid=chat_uuid, viewer_id=viewer_id)

    # AC-07 стори 8.2 (D-25): блокировка строки чата ДО вставки. Без неё
    # сообщение могло быть записано в диалог, который в этот же момент
    # скрывается жалобой: `_resolve_chat` увидел `active`, а `INSERT` прошёл
    # уже после `UPDATE ... status='hidden'`. Порядок захвата един (одна
    # строка), поэтому дедлока нет.
    #
    # CR 8.2 H-1: одной блокировки НЕ достаточно. `_resolve_chat` читает чат
    # со своим фильтром ДО `FOR UPDATE`, поэтому жалоба, закоммитившая скрытие
    # в этом промежутке, оставляла отправку с устаревшим «active» — сообщение
    # уходило в скрытый диалог (воспроизведено на двух соединениях). Статус
    # читается ПОД блокировкой, после `commit` жалобы: тогда он актуален.
    locked_status = await ChatRepository(session).lock_for_update(chat_id=access.chat_id)
    if locked_status != "active":
        # Тот же `404`, что у скрытого чата в `_resolve_chat`: существование
        # диалога и факт жалобы не раскрываются (ADR-13, NFR33).
        raise ChatNotFoundError()

    moment = now if now is not None else datetime.now(UTC)
    ciphertext = encrypt_message_body(
        body,
        chat_id=access.chat_id,
        sender_id=viewer_id,
        key=settings.chat_message_key,
    )
    message_uuid = uuid_lib.uuid4()

    messages = MessageRepository(session)
    inserted = await messages.insert_if_absent(
        chat_id=access.chat_id,
        sender_id=viewer_id,
        body=ciphertext,
        message_uuid=message_uuid,
        client_message_id=client_message_id,
        # Стори 6.7: пользовательское сообщение системным не является.
        # Явно, а не «по умолчанию»: `CHECK`-инвариант связывает `is_system`
        # с `sender_id`, и умолчание скрыло бы рассинхрон при будущей правке.
        is_system=False,
    )

    if inserted is None:
        # AC-04: сообщение с этим `client_message_id` уже есть — ретрай после
        # обрыва. Ошибки НЕТ: дочитываем существующее и отдаём тот же ответ.
        if client_message_id is None:  # pragma: no cover - защита от логической ошибки
            raise MessageCorruptedError()
        existing = await messages.get_by_client_id(
            chat_id=access.chat_id, client_message_id=client_message_id
        )
        if existing is None:  # pragma: no cover - гонка с удалением
            raise ChatNotFoundError()
        stored_uuid, stored_created_at, stored_body, stored_sender_id = existing
        # CR 6.4, находка F-1 (AC-04, NFR18): отдаётся СОХРАНЁННЫЙ текст.
        # Прежний код возвращал `body` нового запроса — под одним `uuid`
        # клиент и собеседник видели РАЗНЫЕ сообщения, и расхождение нельзя
        # было заметить без сверки с БД. Расшифровка идёт ключом и AAD той
        # строки, что уже лежит в БД; при повреждении — явная ошибка.
        try:
            stored_body_text = decrypt_message_body(
                stored_body,
                chat_id=access.chat_id,
                sender_id=stored_sender_id,
                key=settings.chat_message_key,
            )
        except (InvalidTag, ValueError) as exc:
            logger.error(
                "chat_message.corrupted",
                chat_uuid=str(chat_uuid),
                message_uuid=str(stored_uuid),
            )
            raise MessageCorruptedError() from exc
        sender_uuid = await _sender_uuid(session, viewer_id=stored_sender_id)
        logger.info(
            "chat_message.idempotent_replay",
            chat_uuid=str(chat_uuid),
            message_uuid=str(stored_uuid),
        )
        return ChatMessage(
            uuid=stored_uuid,
            sender_uuid=sender_uuid,
            body=stored_body_text,
            created_at=stored_created_at,
            is_system=False,
        )

    stored_uuid, stored_created_at, _internal_id = inserted
    sender_uuid = await _sender_uuid(session, viewer_id=viewer_id)
    await session.commit()

    # Стори 6.5 (Task 4.1, AC-04, ADR-11): публикация ТОЛЬКО после commit.
    # Порядок — контракт, а не стиль: событие о незакоммиченных данных
    # показало бы участнику сообщение, которого ещё нет в БД, и следующий
    # `GET` вернул бы пустоту (гонка чтения). Порядок доказывается перехватом
    # (W5 TD 6.5) И видимостью из второго соединения (W4) — ассерт состояния
    # его не доказывает.
    await publish_to_chat(
        chat_uuid=chat_uuid,
        envelope=WsEnvelope.build(
            "message.created",
            {
                "message_uuid": str(stored_uuid),
                "chat_uuid": str(chat_uuid),
                "sender_uuid": str(sender_uuid),
                # Стори 6.7 (AC-04): поле есть ВСЕГДА, чтобы клиент не
                # угадывал системность по отсутствию ключа.
                "is_system": False,
                # Расшифровка на сервере: ключ не покидает сервер (AC-01).
                "body": body,
                "created_at": stored_created_at.isoformat().replace("+00:00", "Z"),
            },
        ),
    )

    # Лог БЕЗ тела и без `client_message_id`: первое — ПД переписки (152-ФЗ),
    # второе — клиентский идентификатор устройства.
    logger.info(
        "chat_message.sent",
        chat_uuid=str(chat_uuid),
        message_uuid=str(stored_uuid),
        sender_uuid=str(sender_uuid),
        at=moment.isoformat(),
    )
    return ChatMessage(
        uuid=stored_uuid,
        sender_uuid=sender_uuid,
        body=body,
        created_at=stored_created_at,
        is_system=False,
    )


async def _sender_uuid(session: AsyncSession, *, viewer_id: int) -> uuid_lib.UUID:
    """Публичный `uuid` отправителя (D-2: внутренний `id` не выходит наружу)."""
    from sqlalchemy import select

    from app.models.user import User

    result = await session.execute(select(User.uuid).where(User.id == viewer_id))
    value = result.scalar_one()
    return value if isinstance(value, uuid_lib.UUID) else uuid_lib.UUID(str(value))


async def list_messages(
    session: AsyncSession,
    *,
    viewer_id: int,
    chat_uuid: uuid_lib.UUID,
    cursor: str | None = None,
    limit: int = MESSAGES_PAGE_SIZE,
    settings: ChatSettings,
) -> ChatMessagePage:
    """AC-02, AC-06: страница истории компании.

    «Без лимита по количеству» (FR31) — это отсутствие верхней границы на
    ЧИСЛО сообщений в чате, а не «вся история одним ответом» (D-6.4-3):
    страница фиксированного размера, история листается курсором до `null`.
    Иначе 10 000 сообщений тянулись бы в память и в один HTTP-ответ.

    Курсор разбирается ДО запроса: битое значение обязано стоить ноль
    запросов к БД (`422 invalid_cursor`, а не `500`).

    Расшифровка — ПОСЛЕ выборки, по строкам: ключ нужен только для текста,
    а `body` в репозитории остаётся байтами. Битая строка валит страницу
    `MessageCorruptedError` (AC-06): тихий мусор в переписке хуже явной
    ошибки, потому что человек прочитал бы чужой/испорченный текст как свой.
    """
    access = await _resolve_chat(session, chat_uuid=chat_uuid, viewer_id=viewer_id)

    after = decode_message_cursor(cursor) if cursor is not None else None
    effective_limit = max(1, min(limit, MAX_MESSAGES_PAGE_SIZE))

    rows = await MessageRepository(session).list_history(
        chat_id=access.chat_id,
        after=after,
        # +1 строка: лишняя — признак «есть ещё». Без неё пришлось бы делать
        # второй запрос на существование следующей страницы.
        limit=effective_limit + 1,
    )
    has_more = len(rows) > effective_limit
    page_rows = rows[:effective_limit]

    items: list[ChatMessage] = []
    for message_uuid, sender_id, sender_uuid, ciphertext, created_at in page_rows:
        # Стори 6.7 (AC-04, D-6.7-3): `sender_id IS NULL` — системное сообщение.
        # AAD детерминирован доменом `system` (`None` → `sender:system`): без
        # него расшифровка системной строки падала бы `InvalidTag` на КАЖДОЙ
        # странице истории, то есть чат становился бы нечитаемым.
        # CR 6.7 (A-1): `None` (системное) уходит в AAD как домен «system»;
        # подстановка числа 0 сделала бы его неотличимым от пользователя `id=0`.
        aad_sender_id = sender_id
        try:
            # `sender_id` приходит из ТОЙ ЖЕ строки истории (JOIN к `users`),
            # а не отдельным запросом: обратный переход `uuid -> id` на
            # каждое сообщение дал бы N+1 на странице до 100 строк.
            body = decrypt_message_body(
                ciphertext,
                chat_id=access.chat_id,
                sender_id=aad_sender_id,
                key=settings.chat_message_key,
            )
        except (InvalidTag, ValueError) as exc:
            logger.error(
                "chat_message.corrupted",
                chat_uuid=str(chat_uuid),
                message_uuid=str(message_uuid),
            )
            raise MessageCorruptedError() from exc
        items.append(
            ChatMessage(
                uuid=message_uuid,
                sender_uuid=sender_uuid,
                body=body,
                created_at=created_at,
                # Признак выводится из `sender_id IS NULL`: это ровно тот
                # инвариант, который держит `CHECK ck_messages_system_sender_exclusive`.
                is_system=sender_id is None,
            )
        )

    next_cursor = None
    # X-6.5-5 (chore P-7): курсор последнего элемента страницы — ВСЕГДА, когда
    # страница непустая. `next_cursor` для этого не годится: он `None` на
    # последней странице, и клиент после реконнекта WS не смог бы попросить
    # «всё, что появилось после того, что я вижу». Формат курсора остаётся
    # непрозрачным для клиента: он возвращает то же значение, что получил.
    last_cursor = None
    if items:
        last = items[-1]
        last_cursor = encode_message_cursor(last.created_at, last.uuid)
        if has_more:
            next_cursor = last_cursor

    return ChatMessagePage(items=items, next_cursor=next_cursor, last_cursor=last_cursor)
