"""Гард 18+ по дате рождения (стори 12.2; AC-4, AC-5; ADR-17).

`users.age_confirmed` — **декларация** при регистрации: флажок «мне есть 18».
Он не проверяет ничего и не обновляется. Дата рождения проверяема: возраст
вычисляется на сервере в момент запроса.

Порядок источников (AC-5, grandfathering):

1. `profiles.birth_date` есть → решает он;
2. `birth_date` нет, но есть `profiles.age` → решает legacy-число;
3. анкеты нет вовсе → решает `users.age_confirmed` (онбординг не пройден).

Краснеет при:
- удалении проверки (M10) — несовершеннолетний получит доступ к UGC;
- замене `is_adult` на `age >= 17` (M11) — 17-летний пройдёт;
- отказе при отсутствии анкеты (M12) — сломается онбординг;
- инверсии приоритета `age` над `birth_date` (M13) — старая учётка
  с датой рождения будет оцениваться по устаревшему числу.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.main import app
from tests.integration.test_profiles_write_api import (
    ACCESS_COOKIE_NAME,
    _new_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент приложения с подменённой зависимостью сессии.

    Своя фикстура, а не импорт из соседнего модуля: pytest видит
    импортированные фикстуры, но имя `client` конфликтует с параметром
    тестов и ruff справедливо ругается `F811`. Здесь три строки —
    дешевле, чем псевдонимы.
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


def _birth_date_years_ago(years: int) -> date:
    """Дата рождения ровно `years` лет назад от сегодняшнего дня.

    Считается от `date.today()`, а не литералом: литерал «2008-01-01»
    через полгода перестанет быть несовершеннолетним, и тест начнёт
    доказывать обратное тому, что заявляет.
    """
    today = date.today()
    return date(today.year - years, today.month, today.day)


async def _insert_profile_with_birth_date(
    session: AsyncSession, user_id: int, birth_date: date
) -> None:
    """Анкета с датой рождения — сырым SQL, в обход сервиса.

    Сервис отклонил бы несовершеннолетнюю дату (`422 underage`), а проверить
    надо именно ГАРД ДОСТУПА: что будет с уже существующей строкой, если
    дата меньше 18 (так выглядит понижение возраста, ошибка ввода или
    подделка при регистрации, прошедшая до появления проверки).
    """
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, birth_date, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', :birth_date, 'female', 'текст', 'complete')"
        ),
        {"user_id": user_id, "birth_date": birth_date},
    )
    await session.commit()


async def test_underage_with_birth_date_gets_403(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-4: 17 полных лет — отказ `403 underage`, не доступ.

    Именно `403`: сессия валидна, пользователь опознан; запрещён ресурс.
    `401` заставил бы клиента обновлять токен и создал бы шторм refresh.
    """
    user, token = await _new_user(session)
    await _insert_profile_with_birth_date(session, user.id, _birth_date_years_ago(17))

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "underage"


async def test_eighteen_today_is_allowed(client: AsyncClient, session: AsyncSession) -> None:
    """Граница AC-6: ровно 18 СЕГОДНЯ — доступ есть.

    День рождения сегодня означает полные 18 лет уже наступили. Мутация
    `<` → `<=` в сравнении месяца-дня отнимает год и отказывает человеку
    в его собственный день рождения.
    """
    user, token = await _new_user(session)
    await _insert_profile_with_birth_date(session, user.id, _birth_date_years_ago(18))

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200


async def test_eighteen_tomorrow_is_denied(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Обратная граница: до дня рождения остался день — 17 полных лет."""
    user, token = await _new_user(session)
    tomorrow_birthday = _birth_date_years_ago(18) + timedelta(days=1)
    await _insert_profile_with_birth_date(session, user.id, tomorrow_birthday)

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "underage"


async def test_grandfathering_legacy_age_is_allowed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-5: 40 008 старых учёток — есть `age`, нет `birth_date` → доступ есть.

    Без этого весь парк существующих пользователей потерял бы вход
    в день деплоя: даты рождения у них нет и появиться не может.
    """
    user, token = await _new_user(session)
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', 32, 'female', 'текст', 'complete')"
        ),
        {"user_id": user.id},
    )
    await session.commit()

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 200
    assert response.json()["age"] == 32


async def test_birth_date_wins_over_legacy_age(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-5: при наличии обоих источников решает `birth_date`.

    Устаревшее число не должно «разрешать» доступ: иначе достаточно
    один раз сохранить `age=30` и обойти проверку возраста навсегда.
    """
    user, token = await _new_user(session)
    await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, birth_date, gender, bio, status) "
            "VALUES (:user_id, 'Аня', 'Томск', 30, :birth_date, 'female', 'текст', 'complete')"
        ),
        {"user_id": user.id, "birth_date": _birth_date_years_ago(17)},
    )
    await session.commit()

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "underage"


async def test_no_profile_keeps_consent_flow(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-5: анкеты нет — гард не отказывает, онбординг продолжается.

    Требовать анкету до её создания значило бы запереть пользователя:
    форма создания анкеты недоступна без анкеты.
    """
    user, token = await _new_user(session)

    response = await client.get(
        "/api/v1/profiles/me", cookies={ACCESS_COOKIE_NAME: token}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "profile_not_found"
