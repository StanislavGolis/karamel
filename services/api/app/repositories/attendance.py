"""Запросы к `attendance` (стори 7.2; AC-01…AC-11; CR 7.2 H-3, M-2, M-3, M-6, L-2).

project-context §6: здесь только SQL, ни правил домена, ни HTTP-кодов.
Единица работы — у сервиса (`commit` в `attendance_service`).

**Почему сырой `text()`, а не `update(Attendance)`.** CAS-запись 7.2 — это
`UPDATE ... FROM (VALUES ...) ... RETURNING`, где `status` считается в БД по
НОВЫМ значениям флагов. ORM-билдер `update()` в форме `SET a = ..., status =
CASE` вычислил бы `CASE` по СТАРОМУ кортежу (дефект C1 CR 7.1, воспроизведён
на PostgreSQL 18): строка упала бы на `ck_attendance_status_consistency`.
Форма `FROM (VALUES ...)` — не стилистика, а условие корректности.

**Почему нет `FOR UPDATE`.** CAS атомарен сам по себе: `WHERE status='pending'`
под `READ COMMITTED` перепроверяется PostgreSQL после снятия блокировки
(EvalPlanQual), поэтому проигравший получает пустой `RETURNING`, а не
перезапись терминального статуса. Явная блокировка добавила бы второй
механизм защиты и второй источник правды про порядок (ADR-09, D-7.2-11).

**CR 7.2 M-6: `CASE` статуса собран здесь явно, а не `str.replace` по
`STATUS_CASE_SQL`.** Подстановка подстрок ломалась бы молча при любом новом
употреблении имени колонки внутри правила модели (комментарий, новая ветка).
Эквивалентность правилу модели проверяется ИСПОЛНЕНИЕМ на живой БД
(`test_truth_table_through_live_update`), а не сравнением текста.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AttendanceStatus, JoinRequestStatus

__all__ = [
    "AttendanceCorruptError",
    "AttendanceRepository",
    "DELETED_ANCHOR_CARD_ID",
    "HistoryRow",
]


class AttendanceCorruptError(RuntimeError):
    """CR 7.2 M-3: флаги автора в строках карточки разошлись.

    Такого состояния быть не может: флаг автора пишется одним CAS сразу во
    все строки. Расхождение — испорченные данные, и честный `500` лучше
    выдуманного «не ответил»: `None` в ответе означал бы «подтверждения не
    было», то есть скрыл бы дефект (инвентаризация предикатов, строка 13).
    """


#: Новое значение флага АВТОРА: своя сторона берёт `arrived`, чужая — колонку.
_AUTHOR_NEW = "(CASE WHEN v.is_author THEN v.arrived ELSE t.author_confirmed END)"
_PARTICIPANT_NEW = "(CASE WHEN v.is_author THEN t.participant_confirmed ELSE v.arrived END)"

#: Статус пары по НОВЫМ значениям (не по колонкам `t`, дефект C1 CR 7.1).
#: Ветки — от частного к общему; `ELSE 'pending'` покрывает все комбинации с
#: `NULL`. `IS TRUE`/`IS FALSE` вместо `= true`: на `NULL` сравнение дало бы
#: `NULL`, и `CASE` ушёл бы не в ту ветку (таблица истинности 7.1, F11/F12).
_STATUS_NEW = f"""CASE
    WHEN {_AUTHOR_NEW} IS TRUE AND {_PARTICIPANT_NEW} IS TRUE THEN 'confirmed'
    WHEN {_AUTHOR_NEW} IS FALSE AND {_PARTICIPANT_NEW} IS FALSE THEN 'no_show'
    WHEN {_AUTHOR_NEW} IS NOT NULL AND {_PARTICIPANT_NEW} IS NOT NULL THEN 'disputed'
    ELSE 'pending'
END"""


#: AC-01…AC-06, AC-10, AC-11: единственный пишущий оператор пути подтверждения.
#:
#: - своя сторона получает `v.arrived` (значение из тела запроса);
#: - чужая — `t.<flag>` КАК ЕСТЬ (M9: иначе подтверждение одной стороны
#:   обнуляло бы уже данное подтверждение другой);
#: - `status` считается по НОВЫМ значениям (форма C1 CR 7.1);
#: - `confirmed_at` — `:now` при терминальном статусе, `NULL` при `pending`
#:   (бикондиционал `ck_attendance_confirmed_at_consistency`);
#: - `updated_at = :now` ЯВНО (AC-11, X-7.1-1): `TimestampMixin.onupdate` —
#:   Python-side ORM-хук, а путь записи сырой;
#: - `participant_id <> :author_id` — AC-10 (X-7.1-7) в самом запросе: тест
#:   обязан ловить обход через репозиторий, а не только через сервис;
#: - `status = 'pending'` — CAS (AC-06, P-3): единственная защита терминального
#:   статуса. Предпроверка запрещена: она открывает окно между чтением и записью;
#: - `(<свой флаг> IS DISTINCT FROM :arrived)` — D-1: повтор с тем же ответом
#:   при `pending` — настоящий no-op, `updated_at` не двигается.
#:
#: `RETURNING` симметричен у обеих форм (CR 7.2 L-2): `status, confirmed_at,
#: updated_at`. Разный набор полей заставлял бы помнить, какая форма что
#: отдаёт, и первая же правка разошлась бы.
_CONFIRM_SQL = f"""
UPDATE attendance AS t
   SET author_confirmed = {_AUTHOR_NEW},
       participant_confirmed = {_PARTICIPANT_NEW},
       status = {_STATUS_NEW},
       confirmed_at = CASE WHEN {_STATUS_NEW} = 'pending' THEN NULL
                           ELSE CAST(:now AS timestamptz) END,
       updated_at = :now
  FROM (VALUES (CAST(:is_author AS boolean), CAST(:arrived AS boolean))) AS v(is_author, arrived)
 WHERE t.card_id = :card_id
   AND t.participant_id = :participant_id
   AND t.participant_id <> :author_id
   AND t.status = 'pending'
   AND (CASE WHEN v.is_author THEN t.author_confirmed ELSE t.participant_confirmed END
        IS DISTINCT FROM CAST(:arrived AS boolean))
 RETURNING t.status, t.confirmed_at, t.updated_at
"""


#: AC-01…AC-06, AC-10, AC-11 (авторская сторона, ADR-04/D-7.1-2): тот же CAS,
#: но БЕЗ фильтра по участнику — флаг автора дублируется в каждой строке
#: компании, и подтверждение автора обязано дойти до всех пар.
_CONFIRM_AUTHOR_SQL = f"""
UPDATE attendance AS t
   SET author_confirmed = {_AUTHOR_NEW},
       participant_confirmed = {_PARTICIPANT_NEW},
       status = {_STATUS_NEW},
       confirmed_at = CASE WHEN {_STATUS_NEW} = 'pending' THEN NULL
                           ELSE CAST(:now AS timestamptz) END,
       updated_at = :now
  FROM (VALUES (CAST(:is_author AS boolean), CAST(:arrived AS boolean))) AS v(is_author, arrived)
 WHERE t.card_id = :card_id
   AND t.participant_id <> :author_id
   AND t.status = 'pending'
   AND t.author_confirmed IS DISTINCT FROM CAST(:arrived AS boolean)
 RETURNING t.status, t.confirmed_at, t.updated_at
"""


#: CR 7.2 H-3: ленивое создание строк пар в той же транзакции, что и CAS.
#:
#: `attendance` наполняется воркером 7.3, но подтверждение обязано работать и
#: до него: иначе первое нажатие автора возвращало `200` с `my_answer=null`,
#: ничего не записав (тихая потеря факта).
#:
#: `ON CONFLICT DO NOTHING` делает операцию идемпотентной, поэтому воркер 7.3
#: сможет создавать строки той же формой, не разбираясь, кто был первым.
#: Источник пар — принятые заявки (`selected`), а не `pending`: компания — это
#: уже состоявшийся состав (ADR-04).
_ENSURE_ROWS_SQL = """
INSERT INTO attendance (card_id, participant_id)
SELECT jr.card_id, jr.applicant_id
  FROM join_requests AS jr
 WHERE jr.card_id = :card_id
   AND jr.status = :selected_status
   AND jr.applicant_id <> :author_id
ON CONFLICT (card_id, participant_id) DO NOTHING
"""


#: CR 7.5 H-1, D-7.5-10: верхняя граница `bigint` — подстановка на место
#: `card_id` удалённой карточки-якоря курсора. `bigint` не хранит большего
#: значения, `ck_event_cards_id_positive` гарантирует `id > 0`, поэтому
#: sentinel не совпадёт ни с одной реальной карточкой.
DELETED_ANCHOR_CARD_ID = 2**63 - 1


class HistoryRow(NamedTuple):
    """AC-02, D-7.5-4: строка истории явок — РОВНО данные четырёх полей ответа.

    `frozen`-эквивалент для кортежа (NamedTuple неизменяем): результат не
    должен меняться после выхода из репозитория.

    Флагов (`author_confirmed`, `participant_confirmed`) и `confirmed_at`
    здесь НЕТ и не будет (P-8, инвентаризация предикатов): не прочитанное
    нельзя раскрыть. `my_answer` не вычисляется вовсе — у модератора нет
    своей стороны в чужой паре (D-7.5-4), а своя история уже отдаётся
    `GET /cards/{uuid}/attendance` (D-7.2-4).

    `card_id` и `created_at` — ВНУТРЕННИЕ ключи (курсор и tie-break), наружу
    они не выходят: сервис собирает элемент только из `card_uuid`,
    `card_title`, `starts_at`, `status`.
    """

    created_at: datetime
    card_id: int
    card_uuid: uuid_lib.UUID
    card_title: str
    starts_at: datetime
    status: AttendanceStatus


#: AC-07, D-7.5-5: сортировка по `attendance.created_at`, НЕ по `starts_at`.
#:
#: X-7.1-4 проверен фактически: `ORDER BY c.starts_at DESC` даёт узел `Sort`
#: при ЛЮБОМ индексе на `attendance` — сортировка по колонке ДРУГОЙ таблицы
#: индексом `attendance` не обслуживается. `created_at` ≈ дата события (воркер
#: 7.3 пишет строку при запросе) и не требует денормализации `starts_at` в
#: `attendance` (класс P-8).
#:
#: Флаги в `SELECT` ОТСУТСТВУЮТ (AC-02, P-8) — это не сокращение ради
#: читаемости, а контракт: чего нет в запросе, то не утечёт в ответ.
_HISTORY_SQL = """
SELECT a.created_at, a.card_id, c.uuid, c.title, c.starts_at, a.status
  FROM attendance AS a
  JOIN event_cards AS c ON c.id = a.card_id
 WHERE a.participant_id = :participant_id
 ORDER BY a.created_at DESC, a.card_id DESC
 LIMIT :limit_plus_one
"""

#: AC-05, AC-08: продолжение страницы. Row-comparison `(created_at, card_id) <`,
#: а НЕ `OR`-цепочка (`created_at < :cur OR (created_at = :cur AND card_id < ...)`):
#: `OR` ломает индексный порядок, и планировщик получает `Sort` — ровно то,
#: от чего защищает AC-07.
#:
#: `card_uuid → card_id` разбирается СКАЛЯРНЫМ ПОДЗАПРОСОМ ВНУТРИ того же
#: запроса (Task 2.4): отдельный `SELECT` перед историей сделал бы число SQL
#: зависимым от наличия курсора, и анти-оракул по числу запросов (O-3/O-4)
#: «дышал» бы между первой и второй страницей.
#:
#: **CR 7.5 H-1, D-7.5-10: sentinel `2^63-1`, а не `0`.** Если карточка-ЯКОРЬ
#: курсора удалена ретенцией (ADR-07, 10.6), `COALESCE(..., 0)` превращал
#: сравнение в `created_at < :cur` и ОТСЕКАЛ ВСЮ tie-группу: строки с
#: `created_at = :cur` пропадали навсегда. Сценарий штатный — воркер 7.3
#: пишет батч одним `INSERT ... SELECT` в одной транзакции, а `now()` в
#: PostgreSQL возвращает время транзакции, то есть у пачки ОДИН `created_at`.
#: Верхняя граница `bigint` даёт `(created_at, card_id) < (:cur, 2^63-1)`,
#: то есть весь остаток группы остаётся в выдаче (AC-08 «не теряют строк»).
#: Плата: строки группы, уже выданные на предыдущей странице, могут прийти
#: повторно (якорь удалён — его `card_id` неизвестен, восстановить границу
#: нельзя). Потерь нет, дубли возможны только внутри одной tie-группы, чей
#: якорь удалён. Это осознанный выбор в пользу AC-08.
#: `2^63-1` не совпадёт ни с одной реальной карточкой: `ck_event_cards_id_positive`
#: гарантирует `id > 0`, а `bigint` не хранит большее значение.
_HISTORY_AFTER_CURSOR_SQL = """
SELECT a.created_at, a.card_id, c.uuid, c.title, c.starts_at, a.status
  FROM attendance AS a
  JOIN event_cards AS c ON c.id = a.card_id
 WHERE a.participant_id = :participant_id
   AND (a.created_at, a.card_id) < (
        CAST(:cur_created_at AS timestamptz),
        COALESCE(
            (SELECT ec.id FROM event_cards AS ec WHERE ec.uuid = :cur_card_uuid),
            :deleted_anchor_card_id
        )
       )
 ORDER BY a.created_at DESC, a.card_id DESC
 LIMIT :limit_plus_one
"""


class AttendanceRepository:
    """Чтение своего состояния и CAS-запись подтверждения."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_rows(self, *, card_id: int, author_id: int) -> int:
        """CR 7.2 H-3: гарантирует строки пар карточки. Возвращает число новых.

        Идемпотентно: повторный вызов и вызов из воркера 7.3 дают `0`.
        """
        result = await self._session.execute(
            text(_ENSURE_ROWS_SQL),
            {
                "card_id": card_id,
                "author_id": author_id,
                "selected_status": JoinRequestStatus.SELECTED.value,
            },
        )
        # `Result` из async-сессии не типизирован как `CursorResult`, поэтому
        # `rowcount` берётся через `getattr`: значение есть у DML-результата.
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_state(
        self, *, card_id: int, viewer_id: int, author_id: int, participant_id: int
    ) -> tuple[bool | None, AttendanceStatus] | None:
        """AC-08 (NFR39): собственный флаг зрителя и статус ЕГО пары — один запрос.

        Предикат адресности `participant_id <> :author_id` (AC-10) стоит и
        здесь: если зритель — автор карточки, он читает ветку автора, а не
        «свою участническую». Без него подделанная строка (`participant_id`
        = автор) отдала бы автору чужой флаг под видом собственного.

        Возвращает `None`, если строки нет: сервис превращает это в `404`
        того же класса, что «карточки нет» (D-7.2-8).
        """
        result = await self._session.execute(
            text(
                "SELECT CASE WHEN :viewer_is_participant "
                "THEN participant_confirmed ELSE author_confirmed END AS my_answer, status "
                "FROM attendance "
                "WHERE card_id = :card_id "
                "AND participant_id = :participant_id "
                "AND participant_id <> :author_id"
            ),
            {
                "card_id": card_id,
                "participant_id": participant_id,
                "author_id": author_id,
                # Зритель читает УЧАСТНИЧЕСКИЙ флаг только если он не автор
                # карточки. Проверять `participant_id == viewer_id` здесь
                # НЕЛЬЗЯ: при подделанной строке (участник = автор) автор
                # прочитал бы чужой флаг как свой (AC-10, A-SELF-03).
                "viewer_is_participant": viewer_id != author_id,
            },
        )
        row = result.first()
        if row is None:
            return None
        # Инвентаризация предикатов, строка 13: `None` (не ответил) НЕ должен
        # стать `False` (ответил «не пришёл»).
        return (row[0], AttendanceStatus(row[1]))

    async def get_overview(
        self, *, card_id: int, author_id: int
    ) -> tuple[bool | None, list[AttendanceStatus]] | None:
        """Флаг автора и статусы ВСЕХ пар карточки — один запрос (CR 7.2 M-2).

        Автор не принадлежит ни одной паре (D-7.1-2), поэтому его флаг живёт
        копией в каждой строке, а «его статуса» не существует: агрегат
        собирает сервис одной функцией (`_aggregate_status`).

        Флаг читается как три состояния: все `NULL` — не ответил, все `TRUE` /
        все `FALSE` — ответ, смесь — `AttendanceCorruptError` (CR 7.2 M-3).
        `None` — строк нет: компания не собрана (D-7.2-10).
        """
        result = await self._session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE author_confirmed IS NULL) AS nulls, "
                "bool_and(author_confirmed IS TRUE) AS all_true, "
                "array_agg(status) AS statuses "
                "FROM attendance "
                "WHERE card_id = :card_id AND participant_id <> :author_id"
            ),
            {"card_id": card_id, "author_id": author_id},
        )
        row = result.first()
        if row is None or not row[0]:
            return None
        total, nulls = int(row[0]), int(row[1])
        if nulls == total:
            my_answer: bool | None = None
        elif nulls == 0:
            my_answer = bool(row[2])
        else:
            raise AttendanceCorruptError(
                "флаг автора разошёлся между строками карточки "
                f"(card_id={card_id}, nulls={nulls}, total={total})"
            )
        statuses = [AttendanceStatus(value) for value in row[3]]
        return (my_answer, statuses)

    async def confirm(
        self,
        *,
        card_id: int,
        participant_id: int,
        author_id: int,
        author_side: bool,
        arrived: bool,
        now: datetime,
    ) -> tuple[AttendanceStatus, datetime] | None:
        """AC-01…AC-06, AC-11, D-1: CAS-подтверждение стороны.

        `author_side=True` — подтверждает АВТОР карточки (пишется
        `author_confirmed`), иначе участник (`participant_confirmed`).

        Возвращает `(status, confirmed_at)` из `RETURNING` при фактическом
        переходе и `None`, если строка не изменилась: терминальный статус уже
        достигнут ИЛИ ответ совпал с прежним (D-1).
        """
        result = await self._session.execute(
            text(_CONFIRM_SQL),
            {
                "is_author": author_side,
                "arrived": arrived,
                "card_id": card_id,
                "participant_id": participant_id,
                "author_id": author_id,
                "now": now,
            },
        )
        row = result.first()
        if row is None:
            return None
        return (AttendanceStatus(row[0]), row[1])

    async def confirm_author_side(
        self, *, card_id: int, author_id: int, arrived: bool, now: datetime
    ) -> list[tuple[AttendanceStatus, datetime | None]]:
        """CAS-подтверждение АВТОРА по всем парам (ADR-04).

        Возвращает по строке на каждую фактически изменённую пару; пустой
        список — идемпотентный повтор (D-1) либо терминальный статус (AC-06).
        """
        result = await self._session.execute(
            text(_CONFIRM_AUTHOR_SQL),
            {
                "is_author": True,
                "arrived": arrived,
                "card_id": card_id,
                "author_id": author_id,
                "now": now,
            },
        )
        return [(AttendanceStatus(row[0]), row[1]) for row in result.all()]

    async def list_participant_history(
        self,
        *,
        participant_id: int,
        limit: int,
        cursor: tuple[datetime, uuid_lib.UUID] | None,
    ) -> list[HistoryRow]:
        """AC-01, AC-05, AC-07, AC-08, AC-10, AC-11: страница истории явок.

        ОДИН SQL на страницу (Task 1.1, P-3): `LIMIT limit + 1` вместо
        «страница + `COUNT`» — `total` не нужен, UI скроллит по `next_cursor`
        (D-7.5-9). `COUNT(*) OVER ()` отвергнут: в 6.6 он усекался курсором,
        а лечение скалярным подзапросом добавило второй SQL.

        `limit + 1` — не оптимизация, а способ отличить «последняя страница»
        от «есть ещё» без второго запроса: сервис отдаёт `limit` строк и
        строит `next_cursor` только если пришла лишняя.

        `cursor` — АРГУМЕНТ, а не query-параметр: разбор строки в `datetime`
        и `UUID` живёт в кодеке (`attendance_history_cursor`), репозиторий
        не знает про base64.

        Порядок `created_at DESC, card_id DESC` детерминирован при равных
        `created_at` (AC-08): tie-break по `card_id`, поэтому страницы не
        пересекаются и не теряют строк.
        """
        params: dict[str, object] = {
            "participant_id": participant_id,
            "limit_plus_one": limit + 1,
        }
        if cursor is None:
            sql = _HISTORY_SQL
        else:
            sql = _HISTORY_AFTER_CURSOR_SQL
            params["cur_created_at"] = cursor[0]
            params["cur_card_uuid"] = cursor[1]
            # CR 7.5 H-1, D-7.5-10: карточка-якорь удалена (ретенция) — граница
            # берётся по верхнему `bigint`, чтобы tie-группа не потерялась.
            params["deleted_anchor_card_id"] = DELETED_ANCHOR_CARD_ID
        result = await self._session.execute(text(sql), params)
        return [
            HistoryRow(
                created_at=row[0],
                card_id=int(row[1]),
                card_uuid=row[2],
                card_title=row[3],
                starts_at=row[4],
                # `AttendanceStatus(row[5])`, а не `str`: неизвестное значение
                # из БД — рассинхрон `CHECK`/`StrEnum`, то есть дефект сервера
                # (`500`), а не строка, уехавшая клиенту (инвентаризация
                # предикатов, строка 17).
                status=AttendanceStatus(row[5]),
            )
            for row in result.all()
        ]
