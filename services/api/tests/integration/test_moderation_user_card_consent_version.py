"""Версия согласия в карточке модератора (стори 11.4; AC-6, NFR36).

**Главное утверждение:** владелец видит, кто принял 1.1, кто 1.2, а кто
ничего не переподтверждал (`null`). Без этого AC-6 невыполним: колонка
существует, но вопроса «кого просить согласиться заново» она не решает.

Проверяется СЕРИАЛИЗОВАННЫЙ ответ, а не модель: поле могло бы уехать в
схему и не попасть в SQL-проекцию — тогда `null` был бы у всех, и тест на
модели это скрыл бы.

Краснеет, если:
- убрать `u.consent_version` из `_PROFILE_SQL` — все три ответа дадут `null`;
- убрать `consent_version=` из сборки `ModerationUserProfile` — то же;
- добавить поле вне allowlist (см. `test_moderation_user_card_privacy`).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_user_card_helpers import (
    build_client,
    get_card,
    make_moderator,
    make_target_user,
    set_profile,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _set_version(session: AsyncSession, *, user_id: int, version: str | None) -> None:
    """Ставит версию напрямую: сервисов, пишущих её, кроме `/consent`, нет.

    Прямой `UPDATE` — единственный способ получить три состояния сразу
    (`NULL`, `1.0`, `1.2`) без трёх живых согласий с разными настройками.
    Проверяется ЧТЕНИЕ карточки, а запись покрыта `test_consent_api`.
    """
    await session.execute(
        text("UPDATE users SET consent_version = :v WHERE id = :i"),
        {"v": version, "i": user_id},
    )


async def test_card_shows_consent_version_for_three_users(session: AsyncSession) -> None:
    """AC-6: `1.0`, `1.2` и `null` различимы в ответе карточки.

    Три учётки в одном тесте — потому что утверждение именно про РАЗЛИЧИЕ:
    тест «поле есть» прошёл бы и при константе `null` у всех.
    """
    target_old, _ = await make_target_user(session)
    target_new, _ = await make_target_user(session)
    target_none, _ = await make_target_user(session)
    for target in (target_old, target_new, target_none):
        await set_profile(session, user=target)

    await _set_version(session, user_id=target_old.id, version="1.0")
    await _set_version(session, user_id=target_new.id, version="1.2")
    # `target_none` остаётся с `NULL` — состояние всех учёток до миграции.

    _, token = await make_moderator(session)

    async with build_client(session) as client:
        versions = {}
        for label, target in (
            ("1.0", target_old),
            ("1.2", target_new),
            ("null", target_none),
        ):
            response = await get_card(client, token=token, user_uuid=target.uuid)
            assert response.status_code == 200, response.text
            versions[label] = response.json()["profile"]["consent_version"]

    assert versions == {"1.0": "1.0", "1.2": "1.2", "null": None}, versions


async def test_card_consent_version_is_null_when_never_given(session: AsyncSession) -> None:
    """AC-2, AC-6: учётка без согласия — `null`, а не строка `""` или `"0"`.

    Краснеет, если в SQL-проекции стоит `coalesce(consent_version, '')`
    или в схеме — дефолт `"0"`: модератор принял бы «неизвестно» за версию.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        profile = response.json()["profile"]

    assert "consent_version" in profile
    assert profile["consent_version"] is None
