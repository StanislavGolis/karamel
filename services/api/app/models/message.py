"""Модель сообщения чата `messages` (стори 6.1; AC-03, AC-09).

AC-03 (FR85, FR86, NFR31): `body` — ШИФРТЕКСТ AES-256-GCM, plaintext в БД
не попадает никогда. Формат блоба — `nonce(12) ‖ ciphertext ‖ tag(16)`
(см. `app/core/crypto.py`): раздельные `nonce`/`tag` дали бы три колонки,
которые обязаны меняться вместе, а один блоб делает «половину шифртекста»
невозможной.

NFR33 (аудит): строки сообщений не удаляются логикой продукта; `ondelete`
стоит ради будущей чистки PII, а не ради штатного удаления.

Индекс `ix_messages_chat_created (chat_id, created_at, id)` — ровно ключ
чтения истории чата: страница истории фильтрует по `chat_id` и сортирует по
`(created_at, id)`. Порядок колонок повторяет `ORDER BY` дословно, иначе
PostgreSQL делает `Sort` в памяти на каждой странице.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# AC-03: минимальная длина корректного блоба — 12 байт nonce + 16 байт тега
# GCM. `CHECK` отбивает «пустое» тело на уровне БД, где ключа нет: проверить
# расшифровку БД не может, но «заведомо короче nonce+tag» видит.
_MIN_BODY_LENGTH = 28
# CR 6.1 (находка 3): ВЕРХНЯЯ граница. `BYTEA` без верхнего предела — это
# «сообщение на 200 МБ»: страница истории тянет блоб целиком в память,
# бэкапы и репликация раздуваются, и `CHECK` снизу это не остановит.
# Предел тот же, что у `crypto.MAX_BODY_BYTES` по плейнтексту: блоб =
# nonce(12) + ciphertext + tag(16), поэтому граница блоба на 28 байт больше.
# Значение ДУБЛИРУЕТСЯ здесь осознанно: Alembic-миграция не может
# импортировать `app.core.crypto` (миграции обязаны читать только `sa`),
# а расхождение ловится тестом схемы (`test_messages_body_upper_bound_check`).
_MAX_PLAINTEXT_BYTES = 65_535
_MAX_BODY_LENGTH = 12 + _MAX_PLAINTEXT_BYTES + 16


class Message(Base):
    """Сообщение чата. `body` — шифртекст, не текст."""

    __tablename__ = "messages"

    # D-2: внутренний PK (наружу не выходит; публичный адрес — `uuid`).
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # Стори 6.4 (AC-01, D-2): публичный адрес сообщения. Внутренний `id` не
    # выходит наружу никогда: клиент адресует сообщение по `uuid` (оптимистичное
    # добавление и квитанции 6.5), а `bigint` в ответе разрешил бы перебор
    # `/1..N` в обход состава компании.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # Стори 6.4 (AC-04, NFR18): клиентский идентификатор для идемпотентного
    # ретрая. Клиент генерирует его ДО отправки и повторяет запрос с тем же
    # значением после обрыва сети; `UNIQUE (chat_id, client_message_id)`
    # превращает повтор в чтение уже записанного сообщения.
    client_message_id: Mapped[uuid_lib.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # AC-03: FK на внутренний `id` чата (дешевле `uuid`). `CASCADE`: карточка
    # удаляется — её чат и переписка уходят вместе с ней.
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    )

    # AC-03: автор сообщения — существующий пользователь. `CASCADE` по той же
    # причине, что и у чата: FK без `ondelete` намертво заблокировал бы чистку
    # PII, которую NFR1–NFR4 решают политикой, а не барьером в схеме.
    #
    # Стори 6.7 (AC-04, X-6.6-5): `NULL` — у СИСТЕМНОГО сообщения автора нет.
    # До 6.7 колонка была `NOT NULL`, и системное сообщение было физически
    # невозможно (`IntegrityError`). Признак системности — отдельная колонка
    # `is_system` (D-6.7-1), а не вывод из `NULL`: см. `__table_args__`.
    sender_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Стори 6.7 (D-6.7-1): признак системного сообщения. `NOT NULL` с
    # `server_default` — существующие строки становятся `false` без
    # backfill-скрипта, а третье состояние (`NULL`) невозможно.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # AC-03, NFR31: шифртекст (nonce ‖ ct ‖ tag). Имя `body` сохранено по
    # контракту эпика; тип `LargeBinary` -> `BYTEA`.
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # AC-03: время создания с серверным `now()`. `onupdate` НЕТ намеренно:
    # сообщение не редактируется (FR86), и «обновлённое» время в истории
    # сбивало бы порядок.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Стори 6.5 (Task 6.1, AC-06): доставка — ОТДЕЛЬНЫЙ факт от прочтения.
    # `delivered_at` заполняется CAS-обновлением при получении события
    # участником; `read_at` появится в 6.6 и здесь его быть не должно
    # (`delivered ≠ read`). `NULL` — сообщение ещё никому не доставлено.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Стори 6.6 (Task 1, AC-02, AC-06; FR35, NFR12): ПРОЧТЕНИЕ — второй,
    # отдельный факт. `delivered_at` — «дошло до устройства», `read_at` —
    # «человек открыл чат». Слияние двух полей сделало бы невозможным ответ
    # «доставлено, но не прочитано», а именно он и есть смысл счётчика.
    # `NULL` — «ещё не читали», а не долг.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # AC-03: ключ чтения истории чата — фильтр + порядок одним индексом.
        Index("ix_messages_chat_created", "chat_id", "created_at", "id"),
        # CR 6.1 (находка 8): `sender_id` не покрыт `ix_messages_chat_created`
        # (ведущая колонка — `chat_id`), поэтому `ON DELETE CASCADE` от
        # `users` и FK-check делали бы seq scan по всей таблице сообщений.
        Index("ix_messages_sender_id", "sender_id"),
        # Стори 6.4 (AC-02, D-2): ключ чтения истории под ПУБЛИЧНЫЙ курсор
        # `(created_at, uuid)`. `ix_messages_chat_created` сортировку по `uuid`
        # не покрывает — на каждой странице был бы `Sort` в памяти.
        Index("ix_messages_chat_created_uuid", "chat_id", "created_at", "uuid"),
        # Стори 6.4 (AC-01, D-2): `uuid` — публичный адрес, значит уникален.
        UniqueConstraint("uuid", name="uq_messages_uuid"),
        # Стори 6.5 (Task 6.1): частичный индекс «недоставленных» строк.
        # Объявлен в модели ОБЯЗАТЕЛЬНО: без него `alembic check` видит
        # `remove_index` — миграция `0017` создаёт индекс, а модель о нём не
        # знает, и автогенерация следующей миграции предложила бы его удалить
        # (проверено: `alembic check` дал FAILED сразу после `upgrade head`).
        Index(
            "ix_messages_chat_undelivered",
            "chat_id",
            "delivered_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        # Стори 6.6 (Task 1.3, AC-02, AC-05; FR35): частичный индекс
        # НЕПРОЧИТАННЫХ. Обслуживает оба запроса: подсчёт `unread_count` в
        # списке чатов и `UPDATE ... WHERE read_at IS NULL` при отметке.
        #
        # Объявлен в модели ОБЯЗАТЕЛЬНО (урок 6.5): без него `alembic check`
        # видит `remove_index` — миграция `0018` индекс создаёт, модель о нём
        # не знает, и следующая автогенерация предложила бы его удалить
        # (проверено: FAILED сразу после `upgrade head`).
        Index(
            "ix_messages_chat_unread",
            "chat_id",
            "read_at",
            postgresql_where=text("read_at IS NULL"),
        ),
        # Стори 6.4 (AC-04, NFR18): идемпотентность отправки. Частичный
        # уникальный ИНДЕКС, а не `UniqueConstraint`: PostgreSQL не умеет
        # частичное ограничение, а безусловное запретило бы второе
        # системное сообщение чата с `NULL`-идентификатором (6.7).
        Index(
            "uq_messages_chat_client_id",
            "chat_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        # Стори 6.7 (D-6.7-1, AC-04): БИКОНДИЦИОНАЛ, а не одна сторона.
        # `is_system = true` ⟺ `sender_id IS NULL`: системное сообщение не
        # имеет автора, а обычное обязано его иметь. Односторонний CHECK
        # (`NOT is_system OR sender_id IS NULL`) разрешил бы обычное
        # сообщение без автора — то самое состояние, которое ломает
        # `own`-логику клиента и счётчик непрочитанных.
        CheckConstraint(
            "(is_system AND sender_id IS NULL) OR (NOT is_system AND sender_id IS NOT NULL)",
            name="system_sender_exclusive",
        ),
        # AC-03: тело заведомо короче nonce+tag — битый шифртекст, а не
        # сообщение. Имя идёт через конвенцию (`ck_messages_body_length`).
        CheckConstraint(
            f"octet_length(body) > {_MIN_BODY_LENGTH} AND octet_length(body) <= {_MAX_BODY_LENGTH}",
            name="body_length",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        """Строка для логов: БЕЗ `body` (шифртекст в лог не пишется)."""
        return f"<Message id={self.id} chat_id={self.chat_id} sender_id={self.sender_id}>"
