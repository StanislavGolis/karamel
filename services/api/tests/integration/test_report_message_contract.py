"""Контракт подачи жалобы (стори 8.2; AC-05, AC-09; D-27, VS-BLOCKER).

**Главное утверждение — два чтения чата не перепутаны.** Роут жалобы читает
чат БЕЗ фильтра статуса (`get_by_uuid_including_hidden`), иначе повторная
жалоба на уже скрытый диалог получила бы `404` вместо `409`, и
`ALREADY_REPORTED` стал бы недостижим. Это BLOCKER, найденный на VS.

**Второе утверждение — порядок «сначала субъект, потом вход»** (AC-09,
`W-1`): битый `accused_uuid` даёт `404` на несуществующем чате и `422` на
существующем. Это КОНТРАКТ, а не дефект: роль и участие зрителя уже известны
из сессии, поэтому различие ничего не раскрывает.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.report_message_helpers import (
    MessageChat,
    make_client,
    make_message_chat,
    post_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _hidden_company(session: AsyncSession) -> MessageChat:
    """Компания, у которой диалог уже скрыт первой жалобой автора."""
    company = await make_message_chat(session)
    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )
        assert response.status_code == 201, response.text
    return company


async def test_repeat_on_hidden_chat_gives_409_not_404(session: AsyncSession) -> None:
    """AC-05 + VS-BLOCKER: повтор на СКРЫТОМ диалоге — `409`, не `404`.

    Доказательство того, что роут жалобы читает чат без фильтра `status`
    (`get_by_uuid_including_hidden`). Мутация M9 (перевести роут на
    `get_by_uuid`) делает тест красным: вернулся бы `404`.
    """
    company = await _hidden_company(session)

    async with make_client(session) as client:
        second = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.member.uuid,
        )

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "ALREADY_REPORTED"


async def test_hidden_chat_other_side_can_report(session: AsyncSession) -> None:
    """AC-05: вторая сторона на СКРЫТОМ диалоге тоже может жаловаться.

    Жалоба адресуется человеку, а не состоянию диалога: скрытие одной
    стороной не лишает вторую права на свою жалобу (FR38/FR39 — обе стороны
    должны узнать о решении).
    """
    company = await _hidden_company(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.member_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.author.uuid,
        )

    assert response.status_code == 201, response.text
    count = await session.execute(text("SELECT count(*) FROM reports"))
    assert count.scalar_one() == 2


async def test_malformed_accused_rejected_by_schema_before_db(session: AsyncSession) -> None:
    """AC-09: битый UUID отбивается СХЕМОЙ (`422`) независимо от чата.

    **Порядок «сначала субъект, потом вход» здесь ненаблюдаем, и это факт.**
    `accused_uuid: uuid.UUID` валидируется Pydantic-ом на входе роута, то есть
    ДО любого обращения к БД. Поэтому синтаксически битое значение даёт `422`
    и на существующем, и на несуществующем чате.

    Это не нарушение приватности: клиент, приславший `не-uuid`, уже знает,
    что ошибся, и `422` не сообщает ничего о существовании диалога.
    Наблюдаемый порядок проверяется ниже — на СЕМАНТИЧЕСКИ валидном адресе,
    которого нет в системе (D-29).
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        on_existing = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid="не-uuid",
        )
        on_missing = await post_report(
            client,
            token=company.author_token,
            chat_uuid="00000000-0000-0000-0000-00000000dead",
            accused_uuid="не-uuid",
        )

    # Оба — `422`: схема отбила вход до работы с БД, и чат не повлиял.
    assert on_existing.status_code == 422, on_existing.text
    assert on_missing.status_code == 422, on_missing.text
    assert on_existing.json()["code"] == on_missing.json()["code"] == "invalid_request"


async def test_unknown_accused_on_missing_chat_gives_404(session: AsyncSession) -> None:
    """AC-09: СЕМАНТИЧЕСКИ валидный, но несуществующий адресат при НЕТ чате → `404`.

    Здесь порядок «сначала субъект, потом вход» НАБЛЮДАЕМ: UUID проходит
    схему, и первым делом сервис проверяет чат. `404` — а не `422`/`404`
    в зависимости от адресата.
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid="00000000-0000-0000-0000-00000000dead",
            accused_uuid="00000000-0000-0000-0000-00000000beef",
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"


async def test_unknown_accused_on_existing_chat_gives_404(session: AsyncSession) -> None:
    """AC-09: тот же несуществующий адресат на СУЩЕСТВУЮЩЕМ чате → тот же `404`.

    Ответ НЕ зависит от существования адресата: иначе перебором `uuid` можно
    было бы выяснить, кто зарегистрирован (ADR-13, NFR33).
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid="00000000-0000-0000-0000-00000000beef",
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"


async def test_report_on_self_gives_404(session: AsyncSession) -> None:
    """AC-06 (8.2): жалоба на себя — `404`, не `422`.

    `422` подтвердил бы «ты в этом диалоге». Проверка стоит до чтения пары:
    иначе для автора она дала бы тот же `404`, но по другому поводу, и тест
    не отличил бы один дефект от другого.
    """
    company = await make_message_chat(session)

    async with make_client(session) as client:
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=company.author.uuid,
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"


async def test_accused_outside_dialog_gives_404(session: AsyncSession) -> None:
    """AC-02: обвиняемый — не сторона диалога → `404`.

    Адрес валиден и пользователь существует, но модератор увидел бы пару,
    которой в диалоге нет. Один код с «не участник» — разделять нельзя
    (ADR-13).
    """
    company = await make_message_chat(session)
    # Третий человек: зарегистрирован, но в компании этой карточки не состоит.
    async with make_client(session) as client:
        from tests.integration.report_message_helpers import make_user

        outsider, _ = await make_user(session)
        response = await post_report(
            client,
            token=company.author_token,
            chat_uuid=company.chat_uuid,
            accused_uuid=outsider.uuid,
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "card_not_found"
