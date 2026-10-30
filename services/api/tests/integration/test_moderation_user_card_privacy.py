"""Приватность карточки модератора (стори 8.5; AC-05, NFR27, D-66).

**Главное утверждение:** в `profile` РОВНО семь полей, и ни одного
идентификатора входа. Проверяется **сериализованный JSON**, а не модель:
поле можно добавить в `Profile` (или в SQL-проекцию), и оно уедет в ответ,
минуя локальный объект схемы.

**Почему allowlist, а не denylist.** `assert "phone" not in body` не поймал
бы ЛИШНИЙ ключ (`email`, `provider_user_id`, `token_version`): тест зелёный,
утечка есть. Сравнение множества ключей ловит и добавление, и удаление.
Тот же довод, что в `test_payload_has_no_complaint_text` (8.2).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.moderation_user_card_helpers import (
    add_reports,
    add_sanctions,
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

#: AC-05, D-66: ровно эти поля. Дополнение множества — утечка.
#:
#: Стори 11.4 (AC-6, NFR36) добавила седьмое поле `consent_version`. Это
#: осознанное расширение allowlist, а не утечка: версия правил — не
#: идентификатор входа и не контакт, а факт согласия, который владелец обязан
#: видеть (`W-10.4-b`). Тест по-прежнему ловит ЛЮБОЕ другое добавление.
EXPECTED_PROFILE_FIELDS = {
    "uuid",
    "name",
    "city",
    "age",
    "gender",
    "status",
    "consent_version",
    # Стори 12.10 (AC-1, AC-9, D-12.10-1): фото — осознанное расширение
    # allowlist, а не утечка. D-66 запрещал фото как СРЕДСТВО
    # ИДЕНТИФИКАЦИИ ВХОДА (по нему нельзя увести аккаунт), а подписанный URL
    # одобренного фото — тот же класс, что `EventCardAuthorProfile` (12.7).
    # Модератор разбирает жалобу на анкету и обязан видеть то же фото, что
    # видел жалобщик. Пара «ссылка + срок» — инвариант D-5a.4-6.
    #
    # Множество по-прежнему сравнивается РАВЕНСТВОМ: любое следующее
    # добавление (в том числе `storage_key`) покраснеет.
    "photo_url",
    "photo_expires_in",
}

#: Идентификаторы входа и служебные поля: их не должно быть в ответе НИКАК —
#: ни ключом, ни значением. `provider_user_id` — телефон или OAuth-id, по
#: которому можно увести аккаунт, а не разобрать жалобу.
FORBIDDEN_FRAGMENTS = (
    "phone",
    "provider_user_id",
    "provider",
    "deletion_requested_at",
    "token_version",
    "storage_key",
    "bio",
)


async def test_profile_has_exactly_allowlisted_fields(session: AsyncSession) -> None:
    """AC-05: allowlist полей профиля — лишний ключ это утечка.

    Стори 11.4: полей стало семь (`consent_version`). Тест не ослаблен —
    он по-прежнему сравнивает МНОЖЕСТВА, поэтому ловит любое добавление,
    включая будущее необоснованное.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        profile = response.json()["profile"]

    assert set(profile) == EXPECTED_PROFILE_FIELDS, set(profile) ^ EXPECTED_PROFILE_FIELDS
    # Стори 12.10 (AC-10, D-11): наружу уходит ПОДПИСАННЫЙ URL, а не ключ
    # объекта в бакете. Ключ — знание о внутренней раскладке хранилища, и по
    # нему нельзя построить ссылку без подписи; отдельный ассерт нужен, потому
    # что `photo_url` мог бы быть собран конкатенацией с ключом.
    assert "storage_key" not in profile


async def test_serialized_json_has_no_login_identifiers(session: AsyncSession) -> None:
    """AC-05, NFR27: идентификаторов входа нет в СЕРИАЛИЗОВАННОМ ответе.

    Проверяется текст ответа целиком, а не только `profile`: телефон мог бы
    уехать в любом блоке (например, в `reason` санкции модератор его
    процитировал). Здесь важно, что СИСТЕМА его не добавляет.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    other, _ = await make_target_user(session)
    await add_reports(session, user=target, filed=1, against=1, other_id=other.id)
    await add_sanctions(session, user=target, count=1, reporter_id=other.id)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        raw = response.text

    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in raw]
    assert leaked == [], f"утечка в ответе: {leaked}"
    # Контроль: телефон пользователя реально существует и не пуст — иначе
    # проверка выше прошла бы на пустой строке.
    assert target.phone is not None and target.phone != ""
    assert target.phone not in raw


async def test_report_brief_does_not_leak_participants(session: AsyncSession) -> None:
    """AC-06: в жалобе нет адресов участников и текста причины.

    Карточка показывает ФАКТ и ИСХОД. Кто на кого пожаловался и что написал
    модератор — это разбор (8.3/8.4); вынос их в сводку расширил бы доступ
    без потребителя.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    other, _ = await make_target_user(session)
    await add_reports(session, user=target, filed=1, against=1, other_id=other.id)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        reports = response.json()["reports"]

    for section in ("filed", "against"):
        for item in reports[section]:
            assert set(item) == {
                "uuid",
                "target_type",
                "status",
                "resolution",
                "reported_at",
                "resolved_at",
            }, set(item)
            assert "reporter_uuid" not in item
            assert "accused_uuid" not in item
            assert "reason" not in item
