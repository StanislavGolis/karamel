"""Уведомления жизненного цикла заявки (стори 9.2; AC-1…AC-5, Task 5/6).

**Почему собственный движок и настоящие коммиты.** `notification_service`
коммитит СВОЮ короткую транзакцию после доменной (D-9.2-4). Через
savepoint-сессию `session` это неотличимо от «ничего не записано»: строка
живёт внутри внешней транзакции и откатывается вместе с ней. Здесь коммиты
настоящие, поэтому в конце каждая строка удаляется вручную (`_cleanup`).

**Почему проверяется и `delivered_at`, и содержимое.** AC-5 требует ОБА
состояния (§18.11): заполнено при успехе шины, пусто при её отказе. Тест,
который смотрит только `count(*)`, зеленел бы и при уведомлении не тому
адресату — поэтому проверяются `type`, адресат и payload.

**Почему Redis подменяется точечно.** `publish_to_user` берёт клиент через
`get_redis()`; подмена этой функции в модуле-источнике даёт исход «шина
недоступна» без остановки живого Redis, которым пользуются другие тесты.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.enums import JoinRequestStatus
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import join_request_service, ws_publisher
from app.services.join_request_service import submit_join_request
from app.services.notification_service import (
    notify_request_received,
    notify_request_selected,
)
from app.services.session_service import issue_session

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="нужны реальные PostgreSQL и Redis: DATABASE_URL + REDIS_URL",
    ),
]


class _RequestSettings:
    """Протокол `JoinRequestSettings` без чтения `.env` (приём 5б.2).

    Настройки передаются в сервис явно: `JoinRequestSettings` — это
    `Protocol`, инстанциировать его нельзя, а чтение `.env` в тесте сделало бы
    результат зависящим от окружения разработчика.
    """

    default_community_slug = settings.default_community_slug
    join_request_active_limit = 2
    s3_signed_url_ttl = 900


_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield created
    await created.dispose()


@pytest.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Убирает созданные строки в порядке, обратном FK-зависимостям.

    `notifications` удаляется ЯВНО: строки создаёт сервис, и держать уборку на
    `ON DELETE CASCADE` от `users` значило бы ломать изоляцию при смене
    правила ретенции (урок D-TD-3).
    """
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    async with factory() as session:
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM notifications WHERE user_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text(
                    "DELETE FROM chats WHERE event_card_id IN "
                    "(SELECT id FROM event_cards WHERE author_id = ANY(:ids))"
                ),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text(
                    "DELETE FROM join_requests WHERE card_id IN "
                    "(SELECT id FROM event_cards WHERE author_id = ANY(:ids))"
                ),
                {"ids": _CREATED_USERS},
            )
            await session.execute(
                text("DELETE FROM event_cards WHERE author_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
        if _CREATED_PLACES:
            await session.execute(
                text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
            )
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": _CREATED_USERS}
            )
        await session.commit()


# ——— помощники ————————————————————————————————————————————————————————————


async def _new_user(session: AsyncSession) -> User:
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
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
    _CREATED_USERS.append(int(user.id))
    return user


async def _new_users(session: AsyncSession, count: int) -> list[User]:
    users: list[User] = []
    for _ in range(count):
        users.append(await _new_user(session))
    return users


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, community_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_community_slug},
    )
    place_id = int(row.scalar_one())
    _CREATED_PLACES.append(place_id)
    return place_id


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    capacity: int = 1,
    status: str = "published",
) -> tuple[uuid_lib.UUID, int]:
    now = datetime.now(UTC)
    starts = now + timedelta(days=3)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, :capacity, 60, "
            ":expires, :status, 0, 'approved', :published_at, NULL) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": starts,
            "capacity": capacity,
            "expires": starts - timedelta(hours=1),
            "status": status,
            "published_at": starts - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return uuid_lib.UUID(str(card_uuid)), int(card_id)


async def _rows_for(session: AsyncSession, *, user_id: int) -> list[tuple[Any, ...]]:
    """Все уведомления адресата: `(type, payload, delivered_at, created_at)`."""
    result = await session.execute(
        text(
            "SELECT type, payload, delivered_at, created_at FROM notifications "
            "WHERE user_id = :uid ORDER BY id"
        ),
        {"uid": user_id},
    )
    return [tuple(row) for row in result]


class _BrokenRedis:
    async def publish(self, channel: str, payload: str) -> int:  # noqa: ARG002
        raise RedisError("шина недоступна")


# ——— AC-1: «заявка пришла» ————————————————————————————————————————————————


async def test_request_received_created_for_author(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-1: подача заявки → автору `request.received` с `{card_uuid, request_uuid}`.

    Краснеет, если уведомление не создаётся, уходит ЗАЯВИТЕЛЮ (перепутан
    адресат) или payload теряет `request_uuid` (M6/M13-родственные дефекты).
    """
    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        await session.commit()

        issued = await issue_session(session, applicant)
        request = await submit_join_request(
            session,
            applicant_id=applicant.id,
            card_uuid=card_uuid,
            message="привет",
            settings=_RequestSettings(),
        )
        await session.commit()

        author_rows = await _rows_for(session, user_id=author.id)
        applicant_rows = await _rows_for(session, user_id=applicant.id)

    assert issued.access_token
    assert [row[0] for row in author_rows] == ["request.received"]
    payload = author_rows[0][1]
    assert payload["card_uuid"] == str(card_uuid)
    assert payload["request_uuid"] == str(request.uuid)
    assert "applicant_id" not in payload
    assert "author_id" not in payload
    assert applicant_rows == []
    # AC-5 (успех): доставка отмечена и не раньше создания.
    assert author_rows[0][2] is not None
    assert author_rows[0][2] >= author_rows[0][3]


async def test_request_received_survives_redis_failure(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: сбой шины → строка в БД есть, `delivered_at IS NULL`, заявка подана.

    Мутации M2/M3 краснят этот тест: `True` при `RedisError` заполнил бы
    `delivered_at` (L-2), а безусловная отметка — то же самое другим путём.
    """
    monkeypatch.setattr(ws_publisher, "get_redis", lambda: _BrokenRedis())

    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, _card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        await session.commit()

        request = await submit_join_request(
            session,
            applicant_id=applicant.id,
            card_uuid=card_uuid,
            message="привет",
            settings=_RequestSettings(),
        )
        await session.commit()

        # Доменная операция состоялась, несмотря на недоступную шину.
        stored = await session.execute(
            text("SELECT status FROM join_requests WHERE uuid = :u"), {"u": request.uuid}
        )
        assert stored.scalar_one() == JoinRequestStatus.PENDING.value

        rows = await _rows_for(session, user_id=author.id)

    assert len(rows) == 1
    assert rows[0][0] == "request.received"
    assert rows[0][2] is None, "delivered_at обязан остаться NULL при сбое PUBLISH"


# ——— AC-2, AC-3: выбор и сборка ————————————————————————————————————————————


async def test_request_selected_and_company_assembled(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-2/AC-3: выбор автора → заявителю `request.selected`, автору `company.assembled`.

    Проверяется ТОЧНЫЙ состав: у заявителя ровно одно уведомление (M8 — если
    ему же слать `company.assembled`, станет два), у автора ровно одно про
    сборку (M10 — при рассылке на каждый отказ их было бы N).
    """
    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=1
        )
        request_uuid = uuid_lib.uuid4()
        await session.execute(
            text(
                "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
                "submitted_at, expires_at) VALUES (:u, :c, :a, '', 'pending', now(), "
                "now() + interval '1 day')"
            ),
            {"u": request_uuid, "c": card_id, "a": applicant.id},
        )
        await session.commit()

        result = await join_request_service.select_join_request(
            session,
            author_id=author.id,
            request_uuid=request_uuid,
            settings=_RequestSettings(),
        )
        await session.commit()

        applicant_rows = await _rows_for(session, user_id=applicant.id)
        author_rows = await _rows_for(session, user_id=author.id)

    assert [row[0] for row in applicant_rows] == ["request.selected"]
    selected_payload = applicant_rows[0][1]
    assert selected_payload["card_uuid"] == str(card_uuid)
    assert selected_payload["chat_uuid"] == str(result.chat_uuid)
    assert "applicant_id" not in selected_payload

    assert [row[0] for row in author_rows] == ["company.assembled"]
    assert author_rows[0][1]["chat_uuid"] == str(result.chat_uuid)

    # AC-5 (успех): обе строки отмечены доставленными.
    assert applicant_rows[0][2] is not None
    assert author_rows[0][2] is not None


# ——— Task 3.3, M6: порядок `commit` → уведомление ————————————————————————


async def test_notification_created_after_domain_commit(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6/ADR-11: уведомление создаётся ПОСЛЕ `commit` доменной операции.

    **Почему порядок проверяется перехватом, а не состоянием.** Ассерт «строка
    есть» проходит и при создании ВНУТРИ транзакции: PostgreSQL видит
    незакоммиченную строку из той же сессии. Только последовательность
    вызовов отличает «после commit» от «до commit».

    Мутация M6 (перенести `notify_request_received` выше `session.commit()`)
    краснит этот тест: первым событием станет `notification`, а не `commit`.
    """
    from app.repositories.notifications import NotificationsRepository

    events: list[str] = []
    original_commit = AsyncSession.commit
    original_create = NotificationsRepository.create_one

    async def spy_commit(self: AsyncSession) -> None:
        await original_commit(self)
        events.append("commit")

    async def spy_create(self: NotificationsRepository, **kwargs: Any) -> uuid_lib.UUID:
        events.append("notification")
        return await original_create(self, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", spy_commit)
    monkeypatch.setattr(NotificationsRepository, "create_one", spy_create)

    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, _card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        await session.commit()
        events.clear()

        await submit_join_request(
            session,
            applicant_id=applicant.id,
            card_uuid=card_uuid,
            message="привет",
            settings=_RequestSettings(),
        )

    assert "notification" in events, events
    first_notification = events.index("notification")
    assert "commit" in events[:first_notification], (
        f"уведомление создано ДО commit домена: {events}"
    )


# ——— Task 4.2, M14: гард `opened_chat_uuid` ——————————————————————————————


async def test_join_into_card_with_existing_chat_sends_no_notification(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """M14: join в карточку с УЖЕ открытым чатом не создаёт `company.assembled`.

    **Что именно ловит мутация.** Гард `opened_chat_uuid is not None` означает
    «место занято ИМЕННО этим вызовом». Если карточка уже имела `chat_uuid`
    (чат открыт раньше), `ensure_for_card` вернёт `None`, событие сборки не
    публикуется — и уведомления быть не должно. Без гарда автор получал бы
    второе «компания собрана» о том же чате.
    """
    from app.services.card_join_service import join_open_card

    async with factory() as session:
        author, actor = await _new_users(session, 2)
        place_id = await _place(session)
        now = datetime.now(UTC)
        # Карточка открытого набора: чат УЖЕ есть, последнее место свободно.
        existing_chat = uuid_lib.uuid4()
        row = await session.execute(
            text(
                "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
                "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
                "filled_count, verification_status, published_at, chat_uuid) "
                "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 2, 60, "
                ":expires, 'published', 1, 'approved', :published_at, :chat) RETURNING uuid"
            ),
            {
                "author": author.id,
                "place": place_id,
                "slug": settings.default_community_slug,
                "starts": now + timedelta(days=3),
                "expires": now + timedelta(days=2),
                "published_at": now - timedelta(days=1),
                "chat": str(existing_chat),
            },
        )
        card_uuid = uuid_lib.UUID(str(row.scalar_one()))
        await session.execute(
            text(
                "INSERT INTO chats (uuid, event_card_id) "
                "SELECT :u, id FROM event_cards WHERE uuid = :c"
            ),
            {"u": str(existing_chat), "c": str(card_uuid)},
        )
        await session.commit()

        await join_open_card(
            session,
            actor_id=actor.id,
            card_uuid=card_uuid,
            settings=_RequestSettings(),
        )
        await session.commit()

        rows = await _rows_for(session, user_id=author.id)

    assert rows == [], f"ожидалось 0 уведомлений, получено {[row[0] for row in rows]}"


# ——— Task 4.3/4.4, M9/M10: автовыбор (воркер и ленивое закрытие) ————————————


async def test_auto_selection_notifies_winner_and_author_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 4.3, AC-2/AC-3, M9/M10: АВТОВЫБОР воркером шлёт ДВА уведомления.

    **Почему нужен отдельный тест, а не авторский `select`.** Это ЧЕТВЁРТЫЙ
    путь сборки: адресат читается `get_selected_applicant` по `card_id`, а не
    `get_applicant` по `request_uuid`. Пока теста не было, две мутации
    выживали (проверено прогоном `mutate.py`):

    - M9 (`selected` → `not_selected` в билдере) — адресатом стал бы
      ПРОИГРАВШИЙ, и никто бы этого не заметил;
    - M10 (лишний `company.assembled`) — у автора оказалось бы два
      уведомления о сборке вместо одного.

    **Почему `run_decisions`, а не прямой `close_due_card`.** Ядро не владеет
    коммитом и не шлёт уведомления: их отправляет владелец транзакции
    (воркер). Проверять надо именно его.
    """
    from app.workers.close_due_cards import run_decisions

    async with factory() as session:
        author, winner, loser = await _new_users(session, 3)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card_with_expired_window_pending(
            session, author_id=author.id, place_id=place_id
        )
        # Очередь автовыбора: `submitted_at ASC, id ASC` (ADR-05).
        for offset, applicant in enumerate((winner, loser)):
            await session.execute(
                text(
                    "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                    "submitted_at, expires_at) VALUES (:c, :a, '', 'pending', "
                    "now() + make_interval(secs => :d), now() + interval '1 day')"
                ),
                {"c": card_id, "a": applicant.id, "d": offset},
            )
        await session.commit()

    await run_decisions(factory)

    async with factory() as session:
        author_rows = await _rows_for(session, user_id=author.id)
        winner_rows = await _rows_for(session, user_id=winner.id)
        loser_rows = await _rows_for(session, user_id=loser.id)

    assert [row[0] for row in author_rows] == ["company.assembled"], (
        f"M10: у автора ожидалось ровно одно уведомление, получено "
        f"{[row[0] for row in author_rows]}"
    )
    assert [row[0] for row in winner_rows] == ["request.selected"], (
        f"M9: победитель не получил `request.selected` — {[row[0] for row in winner_rows]}"
    )
    assert loser_rows == [], "AC-4: проигравший не должен получать уведомлений"
    assert winner_rows[0][1]["card_uuid"] == str(card_uuid)
    assert "applicant_id" not in winner_rows[0][1]


async def _insert_card_with_expired_window_pending(
    session: AsyncSession, *, author_id: int, place_id: int
) -> tuple[uuid_lib.UUID, int]:
    """Карточка режима заявок с ИСТЁКШИМ окном — вход автовыбора воркера."""
    now = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at, NULL) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": now + timedelta(days=3),
            "expires": now - timedelta(minutes=1),
            "published_at": now - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return uuid_lib.UUID(str(card_uuid)), int(card_id)


# ——— CR 9.2 r1 HIGH-1: домен не зависит от уведомлений (3 пути) ————————


class _CardSettings:
    """Протокол `CardSettings` без чтения `.env` (приём 4.x)."""

    default_community_slug = settings.default_community_slug
    community_slugs = (settings.default_community_slug,)
    card_default_decision_window_minutes = 60
    card_content_stop_words: tuple[str, ...] = ()
    card_active_limit = 10


class _CardJoinSettings:
    """Протокол `CardJoinSettings` без чтения `.env` (приём 5а.5)."""

    default_community_slug = settings.default_community_slug


async def test_select_survives_recipient_read_failure(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-1: сбой ЧТЕНИЯ адресата после `commit` не ломает выбор автора.

    **Дефект, который закрывает тест.** `UserRepository.get_by_id` стоял
    ПОСЛЕ `await session.commit()` ВНЕ `try/except`: близ БД или таймаут пула
    давали `500` на уже выполненном выборе. Клиент не мог ни повторить
    (`409 REQUEST_NOT_PENDING`), ни понять, что заявка выбрана.

    Подменяется именно ЧТЕНИЕ (не `notify_*`): `notify_*` гасит свои ошибки
    сам, а незащищённым был вызов репозитория в сервисе.
    """
    from app.repositories.users import UserRepository

    async def _boom(self: UserRepository, user_id: int) -> None:  # noqa: ARG001
        raise RuntimeError("блип БД после commit")

    monkeypatch.setattr(UserRepository, "get_by_id", _boom)

    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        _card_uuid, card_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=1
        )
        request_uuid = uuid_lib.uuid4()
        await session.execute(
            text(
                "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
                "submitted_at, expires_at) VALUES (:u, :c, :a, '', 'pending', now(), "
                "now() + interval '1 day')"
            ),
            {"u": request_uuid, "c": card_id, "a": applicant.id},
        )
        await session.commit()

        # Не должно бросить: домен уже зафиксирован, ответ обязан быть успешным.
        result = await join_request_service.select_join_request(
            session,
            author_id=author.id,
            request_uuid=request_uuid,
            settings=_RequestSettings(),
        )
        await session.commit()

        # Домен зафиксирован: заявка `selected`, карточка `full` с чатом.
        stored = await session.execute(
            text("SELECT status FROM join_requests WHERE uuid = :u"), {"u": request_uuid}
        )
        card_row = await session.execute(
            text("SELECT status, chat_uuid FROM event_cards WHERE id = :c"), {"c": card_id}
        )
        status, chat_uuid = card_row.one()

    assert result.chat_uuid is not None
    assert stored.scalar_one() == JoinRequestStatus.SELECTED.value
    assert status == "full"
    assert chat_uuid is not None


async def test_join_open_card_survives_recipient_read_failure(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-1: сбой чтения автора после `commit` не ломает вступление.

    Краснеет, если `users.get_by_id` вернётся вне `try/except`: участник
    получит `500` на успешно занятом месте и не сможет повторить
    (`409 ALREADY_JOINED`).

    **CR r2, MEDIUM-1: компания должна собираться ЭТИМ вызовом.** Со
    `capacity=2` и без предварительного участника `join_open_card` даёт
    `filled_count=1`, `opened_chat_uuid is None` — блок уведомлений под
    `if opened_chat_uuid is not None:` НЕ ИСПОЛНЯЛСЯ, и подмена чтения на
    `raise` ничего не проверяла (мутация SURVIVED дважды).

    `capacity=1` тоже не годится: это режим заявок, и `join_open_card`
    отвергает его `CapacityOneUsesRequestsError` ещё до записи (проверено
    прогоном — baseline краснел). Поэтому: открытый набор на 2 места, одно
    уже занято, join заполняет последнее → `full`, `opened_chat_uuid`
    заполнен, блок уведомлений достижим.
    """
    from app.repositories.users import UserRepository
    from app.services.card_join_service import join_open_card

    async def _boom(self: UserRepository, user_id: int) -> None:  # noqa: ARG001
        raise RuntimeError("блип БД после commit")

    async with factory() as session:
        author, actor = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=2
        )
        await session.commit()

        # Одно место уже занято: следующий join собирает компанию.
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                "submitted_at, decided_at, expires_at) VALUES "
                "(:c, :a, '', 'selected', now(), now(), now() + interval '1 day')"
            ),
            {"c": card_id, "a": author.id},
        )
        await session.execute(
            text("UPDATE event_cards SET filled_count = 1 WHERE id = :c"), {"c": card_id}
        )
        await session.commit()

        monkeypatch.setattr(UserRepository, "get_by_id", _boom)

        result = await join_open_card(
            session,
            actor_id=actor.id,
            card_uuid=card_uuid,
            settings=_CardJoinSettings(),
        )
        await session.commit()

        card_row = await session.execute(
            text("SELECT status, filled_count, chat_uuid FROM event_cards WHERE id = :c"),
            {"c": card_id},
        )
        status, filled_count, chat_uuid = card_row.one()

    # Компания СОБРАНА этим вызовом — блок уведомлений был достижим.
    assert result.filled_count == 2
    assert filled_count == 2
    assert status == "full"
    assert chat_uuid is not None
    assert result.chat_uuid is not None


async def test_lazy_close_survives_recipient_read_failure(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-1: сбой чтения адресата в ленивом закрытии не ломает `GET` карточки.

    Краснеет, если чтение адресата в `get_card` вернётся вне `try/except`:
    открывающий карточку получает `500` при уже собранной компании и
    заполненном `chat_uuid`. Повторить `GET` бессмысленно — карточка `full`.
    """
    from app.repositories.join_requests import JoinRequestRepository
    from app.services.event_card_service import get_card

    async def _boom(self: JoinRequestRepository, card_id: int) -> None:  # noqa: ARG001
        raise RuntimeError("блип БД после commit")

    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card_with_expired_window_pending(
            session, author_id=author.id, place_id=place_id
        )
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                "submitted_at, expires_at) VALUES (:c, :a, '', 'pending', now(), "
                "now() + interval '1 day')"
            ),
            {"c": card_id, "a": applicant.id},
        )
        await session.commit()

        monkeypatch.setattr(JoinRequestRepository, "get_selected_applicant", _boom)

        # `GET` карточки запускает ленивое закрытие с автовыбором.
        item = await get_card(
            session,
            card_uuid=card_uuid,
            viewer_id=applicant.id,
            settings=_CardSettings(),
        )
        await session.commit()

        card_row = await session.execute(
            text("SELECT status, chat_uuid FROM event_cards WHERE id = :c"), {"c": card_id}
        )
        status, chat_uuid = card_row.one()

    assert item.card.uuid == card_uuid
    assert status == "full"
    assert chat_uuid is not None


# ——— CR 9.2 r1 MEDIUM-1: два пути сборки, которые не были покрыты ————————


async def test_join_open_card_creates_company_assembled(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """MEDIUM-1 (R2, класс X-6.5-1): join в открытый набор СОЗДАЁТ уведомление.

    **Почему тест был необходим.** Существующий `test_join_into_card_with_
    existing_chat_sends_no_notification` проверяет только НЕГАТИВНЫЙ случай
    (гард сработал → уведомления нет). Снятие самого вызова
    `notify_company_assembled` из `join_open_card` не краснило НИ ОДНОГО
    теста (мутация GAP-cardjoin → SURVIVED, доказано CR r1).

    Здесь: карточка открытого набора на 2 места, чата ещё нет → join второго
    участника закрывает набор → автору РОВНО ОДНО `company.assembled`.
    """
    from app.services.card_join_service import join_open_card

    async with factory() as session:
        author, actor = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=2
        )
        await session.commit()

        # Первый участник — чтобы место осталось последним.
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                "submitted_at, decided_at, expires_at) VALUES "
                "(:c, :a, '', 'selected', now(), now(), now() + interval '1 day')"
            ),
            {"c": card_id, "a": author.id},
        )
        await session.execute(
            text("UPDATE event_cards SET filled_count = 1 WHERE id = :c"), {"c": card_id}
        )
        await session.commit()

        result = await join_open_card(
            session,
            actor_id=actor.id,
            card_uuid=card_uuid,
            settings=_CardJoinSettings(),
        )
        await session.commit()

        rows = await _rows_for(session, user_id=author.id)
        card_row = await session.execute(
            text("SELECT status, chat_uuid FROM event_cards WHERE id = :c"), {"c": card_id}
        )
        status, chat_uuid = card_row.one()

    assert result.status == "full"
    assert status == "full"
    assert chat_uuid is not None
    assert [row[0] for row in rows] == ["company.assembled"], (
        f"автору ожидалось ровно одно `company.assembled`, получено {[row[0] for row in rows]}"
    )
    assert rows[0][1]["chat_uuid"] == str(chat_uuid)
    assert "author_id" not in rows[0][1]


async def test_lazy_close_creates_notifications(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """MEDIUM-1 (Task 4.4): ленивое закрытие через `get_card` СОЗДАЁТ уведомления.

    **Почему теста не было вовсе.** Стори прямо называет ленивое закрытие
    «четвёртым путём сборки», но ни один тест через него не проходил:
    снятие всего блока уведомлений в `get_card` не краснило ничего
    (мутация GAP-lazy → SURVIVED, доказано CR r1).

    Здесь: карточка с истёкшим окном и `pending`-заявкой, наблюдатель
    открывает её первым → `get_card` запускает автовыбор → победителю
    `request.selected`, автору `company.assembled`.
    """
    from app.services.event_card_service import get_card

    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card_with_expired_window_pending(
            session, author_id=author.id, place_id=place_id
        )
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                "submitted_at, expires_at) VALUES (:c, :a, '', 'pending', now(), "
                "now() + interval '1 day')"
            ),
            {"c": card_id, "a": applicant.id},
        )
        await session.commit()

        await get_card(
            session,
            card_uuid=card_uuid,
            viewer_id=applicant.id,
            settings=_CardSettings(),
        )
        await session.commit()

        applicant_rows = await _rows_for(session, user_id=applicant.id)
        author_rows = await _rows_for(session, user_id=author.id)

    assert [row[0] for row in applicant_rows] == ["request.selected"], (
        f"победителю ожидался `request.selected`, получено {[row[0] for row in applicant_rows]}"
    )
    assert [row[0] for row in author_rows] == ["company.assembled"], (
        f"автору ожидалось `company.assembled`, получено {[row[0] for row in author_rows]}"
    )


# ——— AC-4: границы безличности ————————————————————————————————————————————


async def test_three_requests_after_selection_give_exactly_three_new_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-4/Task 6.1: 3 заявки → ровно 3 новых строки, проигравшим — 0.

    Считаются строки, созданные ВЫБОРОМ: у автора `company.assembled`,
    у победителя `request.selected`, у двух проигравших — ничего.
    M10 (сборка на каждый `not_selected`) даёт у автора лишние строки и
    краснит этот тест.
    """
    async with factory() as session:
        author = await _new_user(session)
        applicants = await _new_users(session, 3)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=1
        )
        request_uuids: list[uuid_lib.UUID] = []
        for applicant in applicants:
            request_uuid = uuid_lib.uuid4()
            request_uuids.append(request_uuid)
            await session.execute(
                text(
                    "INSERT INTO join_requests (uuid, card_id, applicant_id, message, status, "
                    "submitted_at, expires_at) VALUES (:u, :c, :a, '', 'pending', now(), "
                    "now() + interval '1 day')"
                ),
                {"u": request_uuid, "c": card_id, "a": applicant.id},
            )
        await session.commit()

        # Выбирается ПЕРВАЯ заявка; остальные становятся `not_selected`.
        await join_request_service.select_join_request(
            session,
            author_id=author.id,
            request_uuid=request_uuids[0],
            settings=_RequestSettings(),
        )
        await session.commit()

        author_rows = await _rows_for(session, user_id=author.id)
        winner_rows = await _rows_for(session, user_id=applicants[0].id)
        loser_rows_1 = await _rows_for(session, user_id=applicants[1].id)
        loser_rows_2 = await _rows_for(session, user_id=applicants[2].id)

    assert [row[0] for row in author_rows] == ["company.assembled"]
    assert [row[0] for row in winner_rows] == ["request.selected"]
    assert loser_rows_1 == []
    assert loser_rows_2 == []


async def test_no_notifications_for_closed_no_choice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-4: закрытие БЕЗ выбора не создаёт ни одного уведомления.

    **Почему вызывается воркер, а не проверяется пустая таблица.** Тест,
    который просто смотрит «строк нет» на карточке, закрытие не запускал
    вовсе: он был бы зелёным и при мутации M7 (слать `card.closed_impersonal`
    в ветке `CLOSED_NO_CHOICE`). Здесь прогоняется РЕАЛЬНЫЙ путь воркера.

    D-9.2-7: `card.closed_impersonal` в 9.2 не пишется ни одним путём.
    """
    from app.workers.close_due_cards import run_decisions

    async with factory() as session:
        author = await _new_user(session)
        place_id = await _place(session)
        # Окно истекло, заявок нет → `close_due_card` даёт CLOSED_NO_CHOICE.
        await _insert_card_with_expired_window(session, author_id=author.id, place_id=place_id)
        await session.commit()

    # Воркер работает своими сессиями: домен коммитится по-настоящему.
    await run_decisions(factory)

    async with factory() as session:
        rows = await _rows_for(session, user_id=author.id)

    assert rows == []


async def _insert_card_with_expired_window(
    session: AsyncSession, *, author_id: int, place_id: int
) -> tuple[uuid_lib.UUID, int]:
    now = datetime.now(UTC)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, community_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 1, 60, "
            ":expires, 'published', 0, 'approved', :published_at, NULL) RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_community_slug,
            "starts": now + timedelta(days=3),
            "expires": now - timedelta(minutes=1),
            "published_at": now - timedelta(days=1),
        },
    )
    card_uuid, card_id = row.one()
    return uuid_lib.UUID(str(card_uuid)), int(card_id)


# ——— Task 2/5: контракт репозитория ————————————————————————————————————————


async def test_mark_delivered_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """§18.11: повторная отметка не сдвигает `delivered_at`.

    Краснеет, если снять `delivered_at IS NULL` из `UPDATE`: время доставки
    переписывалось бы, и порядок `created_at <= delivered_at` держался бы на
    удаче.
    """
    async with factory() as session:
        author = await _new_user(session)
        await session.commit()

        created = await notify_request_received(
            session,
            author_uuid=author.uuid,
            author_id=author.id,
            card_uuid=uuid_lib.uuid4(),
            request_uuid=uuid_lib.uuid4(),
        )
        first = await _rows_for(session, user_id=author.id)
        notification_uuid = await _notification_uuid(session, user_id=author.id)

        # Вторая отметка того же uuid — ничего не меняет.
        from app.repositories.notifications import NotificationsRepository

        updated = await NotificationsRepository(session).mark_delivered_by_uuid(
            notification_uuid=notification_uuid
        )
        await session.commit()
        second = await _rows_for(session, user_id=author.id)

    assert created is True
    assert updated == 0, "повторная отметка обязана обновить 0 строк"
    assert first[0][2] == second[0][2]


async def _notification_uuid(session: AsyncSession, *, user_id: int) -> uuid_lib.UUID:
    row = await session.execute(
        text("SELECT uuid FROM notifications WHERE user_id = :uid ORDER BY id LIMIT 1"),
        {"uid": user_id},
    )
    return uuid_lib.UUID(str(row.scalar_one()))


async def test_notification_delivery_path_does_not_touch_new_types(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 5.4: `mark_notifications_delivered` по `chat_uuid` не берёт новые строки.

    Новые типы несут `chat_uuid` в payload (`request.selected`), и старый
    предикат по `type` + `chat_uuid` мог бы отметить их «доставленными»
    заочно. Доставка 9.2 идёт по `uuid` — регрессия проверяется здесь.
    """
    from app.services.notification_delivery import mark_notifications_delivered

    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        await session.commit()
        chat_uuid = uuid_lib.uuid4()

        await notify_request_selected(
            session,
            applicant_uuid=applicant.uuid,
            applicant_id=applicant.id,
            card_uuid=uuid_lib.uuid4(),
            chat_uuid=chat_uuid,
        )
        rows_before = await _rows_for(session, user_id=applicant.id)
        assert rows_before[0][2] is not None

        # Старый путь отметки по `chat_uuid`: у него другой `type`, строки 9.2
        # он не видит вовсе.
        touched = await mark_notifications_delivered(
            session, chat_uuid=chat_uuid, type_="report.created"
        )

    assert touched == 0
