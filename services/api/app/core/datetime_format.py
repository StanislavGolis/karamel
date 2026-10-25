"""Единый формат даты для API (project-context §4).

Патч 11 код-ревью стори 1.6: логика naive→UTC + `Z` жила в двух местах
(`api/v1/auth.py` и `schemas/consent.py`) — два источника правды для одного
формата. Любая правка в одном из них расходила бы ответы `/auth/me`
и `/auth/consent`. Теперь функция одна.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc(value: datetime | None) -> str | None:
    """project-context §4: дата — ISO 8601 UTC с `Z`; отсутствие — `null`.

    Naive datetime трактуется как UTC: иначе один и тот же объект
    сериализовался бы по-разному в зависимости от зоны процесса.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
