"""Видимость анкет в выборке: сервисный вход (Task 3.1–3.3; AC-03, AC-04).

Правило «какие анкеты видит пользователь» живёт в одном месте — в билдере
запроса `ProfileRepository._build_visible_in_city_statement` (AC-04). Этот
модуль ничего не добавляет к условию: он только принимает город и передаёт
его в репозиторий.

Почему город — аргумент, а не `get_settings()` внутри (AC-03): значение
`PILOT_CITY` читается на уровне вызова (роутер или выборка 4.2) в момент
запроса. Если бы сервис читал настройки сам, смена города в конфигурации
требовала бы очистки кэша настроек в тестах и не была бы видна вызывающему.

`commit()` здесь не вызывается (D-4 стори 1.2): выдача только читает.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile
from app.repositories.profiles import ProfileRepository


async def list_visible_profiles(
    session: AsyncSession,
    *,
    pilot_city: str,
    limit: int,
) -> list[Profile]:
    """Task 3.1–3.3 (AC-01, AC-03, AC-04): видимые анкеты города пилота.

    `pilot_city` приходит аргументом от вызывающего слоя (обычно
    `settings.pilot_city`), поэтому смена `PILOT_CITY` меняет выдачу без
    правки кода (AC-03).

    Условие видимости сервис не собирает: единственная строка — вызов
    репозитория (AC-04). Второй текст `city = ...` в приложении недопустим.
    """
    repository = ProfileRepository(session)
    return await repository.list_visible_in_city(pilot_city, limit=limit)
