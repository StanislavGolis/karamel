"""Жалоба на неявку (стори 7.4; AC-03…AC-13).

**Порядок шагов — контракт анти-оракула, а не стиль** (D-7.4-9, ADR-13):
видимость карточки проверяется ПЕРВОЙ (`_load_card`), и только потом всё
остальное. Обратный порядок дал бы постороннему `422`/`409` вместо `404` и
раскрыл бы существование карточки и состав компании по разнице кодов.

**Чего в сервисе НЕТ и почему.**

- Нет чтения `author_confirmed`/`participant_confirmed` (AC-09, D-7.4-10).
  Проверка «а ты сам пришёл?» вернула бы `403` одной из двух сторон при
  `disputed`, и по коду она восстановила бы чужой флаг — обход FR89 на уровне
  API (класс D-7.2-5). Цена: при `disputed` пожалуется и непришедший;
  модератор увидит оба флага в кабинете 8.3.
- Нет предпроверки «жалоба уже есть» (AC-07, P-3). Окно между `SELECT EXISTS`
  и `INSERT` дало бы две строки. Единственная защита — `UNIQUE` + `ON CONFLICT`.
  Пустой `RETURNING` превращается в `409` уже после попытки записи.
- Нет публикации WS-события (AC-13, D-7.4-12). Получатель жалобы — модератор
  без подписки на чат компании; публикация в чат раскрыла бы факт жалобы
  третьим лицам (NFR39). «commit → publish» (ADR-11) не применяется —
  публиковать нечего.
- Нет уведомления через `notifications` (AC-13, D-7.4-6). Адресный аутбокс
  требует `user_id`, а рассылка всем модераторам создала бы второй механизм
  адресации. Очередь модератора читает САМУ таблицу `reports` (8.3).

**`ReportCreated` без `reporter_id`/`attendance_id`** — не удобство, а защита
AC-08: чего нет в объекте, нельзя случайно сериализовать в ответ.
"""

from __future__ import annotations

import json
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.chats import ChatRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.reports import ReportsRepository
from app.services.attendance_service import (
    AttendanceNotFoundError,
    AttendanceSettings,
    _load_card,
)

__all__ = [
    "AttendanceConfirmedError",
    "AttendancePendingError",
    "MessageReportAlreadyExistsError",
    "ReportAlreadyExistsError",
    "ReportCreated",
    "ReportSubjectNotFoundError",
    "report_message",
    "report_no_show",
]

#: AC-04, D-7.4-7: `422`, не `409`. Значение пришло от клиента (подал слишком
#: рано), а `409` закреплён за конфликтом состояния сервера (§7.2).
ATTENDANCE_PENDING_CODE = "ATTENDANCE_PENDING"

#: CR 7.4 H-1 (решение пользователя 2026-10-24): `422`, не `409`/`404`.
#: `confirmed` — «оба подтвердили, что пришли». Жалоба на неявку здесь ложна
#: по построению, то есть нарушена семантика домена, а не состояние гонки
#: (`409`) и не видимость (`404`): карточка и пара существуют, скрывать
#: нечего — `confirmed` не раскрывает чужой флаг, только «встреча состоялась»,
#: что обе стороны уже видят. `ATTENDANCE_PENDING` неточен: подтверждение
#: ЗАВЕРШЕНО, просто успешно.
ATTENDANCE_CONFIRMED_CODE = "ATTENDANCE_CONFIRMED"

#: AC-03: единственные состояния, при которых неявка зафиксирована или
#: оспорена. Любое иное значение — рассинхрон `CHECK`/`StrEnum` (класс
#: `A-ENUM-01` из 7.2): это дефект сервера, а не ввод клиента, поэтому `500`.
REPORTABLE_ATTENDANCE_STATUSES: tuple[str, ...] = ("no_show", "disputed")

#: AC-07, D-7.4-3: `409`, а не тихий `200`: клиент обязан отличать «принята
#: сейчас» от «я уже жаловался», иначе второй успех создал бы ложное
#: впечатление новой жалобы.
ATTENDANCE_ALREADY_REPORTED_CODE = "ATTENDANCE_ALREADY_REPORTED"

#: AC-05 стори 8.2, D-24: `409`, но код ДРУГОЙ, чем у неявки. Это разные
#: объекты (диалог vs пара явки), и клиенту нужен разный текст: «вы уже
#: жаловались на этот диалог» против «вы уже жаловались на эту неявку».
ALREADY_REPORTED_CODE = "ALREADY_REPORTED"

#: AC-06 стори 8.2: тип уведомления обеим сторонам диалога. Форма
#: `сущность.действие` (NFR60) — та же, что у `attendance.request`.
REPORT_CREATED_TYPE = "report.created"

#: CR 8.2, M-3 / D-36 (ось NFR39): заведомо несуществующий `card_id` для
#: выравнивания числа SQL на `404`-ветке «чата нет». `event_cards.id` —
#: IDENTITY, начинается с 1, поэтому 0 не совпадёт ни с одной карточкой.
#: Тот же приём, что в `_load_card` (chore X-7.4-4) и `_resolve_chat`.
_MISSING_CARD_ID = 0

#: DS 8.3, D-38 (ось NFR39): заведомо несуществующий `chat_id` для
#: выравнивания ВТОРОГО запроса негативной ветви — блокировки строки чата.
#: `chats.id` — IDENTITY, начинается с 1, поэтому 0 не совпадёт ни с одним
#: чатом, а `SELECT ... FOR UPDATE` по нему вернёт пусто и ничего не заблокирует.
#: Без этого «чата нет» стоило 3 запроса, «не участник» — 4 (тест
#: `test_report_message_sql_parity.py`), и существование диалога читалось по
#: числу обращений — тот же оракул, что закрывал D-36, только на шаг позже.
_MISSING_CHAT_ID = 0


class ReportSubjectNotFoundError(Exception):
    """AC-06, AC-12, D-7.4-9: нет карточки / не участник / пары нет / на себя.

    Один класс и один код на все четыре случая: отдельный код или тело
    разошлись бы при первой правке и выдали бы существование карточки и
    состав компании (ADR-13, NFR33). Код — ТОТ ЖЕ, что у
    `AttendanceNotFoundError` (D-7.2-8), иначе разница кодов разделила бы
    «нет карточки» и «нет пары».
    """

    code = "card_not_found"


class AttendancePendingError(Exception):
    """AC-04: подтверждение не завершено — жаловаться не на что."""

    code = ATTENDANCE_PENDING_CODE


class AttendanceConfirmedError(Exception):
    """CR 7.4 H-1: пара `confirmed` — оба подтвердили, что пришли.

    Жалоба «на неявку» здесь ложна по построению. `422`: значение состояния
    пришло от клиента (он пожаловался не в том состоянии), сервер корректен.
    """

    code = ATTENDANCE_CONFIRMED_CODE


class ReportAlreadyExistsError(Exception):
    """AC-07: этот участник уже жаловался на эту пару."""

    code = ATTENDANCE_ALREADY_REPORTED_CODE


class MessageReportAlreadyExistsError(Exception):
    """AC-05 стори 8.2: этот участник уже жаловался на этот диалог.

    Отдельный класс от `ReportAlreadyExistsError`: разные объекты и разные
    коды (D-24). Переиспользование кода неявки сбило бы клиент.
    """

    code = ALREADY_REPORTED_CODE


@dataclass(frozen=True, slots=True)
class ReportCreated:
    """AC-08: РОВНО четыре поля ответа.

    `frozen` — результат не должен меняться после выхода из сервиса;
    `slots` — нельзя добавить поле по неосторожности в другом месте.
    """

    uuid: uuid_lib.UUID
    target_type: str
    status: str
    reported_at: datetime


async def report_no_show(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    viewer_id: int,
    accused_uuid: uuid_lib.UUID,
    viewer_community_slug: str,
    settings: AttendanceSettings,
    now: datetime | None = None,
) -> ReportCreated:
    """AC-03…AC-12: жалоба участника компании на неявку второй стороны.

    Порядок — КОНТРАКТ (3.1):
    (1) `_load_card` — карточка, `is_author`, анти-оракул `404` (переиспользуется
        из 7.2, а не переписывается: второй вход видимости разошёлся бы с
        первым при первой правке `get_card`);
    (2) `accused_uuid` → внутренний `id`;
    (3) `get_attendance_pair` — пары нет → `404`;
    (4) `status == 'pending'` → `422`;
    (5) `create_no_show_report` — `ON CONFLICT DO NOTHING RETURNING`;
    (6) пустой `RETURNING` → `409`.

    `now` — ИНЪЕКЦИЯ (TD, A-TIME-02): без неё момент жалобы непроверяем, а
    `freezegun`/`sleep` в проекте запрещены (`testing-rules.md`).
    """
    moment = now if now is not None else datetime.now(UTC)

    # (1) AC-12: единственный вход видимости. Посторонний получает здесь тот
    # же `404`, что и «карточки нет» — до любых других проверок.
    #
    # CR 7.4 H-1: `_load_card` (7.2) бросает СВОЙ `AttendanceNotFoundError`,
    # и без этой конвертации он долетел бы до обработчика FastAPI как
    # необработанное исключение — `500` вместо `404`. Поймано тестом
    # `test_registered_stranger_gets_404` на живом коде: нарушение AC-12 и
    # анти-оракула ADR-13 (по разнице `500`/`404` посторонний узнал бы о
    # существовании карточки). Код `card_not_found` совпадает намеренно.
    try:
        card, is_author = await _load_card(
            session,
            card_uuid=card_uuid,
            viewer_id=viewer_id,
            # AC-05 стори 10.1: срез сообщества — колонка пользователя.
            viewer_community_slug=viewer_community_slug,
            settings=settings,
            now=moment,
        )
    except AttendanceNotFoundError as error:
        raise ReportSubjectNotFoundError() from error
    author_id = card.card.author_id
    card_id = card.card.id

    repo = ReportsRepository(session)

    # (2) AC-03: `accused_uuid` — публичный адрес обвиняемого. `None` — такого
    # пользователя нет; ответ тот же `404` (иначе перебором `uuid` можно было
    # бы выяснить, кто зарегистрирован).
    accused_id = await repo.user_id_by_uuid(accused_uuid)
    if accused_id is None:
        raise ReportSubjectNotFoundError()

    # AC-06, D-7.4-2: обвиняемый ВЫЧИСЛИМ из пары — денормализации нет.
    # У автора карточки «вторая сторона» — участник (`accused_id`), у участника —
    # автор карточки (его `id`), а `accused_id` обязан с ним совпасть.
    if is_author:
        participant_id = accused_id
    else:
        # Участник может жаловаться только на автора карточки: жалуясь на
        # другого участника, он не указал бы ПАРУ, в которой тот участник
        # фигурирует (ADR-04). Несовпадение — тот же `404`.
        if accused_id != author_id:
            raise ReportSubjectNotFoundError()
        participant_id = viewer_id

    # AC-06, 3.3: жалоба на себя — `404`, не `422`. `422` подтвердил бы «ты в
    # этой карточке», то есть участие зрителя. Проверка стоит до чтения пары:
    # иначе для автора `participant_id == accused_id == author_id` дала бы
    # guard `participant_id <> author_id` и тоже `404`, но по другому поводу.
    if viewer_id == accused_id:
        raise ReportSubjectNotFoundError()

    # (3) AC-06: пары нет — нет участия, нет карточки или участник = автор
    # (guard X-7.1-7 внутри запроса). Один `404` на все случаи.
    pair = await repo.get_attendance_pair(
        card_id=card_id, author_id=author_id, participant_id=participant_id
    )
    if pair is None:
        raise ReportSubjectNotFoundError()
    attendance_id, status = pair

    # (4) AC-03, AC-04, D-7.4-7: жалоба допустима ТОЛЬКО при зафиксированной
    # неявке (`no_show`) или расхождении (`disputed`). `422`, а не `409`:
    # клиент подал в неподходящем состоянии, сервер корректен.
    #
    # CR 7.4 H-1: до этой правки блокировался только `pending`, и пара
    # `confirmed` («оба пришли») принимала жалобу на неявку — ложную по
    # построению. Теперь список разрешённых состояний ЯВНЫЙ, поэтому новое
    # значение статуса в схеме не станет молча разрешённым.
    if status == "pending":
        raise AttendancePendingError()
    if status == "confirmed":
        raise AttendanceConfirmedError()
    if status not in REPORTABLE_ATTENDANCE_STATUSES:
        # Класс A-ENUM-01 (7.2): рассинхрон `CHECK`/`StrEnum`. Это дефект
        # сервера, а не ввод клиента, поэтому не `422`, а честный `500`.
        raise ValueError(f"Неизвестный статус attendance: {status!r}")

    # (5)–(6) AC-07: единственная защита от дубля — `UNIQUE` + `ON CONFLICT`.
    # Предпроверки нет: `SELECT EXISTS` перед `INSERT` открыл бы окно и дал
    # две строки (P-3, урок D-7.2-1).
    created = await repo.create_no_show_report(
        attendance_id=attendance_id,
        event_card_id=card_id,
        reporter_id=viewer_id,
        # 8.5 AC-13: обвиняемый уже разрешён и проверен шагом (2)–(3).
        # `accused_id`, а НЕ `participant_id`: у жалобы участника обвиняемый —
        # автор карточки, а `participant_id` там равен жалобщику.
        accused_id=accused_id,
        now=moment,
    )
    if created is None:
        raise ReportAlreadyExistsError()

    report_uuid, target_type, report_status, reported_at = created
    # AC-13, 3.6: `commit` делает роут. Публикации нет (D-7.4-12).
    return ReportCreated(
        uuid=report_uuid, target_type=target_type, status=report_status, reported_at=reported_at
    )


async def report_message(
    session: AsyncSession,
    *,
    chat_uuid: uuid_lib.UUID,
    viewer_id: int,
    accused_uuid: uuid_lib.UUID,
    now: datetime | None = None,
) -> ReportCreated:
    """AC-02…AC-05 стори 8.2: жалоба на собеседника в диалоге.

    Порядок — КОНТРАКТ (AC-09, D-27, `W-1`):

    (1) блокировка строки чата (`FOR UPDATE`, D-25) — сериализует скрытие и
        отправку сообщения;
    (2) чат существует и зритель участник — иначе `404`. **Чтение БЕЗ фильтра
        `status`** (`get_by_uuid_including_hidden`): иначе повторная жалоба на
        уже скрытый диалог получила бы `404` вместо `409` (AC-05);
    (3) `accused_uuid` → внутренний `id`; обвиняемый обязан быть второй
        стороной диалога. **После** шага (2), а не до него: иначе битый UUID
        при несуществующем чате давал бы `422` и раскрывал существование;
    (4) `INSERT ... ON CONFLICT DO NOTHING RETURNING`; пусто → `409`;
    (5) скрытие в ТОЙ ЖЕ транзакции (AC-03);
    (6) уведомления обеим сторонам (AC-06).

    `commit` и публикацию делает роут: порядок «commit → publish» проверяется
    перехватом (ADR-11, R8).
    """
    moment = now if now is not None else datetime.now(UTC)
    chats = ChatRepository(session)

    # (2) AC-04/AC-05: скрытый чат здесь ВИДЕН — скрывать адрес от жалобщика
    # бессмысленно, он уже знает о диалоге.
    found = await chats.get_by_uuid_including_hidden(chat_uuid=chat_uuid)
    if found is None:
        # CR 8.2, M-3 / D-36 + DS 8.3, D-38 (ось NFR39): выравнивание числа SQL.
        # Без этого «чата нет» стоило 2 запроса, «не участник» — 4, и
        # существование диалога читалось по числу обращений (тело, код и
        # заголовки совпадали).
        #
        # D-38: выровнены ОБА запроса успешного пути, а не только участие.
        # Блокировка строки чата стоит здесь по той же причине, что участие:
        # без неё разрыв стал 3 против 4 — оракул никуда не делся, только
        # подешевел. Порядок запросов на обеих ветвях совпадает (чтение чата →
        # блокировка → участие), поэтому число обращений одно и то же, и
        # Postgres видит одинаковую последовательность блокировок.
        #
        # `_MISSING_CHAT_ID`/`_MISSING_CARD_ID` — заведомо несуществующие
        # ключи (IDENTITY начинается с 1): `FOR UPDATE` по ним вернёт пусто и
        # ничего не заблокирует, а участие даст пустое множество.
        await chats.lock_for_update(chat_id=_MISSING_CHAT_ID)
        await JoinRequestRepository(session).viewer_participation_card_ids(
            applicant_id=viewer_id, card_ids=[_MISSING_CARD_ID]
        )
        raise ReportSubjectNotFoundError()
    chat_id, event_card_id, _card_uuid, author_id = found

    # (1) AC-07, D-25: блокировка ПОСЛЕ первого чтения (строки уже прочитаны),
    # но ДО любых проверок участия и записи. Второй запрос здесь не лишний:
    # без него между чтением статуса и `INSERT` прошла бы конкурентная
    # транзакция, и сообщение оказалось бы в уже скрытом чате.
    #
    # CR 8.2 H-1: статус под блокировкой возвращается, но здесь он НЕ
    # проверяется намеренно. Жалоба на уже скрытый диалог легальна (AC-05:
    # иначе повтор давал бы `404` вместо `409`), поэтому `hidden` — не отказ.
    # Отказ только при `None`: строку удалили между чтением и блокировкой.
    if await chats.lock_for_update(chat_id=chat_id) is None:
        raise ReportSubjectNotFoundError()

    participating = await JoinRequestRepository(session).viewer_participation_card_ids(
        applicant_id=viewer_id, card_ids=[event_card_id]
    )
    if author_id != viewer_id and event_card_id not in participating:
        raise ReportSubjectNotFoundError()

    # (3) AC-03: `accused_uuid` — публичный адрес. `None` → тот же `404`,
    # иначе перебором `uuid` можно было бы выяснить, кто зарегистрирован.
    accused_id = await ReportsRepository(session).user_id_by_uuid(accused_uuid)
    if accused_id is None:
        raise ReportSubjectNotFoundError()

    # AC-06 стори 8.2, ADR-04: жалоба на себя невозможна. `404`, не `422`:
    # `422` подтвердил бы «ты в этом диалоге».
    if accused_id == viewer_id:
        raise ReportSubjectNotFoundError()

    # Обвиняемый обязан быть ВТОРОЙ стороной ЭТОГО диалога — автор карточки или
    # выбранный участник. Иначе адрес валиден, но жалоба адресована не туда и
    # модератор увидел бы пару, которой в диалоге нет.
    if not await _is_chat_side(
        session, chat_id=chat_id, event_card_id=event_card_id, user_id=accused_id
    ):
        raise ReportSubjectNotFoundError()

    # (4) AC-05: единственная защита от дубля — частичный `UNIQUE` + `ON
    # CONFLICT`. Предпроверки нет: `SELECT EXISTS` открыл бы окно (P-3).
    created = await ReportsRepository(session).create_message_report(
        chat_id=chat_id,
        reporter_id=viewer_id,
        # 8.5 AC-13: `accused_id` разрешён шагом (3) и проверен как вторая
        # сторона диалога. Без записи карточка обвиняемого не увидела бы
        # НИ ОДНОЙ новой жалобы (FR54 работал бы только на backfill `0028`).
        accused_id=accused_id,
        now=moment,
    )
    if created is None:
        raise MessageReportAlreadyExistsError()

    report_uuid, target_type, report_status, reported_at = created

    # (5) AC-03: скрытие в ТОЙ ЖЕ транзакции — иначе жалоба существовала бы
    # без скрытия, и диалог остался бы доступен обеим сторонам.
    await ReportsRepository(session).hide_chat(chat_id=chat_id)

    # (6) AC-06: обе стороны узнают о жалобе. `payload` собирается ОДИН раз
    # и не содержит текста сообщений (NFR27).
    payload = json.dumps(
        {"report_uuid": str(report_uuid), "chat_uuid": str(chat_uuid)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    await ReportsRepository(session).create_report_notifications(
        chat_id=chat_id, payload=payload, type_=REPORT_CREATED_TYPE
    )

    return ReportCreated(
        uuid=report_uuid, target_type=target_type, status=report_status, reported_at=reported_at
    )


async def _is_chat_side(
    session: AsyncSession, *, chat_id: int, event_card_id: int, user_id: int
) -> bool:
    """AC-02 стори 8.2: состоит ли `user_id` в диалоге (автор или `selected`).

    Отдельная функция, а не повторное использование
    `viewer_participation_card_ids`: у автора карточки строки в
    `join_requests` НЕТ (ADR-04), поэтому одним предикатом участия проверка
    не выражается — нужны оба условия, и они должны быть в ОДНОМ месте.
    """
    return await ReportsRepository(session).is_chat_side(
        chat_id=chat_id, event_card_id=event_card_id, user_id=user_id
    )
