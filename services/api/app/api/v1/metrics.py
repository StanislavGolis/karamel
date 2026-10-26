"""Метрики пилота для кабинета администратора (стори 10.5, Task 4.2; AC-01…AC-05).

Роут без бизнес-логики: расчёт живёт в `services/pilot_metrics_service.py`,
SQL — в `repositories/pilot_metrics.py` (project-context §6). Здесь только
HTTP-форма: гард, валидация окна, конверт ошибки и сериализация.

**Гард — `ModeratorUser`, путь — `/metrics`** (D-10.5-1). Роли `admin` в
`UserRole` НЕТ (`user` · `moderator`), и заводить её ради одного роута
означало бы миграцию `users` + `CHECK` + три гарда (AR30: «отдельной роли не
заводим»). Путь описывает НАЗНАЧЕНИЕ ресурса — метрики администратора, — а
не имя роли. Отказ — тот же `403 {"code": "forbidden"}`, что у
`/moderation/cards`: две формы одной ошибки дали бы клиенту два разбора.

**Кэш не вводится** (AC-02.3, ADR-14): ответ обязан меняться сразу после
изменения данных, иначе `GET` перестаёт быть окном в живую базу и становится
вторым источником истины. `no-store` ставит `NoStoreMiddleware` на каждый
ответ — ручная установка заголовка создала бы второй путь (QD A6).

**Валидация окна — здесь, а не в Pydantic-схеме ответа.** `from`/`to`
приходят query-параметрами: `422 invalid_period` и `422 period_too_long` —
семантика запроса, а не формы тела. Pydantic уже отбил не-даты, поэтому
остаётся сравнение.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ModeratorUser
from app.core.db import get_session
from app.schemas.pilot_metrics import PilotMetricsRead
from app.services.pilot_metrics_service import build_metrics

router = APIRouter(prefix="/metrics", tags=["metrics"])

#: AC-01.4: верхняя граница окна — год. Без неё `from=1970-01-01` превращал
#: бы запрос в полный проход по таблице с `Seq Scan` и обходил бы бюджет p95.
MAX_WINDOW_DAYS = 365

_NO_STORE = {"cache-control": "no-store"}


def _require_aware(value: datetime) -> datetime | None:
    """AC-01.3: `from`/`to` обязаны содержать пояс; naive → `None` (→ 422).

    P-1 (BLOCKER): Pydantic принимает `2026-05-01T00:00:00` — `format:
    date-time` в OpenAPI разрешает значение без смещения. Дальше сравнение
    `period_from >= period_to` на naive-значении даёт `TypeError: can't
    compare offset-naive and offset-aware datetimes` (`500`), а при заданном
    `PILOT_STARTED_AT` — `TypeError` уже на вычитании в `_pilot_day`.

    Отвергается именно `422 invalid_period`, а НЕ «додумываем UTC»: пояс —
    часть контракта, и молчаливое угадывание дало бы окно, сдвинутое на
    часовой пояс процесса (на машине разработчика и в контейнере разное).
    Тот же конверт, что у перевёрнутого окна: клиенту один разбор.

    Значение со смещением (`+07:00`) принимается и приводится к UTC: Томск
    живёт в `+07:00`, и отбивать валидный `date-time` было бы регрессом.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return None
    return value.astimezone(UTC)


def _error(code: str, status_code: int) -> JSONResponse:
    """Конверт `{"code": ...}` (project-context §4).

    `details` не передаётся: коды `invalid_period`/`period_too_long`
    самодостаточны, а произвольные подробности стали бы второй формой
    контракта, которую клиент начнёт разбирать.
    """
    return JSONResponse({"code": code}, status_code=status_code, headers=_NO_STORE)


@router.get("", response_model=PilotMetricsRead)
async def get_pilot_metrics(
    _moderator: ModeratorUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    period_from: Annotated[datetime, Query(alias="from")],
    period_to: Annotated[datetime, Query(alias="to")],
) -> PilotMetricsRead | JSONResponse:
    """AC-01: четыре метрики пилота на выбранное окно `[from, to)`.

    Все четыре метрики присутствуют ВСЕГДА; при отсутствии данных
    `value: null` и `passed: null` (AC-01.1, AC-05.3). Ответ — всегда `200`
    при валидном окне: предупреждение NFR6 не блокирует пилот (AC-03.3).

    `from < to` обязательно (AC-01.3): пустое и перевёрнутое окно не «ноль
    данных», а ошибка запроса — иначе администратор увидел бы «нет данных»
    там, где он перепутал даты.
    """
    # AC-01.3, P-1: пояс обязателен — до любых сравнений и вычитаний.
    # `None` от `_require_aware` означает naive-вход; `422 invalid_period`
    # вместо `TypeError` → `500` на валидном по контракту `format: date-time`.
    aware_from = _require_aware(period_from)
    aware_to = _require_aware(period_to)
    if aware_from is None or aware_to is None:
        return _error("invalid_period", 422)
    period_from, period_to = aware_from, aware_to

    # AC-01.3: `from < to` — включительно-строгое сравнение. Равенство
    # отвергается: окно нулевой длины не содержит ни одной карточки, и ответ
    # «нет данных» скрыл бы ошибку ввода.
    if period_from >= period_to:
        return _error("invalid_period", 422)

    # AC-01.4: год — предел окна. `<=`, не `<`: ровно 365 дней легальны.
    if (period_to - period_from).days > MAX_WINDOW_DAYS:
        return _error("period_too_long", 422)

    metrics = await build_metrics(session, period_from=period_from, period_to=period_to)

    # `model_validate` с `from_attributes=True` в схеме: датаклассы сервиса
    # имеют ровно те же имена полей, и ручная пересборка словарей была бы
    # вторым (и нетипизированным) описанием того же контракта — `mypy`
    # отвергал её как `dict[str, object]` в `List[MetricRead]`.
    return PilotMetricsRead.model_validate(metrics)
