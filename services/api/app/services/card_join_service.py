"""Мгновенное присоединение к открытому набору (стори 5а.5; AC-01…AC-14).

Домен участия ОТДЕЛЁН от домена заявки (D-5а.5-1, OQ-5а.5-2): заявка — это
`pending` и лимит 2 (FR105), участие — `selected` и место в карточке (FR83).
Связь модулей — только импорт кодов ошибок: тело `403 JOIN_REQUEST_OWN_CARD`
обязано совпадать с 5а.2, иначе клиент увидит две разные формы одного запрета.

**Схема БД не меняется** (X-39): `full` достижим, ломался репозиторий — он
ставил `full` отдельным `UPDATE`, а между инкрементом и им существовала
строка `published` + `filled_count = capacity`, запрещённая
`ck_event_cards_full_capacity` (0011) → `500` на легальном действии.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import EventCardStatus, JoinRequestStatus
from app.repositories.chats import ChatRepository
from app.repositories.errors import constraint_name_of
from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.users import UserRepository
from app.schemas.ws import WsEnvelope
from app.services.chat_message_service import ChatSettings
from app.services.join_request_service import (
    CARD_APPLICANT_UNIQUE,
    CapacityOneUsesRequestsError,
    CardNotAcceptingError,
    JoinRequestOwnCardError,
)
from app.services.notification_service import notify_company_assembled
from app.services.system_message_service import (
    SystemMessageKind,
    append_system_message,
    publish_system_message,
)
from app.services.ws_publisher import publish_to_chat

logger = structlog.get_logger(__name__)

# AC-11, ADR-13: карточки нет ИЛИ она чужого сообщества — один код и одно тело.
# Строчное значение повторяет `card_not_found` карточек (4.2) и заявок (5а.2):
# клиент ветвится по `code` одинаково на всех маршрутах `/cards/*`.
JOIN_CARD_NOT_FOUND_CODE = "card_not_found"

# AC-08 (FR84): свободных мест нет. `409` — конфликт состояния карточки, а не
# запрет домена и не лимит. Код ОТДЕЛЬНЫЙ от `CARD_NOT_ACCEPTING` (D-5а.5-4):
# «мест нет» и «набор закрыт» требуют разных действий клиента (ждать отмены
# участия либо искать другую карточку).
CARD_IS_FULL_CODE = "CARD_IS_FULL"

# AC-09 (FR83): участник уже занимает место в этой карточке. `409` — конфликт
# состояния пары «карточка + участник», а не запрет домена. Идемпотентность
# пары держит частичный `uq_join_requests_card_applicant_active`
# (`pending`,`selected`), а не проверка-перед-вставкой (§18.3):
# предварительный `SELECT` пропускает две параллельные вставки.
ALREADY_JOINED_CODE = "ALREADY_JOINED"

# AC-05 (FR104): участия нет — либо его не было, либо карточка режима заявок
# (`capacity=1`), где участие не создаётся вовсе. КОД строчный, как
# `card_not_found`: клиент ветвится по `code` на всех маршрутах `/cards/*`.
# Отдельный от `card_not_found`: карточка существует и видна, отсутствует
# именно пара «карточка + участник», и клиенту нужно разное действие
# (перечитать карточку против «выйти из компании»).
PARTICIPATION_NOT_FOUND_CODE = "participation_not_found"


class CardJoinSettings(Protocol):
    """AC-11: минимум настроек, который нужен сервису.

    `Protocol`, а не импорт `Settings`: тест подставляет объект с одним полем
    и не зависит от `.env` (приём `JoinRequestSettings`).
    """

    default_community_slug: str


class CardLeaveSettings(Protocol):
    """AC-08: минимум настроек для отмены участия.

    `Protocol`, а не импорт `Settings`: тест подставляет объект с одним полем
    и не зависит от `.env` (приём `CardJoinSettings`).
    """

    default_community_slug: str


def _chat_settings() -> ChatSettings:
    """Стори 6.7: настройки шифрования переписки для системных сообщений.

    Берутся из глобального `Settings`, а не из `CardJoinSettings`/
    `CardLeaveSettings`: протоколы описывают МИНИМУМ полей конкретной
    операции (и их подменяют unit-тесты), а ключ переписки — общая
    инфраструктура, которая есть в приложении всегда. Добавление поля в
    протокол заставило бы править каждый существующий стаб ради того,
    чего в них нет.
    """
    from app.core.config import settings as app_settings

    return app_settings


# AC-11, ADR-13: карточки нет ИЛИ она чужая — различие раскрыло бы
# существование чужих карточек перебором `uuid`.
class CardJoinNotFoundError(Exception):
    """AC-11 (ADR-13): карточки нет ИЛИ она чужого сообщества.

    Отдельный класс, а не переиспользование `JoinRequestCardNotFoundError`:
    у маршрутов разные `except`-ветки, а КОД совпадает — клиент не различает
    источник отказа.
    """

    code = JOIN_CARD_NOT_FOUND_CODE


class CardIsFullError(Exception):
    """AC-08 (FR84): свободных мест нет (`filled_count >= capacity`).

    Не сливается с `CardNotAcceptingError`: `full` — легальное состояние
    карточки, и участник может вернуться после отмены чужого участия (5а.6),
    тогда как `closed`/`cancelled`/`finished` — окончательный отказ.
    """

    code = CARD_IS_FULL_CODE


class AlreadyJoinedError(Exception):
    """AC-09 (FR83): место в этой карточке уже занято этим участником.

    Гонку отбивает частичный `uq_join_requests_card_applicant_active`
    (CR 3.1: имя читается через `constraint_name_of`, форма
    `error.orig.orig.constraint_name`). `filled_count` при этом НЕ меняется:
    `UPDATE` мест выполняется до вставки, поэтому при повторном `POST` вторая
    попытка либо находит свободное место и откатывается вместе с `SAVEPOINT`,
    либо получает `CARD_IS_FULL`. Оба исхода честны и проверяются тестом.
    """

    code = ALREADY_JOINED_CODE


class ParticipationNotFoundError(Exception):
    """AC-05 (FR104): участия в этой карточке нет.

    Покрывает три разных случая, которые клиент НЕ различает намеренно:
    участия не было; на карточке режима заявок (`capacity=1`); у участника
    только `pending`-заявка (домены разделены, `D-5а.5-1`). Во всех трёх
    действий «выйти из компании» не существует, и ответ `404` с одним телом
    не раскрывает, что именно было у пользователя.
    """

    code = PARTICIPATION_NOT_FOUND_CODE


@dataclass(frozen=True)
class CardJoinResult:
    """AC-01, AC-02: результат присоединения — доменный объект, не схема.

    `filled_count`/`capacity`/`status` приходят из `RETURNING` (D-5а.5-7):
    повторный `SELECT` прочитал бы чужую зафиксированную версию и отдал бы
    клиенту неверное число мест. `status` — строка из БД, роутер приводит её
    к `EventCardStatus` так же, как `_card_to_read` ленты.

    `chat_uuid` (AC-02 стори 6.2, D-6.2-5): заполнен, если карточка собрана
    этим присоединением ИЛИ была собрана ранее (чат уже существует). `None` —
    компания не собрана, чата нет. Поле присутствует в ответе всегда, а не
    только после эпика 6: клиент не переписывается дважды.
    """

    card_uuid: uuid_lib.UUID
    status: EventCardStatus
    verification_status: str
    created_at: datetime
    filled_count: int
    capacity: int
    chat_uuid: uuid_lib.UUID | None = None


async def join_open_card(
    session: AsyncSession,
    *,
    actor_id: int,
    card_uuid: uuid_lib.UUID,
    settings: CardJoinSettings,
    user_repository: UserRepository | None = None,
    card_repository: EventCardRepository | None = None,
    request_repository: JoinRequestRepository | None = None,
    now: datetime | None = None,
) -> CardJoinResult:
    """AC-01…AC-12: мгновенное присоединение к карточке открытого набора.

    Порядок шагов — КОНТРАКТ (Task 3.1), каждый следующий опирается на
    предыдущий:

    1. `SELECT users.id ... FOR UPDATE` ПЕРВЫМ (AC-05, ADR-09): блокировка
       актора — точка сериализации. Два присоединения одного человека к
       РАЗНЫМ карточкам встают в очередь по `users`, а не по `event_cards`,
       поэтому дедлок `users ↔ cards` невозможен; обратный порядок
       (`cards → users`) даёт встречную очередь и дедлок;
    2. карточка по `uuid` + `community_slug` (AC-11): чужое сообщество
       неотличимо от несуществующей карточки;
    3. `404` — карточки нет (после блокировки, но до проверок прав);
    4. `403 JOIN_REQUEST_OWN_CARD` (AC-10) — своя карточка. Раньше статуса:
       запрет домена не зависит от состояния карточки, и «нельзя
       присоединиться к своей» остаётся верным для черновика и для закрытой;
    5. `409 CAPACITY_ONE_USES_REQUESTS` (AC-06) — `capacity=1` (FR70). Режим
       проверяется ПЕРВЫМ из содержательных отказов и ДО статусных веток:
       AC-06 не оговаривает состояние карточки, и на собранной карточке
       `capacity=1` прежний порядок отдавал `CARD_IS_FULL` — клиент не
       предлагал бы единственное доступное действие (подать заявку). Режим
       берётся у модели (`is_request_mode`), а не повторным сравнением
       вместимости: вторая копия условия разошлась бы при добавлении
       промежуточной вместимости;
    6. `409 CARD_NOT_ACCEPTING` (AC-07) — `draft`/`closed`/`finished`/
       `cancelled`. Окончательный отказ карточки важнее состояния конкретного
       участника: `CARD_NOT_ACCEPTING` остаётся верным и для того, кто уже
       состоит в компании отменённой встречи;
    7. `409 ALREADY_JOINED` (AC-09) — пара «карточка + участник» занята.
       Проверка ДО `UPDATE` мест: иначе повторный `POST` на ПОЛНОЙ карточке
       (участник занял последнее место — штатный сценарий FR84) отдавал бы
       `409 CARD_IS_FULL`. Гарантия уникальности остаётся за частичным `uq`
       на `INSERT` — этот `SELECT` закрывает только последовательный повтор;
    8. `409 CARD_IS_FULL` (AC-08) — карточка `full`. Отдельно от AC-07:
       `full` — легальное состояние, участник может вернуться после отмены
       чужого участия (5а.6);
    9. `UPDATE` мест (AC-02, X-39) — инкремент и `full` ОДНИМ запросом. CAS
       идёт ВТОРЫМ шагом после блокировки актора, а не первым: иначе порядок
       был бы `cards → users`;
    10. `rowcount == 0` — перечитать статус: `full` → `409 CARD_IS_FULL`,
        иначе → `409 CARD_NOT_ACCEPTING` (карточку закрыли/отменили
        параллельно). Перечитывание обязательно: `UPDATE` не различает
        «мест нет» и «набор закрыт» — он просто не совпал;
    11. `INSERT` участия в `SAVEPOINT` (AC-09): гонку пары отбивает частичный
        `uq`; `begin_nested` откатывает только вставку, оставляя транзакцию
        живой (голый `INSERT` после `IntegrityError` увёл бы сессию в
        `PendingRollbackError` → `500` на втором действии пользователя);
    12. `commit` ДО ответа (AC-13): данные обязаны быть видны следующему
        соединению сразу после `200`.

    `filled_count` увеличивается РОВНО на 1 и только при свободном месте:
    предикат `filled_count < capacity` — единственный гард NFR47. Считать
    состав через `COUNT(join_requests)` вместо `filled_count` нельзя:
    это прямая гонка (два чтения видят одно и то же число).

    WS-публикации НЕТ (AC-13, D-5а.5-5): в `app/` нет socket.io/pub-sub,
    и заглушка «событие отправлено» дороже отсутствия — долг `X-43`, эпик 6.
    """
    users = user_repository if user_repository is not None else UserRepository(session)
    cards = card_repository if card_repository is not None else EventCardRepository(session)
    requests = (
        request_repository if request_repository is not None else JoinRequestRepository(session)
    )
    moment = now if now is not None else datetime.now(UTC)

    # 1. AC-05, ADR-09: блокировка актора ПЕРВОЙ — точка сериализации.
    await users.lock_for_update(actor_id)

    # 2. AC-11: чтение со срезом сообщества (чужая == несуществующая).
    card = await requests.get_card_for_join(card_uuid, settings.default_community_slug)
    # 3. AC-11, ADR-13: `404` с тем же телом, что у несуществующей.
    if card is None:
        raise CardJoinNotFoundError()
    # 4. AC-10: своя карточка — `403` тем же кодом, что у заявок (5а.2).
    if card.author_id == actor_id:
        raise JoinRequestOwnCardError()
    # 5. AC-08 (FR84): карточка уже собрана — «мест нет», а не «набор закрыт».
    #
    # Проверка `full` стоит ПЕРЕД общей проверкой статуса, и это отступление
    # от буквального порядка Task 3.1 (шаг 5 перечисляет `full` среди
    # `CARD_NOT_ACCEPTING`). Причина: после объединённого `UPDATE` (X-39)
    # состояние `published` + `filled_count = capacity` НЕ СУЩЕСТВУЕТ — его
    # запрещает `ck_event_cards_full_capacity`, и `full` выставляется тем же
    # запросом, что инкремент. Значит ветка «`UPDATE` не совпал, статус
    # остался `published`» недостижима, и код `CARD_IS_FULL` из AC-08 был бы
    # мёртвым: тест на него не мог бы упасть (§18.2, тавтология).
    #
    # Разделение продуктово верно и совпадает с AC-08: `full` — «мест нет»
    # (участник может вернуться после отмены участия, 5а.6), `draft`/`closed`/
    # `finished`/`cancelled` — «набор закрыт» окончательно. Достижимость обоих
    # кодов доказана тестами `test_full_card_*` и `test_non_published_card_*`.
    # 5a. AC-06 (FR70): `capacity=1` — режим заявок, мгновенного присоединения
    # нет В ПРИНЦИПЕ, поэтому проверка режима идёт ПЕРЕД статусными ветками.
    # Прежний порядок давал `CARD_IS_FULL` на собранной карточке с
    # `capacity=1` (AC-06 без оговорки о статусе): клиент показывал бы «мест
    # нет» там, где единственное действие — подать заявку, и не предлагал бы
    # её вовсе (режим берётся у модели, а не сравнением вместимости).
    if card.is_request_mode:
        raise CapacityOneUsesRequestsError()
    # 5b. AC-07: `draft`/`closed`/`finished`/`cancelled` — «набор закрыт».
    # Проверяется ДО пары и до `full`: окончательный отказ карточки важнее
    # состояния конкретного участника.
    if card.status not in (EventCardStatus.PUBLISHED, EventCardStatus.FULL):
        raise CardNotAcceptingError()
    # AC-02 стори 5б.1 (NFR20): окно решения истекло — набор НЕ принимает
    # участие, даже если воркер `card_windows` ещё не перевёл карточку в
    # `closed` (до 60 с). Код тот же `CARD_NOT_ACCEPTING`, что у `closed`:
    # для участника это одно и то же состояние «набор закрыт», и отдельный код
    # заставил бы клиент ветвиться по причине, которая уже не важна.
    #
    # Ленивого `close_if_due` здесь НЕТ намеренно: запись под блокировкой
    # `users` удлинила бы транзакцию присоединения и добавила второй `UPDATE`
    # на горячем пути; воркер закрывает окно и без нас (ADR-03).
    if card.expires_at <= moment:
        raise CardNotAcceptingError()
    # 6. AC-09 (FR83): участник уже занимает пару «карточка + заявитель».
    # Проверка ДО `UPDATE` мест, потому что иначе повторный `POST` на ПОЛНОЙ
    # карточке — штатный сценарий, когда участник занял последнее место, —
    # отдавал бы `409 CARD_IS_FULL` вместо `409 ALREADY_JOINED`: клиент
    # сообщал бы «мест нет» человеку, который в этой компании уже состоит.
    # Гонку двух одновременных первых `POST` этот `SELECT` не ловит и не
    # должен: её отбивает частичный `uq` на `INSERT` (ниже).
    if await requests.has_participation(card_id=card.id, applicant_id=actor_id):
        raise AlreadyJoinedError()
    # 7. AC-08 (FR84): карточка собрана — «мест нет». Отдельно от AC-07:
    # `full` — легальное состояние, участник может вернуться после отмены
    # чужого участия (5а.6).
    if card.status == EventCardStatus.FULL:
        raise CardIsFullError()

    # Снимок неизменяемых полей ДО `UPDATE`: `RETURNING` отдаёт только места
    # и статус, а `synchronize_session=False` оставляет объект в сессии
    # устаревшим — читать из него `filled_count` после записи нельзя.
    card_id = card.id
    # Стори 9.2 (Task 4.2): адресат `company.assembled` — автор карточки.
    # Снимок ДО `UPDATE` по той же причине, что `filled_count`: после записи
    # `synchronize_session=False` оставляет объект устаревшим.
    card_author_id = card.author_id
    card_verification_status = card.verification_status
    card_created_at = card.created_at
    card_expires_at = card.expires_at
    # CR 6.2: фактический `chat_uuid`, записанный этим присоединением (`None`,
    # если место не последнее). Нужен логу ПОСЛЕ `commit`.
    opened_chat_uuid: uuid_lib.UUID | None = None
    # Стори 6.7 (AC-02, D-6.7-7): системное сообщение о НОВОМ участнике.
    # Заполняется в ТОЙ ЖЕ транзакции, публикуется после `commit`.
    joined_system_message: tuple[uuid_lib.UUID, datetime, str] | None = None
    joined_chat_uuid: uuid_lib.UUID | None = None

    # 7-9. AC-02, AC-09, X-39: инкремент места, `full` и вставка участия —
    # В ОДНОМ `SAVEPOINT`.
    #
    # Почему `UPDATE` мест тоже внутри `begin_nested`, а не до него: при
    # `ALREADY_JOINED` (повторный `POST`) вставка откатывается, а `UPDATE`
    # вне SAVEPOINT остался бы в силе — место утекло бы без участника
    # (`filled_count` вырос, строки участия нет). Тест
    # `test_repeat_join_returns_already_joined` доказывает именно это.
    # Транзакция при этом не гибнет: откат доходит только до SAVEPOINT.
    try:
        async with session.begin_nested():
            # AC-02 стори 6.2 (D-6.2-1): `uuid` чата генерируется ДО CAS и
            # пишется тем же `UPDATE`, если место последнее. Сгенерировать
            # после `UPDATE` нельзя: понадобился бы второй `UPDATE`, а
            # `ck_event_cards_full_capacity` запрещает промежуточное
            # состояние `published` + `filled_count = capacity` (X-39).
            chat_uuid = uuid_lib.uuid4()
            seat = await cards.join_seat(card_id=card_id, chat_uuid=chat_uuid)
            if seat is None:
                # 8. AC-07/AC-08: `UPDATE` не совпал — перечитываем статус,
                # чтобы отличить «мест нет» от «набор закрыт». Ветка
                # `published` здесь недостижима (`ck_event_cards_full_capacity`
                # запрещает `published` при `filled_count = capacity`), но
                # оставлена: неожиданное состояние обязано дать честный `409`,
                # а не `500`.
                await session.refresh(card)
                if card.status in (EventCardStatus.PUBLISHED, EventCardStatus.FULL):
                    raise CardIsFullError()
                raise CardNotAcceptingError()
            await requests.create_participation(
                card_id=card_id,
                applicant_id=actor_id,
                now=moment,
                expires_at=card_expires_at,
            )
            # AC-02 стори 6.2 (TD п.4, мутация M12): чат открывается ТОЛЬКО
            # если `UPDATE` действительно записал `chat_uuid`, то есть место
            # было последним. Без guard'а строка `chats` появилась бы у
            # несобранной карточки (риск R11) — «чай до сборки компании»
            # нарушил бы FR30.
            if seat.chat_uuid is not None:
                # AC-06: порядок `users → event_cards → chats` — вставка чата
                # ТРЕТЬИМ шагом, после `UPDATE` карточки. `Chats` не берёт
                # `FOR UPDATE` (D-6.2-3): row-lock `INSERT` достаточно, а
                # явная блокировка стала бы лишней точкой ожидания.
                inserted = await ChatRepository(session).ensure_for_card(
                    chat_uuid=seat.chat_uuid, event_card_id=card_id
                )
                # CR 6.2 (blind #7), исправлено CR-2: `chat.opened` пишется
                # ТОЛЬКО когда строка `chats` действительно создана
                # (`inserted is not None`). `seat.chat_uuid` непуст и при
                # повторном присоединении к ранее собранной карточке (5а.6,
                # `full → published`), и тогда `ensure_for_card` вернул `None`:
                # лог «чат открыт» на существующем чате — ложный аудит.
                if inserted is not None:
                    opened_chat_uuid = seat.chat_uuid
                else:
                    # Стори 6.7 (AC-02): чат УЖЕ существовал — значит место
                    # освободилось (5а.6, `full → published`) и сейчас занято
                    # снова. Только этот случай и есть «новый участник»:
                    # при первой сборке (`inserted is not None`) сообщение
                    # «в компании новый участник» было бы ложным — компания
                    # только что появилась, и объяснять изменение нечего.
                    joined_chat_uuid = seat.chat_uuid
                    chat_row = await ChatRepository(session).get_by_uuid(chat_uuid=seat.chat_uuid)
                    if chat_row is not None:
                        joined_system_message = await append_system_message(
                            session,
                            chat_id=chat_row[0],
                            kind=SystemMessageKind.PARTICIPANT_JOINED,
                            settings=_chat_settings(),
                        )
    except IntegrityError as error:
        # AC-09: имя ограничения через `constraint_name_of` (форма
        # `error.orig.orig.constraint_name`, CR 3.1) — одиночный `getattr`
        # возвращал бы `None` всегда, и дубль уходил бы сырым `500`.
        if constraint_name_of(error) == CARD_APPLICANT_UNIQUE:
            raise AlreadyJoinedError() from error
        # Прочие `IntegrityError` (CHECK `message`, FK) — честный `500`:
        # ложный `409` скрыл бы дефект валидации.
        # CR 6.2 (edge #5): `actor_id` в журнал НЕ пишется — тот же контракт
        # безличности, что у события `chat.opened` (AC-08/ADR-13).
        logger.error("card_join.insert.failed")
        raise

    # 10. AC-13: `commit` до ответа — данные видны второму соединению.
    await session.commit()

    # Стори 6.5 (Task 4.1, AC-04, ADR-11): публикация `card.full` только
    # после commit. Условие `opened_chat_uuid is not None` — тот же признак
    # «карточка стала полной ИМЕННО этим вызовом»: на повторном join место
    # уже занято, `ensure_for_card` вернул `None`, и событие было бы ложным.
    if opened_chat_uuid is not None:
        await publish_to_chat(
            chat_uuid=opened_chat_uuid,
            envelope=WsEnvelope.build(
                "card.full", {"card_uuid": str(card_uuid), "chat_uuid": str(opened_chat_uuid)}
            ),
        )

    # Стори 9.2 (Task 4.2, AC-3, FR45): автору — «компания собрана», ТОЛЬКО
    # после `commit`. Признак «собрана ИМЕННО этим вызовом» — тот же, что у
    # `card.full`: `opened_chat_uuid is not None`. Мутация M14 (снять гард)
    # даёт второе уведомление на повторном join и краснит AC-3.
    #
    # Заявителю уведомления НЕТ (D-9.2-8): его участие — факт, который он сам
    # только что совершил.
    #
    # **CR 9.2 r1, HIGH-1: ВЕСЬ блок в `try/except`** (D-9.2-12). До фикса
    # `users.get_by_id` стоял вне защиты: близ БД после успешного `commit`
    # давал `500` участнику, чьё вступление уже зафиксировано (`full`, место
    # занято). Повтор невозможен — `409 ALREADY_JOINED`.
    if opened_chat_uuid is not None:
        try:
            author = await users.get_by_id(card_author_id)
            if author is not None:
                await notify_company_assembled(
                    session,
                    author_uuid=author.uuid,
                    author_id=author.id,
                    card_uuid=card_uuid,
                    chat_uuid=opened_chat_uuid,
                )
        except Exception as exc:  # noqa: BLE001 — домен закоммичен (D-9.2-12)
            # Без `card_author_id`/`card_uuid` в полях личности: FR81.
            logger.error(
                "notification.create.failed",
                error=type(exc).__name__,
            )

    # Стори 6.7 (AC-02, ADR-11): сообщение о новом участнике — после commit.
    # До `commit` событие показало бы участникам факт, которого ещё нет в БД.
    if joined_system_message is not None and joined_chat_uuid is not None:
        sys_uuid, sys_created_at, sys_body = joined_system_message
        await publish_system_message(
            chat_uuid=joined_chat_uuid,
            message_uuid=sys_uuid,
            body=sys_body,
            created_at=sys_created_at,
        )

    # AC-08 (TD п.6, ADR-13/FR81): в логе ТОЛЬКО `card_uuid` и `chat_uuid`,
    # и только ПОСЛЕ коммита (CR 6.2, blind #7): запись «чат открыт» при
    # откатанной транзакции — ложный аудит. Идентификатор заявителя не
    # пишется никогда.
    if opened_chat_uuid is not None:
        logger.info(
            "chat.opened",
            card_uuid=str(card_uuid),
            chat_uuid=str(opened_chat_uuid),
        )

    return CardJoinResult(
        card_uuid=card_uuid,
        status=EventCardStatus(seat.status),
        verification_status=card_verification_status,
        created_at=card_created_at,
        filled_count=int(seat.filled_count),
        capacity=int(seat.capacity),
        # AC-02 стори 6.2, D-6.2-5: `seat.chat_uuid` — фактически записанное
        # этим `UPDATE` значение. Для незаполненной карточки `NULL` (FR30),
        # для собранной — `uuid` чата, который автор/участник откроет сразу.
        chat_uuid=seat.chat_uuid,
    )


async def leave_open_card(
    session: AsyncSession,
    *,
    actor_id: int,
    card_uuid: uuid_lib.UUID,
    settings: CardLeaveSettings,
    user_repository: UserRepository | None = None,
    card_repository: EventCardRepository | None = None,
    request_repository: JoinRequestRepository | None = None,
) -> None:
    """AC-01…AC-12: отмена участия в открытом наборе.

    Зеркальная операция к `join_open_card` и с ТЕМ ЖЕ порядком блокировок
    (`D-5a.6-1`):

    1. `SELECT users.id ... FOR UPDATE` ПЕРВЫМ (AC-03, ADR-09): блокировка
       актора — точка сериализации. Порядок `users → event_cards →
       join_requests` совпадает с присоединением; обратный порядок двух
       параллельных операций (join A, leave B) дал бы встречную очередь
       и дедлок;
    2. карточка по `uuid` + `community_slug` (AC-08): чужое сообщество
       неотличимо от несуществующей карточки;
    3. `404 card_not_found` — карточки нет;
    4. `UPDATE` мест (`release_seat`) ПЕРВЫМ внутри `begin_nested` (AC-02):
       декремент и возврат в `published` — ОДИН запрос, иначе между ними
       существовала бы строка `full` + `filled_count < capacity`, которую
       запрещает `ck_event_cards_full_capacity` (`0011`) → `500`;
    5. `UPDATE` участия (`cancel_participation`) ВТОРЫМ в том же SAVEPOINT
       (AC-01): предикат `status='selected'` отделяет участие от `pending`-
       заявки (AC-07). Оба `UPDATE` в одном `begin_nested` по той же причине,
       что в 5а.5: при `rowcount == 0` на участии откат доходит до SAVEPOINT
       и место НЕ утекает (иначе `filled_count` уменьшился бы без отмены);
    6. `commit` ДО ответа (AC-14).

    `release_seat` вернул `None` — НЕ ошибка (AC-11, D-5a.6-7): карточка
    `closed`/`finished`/`cancelled` не возвращается в ленту, а `filled_count`
    у неё остаётся историческим фактом. Участие при этом отменяется.

    Диагностика отказа участия (AC-05, AC-06): нет строки → `404`; строка уже
    `cancelled` → идемпотентный успех (`204`, обрыв сети после `commit` не должен
    выглядеть сбоем); `pending`/`expired`/`not_selected` → `404` (действия
    «выйти» нет).

    WS-публикации НЕТ (AC-14, D-5a.5-5): в `app/` нет socket.io/pub-sub,
    и заглушка «событие отправлено» дороже отсутствия — долг `X-43`, эпик 6.
    """
    users = user_repository if user_repository is not None else UserRepository(session)
    cards = card_repository if card_repository is not None else EventCardRepository(session)
    requests = (
        request_repository if request_repository is not None else JoinRequestRepository(session)
    )

    # 1. AC-03, ADR-09: блокировка актора ПЕРВОЙ — точка сериализации.
    await users.lock_for_update(actor_id)

    # 2. AC-08: чтение со срезом сообщества (чужая == несуществующая).
    card = await requests.get_card_for_join(card_uuid, settings.default_community_slug)
    # 3. AC-08, ADR-13: `404` с тем же телом, что у несуществующей.
    if card is None:
        raise CardJoinNotFoundError()

    card_id = card.id

    # 4-5. AC-01, AC-02, AC-11: место и участие — В ОДНОМ `SAVEPOINT`.
    # Если участие не отменилось, откат до SAVEPOINT вернёт и место:
    # иначе `filled_count` уменьшился бы без отмены (утечка места).
    # Стори 6.7 (AC-01, D-6.7-7): системное сообщение пишется в ТОЙ ЖЕ
    # транзакции, что и отмена участия. Переменные объявлены ДО `try`: на
    # идемпотентном повторе (`cancelled == 0`) сообщения не будет, и
    # публиковать нечего.
    system_message: tuple[uuid_lib.UUID, datetime, str] | None = None
    chat_uuid: uuid_lib.UUID | None = None
    try:
        async with session.begin_nested():
            # `None` — карточка закрыта/отменена/архивна либо счётчик уже 0;
            # это не ошибка отмены участия (D-5a.6-7).
            await cards.release_seat(card_id=card_id)
            cancelled = await requests.cancel_participation(card_id=card_id, applicant_id=actor_id)
            if cancelled == 0:
                # Откат SAVEPOINT вернёт `filled_count` — место не утечёт.
                raise _ParticipationRollback()
            # AC-01: чат существует только у СОБРАННОЙ карточки (FR30).
            # `get_by_event_card_id` возвращает `None` для несобранной — тогда
            # объяснять нечего, и сообщения нет.
            chat_uuid = await ChatRepository(session).get_by_event_card_id(event_card_id=card_id)
            if chat_uuid is not None:
                chat_row = await ChatRepository(session).get_by_uuid(chat_uuid=chat_uuid)
                if chat_row is not None:
                    system_message = await append_system_message(
                        session,
                        chat_id=chat_row[0],
                        kind=SystemMessageKind.PARTICIPANT_LEFT,
                        settings=_chat_settings(),
                    )
    except _ParticipationRollback:
        # AC-05, AC-06: разбор отказа ВНЕ SAVEPOINT — сессия уже жива.
        status = await requests.get_participation_status(card_id=card_id, applicant_id=actor_id)
        if status is None:
            raise ParticipationNotFoundError() from None
        if status == JoinRequestStatus.CANCELLED:
            # AC-06: повторная отмена — успех. `decided_at` не переписывается:
            # CAS его не нашёл, а второй `UPDATE` подменил бы момент
            # фактической отмены (NFR1–NFR4).
            return
        # AC-07: `pending`-заявка, `expired`, `not_selected` и любое
        # неизвестное значение — «выйти из компании» нечего.
        raise ParticipationNotFoundError() from None

    # 6. AC-14: `commit` до ответа — данные видны второму соединению.
    await session.commit()

    # Стори 6.7 (AC-01, ADR-11): публикация ТОЛЬКО после commit. Событие о
    # незакоммиченном системном сообщении показало бы участнику факт,
    # которого ещё нет в БД.
    if system_message is not None and chat_uuid is not None:
        message_uuid, created_at, body = system_message
        await publish_system_message(
            chat_uuid=chat_uuid,
            message_uuid=message_uuid,
            body=body,
            created_at=created_at,
        )


class _ParticipationRollback(Exception):
    """Внутренний сигнал: участие не отменилось, SAVEPOINT откатан.

    Отдельный класс, а не `ParticipationNotFoundError`, потому что `except`
    вокруг `begin_nested` обязан отличить «CAS не совпал» от любого другого
    сбоя внутри блока: доменная ошибка поднялась бы уже после отката и
    потеряла бы возможность перечитать статус. Наружу не выходит:
    преобразуется в `ParticipationNotFoundError` или в идемпотентный успех.
    """
