"""Подтверждение явки: API-сценарии (стори 7.2; TD 7.2 A-*).

Покрытие TD: `A-POST-01…04`, `A-TRUTH-06…09`, `A-TRUTH-ALL-09`,
`A-NOSANCTION-01`, `A-SELF-01…03`, `A-SELF-ORDER-01`, `A-TIME-01…03`,
`A-IDEM-01`, `A-UPDATED-01/02`, `A-ORACLE-01…07`, `A-NULL-ANSWER-01`,
`A-DISPUTED-SYMMETRY-01`, `A-SRC-01`, `A-ENUM-01`, `A-LOCK-01`.

**Состояние фикстуры — часть контракта анти-оракула** (TD, O-5). Точное число
SQL валидно ТОЛЬКО при `status='full'`, `expires_at` в прошлом И `starts_at`
в прошлом: `get_card` делает ленивое закрытие окна только для `published`,
поэтому `full` не мутирует и число запросов стабильно. Иначе тест «точного
числа» падал бы на КОРРЕКТНОМ коде (TD прямо предупреждает об этом).

**Время — данными и инъекцией `now`,** не `freezegun`/`sleep` (TD, R8).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.attendance import derive_attendance_status
from app.models.enums import AttendanceStatus
from app.models.user import User
from app.repositories.attendance import AttendanceRepository
from app.repositories.users import UserRepository
from app.services import attendance_service
from app.services.attendance_service import (
    AttendanceSelfConfirmError,
    confirm_attendance,
)
from app.services.session_service import ACCESS_COOKIE_NAME, issue_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

#: D-7.2-12 / ADR-04: у автора нет строки участия, поэтому «свой» ответ автора
#: читается как агрегат по всем парам карточки.
AUTHOR_ANSWER_AGGREGATED = True


class _Settings:
    """Минимум настроек: протокол `CardSettings` без чтения `.env`.

    Поля обязаны совпадать с протоколом целиком: `get_card` читает не только
    срез сообщества, но и окно решения (`card_default_decision_window_minutes`,
    `card_active_limit`, стоп-слова). Урезанная заглушка падала бы
    `AttributeError` на живом коде — то есть тест проверял бы заглушку.
    """

    default_community_slug = "tomsk"
    community_slugs = ("tomsk",)
    card_default_decision_window_minutes = 60
    card_content_stop_words: tuple[str, ...] = ()
    card_active_limit = 5


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """ASGI-клиент на тестовой сессии (SAVEPOINT-изоляция, §18.8)."""

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_session, None)


async def _user(session: AsyncSession) -> tuple[User, str]:
    """Пользователь с согласием (нужен `ConsentUser`) и access-токеном."""
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone",
        provider_user_id=phone,
        phone=phone,
        role="user",
        gender="female",
        status="active",
    )
    user.age_confirmed = True
    user.consent_given_at = datetime.now(UTC)
    await session.flush()
    issued = await issue_session(session, user)
    return user, issued.access_token


async def _company(
    session: AsyncSession,
    *,
    starts_in: timedelta = timedelta(hours=-1),
    expires_in: timedelta | None = None,
    status: str = "full",
    with_rows: bool = True,
    chat_uuid: uuid_lib.UUID | None = None,
    extra_members: int = 0,
) -> dict[str, Any]:
    """Автор + участник(+N) + опубликованная карточка (+ пары в `attendance`).

    `status='full'` по умолчанию намеренно: `get_card` лениво закрывает окно
    только у `published`, поэтому число SQL не плавает (контракт TD, O-5).

    `extra_members` (CR 7.2 H-5) — дополнительные участники компании. Нужны,
    чтобы проверить главное отклонение DS от TD (D-7.2-12): флаг автора
    дублируется по N строкам, событие одно на КАРТОЧКУ, а `card_status` —
    агрегат по всем парам. С одним участником эти свойства неотличимы от
    «одна пара = одна карточка», и дефекты H-1/H-2 проходили незамеченными.
    """
    author, author_token = await _user(session)
    member, member_token = await _user(session)

    place = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', :s, true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}", "s": settings.default_community_slug},
    )
    place_id = int(place.scalar_one())

    starts_at = datetime.now(UTC) + starts_in
    # По умолчанию окно решения закрыто до начала встречи (`expires_at` = −1 ч
    # от `starts_at`), как в реальной карточке. Для тестов числа SQL окно
    # оставляют ОТКРЫТЫМ, чтобы `get_card` не мутировал.
    expires_at = (
        datetime.now(UTC) + expires_in if expires_in is not None else starts_at - timedelta(hours=1)
    )
    capacity = 2
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid) "
            "VALUES (:a, :p, :s, 'Встреча', NULL, 'cafe', :st, :cap, 60, :e, :status, "
            ":filled, 'approved', :pub, :chat) "
            "RETURNING uuid, id"
        ),
        {
            "a": author.id,
            "p": place_id,
            "s": settings.default_community_slug,
            "st": starts_at,
            "cap": capacity,
            "e": expires_at,
            "status": status,
            "filled": capacity if status == "full" else 0,
            "pub": starts_at - timedelta(days=1),
            "chat": chat_uuid,
        },
    )
    card_uuid, card_id = card.one()

    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at, decided_at) VALUES (:c, :a, '', 'selected', now(), "
            "now() + interval '1 day', now())"
        ),
        {"c": int(card_id), "a": member.id},
    )
    # CR 7.2 H-5: дополнительные пары компании — по строке `attendance` на
    # каждого участника, как в проде после воркера 7.3.
    extra: list[tuple[Any, str]] = []
    for _ in range(extra_members):
        one_more, one_more_token = await _user(session)
        extra.append((one_more, one_more_token))
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
                "expires_at, decided_at) VALUES (:c, :a, '', 'selected', now(), "
                "now() + interval '1 day', now())"
            ),
            {"c": int(card_id), "a": one_more.id},
        )
    if with_rows:
        await session.execute(
            text("INSERT INTO attendance (card_id, participant_id) VALUES (:c, :p)"),
            {"c": int(card_id), "p": member.id},
        )
        for one_more, _ in extra:
            await session.execute(
                text("INSERT INTO attendance (card_id, participant_id) VALUES (:c, :p)"),
                {"c": int(card_id), "p": one_more.id},
            )
    await session.flush()

    return {
        "author": author,
        "author_token": author_token,
        "member": member,
        "member_token": member_token,
        "card_uuid": str(card_uuid),
        "card_id": int(card_id),
        "starts_at": starts_at,
        "extra": [{"user": user, "token": token} for user, token in extra],
    }


def _cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{ACCESS_COOKIE_NAME}={token}"}


async def _post(
    client: AsyncClient, company: dict[str, Any], *, token: str, arrived: Any
) -> Response:
    return await client.post(
        f"/api/v1/cards/{company['card_uuid']}/attendance",
        json={"arrived": arrived},
        headers=_cookie(token),
    )


async def _get(client: AsyncClient, company: dict[str, Any], *, token: str) -> Response:
    return await client.get(
        f"/api/v1/cards/{company['card_uuid']}/attendance", headers=_cookie(token)
    )


# ——— AC-01: запись своего флага (A-POST-01…04) ————————————————————————————


async def test_post_author_writes_own_flag(client: AsyncClient, session: AsyncSession) -> None:
    """A-POST-01 (AC-01): автор `true` → свой флаг записан, статус `pending`.

    Мутация M15 (`participant_confirmed` в `SET` вместо `author_confirmed`)
    делает тест красным: флаг оказался бы в чужой колонке.
    """
    company = await _company(session)

    response = await _post(client, company, token=company["author_token"], arrived=True)

    assert response.status_code == 200
    # CR 7.2 M-1: у автора пары нет, поэтому кроме `status` приходит агрегат
    # карточки `card_status` (у участника этого поля нет вовсе).
    assert response.json() == {"my_answer": True, "status": "pending", "card_status": "pending"}
    row = await session.execute(
        text(
            "SELECT author_confirmed, participant_confirmed, status FROM attendance "
            "WHERE card_id = :c"
        ),
        {"c": company["card_id"]},
    )
    assert row.one() == (True, None, "pending")


async def test_post_member_writes_own_flag(client: AsyncClient, session: AsyncSession) -> None:
    """A-POST-02 (AC-01): участник `false` → `my_answer=false`, статус `pending`.

    P-11: признак проверяется в ОБОИХ значениях — тест только на `true`
    прошёл бы и при константе `True`, и «не пришёл» стало бы невыразимо.
    """
    company = await _company(session)

    response = await _post(client, company, token=company["member_token"], arrived=False)

    assert response.status_code == 200
    assert response.json() == {"my_answer": False, "status": "pending"}
    row = await session.execute(
        text("SELECT author_confirmed, participant_confirmed FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert row.one() == (None, False)


async def test_second_confirmation_confirms(client: AsyncClient, session: AsyncSession) -> None:
    """A-POST-03 (AC-02): обе стороны `true` → `confirmed`."""
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)

    response = await _post(client, company, token=company["member_token"], arrived=True)

    assert response.status_code == 200
    assert response.json() == {"my_answer": True, "status": "confirmed"}
    row = await session.execute(
        text("SELECT status, confirmed_at IS NOT NULL FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert row.one() == ("confirmed", True)


async def test_second_confirmation_keeps_other_flag(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A-POST-04 / M9: чужой флаг НЕ перезаписывается своим.

    `ELSE t.<flag>` в `SET` — не стилистика: при `ELSE v.arrived` подтверждение
    участника обнулило бы уже данное подтверждение автора, и терминальный
    статус не наступил бы НИКОГДА (второе подтверждение терялось бы).
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)

    await _post(client, company, token=company["member_token"], arrived=True)

    row = await session.execute(
        text("SELECT author_confirmed, participant_confirmed FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert row.one() == (True, True)


# ——— AC-02…AC-04: матрица истинности на ЖИВОМ UPDATE (A-TRUTH-*) ——————


@pytest.mark.parametrize(
    ("author", "participant", "expected"),
    [
        (True, True, AttendanceStatus.CONFIRMED),
        (False, False, AttendanceStatus.NO_SHOW),
        (True, False, AttendanceStatus.DISPUTED),
        (False, True, AttendanceStatus.DISPUTED),
        (True, None, AttendanceStatus.PENDING),
        (False, None, AttendanceStatus.PENDING),
        (None, True, AttendanceStatus.PENDING),
        (None, False, AttendanceStatus.PENDING),
        (None, None, AttendanceStatus.PENDING),
    ],
)
async def test_truth_table_through_live_update(
    session: AsyncSession,
    author: bool | None,
    participant: bool | None,
    expected: AttendanceStatus,
) -> None:
    """A-TRUTH-ALL-09 (R2, M2, M3): 9 строк таблицы истинности — через SQL, не Python.

    Единственный способ поймать расхождение `STATUS_CASE_SQL` и
    `derive_attendance_status`: урок CR 7.1 — тест сравнивал Python с Python
    и оставался зелёным при мутации SQL.

    Мутации: M2 (форма `SET` вместо `FROM (VALUES …)`) → строка падает на
    `ck_attendance_status_consistency`; M3 (`THEN 'no_show' → 'confirmed'`)
    → расходится на `(false, false)`.
    """
    company = await _company(session)
    repo = AttendanceRepository(session)
    now = datetime.now(UTC)
    author_id = company["author"].id
    participant_id = company["member"].id

    if author is not None:
        await repo.confirm(
            card_id=company["card_id"],
            participant_id=participant_id,
            author_id=author_id,
            author_side=True,
            arrived=author,
            now=now,
        )
    if participant is not None:
        await repo.confirm(
            card_id=company["card_id"],
            participant_id=participant_id,
            author_id=author_id,
            author_side=False,
            arrived=participant,
            now=now,
        )

    row = await session.execute(
        text("SELECT status FROM attendance WHERE card_id = :c AND participant_id = :p"),
        {"c": company["card_id"], "p": participant_id},
    )
    status_from_db = row.scalar_one()

    assert status_from_db == expected.value
    # Второе свидетельство: Python-правило обязано дать тот же ответ.
    assert derive_attendance_status(author, participant) == expected


async def test_no_sanction_records_created(session: AsyncSession) -> None:
    """A-NOSANCTION-01 (AC-03): `no_show` НЕ создаёт записей о санкции.

    FR109 применяется в эпике 8; в 7.2 `no_show` только создаётся. Тест
    структурный: таблицы санкций в схеме нет вовсе, поэтому «санкция не
    применена» — проверяемое свойство, а не отсутствие кода в одном месте.
    """
    company = await _company(session)
    repo = AttendanceRepository(session)
    now = datetime.now(UTC)
    for side in (True, False):
        await repo.confirm(
            card_id=company["card_id"],
            participant_id=company["member"].id,
            author_id=company["author"].id,
            author_side=side,
            arrived=False,
            now=now,
        )

    status = await session.execute(
        text("SELECT status FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert status.scalar_one() == "no_show"
    # Стори 8.1 создала `sanctions` — «таблицы нет» больше не доказывает
    # «санкция не применена». Проверяется отсутствие СТРОК: `no_show` сам по
    # себе санкцию не создаёт, её выносит модератор в 8.4 (FR109).
    sanction_rows = await session.execute(text("SELECT count(*) FROM sanctions"))
    assert sanction_rows.scalar_one() == 0, (
        "`no_show` не должен создавать санкцию: её выносит модератор (FR109)"
    )


# ——— AC-08: анти-оракул по СЕМИ осям (A-ORACLE-01…07) —————————————


def _header_map(response: Response) -> dict[str, str]:
    """Заголовки ответа без волатильных: `date` меняется между запросами."""
    return {key: value for key, value in response.headers.items() if key.lower() not in {"date"}}


async def test_oracle_body_has_exactly_two_keys(client: AsyncClient, session: AsyncSession) -> None:
    """O-1: множество ключей тела — ровно `{my_answer, status}`.

    Любое добавленное поле (даже полезное вроде `confirmed_at`) делает тест
    красным: имя поля — это API-контракт, и третье поле пришлось бы
    поддерживать совместимым навсегда.
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)

    response = await _get(client, company, token=company["member_token"])

    assert response.status_code == 200
    assert set(response.json().keys()) == {"my_answer", "status"}


async def test_oracle_raw_body_has_no_leaking_substrings(
    client: AsyncClient, session: AsyncSession
) -> None:
    """O-2: в СЫРОМ теле нет подстрок `author`, `participant`, `arrived`, `confirmed_at`.

    Проверка текста, а не разобранного JSON: переименование поля в утечку
    (`author_arrived`) словарём не поймать, а байтами — да.
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)

    response = await _get(client, company, token=company["member_token"])

    for substring in ("author", "participant", "arrived", "confirmed_at"):
        assert substring not in response.text, f"утечка в теле ответа: {substring!r}"


async def test_oracle_own_flag_bytes_are_exact(client: AsyncClient, session: AsyncSession) -> None:
    """O-3 / M11: байт-в-байт для ответившего и для неответившего.

    Участник не ответил → `my_answer: null`, а НЕ `false`: «не подтвердил» и
    «не пришёл» — разные состояния (P-8, инвентаризация предикатов строка 13).
    Мутация M11 (`None` → `False`) делает тест красным.
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)

    member_view = await _get(client, company, token=company["member_token"])
    author_view = await _get(client, company, token=company["author_token"])

    # Участник: РОВНО два поля (анти-оракул O-1/O-3, D-7.2-4).
    assert member_view.json() == {"my_answer": None, "status": "pending"}
    # Автор: третье поле `card_status` — агрегат по всем парам (CR 7.2 M-1).
    assert author_view.json() == {
        "my_answer": True,
        "status": "pending",
        "card_status": "pending",
    }


async def test_oracle_headers_are_fixed(client: AsyncClient, session: AsyncSession) -> None:
    """O-4: полный набор заголовков — без `etag`, `last-modified` и `x-*` с id.

    `etag`/`last-modified` выдали бы МОМЕНТ второго ответа, то есть косвенно
    поведение второй стороны — та же утечка, что чужой флаг в теле.
    """
    company = await _company(session)

    response = await _get(client, company, token=company["member_token"])

    headers = _header_map(response)
    assert headers.get("cache-control") == "no-store"
    assert "etag" not in headers
    assert "last-modified" not in headers
    assert not any(key.startswith("x-") for key in headers)
    # `vary` ставит сам ASGI-слой для согласования содержимого: он одинаков
    # на всех ответах и потому не оракул. Проверяется отсутствие ВОЛАТИЛЬНЫХ
    # и идентифицирующих заголовков, а не точный список фреймворка.
    assert set(headers) <= {"content-type", "content-length", "cache-control", "vary"}


@contextmanager
def _capture_sql(session: AsyncSession) -> Iterator[list[str]]:
    """Перехватывает текст каждого SQL-запроса на движке сессии.

    Образец — `test_participants_oracle_headers.py:168-184`. Снятие слушателя
    в `finally` обязательно: иначе он продолжит считать запросы следующих
    тестов, и «точное число» станет случайным.
    """
    statements: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    bind_engine = session.get_bind()
    listen_engine = getattr(bind_engine, "sync_engine", bind_engine)
    event.listen(listen_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(listen_engine, "before_cursor_execute", _record)


async def test_oracle_exact_query_count(client: AsyncClient, session: AsyncSession) -> None:
    """O-5: ТОЧНОЕ число SQL на `GET` — контракт анти-оракула (D-3).

    Складывается из: (1) карточка с местом и автором — `get_visible_card`
    (`build_get_visible_statement`), (2) состояние явки — `get_state`.
    Второго запроса видимости нет; `chat_uuid` приходит из того же запроса
    (D-3 TD), поэтому лишний `SELECT` сделал бы тест красным.

    Фикстура зафиксирована: `status='full'`, `expires_at` и `starts_at`
    в прошлом → `get_card` не выполняет ленивых переходов и число стабильно
    (TD, «Фиксация состояния фикстуры — часть контракта»).
    """
    # Фикстура: окно решения ЕЩЁ ОТКРЫТО (`expires_at > now`) и встреча НЕ
    # началась (`starts_at > now`). Тогда `get_card` не выполняет ни
    # `close_due_card`, ни ленивый архив — иначе лишний `UPDATE` попал бы в
    # счёт, и тест падал бы на КОРРЕКТНОМ коде (TD прямо предупреждает).
    # `CHECK expires_at <= starts_at` соблюдён: 1 день < 3 дня.
    company = await _company(session, starts_in=timedelta(days=3), expires_in=timedelta(days=1))
    await session.flush()

    with _capture_sql(session) as statements:
        response = await _get(client, company, token=company["member_token"])

    assert response.status_code == 200
    # Предусловие фикстуры — часть теста: иначе падение было бы по чужой причине.
    #
    # Проверяется ПЕРВОЕ слово оператора, а не подстрока `UPDATE`: колонка
    # `updated_at` встречается в обычных `SELECT` и дала бы ложное срабатывание
    # (проверено прогоном — именно так тест падал на КОРРЕКТНОМ коде).
    operators = [statement.lstrip().split()[0].upper() for statement in statements]
    assert "UPDATE" not in operators, (
        f"ленивый переход в фикстуре: число SQL не про анти-оракул: {operators}"
    )
    # Состав числа — часть контракта (TD: «не магическое 4 без комментария»):
    #   1. `users`         — зритель, загруженный зависимостью `ConsentUser`;
    #   2. `event_cards`   — видимость карточки (`get_visible_card`); здесь же
    #                        приходит `chat_uuid` (D-3), второго запроса нет;
    #   3. `join_requests` — `viewer_participates` (участие зрителя);
    #   4. `attendance`    — собственное состояние (`get_state`), один запрос.
    # Любой лишний `SELECT` (второй запрос видимости, отдельный `chat_uuid`)
    # делает тест красным — это и есть анти-оракул по числу запросов.
    assert len(statements) == 4, f"точное число SQL изменилось ({len(statements)}): {operators}"


async def test_outsider_indistinguishable_from_missing_card(
    client: AsyncClient, session: AsyncSession
) -> None:
    """O-6 / M10: посторонний == несуществующая карточка (тело, статус, SQL).

    Мутация M10 (`404` постороннему → `422 ATTENDANCE_NOT_OPENED`) делает
    тест красным: посторонний узнал бы, что карточка существует (R7).
    """
    company = await _company(session)
    outsider, outsider_token = await _user(session)
    await session.flush()

    existing = await _get(client, company, token=outsider_token)
    missing = await client.get(
        f"/api/v1/cards/{uuid_lib.uuid4()}/attendance", headers=_cookie(outsider_token)
    )

    assert existing.status_code == missing.status_code == 404
    assert existing.content == missing.content
    assert _header_map(existing) == _header_map(missing)


async def test_disputed_status_is_symmetric(client: AsyncClient, session: AsyncSession) -> None:
    """O-7 / A-DISPUTED-SYMMETRY-01 (AC-04): `disputed` одинаков обеим сторонам.

    Различие только в `my_answer`; по `status` нельзя вывести, КТО ответил
    `false`. Иначе FR89 («без санкции и без указания») обходился бы на уровне
    API, а не UI (D-7.2-5).
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)
    await _post(client, company, token=company["member_token"], arrived=False)

    member_view = await _get(client, company, token=company["member_token"])
    author_view = await _get(client, company, token=company["author_token"])

    assert member_view.json() == {"my_answer": False, "status": "disputed"}
    assert author_view.json() == {
        "my_answer": True,
        "status": "disputed",
        "card_status": "disputed",
    }


async def test_null_answer_is_not_false(client: AsyncClient, session: AsyncSession) -> None:
    """A-NULL-ANSWER-01 (P-8, строка 13): не ответившая сторона — `null`, не `false`."""
    company = await _company(session)

    author_view = await _get(client, company, token=company["author_token"])
    member_view = await _get(client, company, token=company["member_token"])

    assert author_view.json() == {
        "my_answer": None,
        "status": "pending",
        "card_status": "pending",
    }
    assert member_view.json() == {"my_answer": None, "status": "pending"}


# ——— AC-10: самоподтверждение (A-SELF-01…03, A-SELF-ORDER-01) ———————————


async def test_self_confirm_error_maps_to_403(client: AsyncClient) -> None:
    """A-SELF-01 (AC-10, X-7.1-7): доменная ошибка самоподтверждения → `403`.

    **Почему не «вызвать сервис с подделанной ролью».** Через API состояние
    «зритель не автор, но его `id` = `author_id`» НЕДОСТИЖИМО: роль
    определяется по `event_cards.author_id`, и автор всегда распознаётся как
    автор. Поэтому сервисный guard — второй, недостижимый снаружи слой; его
    ДОСТИЖИМОЕ следствие проверяется здесь (код ответа), а сам запрет записи —
    тестом `A-SELF-02` через репозиторий.

    `403`, а не `422`: тело корректно, запрещено ДЕЙСТВИЕ.
    """
    from app.api.v1.cards import _domain_error

    response = _domain_error(AttendanceSelfConfirmError())

    assert response.status_code == 403
    assert response.body == b'{"code":"ATTENDANCE_SELF_CONFIRM"}'


async def test_self_confirm_blocked_in_repository(session: AsyncSession) -> None:
    """A-SELF-02 / M5: обход ЧЕРЕЗ РЕПОЗИТОРИЙ тоже не проходит.

    Второй уровень защиты (D-7.2-3): `participant_id <> :author_id` в самом
    `WHERE`. Без него мутация «убрать guard сервиса» осталась бы зелёной —
    ровно поэтому тест идёт мимо сервиса.
    """
    company = await _company(session, with_rows=False)
    author_id = company["author"].id
    await session.execute(
        text("INSERT INTO attendance (card_id, participant_id) VALUES (:c, :p)"),
        {"c": company["card_id"], "p": author_id},
    )
    await session.flush()

    result = await AttendanceRepository(session).confirm(
        card_id=company["card_id"],
        participant_id=author_id,
        author_id=author_id,
        author_side=True,
        arrived=True,
        now=datetime.now(UTC),
    )

    assert result is None, "подделанная строка не должна обновляться"
    row = await session.execute(
        text("SELECT status, author_confirmed FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert row.one() == ("pending", None)


async def test_self_confirm_read_uses_author_branch(session: AsyncSession) -> None:
    """A-SELF-03: автор в подделанной строке читает СВОЮ ветку, не участническую.

    Если бы ветка выбиралась по `participant_id == viewer_id`, автор счёл бы
    `participant_confirmed` своим флагом — то есть прочитал бы чужой ответ
    как собственный.
    """
    company = await _company(session, with_rows=False)
    author_id = company["author"].id
    await session.execute(
        text(
            "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
            "participant_confirmed, status, confirmed_at) "
            "VALUES (:c, :p, false, true, 'disputed', now())"
        ),
        {"c": company["card_id"], "p": author_id},
    )
    await session.flush()

    state = await AttendanceRepository(session).get_state(
        card_id=company["card_id"],
        viewer_id=author_id,
        author_id=author_id,
        participant_id=author_id,
    )

    assert state is None, "подделанная строка не читается как своя"


async def test_outsider_gets_404_before_time_check(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A-SELF-ORDER-01 (R7, порядок 2→3): посторонний получает `404`, не `422`.

    Карточка с `starts_at` в БУДУЩЕМ: если бы время проверялось раньше
    участия, посторонний получил бы `422 ATTENDANCE_NOT_OPENED` и узнал бы,
    что карточка существует.
    """
    company = await _company(session, starts_in=timedelta(hours=2), status="published")
    outsider, outsider_token = await _user(session)
    await session.flush()

    response = await _post(client, company, token=outsider_token, arrived=True)

    assert response.status_code == 404
    assert response.json() == {"code": "card_not_found"}


# ——— AC-07: границы времени (A-TIME-01…03) —————————————————————————————


async def test_before_starts_at_is_422(client: AsyncClient, session: AsyncSession) -> None:
    """A-TIME-01 (AC-07): до `starts_at` — `422 ATTENDANCE_NOT_OPENED`.

    Время — ДАННЫМИ (карточка с будущим `starts_at`), не `freezegun`: это
    нормальное опубликованное состояние, а не искусственное (TD, R8).
    Строка факта при этом не меняется, и карточка НЕ переходит в `finished`
    этим запросом.
    """
    company = await _company(session, starts_in=timedelta(hours=2), status="published")

    response = await _post(client, company, token=company["member_token"], arrived=True)

    assert response.status_code == 422
    assert response.json() == {"code": "ATTENDANCE_NOT_OPENED"}
    row = await session.execute(
        text("SELECT author_confirmed, participant_confirmed FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert row.one() == (None, None)
    card = await session.execute(
        text("SELECT status FROM event_cards WHERE id = :c"), {"c": company["card_id"]}
    )
    assert card.scalar_one() == "published", "ленивый переход не должен случаться от POST"


async def test_boundary_starts_at_equals_now_allows(
    session: AsyncSession,
) -> None:
    """A-TIME-02 (AC-07): `starts_at == now` — подтверждение РАЗРЕШЕНО.

    AC-07 говорит «до `starts_at`», а не «включительно до»: в момент начала
    встреча уже идёт. Требует инъекции `now` в сервис — без неё граница
    непроверяема (TD, требование к реализации).
    """
    company = await _company(session)
    starts_at: datetime = company["starts_at"]

    state = await confirm_attendance(
        session,
        card_uuid=uuid_lib.UUID(company["card_uuid"]),
        viewer_id=company["member"].id,
        arrived=True,
        viewer_community_slug=_Settings.default_community_slug,
        settings=_Settings(),
        now=starts_at,
    )

    assert state.my_answer is True
    assert state.status is AttendanceStatus.PENDING


async def test_published_past_card_allows_confirmation(
    session: AsyncSession,
) -> None:
    """A-TIME-03 / M14: признак времени — `starts_at`, а НЕ `status='finished'`.

    Карточка `published` с прошедшим `starts_at` (ленивый переход не
    выполнялся) обязана принимать подтверждение. Мутация M14 (проверять
    `status == finished`) делает тест красным.
    """
    company = await _company(session, starts_in=timedelta(hours=-1), status="published")

    state = await confirm_attendance(
        session,
        card_uuid=uuid_lib.UUID(company["card_uuid"]),
        viewer_id=company["member"].id,
        arrived=True,
        viewer_community_slug=_Settings.default_community_slug,
        settings=_Settings(),
    )

    assert state.my_answer is True


# ——— AC-05: идемпотентность (A-IDEM-01, A-UPDATED-01/02) ————————————————


async def test_repeat_post_is_noop(client: AsyncClient, session: AsyncSession) -> None:
    """A-IDEM-01 / M13 (AC-05, D-1): повтор с тем же ответом — `200` и no-op.

    Мутация M13 (пустой `RETURNING` → `409`) делает тест красным. D-1:
    повтор при `pending` — НАСТОЯЩИЙ no-op: `updated_at` не двигается, иначе
    аудит получал бы шум на каждом повторном нажатии.
    """
    company = await _company(session)
    await _post(client, company, token=company["member_token"], arrived=True)
    first = await session.execute(
        text("SELECT updated_at FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    first_updated = first.scalar_one()

    response = await _post(client, company, token=company["member_token"], arrived=True)

    assert response.status_code == 200
    assert response.json() == {"my_answer": True, "status": "pending"}
    second = await session.execute(
        text("SELECT updated_at FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert second.scalar_one() == first_updated, "повтор сдвинул updated_at (D-1 нарушен)"


async def test_repeat_after_terminal_keeps_result(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-05, AC-06 / M1: повтор ПОСЛЕ терминального статуса ничего не меняет.

    Мутация M1 (убрать `WHERE status='pending'`) делает тест красным: второй
    `UPDATE` перезаписал бы `status` и сдвинул `confirmed_at`, то есть
    терминальный статус вычислялся бы дважды (R1).
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)
    await _post(client, company, token=company["member_token"], arrived=True)
    before = await session.execute(
        text("SELECT status, confirmed_at FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    status_before, confirmed_before = before.one()

    response = await _post(client, company, token=company["author_token"], arrived=False)

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    after = await session.execute(
        text("SELECT status, confirmed_at, author_confirmed FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    status_after, confirmed_after, author_flag = after.one()
    assert (status_after, confirmed_after) == (status_before, confirmed_before)
    assert author_flag is True


async def test_updated_at_moves_on_real_change(client: AsyncClient, session: AsyncSession) -> None:
    """A-UPDATED-01 / M4 (AC-11, X-7.1-1): `updated_at > created_at` после записи.

    Мутация M4 (убрать `updated_at = :now` из `UPDATE`) делает тест красным:
    ORM-хук `onupdate` на сыром пути не срабатывает, и аудит не отличил бы
    «обновлено» от «создано и брошено».
    """
    company = await _company(session)

    await _post(client, company, token=company["member_token"], arrived=True)

    row = await session.execute(
        text("SELECT updated_at > created_at FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    assert row.scalar_one() is True


async def test_participant_repeat_after_terminal_does_not_overwrite(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-06 / M1 (R1): участник с ДРУГИМ ответом после терминального — no-op.

    **Почему этот тест нужен отдельно от `test_repeat_after_terminal_keeps_result`.**
    Предикат D-1 (`<свой флаг> IS DISTINCT FROM :arrived`) делает повтор с ТЕМ
    ЖЕ ответом no-op сам по себе — то есть маскирует отсутствие
    `WHERE status='pending'`. Защиту CAS видно только при ДРУГОМ ответе:
    `true` → `false` проходит предикат D-1, и остановить перезапись может
    только `status='pending'`.

    Мутация M1 (убрать `AND t.status = 'pending'`) делает тест красным:
    `status` пересчитался бы в `disputed`, `confirmed_at` сдвинулся бы — то
    есть терминальный статус вычислялся бы ДВАЖДЫ (ложная санкция в эпике 8).
    """
    company = await _company(session)
    await _post(client, company, token=company["author_token"], arrived=True)
    await _post(client, company, token=company["member_token"], arrived=True)
    before = await session.execute(
        text("SELECT status, confirmed_at FROM attendance WHERE card_id = :c"),
        {"c": company["card_id"]},
    )
    status_before, confirmed_before = before.one()

    # ДРУГОЙ ответ той же стороны: предикат D-1 его пропускает.
    response = await _post(client, company, token=company["member_token"], arrived=False)

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    after = await session.execute(
        text(
            "SELECT status, confirmed_at, participant_confirmed FROM attendance WHERE card_id = :c"
        ),
        {"c": company["card_id"]},
    )
    status_after, confirmed_after, participant_flag = after.one()
    assert (status_after, confirmed_after) == (status_before, confirmed_before), (
        "терминальный статус перезаписан: CAS-предикат не сработал"
    )
    assert participant_flag is True


# ——— Структурные тесты (A-SRC-01, A-ENUM-01, A-LOCK-01) ——————————————


def test_write_path_does_not_use_python_rule() -> None:
    """A-SRC-01 (R15): `derive_attendance_status` НЕ вызывается в пути записи.

    Два источника правды на одном переходе недопустимы: статус в БД считает
    `STATUS_CASE_SQL`. Python-правило используется только при ЧТЕНИИ ответа.

    Проверяется исходник сервиса, а не поведение: поведенческий тест не
    отличит «SQL посчитал» от «Python посчитал и записал».
    """
    import ast

    path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "app/services/attendance_service.py"
    )
    tree = ast.parse(path.read_text())
    # Проверяется ФАКТ импорта и вызовов, а не упоминание в тексте: имя
    # встречается в докстринге, и проверка подстроки была бы красной на
    # корректном коде (и зеленела бы при вызове через алиас).
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "derive_attendance_status" not in imported
    assert "derive_attendance_status" not in called


async def test_unknown_status_in_db_raises(session: AsyncSession) -> None:
    """A-ENUM-01 (R16): неизвестный `status` из БД → исключение, не тихое приведение.

    Рассинхрон `CHECK` со `StrEnum` обязан быть ВИДЕН как `500` на
    собственном дефекте. Подделать значение можно только в обход CHECK,
    поэтому строка вставляется сырым SQL с временно снятым ограничением.
    """
    company = await _company(session)
    # Подделка «неизвестного значения» невозможна при живых CHECK, поэтому
    # они снимаются в этой транзакции: тест проверяет РЕАКЦИЮ кода на
    # рассинхрон `CHECK` ↔ `StrEnum`, а не саму возможность его создать.
    await session.execute(text("ALTER TABLE attendance DROP CONSTRAINT ck_attendance_status"))
    await session.execute(
        text("ALTER TABLE attendance DROP CONSTRAINT ck_attendance_confirmed_at_consistency")
    )
    await session.execute(
        text("ALTER TABLE attendance DROP CONSTRAINT ck_attendance_status_consistency")
    )
    await session.execute(
        text("UPDATE attendance SET status = 'maybe' WHERE card_id = :c"),
        {"c": company["card_id"]},
    )

    with pytest.raises(ValueError):
        await AttendanceRepository(session).get_state(
            card_id=company["card_id"],
            viewer_id=company["member"].id,
            author_id=company["author"].id,
            participant_id=company["member"].id,
        )


# ——— CR 7.2 H-5 / H-1 / H-2: компания из N участников (D-7.2-12) ————————


async def test_author_confirms_all_rows_in_multi_member_company(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 7.2 H-5 (D-7.2-12, ADR-04): автор пишет СВОЙ флаг во ВСЕ строки.

    С одним участником это свойство неотличимо от «одна пара = одна
    карточка», поэтому дефект «подтверждение автора пишет только первую
    пару» проходил незамеченным (H-1/H-2).
    """
    company = await _company(session, extra_members=2)

    response = await _post(client, company, token=company["author_token"], arrived=True)

    assert response.status_code == 200
    body = response.json()
    assert body["my_answer"] is True
    # Агрегат карточки: все пары ещё `pending` — участники не ответили.
    assert body["status"] == "pending"
    assert body["card_status"] == "pending"

    rows = await session.execute(
        text(
            "SELECT author_confirmed, participant_confirmed, status "
            "FROM attendance WHERE card_id = :c"
        ),
        {"c": company["card_id"]},
    )
    all_rows = rows.all()
    assert len(all_rows) == 3, f"строк должно быть 3 (автор + 2 участника), {all_rows}"
    for author_flag, participant_flag, status in all_rows:
        assert author_flag is True, "флаг автора не дошёл до всех пар"
        assert participant_flag is None
        assert status == "pending"


async def test_partial_company_stays_pending_and_emits_no_event(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 7.2 H-1 / M16: одна из двух пар финальна — событие НЕ публикуется.

    Участник 1 подтверждает `true`, автор уже `true` → ЕГО пара `confirmed`,
    но вторая пара ещё `pending`. Агрегат карточки — `pending`, значит
    `attendance.done` объявил бы фиксацию, которой нет (дефект CR).
    """
    company = await _company(session, extra_members=1, chat_uuid=uuid_lib.uuid4())
    published: list[str] = []

    async def _fake_publish(*, chat_uuid: Any, envelope: Any) -> None:
        published.append(envelope.payload.get("status", "?"))

    monkeypatch.setattr(attendance_service, "publish_to_chat", _fake_publish)

    await _post(client, company, token=company["author_token"], arrived=True)
    response = await _post(client, company, token=company["member_token"], arrived=True)

    assert response.status_code == 200
    # Пара участника финальна — он видит ЕЁ статус.
    assert response.json() == {"my_answer": True, "status": "confirmed"}
    # Но карточка целиком — нет, и события быть не должно.
    assert published == [], f"событие при неполной карточке (H-1): {published}"

    author_view = await _get(client, company, token=company["author_token"])
    assert author_view.json()["card_status"] == "pending"


async def test_full_company_emits_single_event(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 7.2 H-2 / M17: последняя пара финализирует карточку — ОДНО событие.

    При двух участниках дефект давал по событию на каждую пару. Событие
    привязано к агрегату карточки, поэтому оно одно и несёт `card_status`.
    """
    company = await _company(session, extra_members=1, chat_uuid=uuid_lib.uuid4())
    published: list[str] = []

    async def _fake_publish(*, chat_uuid: Any, envelope: Any) -> None:
        published.append(envelope.payload.get("status", "?"))

    monkeypatch.setattr(attendance_service, "publish_to_chat", _fake_publish)

    await _post(client, company, token=company["author_token"], arrived=True)
    await _post(client, company, token=company["member_token"], arrived=True)
    assert published == [], "событие до финализации карточки"

    response = await _post(client, company, token=company["extra"][0]["token"], arrived=True)

    assert response.status_code == 200
    assert response.json() == {"my_answer": True, "status": "confirmed"}
    assert published == ["confirmed"], f"ожидалось ровно одно событие: {published}"

    author_view = await _get(client, company, token=company["author_token"])
    assert author_view.json()["card_status"] == "confirmed"


async def test_mixed_terminal_pairs_aggregate_to_disputed(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 7.2 M18 / D-7.2-12: разные терминальные статусы пар → `disputed`.

    Автор `true`; участник 1 `true` (пара `confirmed`), участник 2 `false`
    (пара `disputed`). Агрегат карточки обязан быть `disputed`, а не
    `confirmed`: иначе часть компаний «подтвердилась» без оснований.
    """
    company = await _company(session, extra_members=1, chat_uuid=uuid_lib.uuid4())
    published: list[str] = []

    async def _fake_publish(*, chat_uuid: Any, envelope: Any) -> None:
        published.append(envelope.payload.get("status", "?"))

    monkeypatch.setattr(attendance_service, "publish_to_chat", _fake_publish)

    await _post(client, company, token=company["author_token"], arrived=True)
    first = await _post(client, company, token=company["member_token"], arrived=True)
    assert first.json()["status"] == "confirmed"

    second = await _post(client, company, token=company["extra"][0]["token"], arrived=False)
    assert second.status_code == 200
    # Пара второго участника: (author=true, participant=false) → `disputed`.
    assert second.json() == {"my_answer": False, "status": "disputed"}

    author_view = await _get(client, company, token=company["author_token"])
    assert author_view.json()["card_status"] == "disputed"
    assert published == ["disputed"], f"событие должно нести агрегат: {published}"

    # У каждой пары — СВОЙ статус: адресность сохранена (ADR-04).
    rows = await session.execute(
        text(
            "SELECT participant_id, status FROM attendance WHERE card_id = :c "
            "ORDER BY participant_id"
        ),
        {"c": company["card_id"]},
    )
    statuses = dict(rows.all())
    assert statuses[company["member"].id] == "confirmed"
    assert statuses[company["extra"][0]["user"].id] == "disputed"


# ——— CR 7.2 H-3: ленивое создание строк и 409 при отсутствии пары ————————


async def test_author_confirm_creates_missing_rows(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 7.2 H-3 / M17: строк `attendance` ещё нет — подтверждение их создаёт.

    В проде строки создаёт воркер 7.3, но пользователь может подтвердить
    раньше. До исправления ответ был `200` с `my_answer=null` и НИЧЕГО не
    записывалось (тихая потеря факта).
    """
    company = await _company(session, with_rows=False, chat_uuid=uuid_lib.uuid4())

    response = await _post(client, company, token=company["author_token"], arrived=True)

    assert response.status_code == 200
    assert response.json()["my_answer"] is True, "подтверждение автора потерялось"

    rows = await session.execute(
        text(
            "SELECT author_confirmed, participant_confirmed, status "
            "FROM attendance WHERE card_id = :c"
        ),
        {"c": company["card_id"]},
    )
    created = rows.all()
    assert len(created) == 1, f"ожидалась ровно одна строка пары: {created}"
    assert created[0] == (True, None, "pending")


async def test_confirm_without_pair_is_409(client: AsyncClient, session: AsyncSession) -> None:
    """CR 7.2 H-3: принятой заявки нет — пары не будет даже после вставки.

    `409 ATTENDANCE_NO_PAIR`, а не тихий `200`: иначе клиент считал бы факт
    записанным, и он терялся бы молча.
    """
    company = await _company(session, with_rows=False)
    await session.execute(
        text("DELETE FROM join_requests WHERE card_id = :c"), {"c": company["card_id"]}
    )
    await session.flush()

    response = await _post(client, company, token=company["author_token"], arrived=True)

    assert response.status_code == 409
    assert response.json() == {"code": "ATTENDANCE_NO_PAIR"}


async def test_self_confirm_guard_blocks_service_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 7.2 L-3: guard AC-10 проверяется ПОВЕДЕНИЕМ сервиса, не кодом ответа.

    Через API состояние «зритель не автор, но `viewer_id == author_id`»
    недостижимо (роль определяется по `event_cards.author_id`), поэтому
    проверка кода ответа доказывала только маппинг `_domain_error`.
    Здесь карточка подменяется так, что сервис видит `is_author=False` при
    совпадающих идентификаторах, — и обязан отбить ВЫЗОВ, а не ответ.
    """
    company = await _company(session)
    author_id = company["author"].id

    async def _fake_load_card(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        card = SimpleNamespace(
            card=SimpleNamespace(
                author_id=author_id,
                id=company["card_id"],
                starts_at=datetime.now(UTC) - timedelta(hours=1),
                chat_uuid=None,
            ),
            viewer_participates=True,
        )
        return card, False

    monkeypatch.setattr(attendance_service, "_load_card", _fake_load_card)

    with pytest.raises(attendance_service.AttendanceSelfConfirmError):
        await confirm_attendance(
            session,
            card_uuid=uuid_lib.UUID(company["card_uuid"]),
            viewer_id=author_id,
            arrived=True,
            viewer_community_slug=_Settings.default_community_slug,
            settings=cast(Any, _Settings()),
        )
