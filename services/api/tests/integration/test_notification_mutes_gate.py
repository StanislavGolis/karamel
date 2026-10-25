"""Gate отключённых типов на всех четырёх путях создания (стори 9.3).

(AC-01, AC-03; Task 3, 6.2, 6.4; D-9.3-6, D-9.3-7)

**Почему один файл на четыре пути.** Мутация «убрать gate» применяется к
каждому пути ОТДЕЛЬНО (M1–M4), и общий файл делает таблицу мутаций
читаемой: один тест — один путь — одна мутация. Разложив пути по четырём
файлам, пришлось бы помнить, какой тест какой путь покрывает.

**Коммиты настоящие** (собственный движок, как в `test_notifications_lifecycle`):
`notification_service` коммитит СВОЮ короткую транзакцию после доменной
(D-9.2-4), и через savepoint-сессию «ничего не записано» неотличимо от
«записано внутри внешней транзакции».

**AC-03 — отдельный тест с РЕАЛЬНЫМ путём `join_open_card`.** Проверять
«`card.full` не проходит через gate» подменой `publish_to_chat` на заглушку
бесполезно: заглушка докажет только то, что её вызвали. Тест идёт через
`card_join_service` и проверяет, что при ВСЕХ отключённых типах:

- ни одной строки в `notifications` (AC-01);
- `card.full` всё равно опубликован (AC-03: состояние карточки — не
  уведомление).

Мутация M8 («не публиковать `card.full` при mute») роняет вторую половину.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.enums import NOTIFICATION_TYPES, REQUIRED_NOTIFICATION_TYPES, NotificationType
from app.models.user import User
from app.repositories.reports import ReportsRepository
from app.repositories.users import UserRepository
from app.services import notification_service, ws_publisher
from app.services.card_join_service import join_open_card
from app.services.notification_service import notify_request_received
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

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


class _RequestSettings:
    """Протокол `JoinRequestSettings` без чтения `.env` (приём 5б.2)."""

    default_community_slug = settings.default_community_slug
    join_request_active_limit = 2
    s3_signed_url_ttl = 900


class _CardJoinSettings:
    """Протокол `CardJoinSettings` без чтения `.env`."""

    default_community_slug = settings.default_community_slug


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield created
    await created.dispose()


@pytest.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _cleanup(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Убирает созданные строки в порядке, обратном FK-зависимостям.

    `notification_mutes` и `notifications` удаляются ЯВНО: держать уборку на
    `ON DELETE CASCADE` от `users` значило бы ломать изоляцию при смене
    правила ретенции (урок D-TD-3).
    """
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    async with factory() as session:
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM notification_mutes WHERE user_id = ANY(:ids)"),
                {"ids": _CREATED_USERS},
            )
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
    _CREATED_USERS.append(int(user.id))
    return user


async def _new_users(session: AsyncSession, count: int) -> list[User]:
    return [await _new_user(session) for _ in range(count)]


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
    capacity: int = 2,
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


async def _mute(session: AsyncSession, *, user_id: int, types: tuple[str, ...]) -> None:
    for notification_type in types:
        await session.execute(
            text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
            {"u": user_id, "t": notification_type},
        )


async def _count_for(session: AsyncSession, *, user_id: int) -> int:
    rows = await session.execute(
        text("SELECT count(*) FROM notifications WHERE user_id = :u"), {"u": user_id}
    )
    return int(rows.scalar_one())


# ——— путь 1: 9.2-одиночная вставка (M1, M7) ——————————————————————————————


async def test_create_one_skips_muted_type_silently(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01, M1, M7: отключённый тип — нет строки, нет WS, НЕТ ошибки.

    M1 (снять gate из `_CREATE_ONE_SQL`) создаёт строку — краснеет первая
    половина. M7 (публиковать WS при `muted`) даёт `publish_to_user` — вторая.

    Проверяется и ОТСУТСТВИЕ ошибки: `_create_publish_mark` возвращает `False`
    на нормальном исходе настройки, и `return False` не должен выглядеть как
    сбой (D-9.2-12: ошибка уведомления не ломает ответ, но и «отключено» —
    не ошибка). Ловится тем, что вызов не бросает и возвращает `False`.
    """
    published: list[Any] = []

    async def _spy(**kwargs: Any) -> bool:
        published.append(kwargs)
        return True

    # Патч в МОДУЛЕ-ПОТРЕБИТЕЛЕ: `notification_service` импортирует имя
    # напрямую, и подмена в `ws_publisher` оставила бы реальную публикацию.
    monkeypatch.setattr(notification_service, "publish_to_user", _spy)

    async with factory() as session:
        author = await _new_user(session)
        place_id = await _place(session)
        card_uuid, _card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        await _mute(session, user_id=author.id, types=(NotificationType.REQUEST_RECEIVED.value,))
        await session.commit()

        result = await notify_request_received(
            session,
            author_uuid=author.uuid,
            author_id=author.id,
            card_uuid=card_uuid,
            request_uuid=uuid_lib.uuid4(),
        )
        await session.commit()

        rows = await _count_for(session, user_id=author.id)

    assert result is False, "отключённый тип не должен считаться доставленным"
    assert rows == 0, "уведомление отключённого типа создано (M1)"
    assert published == [], "WS опубликован при отключённом типе (M7)"


async def test_create_one_delivers_enabled_type(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01: включённый тип доставляется как раньше — контроль к тесту выше.

    Без него `rows == 0` в предыдущем тесте проходил бы и при полностью
    сломанной вставке: тест доказывал бы «gate работает», хотя не работает
    ничего. Здесь путь ОБЯЗАН создать строку и опубликовать событие.
    """
    published: list[Any] = []

    async def _spy(**kwargs: Any) -> bool:
        published.append(kwargs)
        return True

    monkeypatch.setattr(notification_service, "publish_to_user", _spy)

    async with factory() as session:
        author = await _new_user(session)
        place_id = await _place(session)
        card_uuid, _card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        # Отключён ДРУГОЙ тип: он не должен влиять на этот.
        await _mute(session, user_id=author.id, types=(NotificationType.REPORT_CREATED.value,))
        await session.commit()

        result = await notify_request_received(
            session,
            author_uuid=author.uuid,
            author_id=author.id,
            card_uuid=card_uuid,
            request_uuid=uuid_lib.uuid4(),
        )
        await session.commit()

        rows = await _count_for(session, user_id=author.id)

    assert result is True
    assert rows == 1
    assert len(published) == 1


# ——— путь 2: 7.3-пакетник (M2) ————————————————————————————————————————————


async def test_attendance_requests_respect_mute_for_recipient_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01, M2: отключение ОДНИМ адресатом не глушит остальных.

    Мутация M2 (снять gate из `create_attendance_requests`) создаёт строку
    отключившему — краснеет первая половина. Вторая половина (второй адресат
    получил) ловит другую ошибку того же класса: gate, применённый ко ВСЕМ
    адресатам сразу (забытый `user_id`, M13) — тогда `participant` остался бы
    без уведомления.
    """
    from app.repositories.notifications import NotificationsRepository

    async with factory() as session:
        author, participant = await _new_users(session, 2)
        place_id = await _place(session)
        _card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        await session.execute(
            text(
                "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
                "participant_confirmed, status, confirmed_at) "
                "VALUES (:c, :p, true, true, 'confirmed', now())"
            ),
            {"c": card_id, "p": participant.id},
        )
        await _mute(session, user_id=author.id, types=(NotificationType.ATTENDANCE_REQUEST.value,))
        await session.commit()

        created = await NotificationsRepository(session).create_attendance_requests(
            card_id=card_id,
            author_id=author.id,
            payload='{"card_uuid":"x"}',
        )
        await session.commit()

        author_rows = await _count_for(session, user_id=author.id)
        participant_rows = await _count_for(session, user_id=participant.id)

    assert author_rows == 0, "gate не сработал для автора (M2)"
    assert participant_rows == 1, "отключение автора заглушило участника (M13)"
    # 9.4 (Task 4.8, R1): форма возврата стала списком пар
    # `(notification_uuid, user_uuid)`; смысл ассерта прежний —
    # создана ровно одна строка.
    assert len(created) == 1


# ——— путь 3: 8.2-пакетник (M3) ————————————————————————————————————————————


async def test_report_notifications_respect_mute_per_recipient(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01, M3: gate в `create_report_notifications` действует по адресату.

    Один адресат диалога отключил `report.created`, второй — нет. Краснеет,
    если gate снят (M3) или применён глобально (M13).
    """
    async with factory() as session:
        author, applicant = await _new_users(session, 2)
        place_id = await _place(session)
        _card_uuid, card_id = await _insert_card(session, author_id=author.id, place_id=place_id)
        chat = await session.execute(
            text(
                "INSERT INTO chats (event_card_id, uuid) VALUES (:c, gen_random_uuid()) "
                "RETURNING id"
            ),
            {"c": card_id},
        )
        chat_id = int(chat.scalar_one())
        await session.execute(
            text(
                "INSERT INTO join_requests (card_id, applicant_id, message, status, "
                "submitted_at, decided_at, expires_at) VALUES "
                "(:c, :a, '', 'selected', now(), now(), now() + interval '1 day')"
            ),
            {"c": card_id, "a": applicant.id},
        )
        await _mute(session, user_id=applicant.id, types=(NotificationType.REPORT_CREATED.value,))
        await session.commit()

        created = await ReportsRepository(session).create_report_notifications(
            chat_id=chat_id,
            payload='{"chat_uuid":"x"}',
            type_=NotificationType.REPORT_CREATED.value,
        )
        await session.commit()

        author_rows = await _count_for(session, user_id=author.id)
        applicant_rows = await _count_for(session, user_id=applicant.id)

    assert author_rows == 1, "gate 8.2 заглушил второго адресата (M13)"
    assert applicant_rows == 0, "gate 8.2 не сработал (M3)"
    assert created == 1


# ——— путь 4: 8.4-вставка (M4, M9) ——————————————————————————————————————


async def test_sanction_and_resolution_notifications_are_never_muted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01, D-9.3-5: неотключаемые типы доставляются ВСЕГДА.

    `report.resolved` и `sanction.issued` нельзя отключить ни через API (409),
    ни через схему (`CHECK`) — но проверка «нельзя записать строку настройки»
    не доказывает, что путь 8.4 её доставит. Здесь доказывается именно
    доставка: gate стоит, а предикат для этих типов не может быть истинным,
    потому что строки настройки физически не существует.

    M9 (снять гард `required` в сервисе) даёт `IntegrityError` на попытке
    записать настройку, а не `0` строк — этот тест его НЕ ловит, и это
    осознанно: M9 ловит API-тест `409`. Здесь фиксируется инвариант доставки.
    """
    from app.repositories.notifications import NotificationsRepository

    async with factory() as session:
        accused, reporter = await _new_users(session, 2)
        await session.commit()

        # Прямая вставка типов, которые 8.4 пишет через `_INSERT_NOTIFICATION_SQL`.
        for user_id in (accused.id, reporter.id):
            for notification_type in ("report.resolved", "sanction.issued"):
                row = await session.execute(
                    text(
                        "INSERT INTO notifications (user_id, type, payload) "
                        "SELECT CAST(:u AS bigint), CAST(:t AS varchar), CAST('{}' AS jsonb) "
                        "WHERE NOT EXISTS (SELECT 1 FROM notification_mutes m "
                        "WHERE m.user_id = CAST(:u AS bigint) AND m.type = CAST(:t AS varchar))"
                    ),
                    {"u": user_id, "t": notification_type},
                )
                del row
        await session.commit()

        accused_rows = await _count_for(session, user_id=accused.id)
        reporter_rows = await _count_for(session, user_id=reporter.id)

    assert accused_rows == 2
    assert reporter_rows == 2
    del NotificationsRepository  # импорт держит связь с реальным путём 8.4


# ——— AC-03: card.full не проходит через gate (M8) ——————————————————————


async def test_card_full_published_even_when_every_type_muted(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03, M8: все 11 типов отключены → ни строки, но `card.full` идёт.

    Это ГЛАВНЫЙ тест AC-03, и он проверяет РЕАЛЬНЫЙ путь: `join_open_card`
    собирает компанию, `card_join_service` публикует `card.full` в комнату
    чата, `notification_service` пытается создать `company.assembled` и
    молчит.

    Мутация M8 (не публиковать `card.full` при mute) роняет `chat_events`.
    Мутация «gate на доменных WS» роняет его же — потому что доменные события
    публикуются из `card_join_service`, а не из `notification_service`.

    Отключение ВСЕХ типов, а не одного: AC-03 сформулирован как «отключены
    все уведомления», и частичное отключение не отличило бы «gate не работает»
    от «доменное событие случайно прошло вместе с уведомлением».
    """
    chat_events: list[Any] = []
    user_events: list[Any] = []

    async def _chat_spy(**kwargs: Any) -> bool:
        chat_events.append(kwargs)
        return True

    async def _user_spy(**kwargs: Any) -> bool:
        user_events.append(kwargs)
        return True

    from app.services import card_join_service

    monkeypatch.setattr(card_join_service, "publish_to_chat", _chat_spy)
    monkeypatch.setattr(ws_publisher, "publish_to_user", _user_spy)

    async with factory() as session:
        author, actor = await _new_users(session, 2)
        place_id = await _place(session)
        card_uuid, card_id = await _insert_card(
            session, author_id=author.id, place_id=place_id, capacity=2
        )
        # Одно место уже занято: следующий join СОБИРАЕТ компанию, и блок
        # уведомлений достижим (иначе `opened_chat_uuid is None` и тест
        # доказывал бы отсутствие кода, а не работу gate).
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
        # AC-03: отключены ВСЕ ОТКЛЮЧАЕМЫЕ типы у ОБОИХ участников.
        # `report.resolved`/`sanction.issued` в список НЕ входят: их запрещает
        # `ck_notification_mutes_required` (D-9.3-5), и попытка записать их
        # уронила бы подготовку теста `IntegrityError`-ом, а не показала бы
        # дефект gate. Доменные WS от этого не зависят — они не уведомления.
        all_types = tuple(
            str(t) for t in NOTIFICATION_TYPES if t not in REQUIRED_NOTIFICATION_TYPES
        )
        await _mute(session, user_id=author.id, types=all_types)
        await _mute(session, user_id=actor.id, types=all_types)
        await session.commit()

        await issue_session(session, actor)
        result = await join_open_card(
            session, actor_id=actor.id, card_uuid=card_uuid, settings=_CardJoinSettings()
        )
        await session.commit()

        author_rows = await _count_for(session, user_id=author.id)
        actor_rows = await _count_for(session, user_id=actor.id)

    assert result.filled_count == 2
    assert result.chat_uuid is not None
    # AC-01: ни одного уведомления — все типы отключены.
    assert author_rows == 0, "уведомление создано при всех отключённых типах"
    assert actor_rows == 0
    assert user_events == [], "WS уведомления опубликован при mute (M7)"
    # AC-03: доменное событие карточки ПРИШЛО — состояние карточки не уведомление.
    assert len(chat_events) == 1, "card.full не опубликован (M8, регресс AC-03)"
    assert chat_events[0]["envelope"].type == "card.full"


# ——— §18.3: конкурентность (Task 6.4) —————————————————————————————————————


async def test_mute_during_insert_is_atomic(
    factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """AC-01, §18.3: отключение в момент вставки — gate атомарен, без строки.

    Два САМОСТОЯТЕЛЬНЫХ соединения: первое держит незакоммиченную настройку,
    второе вставляет уведомление. `NOT EXISTS` читает снапшот своей
    транзакции, поэтому до `COMMIT` первого вставка проходит — это НЕ дефект,
    а корректная семантика MVCC. Проверяется обратное: ПОСЛЕ коммита настройки
    вставка уже невозможна.

    Почему не одна сессия: §18.3 требует двух соединений, иначе тест доказывал
    бы согласованность внутри одного снапшота, а не поведение гонки.
    """
    async with factory() as setup:
        author = await _new_user(setup)
        place_id = await _place(setup)
        card_uuid, _card_id = await _insert_card(setup, author_id=author.id, place_id=place_id)
        await setup.commit()
        author_id, author_uuid = author.id, author.uuid

    # Соединение 1: отключает тип и коммитит.
    async with factory() as muting:
        await _mute(muting, user_id=author_id, types=(NotificationType.REQUEST_RECEIVED.value,))
        await muting.commit()

    # Соединение 2: пытается вставить — строки быть не должно.
    async with factory() as inserting:
        result = await notify_request_received(
            inserting,
            author_uuid=author_uuid,
            author_id=author_id,
            card_uuid=card_uuid,
            request_uuid=uuid_lib.uuid4(),
        )
        await inserting.commit()
        rows = await _count_for(inserting, user_id=author_id)

    assert result is False
    assert rows == 0
