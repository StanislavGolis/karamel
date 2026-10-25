"""Модель факта явки `attendance` (стори 7.1; AC-01…AC-05).

**N записей по паре «автор ↔ участник» (ADR-04), не строка на карточку.**
При вместимости 2–6 компания — это автор + N участников, а FR108/FR109
требуют адресности («кто именно не пришёл»). Агрегат её теряет: по нему
нельзя ни пожаловаться (7.4), ни показать историю (7.5). NFR3 («метрика по
компаниям») при этом считается агрегатным запросом `WHERE card_id = ...`
и схему не меняет.

**`status` — ПРОИЗВОДНОЕ от пары флагов**, а не независимое поле. Правило
перехода существует в ДВУХ представлениях, и они обязаны совпадать:

- `derive_attendance_status()` — Python (сервисы 7.2–7.5 читают и отвечают);
- `STATUS_CASE_SQL` — SQL `CASE` для атомарного `UPDATE ... RETURNING` 7.2,
  где статус вычисляется в БД, а не в приложении.

Третье представление — `CHECK status_consistency` — стережёт инвариант для
вставок сырым SQL и из воркера 7.3. Расхождение любого из трёх — красный
тест (`test_attendance_truth_table.py`, `test_attendance_schema.py`).

**`uuid` нет намеренно.** Таблица не адресуется по API: публичный адрес
в 7.2 — `uuid` карточки (`POST /cards/{uuid}/attendance`), а внутренняя
адресация — пара `(card_id, participant_id)`. `uuid` у неадресуемых таблиц
запрещён project-context §7 (`likes`, `profile_views`, `refresh_tokens`).

**`attendance_prompted_at` здесь НЕТ.** Маркер запроса подтверждения
принадлежит карточке (`event_cards.attendance_prompted_at`, с 4.1): воркер
7.3 идёт по карточкам, а не по парам, и CAS (ADR-15) делается по карточке.
Копия в `attendance` дала бы два источника правды — класс дефекта P-8.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import ATTENDANCE_STATUSES, AttendanceStatus


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений StrEnum.

    Седьмая копия функции (`user.py`, `place.py`, `event_card.py`,
    `profile.py`, `join_request.py`, `profile_photo.py`, здесь) —
    существующий долг в `deferred-work.md`. CR 7.1 исправил прежнее
    утверждение «четвёртая копия»: фактических копий семь, и объём долга
    был описан неверно. Вынос в общий модуль затронул бы шесть закрытых
    стори, поэтому внутри 7.1 он не делается.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


def derive_attendance_status(
    author_confirmed: bool | None,
    participant_confirmed: bool | None,
) -> AttendanceStatus:
    """AC-03: единственное Python-представление таблицы истинности (9 строк).

    Сравнения — ТОЛЬКО `is True` / `is False`, никогда `if flag:`: `None`
    в булевом контексте даёт `False`, и `pending` превратился бы в `no_show`
    (класс дефекта 6.7, правило P-8).

    Правило (совпадает с `CHECK status_consistency` дословно):

    - обе стороны `true` → `confirmed`;
    - обе `false` → `no_show` (единственный статус, влекущий санкцию, FR109);
    - обе известны и различаются → `disputed` (санкции НЕТ, FR89);
    - иначе (хотя бы одна сторона `NULL`) → `pending`.
    """
    # AC-03: согласие — только при ЯВНОМ `true` у обеих сторон.
    if author_confirmed is True and participant_confirmed is True:
        return AttendanceStatus.CONFIRMED
    # AC-03: обоюдная неявка — только при ЯВНОМ `false` у обеих сторон.
    if author_confirmed is False and participant_confirmed is False:
        return AttendanceStatus.NO_SHOW
    # AC-03: расхождение — только когда ОБЕ стороны известны и различаются.
    # Проверка `is not None` обязательна: `True != None` даёт `True`, и без неё
    # односторонний ответ стал бы `disputed` вместо `pending`.
    if (
        author_confirmed is not None
        and participant_confirmed is not None
        and author_confirmed != participant_confirmed
    ):
        return AttendanceStatus.DISPUTED
    return AttendanceStatus.PENDING


#: AC-03: то же правило в SQL для `UPDATE ... RETURNING` (7.2). Ветки — от
#: самого частного к общему; `ELSE 'pending'` покрывает все комбинации с
#: `NULL`. `IS TRUE`/`IS FALSE` вместо `= true`: на `NULL` сравнение дало
#: бы `NULL`, и `CASE` ушёл бы не в ту ветку.
#:
#: КРИТИЧНО ДЛЯ 7.2 (CR 7.1, C1). В `UPDATE t SET a = ..., status = <CASE> ...`
#: выражения `SET` вычисляются по СТАРОМУ кортежу, а не по новому. Проверено
#: на PostgreSQL 18: `UPDATE t SET a=true, p=true, status=<этот CASE>` даёт
#: `a=t, p=t, status='pending'`, и строка падает на `ck_attendance_status_consistency`.
#: Поэтому подставлять `STATUS_CASE_SQL` можно ТОЛЬКО в форму, где `CASE`
#: читает новые значения через отдельный источник строк:
#:
#: ```sql
#: UPDATE attendance AS t
#:    SET author_confirmed = v.a,
#:        participant_confirmed = v.p,
#:        status = <STATUS_CASE_SQL с v.a/v.p>,
#:        confirmed_at = CASE WHEN <терминально> THEN now() ELSE NULL END
#:   FROM (VALUES (:a, :p)) AS v(a, p)
#:  WHERE t.card_id = :card AND t.participant_id = :participant
#:    AND t.status = 'pending'
#: RETURNING t.status;
#: ```
#:
#: В этой форме `CASE` ссылается на `v.a`/`v.p` (новые значения), а не на
#: колонки `t`, которые ещё старые. Эквивалентность Python-версии проверяется
#: исполнением на живой БД (`test_status_case_sql_matches_derive`).
#:
#: Строка, а не билдер: 7.2 подставит её в один `UPDATE` с NEW-значениями
#: флагов, и любая параметризация здесь сделала бы правило зависящим от
#: места вызова.
STATUS_CASE_SQL = """CASE
    WHEN author_confirmed IS TRUE AND participant_confirmed IS TRUE THEN 'confirmed'
    WHEN author_confirmed IS FALSE AND participant_confirmed IS FALSE THEN 'no_show'
    WHEN author_confirmed IS NOT NULL AND participant_confirmed IS NOT NULL THEN 'disputed'
    ELSE 'pending'
END"""

#: AC-02: предикат консистентности статуса и флагов. Четыре ветки — ровно
#: те строки таблицы истинности, где `status` терминален; всё прочее
#: (`pending`) допускается только при неполном наборе флагов.
#:
#: `IS TRUE`/`IS FALSE`/`IS NOT NULL` и `<>` — не стилистика: `NOT
#: author_confirmed` на `NULL` даёт `NULL`, и `CHECK` пропустил бы строку
#: `(NULL, NULL, 'confirmed')` — то есть факт «все пришли» без единого
#: подтверждения.
STATUS_CONSISTENCY_SQL = """(status = 'pending'
        AND NOT (author_confirmed IS NOT NULL AND participant_confirmed IS NOT NULL))
    OR (status = 'confirmed'
        AND author_confirmed IS TRUE AND participant_confirmed IS TRUE)
    OR (status = 'no_show'
        AND author_confirmed IS FALSE AND participant_confirmed IS FALSE)
    OR (status = 'disputed'
        AND author_confirmed IS NOT NULL AND participant_confirmed IS NOT NULL
        AND author_confirmed <> participant_confirmed)"""

#: AC-02: бикондиционал `pending ⟺ confirmed_at IS NULL`. Терминальный
#: статус без метки фиксации потерял бы момент, ради которого он существует
#: (ретенция ADR-07, аудит модератора 7.5); `pending` с меткой утверждал бы
#: фиксацию, которой не было.
CONFIRMED_AT_CONSISTENCY_SQL = "(status = 'pending') = (confirmed_at IS NULL)"


class Attendance(Base, TimestampMixin):
    """Факт явки участника на встречу автора. Одна строка — одна пара."""

    __tablename__ = "attendance"

    # D-2: внутренний PK, наружу не выходит никогда.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # AC-01: карточка встречи. `CASCADE` — удаление карточки уносит факты её
    # явки: без встречи они не имеют смысла ни для жалобы (7.4), ни для аудита.
    card_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("event_cards.id", ondelete="CASCADE"), nullable=False
    )

    # AC-01: УЧАСТНИК, не автор. Автор здесь вторым FK не нужен: он один на
    # карточку (`event_cards.author_id`), и его флаг дублируется в каждой
    # строке компании (D-7.1-2). Так адресность сохраняется без ещё одной
    # таблицы «кто был автором на момент встречи».
    participant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # AC-01, AC-03: подтверждение автора. `NULL` — не подтвердил.
    # Три состояния (`NULL`/`true`/`false`) — часть контракта, а не
    # недоделка: «ещё не ответил» и «ответил: не пришёл» различаются, и
    # именно на этом различии стоит `pending` (таблица истинности, строки 1–5).
    author_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # AC-01, AC-03: подтверждение участника. Симметрично авторскому.
    participant_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # AC-02: состояние факта. `server_default` нужен вставкам вне ORM
    # (воркер 7.3 создаёт строки пар сырым `INSERT`): без него NOT NULL
    # потребовал бы значения, а «по умолчанию» здесь ровно `pending`.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=AttendanceStatus.PENDING,
        server_default=text("'pending'"),
    )

    # AC-02: момент фиксации терминального статуса. `NULL` у `pending`
    # (бикондиционал `ck_attendance_confirmed_at_consistency`).
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # AC-02: имя PK строится конвенцией (`pk_attendance`) — как в остальных
        # моделях, где `PrimaryKeyConstraint` объявлен явно ради совпадения
        # с миграцией.
        PrimaryKeyConstraint("id"),
        # AC-02: одна запись на пару «карточка + участник». Имя НЕ задаётся:
        # у конвенции `uq` нет токена `%(constraint_name)s`, поэтому явный
        # короткий `name` дал бы имя без префикса и разошёлся бы с миграцией
        # (дефект 3.1). Конвенция соберёт `uq_attendance_card_id_participant_id`.
        UniqueConstraint("card_id", "participant_id"),
        # AC-04 стори 8.1 (D-14): цель составного FK из `reports`.
        # `(attendance_id, event_card_id) -> attendance (id, card_id)` требует
        # уникальности ссылаемой пары; `id` — уже PK, поэтому индекс
        # расширяет, а не дублирует. В `0025` строится `CONCURRENTLY`
        # (на объёме `ACCESS EXCLUSIVE` недопустим) — объявление здесь нужно,
        # чтобы `alembic check` видел объект и не считал его лишним.
        UniqueConstraint("id", "card_id"),
        # AC-02: состояния — только значения `AttendanceStatus`.
        CheckConstraint(_in_clause("status", tuple(ATTENDANCE_STATUSES)), name="status"),
        # CR 7.1, D2: `id > 0` — симметрично `ck_users_id_positive`. `Identity()`
        # = `GENERATED BY DEFAULT`, значит явная вставка `id = 0` физически
        # проходит; без этого ограничения асимметрия с `users` была случайной.
        CheckConstraint("id > 0", name="id_positive"),
        # AC-02, AC-03: инвариант таблицы истинности (см. `STATUS_CONSISTENCY_SQL`).
        CheckConstraint(STATUS_CONSISTENCY_SQL, name="status_consistency"),
        # AC-02: терминальный статус ⟺ метка фиксации.
        CheckConstraint(CONFIRMED_AT_CONSISTENCY_SQL, name="confirmed_at_consistency"),
        # AC-02, FR108: история явок пользователя (7.5) — предикат
        # `WHERE participant_id = :id` с фильтром и сортировкой по `status`.
        # Индекс по `card_id` НЕ создаётся: префикс `UNIQUE (card_id,
        # participant_id)` его уже покрывает (принцип 6.1 — не дублировать).
        Index("ix_attendance_participant_status", "participant_id", "status"),
        # AC-07 стори 7.5 (X-7.1-4, D-7.5-5): история явок (7.5) сортируется по
        # `created_at DESC, card_id DESC` с keyset-пагинацией. Индекс выше
        # обслуживает `WHERE participant_id=? AND status=?`, но второй столбец
        # `status` без равенства-фильтра бесполезен, а `created_at` в нём нет —
        # `ORDER BY` уходил в `Sort` (проверено `EXPLAIN` на 20 000 строк).
        # Старый индекс НЕ удаляется: он обслуживает фильтр по статусу.
        #
        # Направления заданы `desc()`, а не `text("created_at DESC")`: так
        # `alembic check` видит те же имена колонок, что и отражение БД, и не
        # рапортует ложный дифф (AC-12).
        Index(
            "ix_attendance_participant_created",
            "participant_id",
            desc("created_at"),
            desc("card_id"),
        ),
    )
