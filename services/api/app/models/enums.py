"""Доменные перечисления (Task 3.1).

D-3: в БД это `text` + `CHECK`, а не нативные PG-enum: изменение набора значений
в PG-enum требует `ALTER TYPE`, а `text + CHECK` меняется одной миграцией.
В Python значения валидируются этими `StrEnum`.
"""

from enum import StrEnum


class UserStatus(StrEnum):
    """AC-01: статус учётной записи."""

    ACTIVE = "active"
    WARNED = "warned"
    BANNED = "banned"


class UserRole(StrEnum):
    """AC-01: роль. Модератор — единственная привилегированная роль продукта."""

    USER = "user"
    MODERATOR = "moderator"


class AuthProvider(StrEnum):
    """AC-01: способ входа. `phone` — SMS, остальные — OAuth."""

    YANDEX = "yandex"
    VK = "vk"
    PHONE = "phone"


class Gender(StrEnum):
    """AC-01: пол. NULL до заполнения анкеты (стори 2.x)."""

    FEMALE = "female"
    MALE = "male"


class PhotoStatus(StrEnum):
    """AC-01 стори 2.2: статус фото в очереди модерации.

    D-3: `text` + `CHECK` в БД, не PG-enum — набор значений меняется
    одной миграцией, без `ALTER TYPE`. Список значений этого класса
    подставляется в `CHECK` модели, поэтому дубля строк нет.

    Уникальность трёх значений важна: частичный индекс очереди
    (`WHERE status = 'pending'`) опирается ровно на `pending`.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlaceCategory(StrEnum):
    """AC-02 (ADR-12): белый список категорий мест — единственный источник.

    Почему `text` + `CHECK`, а не справочная таблица и не нативный PG-enum
    (три причины ADR-12):

    1. Таблица категорий — это `join` на восемь строк в каждом списке мест;
       выигрыша нет, а «место без категории» становится возможным через
       забытый `INNER JOIN`.
    2. Свободный текст ломает фильтр: `'кафе'`, `'cafe'` и `'Кафе'` — три
       разных категории для `WHERE category = ...`, и выпадающий список
       в админке вырастает из мусора.
    3. Нативный PG-enum требует `ALTER TYPE ... ADD VALUE`, который нельзя
       откатить в транзакции. Миграция на `text + CHECK` переписывает одно
       ограничение и откатывается.

    Список значений подставляется в `CHECK` модели и в миграцию из этого
    класса: добавление категории — одна миграция, без правки роутера
    (AC-02, «новое значение требует миграции, а не правки роутера»).
    """

    CAFE = "cafe"
    CINEMA = "cinema"
    QUIZ = "quiz"
    SKATING = "skating"
    PARK = "park"
    SPORT = "sport"
    WALK = "walk"
    OTHER = "other"


class EventCardStatus(StrEnum):
    """AC-01 (FR69, FR73, FR74, FR100): состояние карточки события.

    D-3: `text` + `CHECK`, не нативный PG-enum — `ALTER TYPE ... ADD VALUE`
    нельзя откатить в транзакции, а миграция на `CHECK` переписывает одно
    ограничение и откатывается целиком.

    На конкретные значения опираются частичные индексы:
    `ix_event_cards_author_active WHERE status IN ('draft','published')`
    и `ix_event_cards_due_windows WHERE status = 'published'`. Переименование
    значения без правки индекса даёт индекс, который никогда не совпадает,
    и воркер молча не находит просроченные окна (NFR20).

    `closed` — автор закрыл набор вручную (FR74); `full` — места кончились
    сами (ADR-10); `finished` — наступило `starts_at` (FR73); `cancelled` —
    автор отменил (FR100). `closed` и `full` различимы намеренно: причина
    остановки видна участнику.
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    FULL = "full"
    CLOSED = "closed"
    FINISHED = "finished"
    CANCELLED = "cancelled"


# Стори 4.4, Task 3 (AC-01, FR71, FR74, FR100): статусы, в которых карточка
# ЗАНИМАЕТ слот автора. Ровно те, где карточка видна в ленте и встреча ещё не
# завершена. Слот освобождают только терминальные `finished` (FR73) и
# `cancelled` (FR100); `closed` — набор закрыт вручную, но встреча состоится,
# поэтому слот НЕ освобождает, а `draft` невидим в ленте (FR99).
#
# Константа живёт здесь, а не в сервисе: репозиторий (`repositories/event_cards.py`)
# импортирует её для COUNT, а сервис уже импортирует репозиторий — определение
# в сервисе дало бы цикл импорта на старте (решение Task 3 стори 4.4).
ACTIVE_STATUSES: tuple[EventCardStatus, ...] = (
    EventCardStatus.PUBLISHED,
    EventCardStatus.FULL,
    EventCardStatus.CLOSED,
)


class CardVerificationStatus(StrEnum):
    """AC-01 (FR101, FR113, ADR-08): результат проверки контента карточки.

    `PhotoStatus` НЕ переиспользуется (ADR-08: «пайплайн фото не трогается»):
    наборы значений совпадают, но пайплайны разные, и общий enum связал бы
    две независимые миграции — правка статусов модерации фото меняла бы
    ограничение карточек.

    На `pending` опирается `ix_event_cards_pending_verification WHERE
    verification_status = 'pending'` — очередь модератора (FR113) без
    сканирования всех карточек.

    NULL-значения нет: «не проверялась» и «проверка не требуется» — это
    `pending` и `approved` соответственно, и различать их третьим состоянием
    незачем (ADR-08: автопроверка первой ставит исход сама).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# AC-01 стори 4.2, D-4.2-4: статусы, в которых карточка принимает правку.
# `draft` не входит намеренно: черновик правится отдельной ветвью (у него нет
# проверенного контента), и общий список смешал бы две разные процедуры.
EDITABLE_STATUSES: tuple[EventCardStatus, ...] = (
    EventCardStatus.PUBLISHED,
    EventCardStatus.FULL,
)

# AC-01 стори 4.5: статусы, видимые в ленте. ПОЛОЖИТЕЛЬНЫЙ список, а не
# `status != 'draft'` (AC-01, тест `test_feed_statement_uses_only_published_and_full`):
# инверсия впустила бы в ленту `closed`, `finished` и `cancelled` — набор
# закрытых и отменённых встреч стал бы выглядеть как приглашение.
FEED_STATUSES: tuple[EventCardStatus, ...] = (
    EventCardStatus.PUBLISHED,
    EventCardStatus.FULL,
)

# AC-05 стори 4.6 (FR73): статусы, которые воркер переводит в архив по времени
# (`starts_at <= now()`). Источник и для воркера, и для предиката `finished`.
# `cancelled` не входит: отменённое не архивируется, а `draft` — ещё не
# публиковалось (правило публикации к нему не применяется).
ARCHIVABLE_STATUSES: tuple[EventCardStatus, ...] = (
    EventCardStatus.PUBLISHED,
    EventCardStatus.FULL,
    EventCardStatus.CLOSED,
)


class JoinRequestStatus(StrEnum):
    """AC-02 (FR76, FR77, FR82; D-3): состояние заявки на участие.

    D-3: `text` + `CHECK` в БД, не нативный PG-enum — `ALTER TYPE ... ADD VALUE`
    нельзя откатить в транзакции, а миграция на `CHECK` переписывает одно
    ограничение и откатывается целиком.

    На конкретные значения опираются два частичных индекса, и переименование
    значения без правки индекса даёт индекс, который никогда не совпадает
    (класс дефекта `EventCardStatus`):

    - `uq_join_requests_card_applicant_active WHERE status IN ('pending','selected')`
      — одна активная заявка на пару «карточка + заявитель»;
    - `ix_join_requests_applicant_pending WHERE status = 'pending'`
      — подсчёт лимита 2 активных заявок и «мои заявки».

    Терминальные состояния (`not_selected`, `expired`, `cancelled`) освобождают
    слот лимита (ADR-09, NFR29): отмена до решения — легальное действие (FR82),
    и повторная подача после неё разрешена.

    `not_selected` никогда не отдаётся заявителю (FR81): строка остаётся
    в БД ради аудита (NFR1–NFR4), а фильтрация — в репозитории (5а.3).
    """

    PENDING = "pending"
    SELECTED = "selected"
    NOT_SELECTED = "not_selected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# Стори 5а.1, Task 1.3 (AC-04, FR77, NFR29): статусы, которые ЗАНИМАЮТ слот
# лимита «не больше двух активных заявок». Ровно `pending`: `selected` — уже
# решение, `not_selected`/`expired`/`cancelled` — терминальные. Отмена
# освобождает слот (FR82, ADR-09) — иначе пользователь, отменивший заявку,
# не смог бы подать новую.
#
# Константа живёт здесь, а не в сервисе: репозиторий
# (`repositories/join_requests.py`) импортирует её для COUNT, а сервис уже
# импортирует репозиторий — определение в сервисе дало бы цикл импорта на
# старте (решение Task 3 стори 4.4).
JOIN_REQUEST_ACTIVE_STATUSES: tuple[JoinRequestStatus, ...] = (JoinRequestStatus.PENDING,)

# Стори 5а.1, Task 1.3 (AC-04, FR82): статусы, попадающие под ЧАСТИЧНЫЙ
# уникальный индекс `uq_join_requests_card_applicant_active`. Шире, чем
# `JOIN_REQUEST_ACTIVE_STATUSES`: `selected` тоже занимает пару «карточка +
# заявитель» — двух активных ролей на одной карточке быть не может.
# Терминальные статусы в предикат НЕ входят: иначе безусловный `UNIQUE`
# запретил бы повторную подачу после отмены (FR82) — проверено тестом.
JOIN_REQUEST_OCCUPYING_STATUSES: tuple[JoinRequestStatus, ...] = (
    JoinRequestStatus.PENDING,
    JoinRequestStatus.SELECTED,
)

# Стори 5а.3, Task 1.1 (AC-03, FR81): статусы, которые заявитель ВИДИТ в
# «Моих заявках» (`GET /my/requests`). `not_selected` в список НЕ входит и не
# может войти случайно: предикат положительный. Инверсия
# (`status <> 'not_selected'`) протекает молча — статус, добавленный в
# `JoinRequestStatus` позже, попал бы в публичную выдачу автоматически.
#
# Модуль выбран по тому же доводу, что `JOIN_REQUEST_ACTIVE_STATUSES`: список
# нужен репозиторию (SQL), а репозиторий не может импортировать сервис —
# получился бы цикл импорта на старте.
JOIN_REQUEST_VISIBLE_STATUSES: tuple[JoinRequestStatus, ...] = (
    JoinRequestStatus.PENDING,
    JoinRequestStatus.SELECTED,
    JoinRequestStatus.CANCELLED,
    JoinRequestStatus.EXPIRED,
)

# Стори 5а.4, Task 1.2 (AC-04, FR81, ADR-05): статусы, которые АВТОР видит
# в списке заявившихся на карточке режима «заявки» (вместимость 1).
# `not_selected` в список НЕ входит и не может войти случайно: предикат
# положительный. Автор сам породил отказ, и очередь — это `pending`, а
# `selected` — уже принятое решение; отказ в очереди не показывается.
# `expired`/`cancelled` тоже не показываются: список отвечает на вопрос
# «кто ждёт моего решения», а не «что стало с заявками».
#
# Отдельная константа, а не `JOIN_REQUEST_OCCUPYING_STATUSES`: совпадение
# состава сегодня (pending + selected) не повод связывать два разных
# правила — смена «занимающих слот» не должна молча менять выдачу автору.
JOIN_REQUEST_APPLICANT_STATUSES: tuple[JoinRequestStatus, ...] = (
    JoinRequestStatus.PENDING,
    JoinRequestStatus.SELECTED,
)


class CardRequestsMode(StrEnum):
    """AC-03, AC-09 (FR70): режим присоединения карточки для списка заявившихся.

    Режим определяется вместимостью (`EventCard.is_request_mode`), но клиенту
    нужен ЯВНЫЙ признак: по нему он решает, рисовать ли кнопку «Выбрать»
    (`selection_available`). Считать это на клиенте из `capacity` значило бы
    завести второй источник правды для правила FR70 — при добавлении
    промежуточной вместимости клиент разошёлся бы с сервером молча.

    `requests` — вместимость 1, заявки с выбором автора (5б.2);
    `open_set` — вместимость 2..6, мгновенное присоединение (5а.5), выбора нет.
    """

    REQUESTS = "requests"
    OPEN_SET = "open_set"


class AttendanceStatus(StrEnum):
    """AC-01 (FR87, FR88, FR89; D-3): состояние факта явки на пару автор↔участник.

    D-3: `text` + `CHECK`, не нативный PG-enum — `ALTER TYPE ... ADD VALUE`
    нельзя откатить в транзакции, а миграция на `CHECK` переписывается одной
    строкой и откатывается целиком.

    `status` — ПРОИЗВОДНОЕ значение от пары `(author_confirmed,
    participant_confirmed)`, а не независимое поле. Таблица истинности
    (9 строк, включая `NULL`) — в стори 7.1, раздел «Таблица истинности»;
    правило живёт в `models/attendance.py` в двух представлениях: Python
    (`derive_attendance_status`) и SQL (`STATUS_CASE_SQL`).

    `disputed` (одна сторона `true`, другая `false`) — НЕ основание санкции
    (FR89): расхождение фиксируется как «не подтверждено», и обе стороны
    видят это без указания, кто не подтвердил.

    `no_show` — единственный статус, влекущий санкцию, и применяет её
    эпик 8 (FR109); здесь он только создаётся.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    NO_SHOW = "no_show"
    DISPUTED = "disputed"


#: AC-02 стори 7.1: полный набор значений для `CHECK status IN (...)`.
#: Кортеж, а не перечисление строкой в модели и миграции: третья копия
#: разошлась бы при добавлении значения (класс дефекта 4.1).
ATTENDANCE_STATUSES: tuple[AttendanceStatus, ...] = (
    AttendanceStatus.PENDING,
    AttendanceStatus.CONFIRMED,
    AttendanceStatus.NO_SHOW,
    AttendanceStatus.DISPUTED,
)

#: AC-02, FR88: терминальные статусы — те, что выставляются ОДИН раз, когда
#: обе стороны подтвердили. `pending` сюда НЕ входит, и это проверяется
#: тестом: пока хотя бы одна сторона не подтвердила, факт не зафиксирован.
#: На этот кортеж опирается `confirmed_at`-бикондиционал и CAS в 7.2.
ATTENDANCE_TERMINAL_STATUSES: tuple[AttendanceStatus, ...] = (
    AttendanceStatus.CONFIRMED,
    AttendanceStatus.NO_SHOW,
    AttendanceStatus.DISPUTED,
)

#: AC-13 стори 7.3 (D-7.3-7): статусы карточки, при которых воркер запрашивает
#: подтверждение явки. ЕДИНСТВЕННЫЙ источник списка: на него опираются и
#: `SELECT` воркера, и предикат CAS, и (дословно) предикат частичного индекса
#: `ix_event_cards_prompt_due` (`app/models/event_card.py:364-371`,
#: `alembic/versions/0008_create_event_cards.py:87`).
#:
#: **Изменение кортежа требует НОВОЙ миграции** на перестройку частичного
#: индекса: предикат индекса записан литералами статусов, и добавление
#: значения здесь без перестройки сделает карточку с новым статусом
#: невидимой для выборки (seq scan вернёт её, индекс — нет; расхождение
#: предикатов = класс дефекта R1 TD 7.3). Тест `S37`/`S38` краснеет при
#: таком расхождении.
#:
#: Почему именно эти три: `full`/`closed` — окно закрыто, состав определён,
#: но встреча ещё не началась или идёт; `finished` — встреча началась
#: (`finish_started_cards` переводит `published → finished` по `starts_at`),
#: и факт явки можно фиксировать. `published`/`draft` исключены: состава ещё
#: нет; `cancelled` исключён: запрос «подтвердите явку» по отменённой встрече
#: ложен (D-7.3-5).
ATTENDANCE_PROMPTABLE_STATUSES: tuple[EventCardStatus, ...] = (
    EventCardStatus.FULL,
    EventCardStatus.CLOSED,
    EventCardStatus.FINISHED,
)


class ReportTargetType(StrEnum):
    """AC-02 стори 8.1: объект жалобы. Два значения вместо одного (7.4).

    `message` — жалоба на собеседника в переписке (FR36/FR37): скрывает диалог.
    `no_show` — жалоба на неявку пары (FR90, 7.4): опирается на факт `attendance`.
    Жалобы на анкету в продукте НЕТ — это осознанное сужение домена.
    """

    MESSAGE = "message"
    NO_SHOW = "no_show"


#: AC-02 стори 8.1: полный набор для `CHECK target_type IN (...)`.
REPORT_TARGET_TYPES: tuple[ReportTargetType, ...] = (
    ReportTargetType.MESSAGE,
    ReportTargetType.NO_SHOW,
)


class ReportResolution(StrEnum):
    """AC-06 стори 8.1: чем закончился разбор жалобы.

    `warning`/`ban` — санкция вынесена (пара к `status='resolved_*'`).
    `dismissed` — закрыто БЕЗ санкции. Это не санкция, а её отсутствие:
    FR89 запрещает предлагать санкцию за `disputed`-явку, и модератор
    закрывает такую жалобу именно `dismissed`.
    """

    WARNING = "warning"
    BAN = "ban"
    DISMISSED = "dismissed"


#: AC-06 стори 8.1: набор для `CHECK resolution IN (...)`.
REPORT_RESOLUTIONS: tuple[ReportResolution, ...] = (
    ReportResolution.WARNING,
    ReportResolution.BAN,
    ReportResolution.DISMISSED,
)


class SanctionType(StrEnum):
    """AC-07 стори 8.1: вид санкции. `dismissed` сюда НЕ входит.

    Санкция — запись о последствии нарушения; закрытие жалобы без санкции
    санкцией не является и строки в `sanctions` не создаёт.
    """

    WARNING = "warning"
    BAN = "ban"


#: AC-07 стори 8.1: набор для `CHECK type IN (...)`.
SANCTION_TYPES: tuple[SanctionType, ...] = (
    SanctionType.WARNING,
    SanctionType.BAN,
)


class NotificationType(StrEnum):
    """AC-02 стори 9.1: перечень допустимых типов уведомлений.

    **Единственный Python-источник перечня.** Миграция `0030` дублирует его
    кортежем-литералом осознанно (миграция не импортирует приложение);
    расхождение ловится тестом сверки трёх источников (Python, литерал
    миграции, фактическая схема).

    **Dot-форма, не underscore.** Архитектура §4.2 перечисляет типы через
    `_` (`request_received`), но `TYPE_PATTERN` (`app/schemas/ws.py:39`) и
    четыре уже записанных в БД типа используют точку. Смена нотации
    потребовала бы миграции данных и сломала бы конверт WS (D-9.1-4).

    **Форма и домен проверяются РАЗНЫМИ ограничениями.** Регекс
    `ck_notifications_type_format` ловит опечатку формы (`BAD`), перечень
    `ck_notifications_type` — валидную по форме чужую сущность (`foo.bar`).
    Второе важнее: NFR60 требует от клиента ИГНОРИРОВАТЬ неизвестный тип,
    то есть ошибку в имени не заметит никто (D-9.1-3).
    """

    # --- уже пишутся в БД до этой стори (обязаны остаться) ---
    # 7.3: запрос подтверждения явки (FR107).
    ATTENDANCE_REQUEST = "attendance.request"
    # 8.2: след жалобы у обеих сторон (FR36).
    REPORT_CREATED = "report.created"
    # 8.4: решение по жалобе получено автором (FR39).
    REPORT_RESOLVED = "report.resolved"
    # 8.4: санкция вынесена обвиняемому (FR38).
    SANCTION_ISSUED = "sanction.issued"

    # --- новые типы эпика 9 ---
    # 9.2, FR43: автору карточки пришла заявка.
    REQUEST_RECEIVED = "request.received"
    # 9.2, FR44: заявителю — его заявка выбрана.
    REQUEST_SELECTED = "request.selected"
    # 9.2, FR45 вариант A: автору — компания собрана.
    COMPANY_ASSEMBLED = "company.assembled"
    # 9.2, FR81: карточка закрыта безлично (`not_selected`).
    CARD_CLOSED_IMPERSONAL = "card.closed_impersonal"
    # 9.4, FR110: окно решения истекает.
    WINDOW_EXPIRING = "window.expiring"
    # 9.4, FR111: факт явки подтверждён/оспорен.
    ATTENDANCE_RESULT = "attendance.result"
    # 9.6, FR57: модератор ответил на обращение.
    SUPPORT_REPLIED = "support.replied"


#: AC-02 стори 9.1: набор для `CHECK type IN (...)`.
#: Порядок фиксирован: он входит в текст ограничения, и его изменение
#: создало бы диф `alembic check` без изменения смысла.
#: 9.3, D-9.3-5: типы, которые НЕЛЬЗЯ отключить.
#:
#: `report.resolved` — исполнение FR39 (автор жалобы узнаёт решение),
#: `sanction.issued` — FR38 (обвиняемый видит причину ограничения).
#: Отключение сделало бы оба требования невыполнимыми по воле пользователя,
#: то есть «настройка» отменяла бы пункт PRD. Защита двойная: гард API
#: (`409 notification_type_required`) и `CHECK` в БД — прямая вставка мимо
#: API санкцию не отключит.
#:
#: Кортеж ОТДЕЛЬНЫЙ от `NOTIFICATION_TYPES`, а не вычитание из него: состав
#: неотключаемых — продуктовое решение, и оно обязано быть видно одной
#: строкой, а не вычисляться из двух перечней.
REQUIRED_NOTIFICATION_TYPES: tuple[NotificationType, ...] = (
    NotificationType.REPORT_RESOLVED,
    NotificationType.SANCTION_ISSUED,
)


NOTIFICATION_TYPES: tuple[NotificationType, ...] = (
    NotificationType.ATTENDANCE_REQUEST,
    NotificationType.ATTENDANCE_RESULT,
    NotificationType.CARD_CLOSED_IMPERSONAL,
    NotificationType.COMPANY_ASSEMBLED,
    NotificationType.REPORT_CREATED,
    NotificationType.REPORT_RESOLVED,
    NotificationType.REQUEST_RECEIVED,
    NotificationType.REQUEST_SELECTED,
    NotificationType.SANCTION_ISSUED,
    NotificationType.SUPPORT_REPLIED,
    NotificationType.WINDOW_EXPIRING,
)


class TicketStatus(StrEnum):
    """AC-01 стори 9.1: жизненный цикл обращения в поддержку (FR55–FR57).

    `open` — создано 9.5, ждёт модератора. `answered` — 9.6 записал ответ.
    `closed` — обращение закрыто (в 9.5/9.6 переход в `closed` не реализуется,
    но значение существует: без него закрытие потребовало бы миграции).

    `text + CHECK`, не PG-enum (D-3): набор меняется миграцией без `ALTER TYPE`.
    """

    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


#: AC-01 стори 9.1: набор для `CHECK status IN (...)`.
TICKET_STATUSES: tuple[TicketStatus, ...] = (
    TicketStatus.OPEN,
    TicketStatus.ANSWERED,
    TicketStatus.CLOSED,
)
