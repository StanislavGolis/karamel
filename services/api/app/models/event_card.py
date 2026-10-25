"""Модель карточки события `event_cards` (стори 4.1; AC-01…AC-06).

AC-01 (FR69): карточка хранит «что, когда, где, сколько человек, окно решения
и описание». Всё, что относится к месту, живёт в `places` (3.1): `place_id` —
FK, а не свободный текст. Координат нет ни в каком виде — местоположение
карточки это ссылка на проверенное место (NFR34).

D-2: `id` — внутренний bigint, наружу не выходит никогда; публичный
идентификатор — `uuid`.

D-1 (AC-06, ADR-12): `category` — `String(32)` + CHECK из `PlaceCategory`,
того же класса, что у `places.category`. Отдельный enum разошёлся бы при
первом добавлении значения: карточка принимала бы категорию, которой нет
у места, и фильтр ленты (4.5) не нашёл бы ни одного совпадения.

D-3: статусы — `String(16)` + CHECK из `StrEnum`, не нативные PG-enum:
`ALTER TYPE ... ADD VALUE` не откатывается в транзакции, а `CHECK`
переписывается одной миграцией.

**D-4.1-1:** `btrim`-CHECK для `title` НЕ ставится. У `places` он есть из-за
реального дефекта дублей (CR 3.1, патч 8), но там `name` участвует
в уникальности. У карточки `title` не уникален: `'   '` нормализуется
`normalize_text` до пустой строки и отбивается `EventCardCreate.min_length=1`.
CHECK дал бы `500` на вставке из воркера без пользы для контракта.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import (
    FEED_STATUSES,
    CardVerificationStatus,
    EventCardStatus,
    PlaceCategory,
)

# AC-03 (FR70): режим «заявки» — это ровно вместимость 1. Отдельного поля
# `mode` нет намеренно: два источника правды разошлись бы при первой правке
# вместимости.
CARD_REQUEST_MODE_CAPACITY = 1
# AC-01, ADR-10: пределы вместимости и окна решения (FR70, FR79). Дублируются
# в CHECK осознанно: схема Pydantic отбивает `422` на вводе, CHECK держит
# инвариант для вставок сырым SQL и из воркера.
CARD_MIN_CAPACITY = 1
CARD_MAX_CAPACITY = 6
CARD_MIN_DECISION_WINDOW_MINUTES = 15
CARD_MAX_DECISION_WINDOW_MINUTES = 10_080


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений StrEnum.

    AC-06, D-3: список не дублируется строкой ни здесь, ни в миграции.
    Иначе добавление значения в `EventCardStatus` не изменило бы ограничение
    в БД, и статус, прошедший Pydantic, отвергался бы PostgreSQL.

    Третья копия функции (`user.py`, `place.py`, здесь) — долг в
    `deferred-work.md`: вынос в общий модуль затронул бы две закрытые стори.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class EventCard(TimestampMixin, Base):
    """Карточка события сообщества (AC-01).

    Жизненный цикл (`EventCardStatus`): `draft` → `published` →
    (`full` | `closed`) → `finished` | `cancelled`. Переходы реализуют 4.2
    и 4.6; здесь только набор значений и инварианты, которые обязаны
    держаться на уровне БД.

    Модерация (`verification_status`, ADR-08) отделена от публикации:
    карточка может быть `draft` с `verification_status='approved'` (проверена,
    но автор ещё не опубликовал) — это штатное состояние, а не ошибка.
    """

    __tablename__ = "event_cards"

    # AC-01, D-2: внутренний PK, наружу не выходит.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # AC-01: публичный идентификатор. `default` в Python нужен, чтобы объект
    # был готов до flush; `server_default` — для вставок вне ORM.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid_lib.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # AC-01: автор. `ON DELETE CASCADE` — удаление учётки уносит её карточки:
    # карточка без автора не может ни закрыться, ни отмениться (FR74/FR100).
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # AC-04, FR98: место из активного справочника своего сообщества.
    # `ON DELETE RESTRICT` — физическое удаление места с карточкой запрещено;
    # «скрыть» место (FR68) работает через `places.is_active`, а не DELETE.
    place_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("places.id", ondelete="RESTRICT"), nullable=False
    )
    # AC-06, ADR-01: срез сообщества — колонка-строка, FK на `communities` нет
    # (таблицы не существует). Значение сверяет сервис 4.2 с
    # `Settings.community_slugs`; CHECK держит форму для вставок вне ORM.
    community_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    # AC-01: «что». Предел 100 — карточка показывается в ленте одной строкой.
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    # AC-01: «описание». NULL допустим — карточка без описания легальна.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AC-06, ADR-12: категория из общего белого списка с местами.
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    # AC-02, ADR-16: «когда». `timestamptz`, конвертация пояса — на входе
    # (схема `EventCardCreate`), в БД всегда UTC.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # AC-01, FR70: вместимость 1..6; режим определяется этим значением.
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # AC-01, FR79: окно решения в минутах. Для режима «заявки» обязательно,
    # для открытого набора приходит из конфига (OQ-1: 2880 = 48 ч).
    decision_window_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # AC-01, ADR-02: момент закрытия окна решения.
    # D-4.1-2: `NOT NULL` с создания — 4.2 вычислит поле при вставке и
    # ПЕРЕСЧИТАЕТ при публикации (`LEAST(starts_at, now() + окно)`).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # AC-01, FR73/74/100: состояние карточки.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=EventCardStatus.DRAFT,
        server_default=text("'draft'"),
    )
    # AC-01, ADR-10: занятые места. `server_default=0` — новая карточка пуста.
    filled_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    # AC-01, ADR-08, FR101: результат проверки контента.
    verification_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CardVerificationStatus.PENDING,
        server_default=text("'pending'"),
    )
    # AC-01, FR113: причина отклонения. Обязательна при `rejected` — это
    # держит CHECK `ck_event_cards_rejection_reason`, а не код.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AC-01, ADR-08: когда и кем проверена. `verified_by IS NULL` =
    # автоматическая проверка (у неё нет пользователя-модератора).
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # AC-01, ADR-02: момент публикации. Обязателен при `published` — CHECK
    # `ck_event_cards_published`. Без него нельзя отличить «опубликована»
    # от «статус выставлен вручную в обход перехода».
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AC-01, FR74: момент ручного закрытия набора.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AC-01, ADR-15: маркер идемпотентности «приглашение на явку отправлено».
    attendance_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AC-01, FR103, ADR-15: маркер идемпотентности напоминания автору об окне.
    decision_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AC-01 стори 5б.2 (D-5b.2-1, FR30, FR78): идентификатор чата компании.
    # Заполняется В МОМЕНТ ВЫБОРА автором (`select`), вместе с `full` — одним
    # `UPDATE`. Таблицы `chats` здесь нет: она эпик 6 (арх. §496), и контракт
    # для неё — `chats.uuid = event_cards.chat_uuid` (НЕ генерировать новое
    # значение). Частичный `uq` (`WHERE chat_uuid IS NOT NULL`) держит
    # однозначность: две карточки не могут делить чат, а `NULL` не занимает
    # индекс — открытые карточки его не касаются.
    chat_uuid: Mapped[uuid_lib.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    # AC-03 (FR70): режим вычисляется из вместимости. Отдельного столбца нет
    # намеренно — иначе `capacity=1` и «открытый набор» могли бы сосуществовать
    # и код 4.2 ветвился бы по двум расходящимся признакам.
    @property
    def is_request_mode(self) -> bool:
        """`True` — режим «заявки» (вместимость 1), `False` — открытый набор."""
        return self.capacity == CARD_REQUEST_MODE_CAPACITY

    __table_args__ = (
        # AC-01: `PrimaryKeyConstraint` объявлен явно, чтобы имя строилось по
        # convention (`pk_event_cards`) и совпадало с миграцией `0008`.
        PrimaryKeyConstraint("id"),
        # AC-01: публичный идентификатор уникален — адресация им однозначна.
        # Имя ПОЛНОЕ: у конвенции `uq` нет токена `%(constraint_name)s`, поэтому
        # короткое `name="uuid"` дало бы `uuid` без префикса и разошлось бы
        # с миграцией (дефект 3.1, см. `models/place.py`).
        UniqueConstraint("uuid", name="uq_event_cards_uuid"),
        # AC-01: состояния — только значения `EventCardStatus`.
        CheckConstraint(_in_clause("status", tuple(EventCardStatus)), name="status"),
        # AC-01 стори 5б.2 (D-5b.2-1): `chat_uuid` уникален, но ЧАСТИЧНО.
        # Безусловный `UNIQUE` в PostgreSQL допускает много `NULL`, поэтому
        # разницы в поведении нет — предикат нужен ради ИМЕНИ и ради явного
        # контракта эпику 6: индекс не занимается открытыми карточками,
        # и вставка чата не наткнётся на `NULL`-строки. Имя ПОЛНОЕ (дефект 3.1).
        Index(
            "uq_event_cards_chat_uuid",
            "chat_uuid",
            unique=True,
            postgresql_where=text("chat_uuid IS NOT NULL"),
        ),
        # AC-01, ADR-08: исход проверки контента — только `CardVerificationStatus`.
        CheckConstraint(
            _in_clause("verification_status", tuple(CardVerificationStatus)),
            name="verification_status",
        ),
        # AC-06, ADR-12: категория — тот же список, что у `places.category`.
        CheckConstraint(_in_clause("category", tuple(PlaceCategory)), name="category"),
        # AC-01, FR70: вместимость 1..6. Верхняя граница — продуктовый предел
        # компании (2–6 человек, FR85), нижняя — режим заявок.
        CheckConstraint(
            f"capacity BETWEEN {CARD_MIN_CAPACITY} AND {CARD_MAX_CAPACITY}",
            name="capacity",
        ),
        # AC-05, ADR-10: занятые места не выходят за вместимость. Без этого
        # `filled_count` мог бы превысить `capacity`, и индикатор мест в ленте
        # показал бы отрицательный остаток.
        CheckConstraint("filled_count BETWEEN 0 AND capacity", name="filled_count"),
        # AC-01, FR79: окно решения 15 минут .. 7 суток.
        CheckConstraint(
            f"decision_window_minutes BETWEEN {CARD_MIN_DECISION_WINDOW_MINUTES}"
            f" AND {CARD_MAX_DECISION_WINDOW_MINUTES}",
            name="decision_window",
        ),
        # AC-01, ADR-02: окно не может закрыться позже начала события.
        # `<=`, не `<`: окно «до самого начала» легально и означает «решение
        # принимается до последней минуты».
        CheckConstraint("expires_at <= starts_at", name="expires_at"),
        # AC-06, ADR-01: тот же шаблон слага, что у `places` и в конфиге.
        # Три копии намеренно: конфиг проверяет `.env` до подключения к БД,
        # CHECK держит инвариант для всех путей записи.
        CheckConstraint("community_slug ~ '^[a-z0-9_-]{3,64}$'", name="community_slug"),
        # AC-05, ADR-02: опубликованная карточка обязана иметь момент публикации.
        # Иначе `expires_at` нечем объяснить, а лента (4.5) не может показать
        # «опубликовано тогда-то».
        CheckConstraint(
            f"status <> '{EventCardStatus.PUBLISHED}' OR published_at IS NOT NULL",
            name="published",
        ),
        # AC-05, FR113: отклонение без причины запрещено — автор не поймёт,
        # что править. Дублирует обязательность поля в схеме 4.3 осознанно:
        # модератор может отклонить карточку и из админского скрипта.
        CheckConstraint(
            f"verification_status <> '{CardVerificationStatus.REJECTED}'"
            " OR rejection_reason IS NOT NULL",
            name="rejection_reason",
        ),
        # AC-05, ADR-10: при полной вместимости статус `published` запрещён —
        # карточка обязана перейти в `full`/`closed`/`finished`/`cancelled`.
        # Без этого лента показывала бы «свободных мест 0» как обычную карточку,
        # и фильтр `has_seats` (4.5) опирался бы на арифметику, а не на статус.
        # AC-07 (X-35, стори 4.6): ограничение ДВУСТОРОННЕЕ. Прежняя редакция
        # (4.1) запрещала только `published` при полной вместимости и пропускала
        # обратное — `full` со свободным местом. Тогда `has_seats=true` (4.5)
        # отдавал карточку «есть места» со статусом «собрана»: два правила об
        # одном факте расходились (дефект X-35).
        #
        # Обе половины нужны вместе: первая держит «нет `published` без мест»,
        # вторая — «`full` только при полной вместимости». Только вторая
        # ослабила бы 4.1 и позволила бы `published` с `filled_count = capacity`.
        CheckConstraint(
            "(filled_count < capacity OR status IN ('full','closed','finished','cancelled'))"
            f" AND (status <> '{EventCardStatus.FULL}' OR filled_count = capacity)",
            name="full_capacity",
        ),
        # AC-03, FR70: режим заявок занимает ровно одно место. Проверка
        # структурная, а не арифметическая: `filled_count <= capacity` уже
        # есть, но при `capacity = 1` дополнительно фиксируется, что «заявок»
        # не может быть больше одной — иначе автовыбор (FR77) выбирал бы из двух.
        CheckConstraint(
            f"capacity <> {CARD_REQUEST_MODE_CAPACITY} OR filled_count <= 1",
            name="request_mode",
        ),
        # Стори 4.4 (AC-01, AC-04, FR71, NFR30): подсчёт активных карточек
        # автора под `FOR UPDATE users`. Существующий
        # `ix_event_cards_author_active WHERE status IN ('draft','published')`
        # для этого предиката не подходит: `full`/`closed` в него не входят,
        # и планировщик читал бы таблицу вместо индекса. Имя ПОЛНОЕ — защита
        # от переименования таблицы (дефект 3.1, короткое имя `UniqueConstraint`).
        Index(
            "ix_event_cards_author_publishable",
            "author_id",
            postgresql_where=text("status IN ('published','full','closed')"),
        ),
        # Стори 4.4 (X-16): FK `places` с `ON DELETE RESTRICT` проверяется
        # при каждом удалении места, а без индекса PostgreSQL делает `Seq Scan`
        # по `event_cards`. Закрывается здесь же: миграция уже создаётся этой
        # стори (OQ-5), отдельная ревизия стоила бы ещё один цикл на проде.
        Index("ix_event_cards_place_id", "place_id"),
        # AC-07: лента (4.5) сортирует по `starts_at` в срезе сообщества
        # и фильтрует по статусу. Порядок колонок: равенство → сортировка →
        # фильтр (тот же принцип, что у `ix_places_community_active`).
        Index("ix_event_cards_community_starts", "community_slug", "starts_at", "status"),
        # Стори 6.1 (AC-06, долг X-5b.1-1): предикат окна ленты. Лента
        # фильтрует `status IN FEED_STATUSES` (published/full) и сортирует
        # `(starts_at, uuid)`. Существующий `ix_event_cards_community_starts`
        # НЕ покрывает: `status` стоит третьей колонкой, а нужен предикат
        # (иначе в индекс попадают архивные карточки) и `uuid` в порядке
        # (иначе `Sort` в памяти на каждой странице).
        Index(
            "ix_event_cards_feed_visible",
            "community_slug",
            "starts_at",
            "uuid",
            # CR 6.1 (находка 7): предикат собирается из доменной константы,
            # а не пишется строкой. Добавление статуса в `FEED_STATUSES`
            # обязано менять и индекс, иначе он молча перестаёт покрывать
            # ленту (Sort в памяти), а `alembic check` этого не видит.
            postgresql_where=text(
                "status IN (" + ",".join(f"'{status.value}'" for status in FEED_STATUSES) + ")"
            ),
        ),
        # AC-07, FR71/NFR30: лимит активных карточек автора (4.4). Частичный —
        # архивных карточек у автора много, активных 1–2.
        Index(
            "ix_event_cards_author_active",
            "author_id",
            postgresql_where=text("status IN ('draft','published')"),
        ),
        # AC-07, FR113; стори 4.3 (AC-09, D-4.3-8): очередь модератора.
        # Ключ `(created_at, uuid)` — РОВНО ключ сортировки и курсора
        # (`created_at ASC, uuid ASC`). Одного `created_at` недостаточно:
        # при равных метках порядок не определён, и `WHERE (created_at, uuid) >`
        # терял бы или дублировал строки между страницами. `uuid`, а не `id`:
        # курсор публичен и внутренний bigint в него попадать не должен.
        Index(
            "ix_event_cards_pending_verification",
            "created_at",
            "uuid",
            postgresql_where=text("verification_status = 'pending'"),
        ),
        # AC-05, NFR20 (стори 4.6): архив по наступлении `starts_at`. Воркер
        # `card_windows` прогоняется раз в 60 с и обязан не сканировать таблицу
        # целиком: предикат `starts_at <= now()` + три статуса. Частичный —
        # архивных `finished` со временем в карточке много, переводить их
        # повторно незачем.
        Index(
            "ix_event_cards_started",
            "starts_at",
            postgresql_where=text("status IN ('published','full','closed')"),
        ),
        # AC-07, NFR20: воркер окон выбирает только просроченные `published`.
        Index(
            "ix_event_cards_due_windows",
            "expires_at",
            postgresql_where=text("status = 'published'"),
        ),
        # AC-07, NFR22: воркер явки выбирает карточки, где пора спрашивать явку.
        Index(
            "ix_event_cards_prompt_due",
            "starts_at",
            postgresql_where=text(
                "attendance_prompted_at IS NULL AND status IN ('full','closed','finished')"
            ),
        ),
    )
