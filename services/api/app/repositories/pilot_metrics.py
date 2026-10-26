"""Запросы метрик пилота (стори 10.5, Task 2; AC-01, AC-02, AC-05).

project-context §6: здесь только SQL — ни порогов, ни `ratio`, ни HTTP-кодов.
Пороги живут в сервисе (`services/pilot_metrics_service.py`), потому что
репозиторий не знает, что такое «провал»: он отдаёт пару
`(numerator, denominator)`.

**Почему пара, а не готовая доля.** AC-05.2 требует, чтобы `numerator` и
`denominator` доходили до клиента даже когда `value: null`: они ОБЪЯСНЯЮТ
`null`. Если бы репозиторий делил сам, при нулевом знаменателе информация о
том, что «знаменатель нулевой», терялась бы — клиент видел бы `null` без
причины. Поэтому деление — только в сервисе (`_ratio`).

**Почему `NULLIF` стоит в SQL всех четырёх метрик (AC-05.1, P-16).**
`NULLIF(знаменатель, 0)` — единственное место рождения `None` из базы. В
Python `except ZeroDivisionError` нет намеренно: он превратил бы отсутствие
`NULLIF` в незаметную деталь и сделал бы мутацию M1 («убрать `NULLIF`»)
неприменимой — мутация молча не нашла бы образец (класс P-20).
Проверяется grep-перечнем: 4 из 4.

**Почему `COALESCE` только у NFR2 (D-TD-3).** `filled_count` объявлен
`NOT NULL DEFAULT 0`, но `sum()` по НУЛЕВОМУ числу строк возвращает
`SQL NULL`, а не `0` — это свойство агрегата, а не колонки. `NULLIF` лечит
знаменатель, но числитель остался бы `NULL`, и `numerator: int` в схеме дал
бы `ValidationError` → `500` (нарушение AC-05.4). У остальных трёх метрик
числитель — `count(*)`, который на пустом множестве даёт `0`, а не `NULL`:
`COALESCE` им не нужен и был бы шумом.

**Ось окна — `event_cards.created_at` у ВСЕХ четырёх метрик (D-TD-1,
TD-10.5-1).** У `attendance` есть свой `created_at`, но это момент
ПОДТВЕРЖДЕНИЯ, а не создания встречи. Окно по нему означало бы «метрика по
дате подтверждения»: знаменатель NFR3 менялся бы задним числом, когда
участники отвечают на старые карточки, и «метрика на любую дату» (AC-01)
переставала бы быть воспроизводимой. Поэтому NFR3 фильтруется через
`JOIN event_cards` — в `attendance` нет ни `community_slug`, ни оси окна.

**Почему сырой `text()`, а не ORM-билдер.** Все четыре запроса — агрегаты с
`FILTER`, `COALESCE`/`NULLIF` и CTE. ORM-форма выражения `count(*) FILTER
(WHERE ...)` читалась бы хуже SQL и потребовала бы `case()` внутри
`func.count()`, где легко потерять `FILTER` и получить счёт по всем строкам
(дефект, который тест на «нулевую базу» не поймает).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.models.enums import AttendanceStatus, EventCardStatus

__all__ = [
    "ASSEMBLED_STATUSES",
    "PilotMetricsRepository",
]

#: AC-01, D-10.5-3: статусы карточки, участвующие в метриках пилота.
#:
#: `draft` исключён — карточку не видел никто, и её «несобранность» не
#: провал пилота. `cancelled` исключён — отмена это решение автора, а не
#: отказ аудитории (FR100). Остальные четыре — карточка существовала для
#: людей, и по ним меряется, собралась ли встреча.
ASSEMBLED_STATUSES: tuple[str, ...] = (
    EventCardStatus.PUBLISHED,
    EventCardStatus.FULL,
    EventCardStatus.CLOSED,
    EventCardStatus.FINISHED,
)

#: Общий предикат окна и среза сообщества для трёх запросов по `event_cards`.
#: Полуинтервал `[from, to)`: граничная карточка не попадёт в оба соседних
#: окна, и сумма двух окон равна окну их объединения (AC-01, P-19).
_WINDOW = """
      community_slug = :slug
  AND created_at >= :period_from
  AND created_at < :period_to
  AND status IN :statuses
"""


def _sql(query: str) -> TextClause:
    """`text()` с разворачиванием `:statuses` в список значений.

    `status IN :statuses` с кортежем SQLAlchemy 2.0 НЕ разворачивает сам: без
    `expanding=True` драйвер получает `IN $4` и PostgreSQL отвечает
    `syntax error at or near "$4"`. Дефект воспроизведён исполнением на живой
    БД до правки — поэтому разворачивание задано здесь, а не подразумевается.

    Хелпер один на четыре запроса: разворачивание, забытое в одном из них,
    дало бы `500` только на этой метрике, и «нулевая база» такой запрос не
    отличила бы от исправного — на пустой таблице ошибка та же, но заметна
    лишь при отрисовке ответа.
    """
    return text(query).bindparams(bindparam("statuses", expanding=True))


class PilotMetricsRepository:
    """Четыре метрики пилота по живым данным (AC-02: без агрегатов и кэша).

    Каждый метод возвращает пару `(numerator, denominator)` целыми числами.
    `denominator == 0` — единственный источник `None` в сервисе (через
    `NULLIF` в SQL, AC-05.1).

    Запросов ровно четыре — по одному на метрику (Task 3.7: ≤ 4 обращений к
    БД за вызов). Сливать их в один `UNION` ради «одного round-trip» значило
    бы получить запрос, у которого `EXPLAIN` показывает четыре разных плана,
    и индекс под метрики нельзя было бы проверить по отдельности (AC-06).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _params(
        self, community_slug: str, period_from: datetime, period_to: datetime
    ) -> dict[str, object]:
        return {
            "slug": community_slug,
            "period_from": period_from,
            "period_to": period_to,
            "statuses": ASSEMBLED_STATUSES,
        }

    async def count_assembled(
        self, *, community_slug: str, period_from: datetime, period_to: datetime
    ) -> tuple[int, int]:
        """NFR1: доля собранных встреч (≥ 30%).

        «Собранная» = `filled_count >= 1` (ADR-10, AC-01 стори 10.5). Занятость
        берётся из `filled_count`, а не из числа заявок: `filled_count` —
        единственная истина о занятости, её держит двусторонний CHECK
        `ck_event_cards_full_capacity` (ADR-10).
        """
        statement = _sql(
            f"""
            SELECT
                count(*) FILTER (WHERE filled_count >= 1) AS numerator,
                NULLIF(count(*), 0) AS denominator
              FROM event_cards
             WHERE {_WINDOW}
            """
        )
        row = (
            await self._session.execute(
                statement, self._params(community_slug, period_from, period_to)
            )
        ).one()
        return int(row.numerator), int(row.denominator or 0)

    async def count_joined_avg(
        self, *, community_slug: str, period_from: datetime, period_to: datetime
    ) -> tuple[int, int]:
        """NFR2: присоединений на карточку (≥ 1.5) — `sum(filled_count) / count(*)`.

        `COALESCE(sum(filled_count), 0)` обязателен (D-TD-3): на пустом окне
        `sum()` даёт `SQL NULL`, и без `COALESCE` ответ стал бы `500` вместо
        `200` с `value: null` (AC-05.4).

        Знаменатель тот же, что у NFR1, — метрики сравнимы между собой.
        """
        statement = _sql(
            f"""
            SELECT
                COALESCE(sum(filled_count), 0) AS numerator,
                NULLIF(count(*), 0) AS denominator
              FROM event_cards
             WHERE {_WINDOW}
            """
        )
        row = (
            await self._session.execute(
                statement, self._params(community_slug, period_from, period_to)
            )
        ).one()
        return int(row.numerator), int(row.denominator or 0)

    async def count_attendance_confirmed(
        self, *, community_slug: str, period_from: datetime, period_to: datetime
    ) -> tuple[int, int]:
        """NFR3: подтверждённая явка (≥ 60%) по карточкам, где явку уже спрашивали.

        **`JOIN event_cards` обязателен** (D-TD-1, TD-10.5-1). В `attendance`
        нет `community_slug`, а его `created_at` — момент подтверждения.
        Без JOIN метрика была бы ОДИНАКОВОЙ для любого окна и по всем
        сообществам сразу: AC-01 («метрики на любую дату») для NFR3 не
        выполнялся бы, а тест на окно был бы либо красным, либо декоративным
        (зелёным просто потому, что данные фикстуры попали в одно окно).

        **Знаменатель — карточки с `attendance` ЛЮБОГО статуса, включая
        `pending`** (TD-10.5-3). Воркер 7.3 создаёт строки `pending` заранее,
        и карточка, где никто не ответил, обязана попасть в знаменатель:
        иначе NFR3 занижается до 0% на 14-й день и NFR6 даёт ЛОЖНОЕ
        предупреждение. Сузить знаменатель до `status='confirmed'` нельзя —
        метрика стала бы тавтологией 100%.

        `disputed` в числитель не входит (FR89: «не подтверждено»), `pending`
        тоже — числитель требует `bool_and(status = 'confirmed')`, то есть
        подтверждения ВСЕХ пар карточки.
        """
        statement = _sql(
            f"""
            WITH cards AS (
                SELECT id
                  FROM event_cards
                 WHERE {_WINDOW}
            ), asked AS (
                SELECT DISTINCT a.card_id
                  FROM attendance a
                  JOIN cards c ON c.id = a.card_id
            ), confirmed AS (
                SELECT a.card_id
                  FROM attendance a
                  JOIN cards c ON c.id = a.card_id
                 GROUP BY a.card_id
                HAVING bool_and(a.status = :confirmed_status)
            )
            SELECT
                (SELECT count(*) FROM confirmed) AS numerator,
                NULLIF((SELECT count(*) FROM asked), 0) AS denominator
            """
        )
        params = self._params(community_slug, period_from, period_to)
        params["confirmed_status"] = AttendanceStatus.CONFIRMED
        row = (await self._session.execute(statement, params)).one()
        return int(row.numerator), int(row.denominator or 0)

    async def count_repeat_authors(
        self, *, community_slug: str, period_from: datetime, period_to: datetime
    ) -> tuple[int, int]:
        """NFR4: доля повторных авторов (≥ 25%).

        Повторный автор — тот, у кого в окне `>= 2` карточки. Знаменатель —
        число АВТОРОВ, а не карточек: метрика про «возвращаются ли люди»,
        и автор с одной карточкой не должен разбавлять знаменатель дважды
        (иначе один активный автор с пятью встречами весил бы как пятеро).

        `GROUP BY author_id` — по идентификатору пользователя. Группировка по
        `uuid` карточки сделала бы каждую группу размером 1, числитель всегда
        0, и метрика молча показывала бы «повторных авторов нет» (мутация M4).
        """
        statement = _sql(
            f"""
            SELECT
                count(*) FILTER (WHERE cards >= 2) AS numerator,
                NULLIF(count(*), 0) AS denominator
              FROM (
                SELECT author_id, count(*) AS cards
                  FROM event_cards
                 WHERE {_WINDOW}
                 GROUP BY author_id
            ) AS per_author
            """
        )
        row = (
            await self._session.execute(
                statement, self._params(community_slug, period_from, period_to)
            )
        ).one()
        return int(row.numerator), int(row.denominator or 0)
