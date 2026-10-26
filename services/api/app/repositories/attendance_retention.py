"""Репозиторий ретенции фактов явки (стори 10.6; AC-01, AC-02, AC-03).

**Почему `DELETE` мимо ORM.** `session.delete(attendance)` загрузил бы объект
в identity map и удалял по одному; каскад `reports` → `sanctions` отрабатывает
PostgreSQL (`0025`), а не unit-of-work. `delete(Attendance).where(...)` идёт
мимо ORM, каскады делает БД — тот же приём, что в `account_deletion.py`
(Ловушка 4 стори 10.2).

**Почему `EXISTS attendance` обязателен** (D-10.6-5). Без него карточки с уже
удалёнными `attendance` попадают в выборку вечно: каждый прогон берёт те же
200 «пустых» карточек, очередь не доходит до следующих. Это не
идемпотентность, а заклинивание — проверяется тестом 5.2.

**Почему срок считает PostgreSQL** (D-10.6-6). `timedelta(days=182)` короче
шести КАЛЕНДАРНЫХ месяцев (181–184 дня) и удалил бы данные на 1–3 дня раньше
срока в месяцах с 31 днём, нарушив AC-01 и NFR43. Поэтому предикат —
`starts_at < :moment - interval '<N> months'`, а не арифметика в Python.

**Почему число месяцев интерполируется** (P-2 CR 10.6). `ATTENDANCE_RETENTION_MONTHS`
объявлена ЗДЕСЬ и подставляется в текст запроса: до патча значение 6 жило
в двух несвязанных местах (константа в сервисе и литерал в SQL), и мутация
константы не красила тест границы срока. Теперь одно значение — один источник
истины, и `f"interval '{ATTENDANCE_RETENTION_MONTHS} months'"` не даёт им
разойтись. Константа в репозитории, а не в сервисе: сервис импортирует
репозиторий, обратный импорт был бы циклическим.

**Почему `FOR UPDATE ... SKIP LOCKED` на `event_cards`** (D-10.6-5, прецедент
`account_deletion.py:79`). Второй контейнер воркера не ждёт первого и не
берёт ту же карточку (NFR46–NFR49). Блокируется карточка, не `attendance`:
`FOR UPDATE` на `attendance` с `EXISTS`-подзапросом заблокировал бы строки,
которые ещё не выбраны, и `SKIP LOCKED` перестал бы разводить воркеры.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ATTENDANCE_RETENTION_MONTHS",
    "AttendanceRetentionRepository",
    "candidate_statement_sql",
]

#: NFR43: «карточка + 6 месяцев». Единственное место, где живёт число:
#: подставляется в текст запроса ниже (`interval '<N> months'`), поэтому
#: правка константы меняет срок и в SQL, и в тестах формы/границы (P-2 CR 10.6).
#: Не в `Settings`: настройка позволила бы на стенде поставить 0 и «доказать»
#: удаление, сломав NFR43 (тот же довод, что `DELETION_WINDOW_DAYS` в 10.2, D-10.2-4).
ATTENDANCE_RETENTION_MONTHS = 6

#: Частичный срез: только карточки, у которых ЕСТЬ факты явки и срок вышел.
#: Число месяцев интерполируется — значение задано ровно один раз, выше.
_EXPIRED_CANDIDATES_SQL = f"""
SELECT ec.id
  FROM event_cards ec
 WHERE ec.starts_at < CAST(:moment AS timestamptz) - interval '{ATTENDANCE_RETENTION_MONTHS} months'
   AND EXISTS (SELECT 1 FROM attendance a WHERE a.card_id = ec.id)
 ORDER BY ec.starts_at
 LIMIT :limit
   FOR UPDATE OF ec SKIP LOCKED
"""

#: Удаление строк пачкой по идентификаторам карточек.
#: `RETURNING id` вместо подсчёта `rowcount`: у `Result` в SQLAlchemy 2.0 нет
#: `rowcount` (`mypy --strict` это ловит), а число удалённых строк нужно
#: честное — «сколько нашли» и «сколько удалили» могут разойтись при гонке.
_DELETE_ATTENDANCE_SQL = "DELETE FROM attendance WHERE card_id = ANY(:card_ids) RETURNING id"


def candidate_statement_sql() -> str:
    """Текст запроса кандидатов — для unit-проверки формы SQL (P-16).

    Вынесен функцией, а не константой в тесте: копия текста в тесте не увидела
    бы правку репозитория и мутация «`interval '6 months'` → `'5 months'`»
    осталась бы SURVIVED (тот же урок, что `build_due_candidates_statement`
    в стори 10.2).
    """
    return _EXPIRED_CANDIDATES_SQL


class AttendanceRetentionRepository:
    """Запросы воркера ретенции явки. `commit` не делает — владелец сервис."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _validate_limit(limit: int) -> int:
        """P-15 (CR 10.2): `limit` проверяется ДО `LIMIT`.

        Без проверки `limit=-1` доходил до PostgreSQL и падал
        `LIMIT must not be negative`; воркер ловил это `except Exception`
        и логировал ошибку КАЖДЫЙ цикл — настоящая причина тонула в шуме.
        """
        if limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")
        return limit

    async def select_expired_card_ids(self, *, moment: datetime, limit: int) -> list[int]:
        """AC-01, AC-03: карточки, у которых срок хранения явки истёк.

        Возвращаются ВНУТРЕННИЕ `id` — наружу они не идут (project-context §7),
        лог несёт только счётчики (ADR-14). Строки заблокированы до коммита:
        второй воркер их пропустит, а не встанет в ожидание.
        """
        checked = self._validate_limit(limit)
        # Один источник текста: `candidate_statement_sql()` для unit-проверки
        # формы SQL и этот вызов. Копия запроса в тесте не увидела бы правку
        # репозитория (P-16), а две копии здесь разошлись бы при первой правке.
        statement = text(candidate_statement_sql())
        result = await self._session.execute(statement, {"moment": moment, "limit": checked})
        return [int(row[0]) for row in result.all()]

    async def delete_attendance_for_cards(self, card_ids: list[int]) -> int:
        """AC-01, AC-02: удаление строк явки по карточкам. Возвращает число строк.

        `ANY(:card_ids)` в одном операторе, а не цикл `DELETE` на карточку:
        200 отдельных запросов в цикле 60 с — это 200 round-trip'ов к БД.
        Каскад `reports`/`sanctions` отрабатывает PostgreSQL (D-10.6-3).
        """
        if not card_ids:
            return 0
        result = await self._session.execute(text(_DELETE_ATTENDANCE_SQL), {"card_ids": card_ids})
        # `RETURNING id` даёт честное число удалённых строк и типизируется:
        # у `Result` в SQLAlchemy 2.0 нет `rowcount` (`mypy --strict` это ловит).
        # Карточки заблокированы `FOR UPDATE SKIP LOCKED`, поэтому расхождения
        # «нашли / удалили» из-за гонки здесь не бывает — но счётчик берётся
        # из факта возврата, а не из предположения.
        return len(result.all())
