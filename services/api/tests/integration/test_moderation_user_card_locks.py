"""Замки карточки модератора (стори 12.10; AC-3…AC-6).

**Почему это замки, а не реализация.** Замер CS (P-28) показал: аудит
(FR60), вычисленный возраст, приоритет `birth_date` и три числа сводки уже
работают — их принесли 8.5, 8.6, 12.2 и 11.4. Стори не переписывает сервис
(риск регрессии 20 тестов 8.5), а фиксирует поведение тестами, которых не
было.

**Что именно не было покрыто.**

- AC-3: строка аудита проверялась только на УСПЕШНОМ чтении; ветвь `404`
  (D-96: факт попытки тоже аудируется) тестом не закрыта.
- AC-4: `birth_date` искали в РАЗОБРАННОМ теле, а не в сыром тексте ответа;
  поле могло бы уехать строкой внутри another значения.
- AC-5: `test_moderation_user_card.py:54` сеет только legacy-`age=28`;
  профиля с `birth_date` в тестах карточки не было вовсе.
- AC-6: три числа проверялись по отдельности в разных тестах, но ни один
  не доказывал, что они НЕ перепутаны между собой (U12: `against_total`
  подменённый на `filed_total` проходил, если значения совпадали).

**Почему три числа РАЗНЫЕ.** Если жалоб, санкций и явок посеять по одному,
подмена любого поля на любое другое останется зелёной. Разные значения —
единственный способ отличить поля друг от друга.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.age_service import override_today
from tests.integration.moderation_user_card_helpers import (
    add_attendance,
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


async def _set_birth_date(session: AsyncSession, *, user_id: int, value: date | None) -> None:
    """`birth_date` пишется напрямую: сервисов, меняющих её, кроме профиля, нет.

    Проверяется ЧТЕНИЕ карточки; запись даты рождения покрыта 12.2.
    """
    await session.execute(
        text("UPDATE profiles SET birth_date = :d WHERE user_id = :u"),
        {"d": value, "u": user_id},
    )
    await session.flush()


async def _audit_rows(session: AsyncSession) -> list[tuple[str, str | None, str | None, str | None]]:
    """Строки аудита: `action`, `moderator_uuid`, `subject_uuid`, `route`."""
    result = await session.execute(
        text(
            "SELECT action, moderator_uuid::text, subject_uuid::text, route "
            "  FROM access_logs ORDER BY id"
        )
    )
    return [
        (row[0], row[1], row[2], row[3])
        for row in result.all()
        # `access_logs` общая на прогон: чужие строки других тестов
        # отфильтрованы по `action` на стороне запроса выше не будут —
        # фильтр делается вызывающим по `subject_uuid`.
    ]


# --- AC-3: аудит FR60 ---------------------------------------------------------


async def test_successful_read_writes_exactly_one_audit_row(session: AsyncSession) -> None:
    """AC-3: успешное чтение — РОВНО одна строка с верными полями.

    «Ровно одна» — отдельное утверждение: дубль строки превратил бы
    журнал в счётчик просмотров, и по нему нельзя было бы отличить одно
    открытие от десяти.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    moderator, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text

    rows = [
        row
        for row in await _audit_rows(session)
        if row[2] == str(target.uuid) and row[0] == "card_read"
    ]
    assert len(rows) == 1, rows
    action, moderator_uuid, subject_uuid, route = rows[0]
    assert action == "card_read"
    assert moderator_uuid == str(moderator.uuid)
    assert subject_uuid == str(target.uuid)
    assert route is not None and route.endswith(str(target.uuid)), route


async def test_missing_user_still_writes_audit_row(session: AsyncSession) -> None:
    """AC-3, D-96: `404` тоже пишет строку — попытка доступа это факт.

    Краснеет, если `log_card_read` перенести ПОСЛЕ `raise UserNotFoundError`:
    тогда перебор адресов модератором не оставлял бы следа, и «искал ли он
    этого человека» стало бы неотвечаемым вопросом.
    """
    moderator, token = await make_moderator(session)
    missing = uuid_lib.uuid4()

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=missing)
        assert response.status_code == 404, response.text

    rows = [row for row in await _audit_rows(session) if row[2] == str(missing)]
    assert len(rows) == 1, rows
    action, moderator_uuid, _, route = rows[0]
    assert action == "card_read"
    assert moderator_uuid == str(moderator.uuid)
    assert route is not None and route.endswith(str(missing)), route


# --- AC-4: birth_date не покидает сервер -------------------------------------


async def test_birth_date_absent_from_raw_body(session: AsyncSession) -> None:
    """AC-4: `birth_date` нет в СЫРОМ тексте ответа, а `age` посчитан из неё.

    Разбор в `dict` недостаточен: поле могло бы уехать строкой внутри
    другого значения (например, в `photo_url` или в причине санкции),
    и обход ключей этого не увидел бы.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, age=99)
    await _set_birth_date(session, user_id=target.id, value=date(2000, 5, 15))
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        raw = response.text
        body = response.json()

    assert "birth_date" not in raw
    # Контроль: возраст посчитан ИЗ ДАТЫ, а не взят из legacy-колонки (99).
    # Без этой половины тест зеленел бы и на ответе без возраста вовсе.
    assert body["profile"]["age"] != 99
    assert body["profile"]["age"] is not None and body["profile"]["age"] > 0


# --- AC-5: grandfathering -----------------------------------------------------


async def test_legacy_age_only_is_shown(session: AsyncSession) -> None:
    """AC-5: профиль только с legacy-`age` — возраст есть, страница жива."""
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, age=28)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text

    assert response.json()["profile"]["age"] == 28


async def test_birth_date_only_is_computed(session: AsyncSession) -> None:
    """AC-5: профиль только с `birth_date` — возраст вычислен на `today`.

    `today` инъецируется (`override_today`), а не берётся из системных часов:
    иначе граница «17 vs 18 лет» работала бы только в день написания теста
    (`W-12.2-b`).
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, age=99)
    await _set_birth_date(session, user_id=target.id, value=date(2008, 10, 30))
    # Колонку `age` обнуляем: только дата рождения, как у новой учётки.
    await session.execute(
        text("UPDATE profiles SET age = NULL WHERE user_id = :u"), {"u": target.id}
    )
    await session.flush()
    _, token = await make_moderator(session)

    with override_today(date(2026, 10, 30)):
        async with build_client(session) as client:
            response = await get_card(client, token=token, user_uuid=target.uuid)
            assert response.status_code == 200, response.text

    assert response.json()["profile"]["age"] == 18


async def test_birth_date_wins_over_legacy_age(session: AsyncSession) -> None:
    """AC-5: при обоих источниках побеждает `birth_date`.

    Краснеет при подмене индексов в сборке профиля (U10): значения заведомо
    разные (28 против 18), поэтому подмена видна.
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target, age=28)
    await _set_birth_date(session, user_id=target.id, value=date(2008, 10, 30))
    _, token = await make_moderator(session)

    with override_today(date(2026, 10, 30)):
        async with build_client(session) as client:
            response = await get_card(client, token=token, user_uuid=target.uuid)
            assert response.status_code == 200, response.text

    assert response.json()["profile"]["age"] == 18


# --- AC-6: три числа сводки ---------------------------------------------------


async def test_three_summary_numbers_are_distinct(session: AsyncSession) -> None:
    """AC-6: три числа совпадают с посеянными и не перепутаны между собой.

    Числа РАЗНЫЕ (5 / 1 / 3) — при равных значениях подмена одного поля
    другим осталась бы зелёной.

    **Почему `against_total` = 5, а не 4.** У санкции есть жалоба-основание
    (`UNIQUE(report_id)`, D-17), и её обвиняемый — тот же пользователь.
    Значит ось `against` = 4 посеянных + 1 от санкции. Это поведение домена,
    а не дефект: проверено прогоном (получили 3 при посеве 2).
    """
    target, _ = await make_target_user(session)
    await set_profile(session, user=target)
    other, _ = await make_target_user(session)
    await add_reports(session, user=target, against=4, other_id=other.id)
    await add_sanctions(session, user=target, count=1, reporter_id=other.id)
    await add_attendance(session, user=target, count=3)
    _, token = await make_moderator(session)

    async with build_client(session) as client:
        response = await get_card(client, token=token, user_uuid=target.uuid)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["reports"]["against_total"] == 5
    assert body["sanctions"]["total"] == 1
    assert body["attendance"]["total"] == 3
    # Три числа РАЗНЫЕ — иначе подмена поля прошла бы незамеченной.
    numbers = {
        body["reports"]["against_total"],
        body["sanctions"]["total"],
        body["attendance"]["total"],
    }
    assert len(numbers) == 3, numbers
