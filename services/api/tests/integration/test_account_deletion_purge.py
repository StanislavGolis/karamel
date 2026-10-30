"""Каскады физического удаления аккаунта (стори 10.2; AC-02, AC-04, AC-07, AC-08).

**Проверяется перечень D-10.2-5 целиком, а не «учётки нет».** Удаление
`users` без каскада оставило бы сирот в 12 таблицах, и `SELECT count(*)`
по одной `users` этого не показал бы. Поэтому сид строит ПОЛНЫЙ граф
доменов, а тест проверяет отсутствие строк в каждой из них.

**`access_logs` обязан ВЫЖИТЬ** (ADR-14, Ловушка 5). FK на `users` там нет
намеренно: журнал аудита переживает удаление. Тест закрепляет это, иначе
первая «оптимизация» добавит `CASCADE` и сотрёт историю модерации.

**Порядок «keys до DELETE, объект после commit»** (AC-08, 18.11) проверяется
ПЕРЕХВАТОМ `media_service.delete_object`, а не итоговым состоянием: по
состоянию нельзя отличить «ключи собрали до» от «собрали после и не нашли».

**Идемпотентность (AC-07)** — два прогона подряд: второй возвращает `0`.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import media_service
from app.services.account_deletion_service import (
    DELETION_WINDOW,
    purge_due_accounts,
)
from tests.integration.report_helpers import PAIR_FLAGS

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


@pytest.fixture(autouse=True)
def _no_real_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Глушит настоящий S3 для ВСЕГО файла (CI run 41137571125).

    **Дефект, который это закрывает.** Тесты зовут `purge_due_accounts`,
    тот — `media_service.delete_object`, а тот — реальный boto3-клиент.
    Локально `.env` даёт живые креды Timeweb, и удаление несуществующего
    ключа возвращает идемпотентный `204` — тест зеленеет СЛУЧАЙНО.
    В CI кредов нет, `s3_bucket` пуст, `delete_object` бросает, сервис
    считает объект сиротой: `PurgeResult(purged=1, orphaned_objects=1)`,
    и `test_worker_wrapper_returns_purge_result` падает на `assert 1 == 0`
    (`:525`) — при том, что проверяемое свойство (обёртка отдаёт
    `PurgeResult`, а не `int`) цело.

    **Почему это не ослабление.** Единица тестирования — логика удаления
    в БД, а не доступность внешнего бакета; сетевая зависимость делала
    результат функцией чужого сервиса и переменных окружения. Тесты,
    которым нужен СБОЙ хранилища (`test_worker_logs_orphaned_objects`,
    `test_storage_failure_does_not_rollback_deletion`), ставят свой
    `monkeypatch` в теле и перекрывают заглушку — их красное состояние
    не тронуто.
    """

    async def _noop(_storage_key: str) -> None:
        return None

    monkeypatch.setattr(media_service, "delete_object", _noop)


async def _user(session: AsyncSession, *, expired: bool = True) -> Any:
    """Учётка с пометкой удаления — с истёкшим окном по умолчанию."""
    phone = f"+7908{uuid_lib.uuid4().int % 10**7:07d}"
    row = await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, city_slug, "
            "role, status, gender, age_confirmed, consent_given_at, deletion_requested_at) "
            "VALUES ('phone', :p, :p, 'tomsk', 'user', 'active', 'female', true, now(), :d) "
            "RETURNING id, uuid"
        ),
        {
            "p": phone,
            "d": datetime.now(UTC) - DELETION_WINDOW - timedelta(days=1)
            if expired
            else datetime.now(UTC),
        },
    )
    user_id, user_uuid = row.one()
    return user_id, user_uuid


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:n, 'cafe', 'ул. Мира, 1', 'tomsk', true) RETURNING id"
        ),
        {"n": f"Кафе {uuid_lib.uuid4().hex[:8]}"},
    )
    return int(row.scalar_one())


async def _profile_with_photo(session: AsyncSession, *, user_id: int) -> str:
    """Анкета + одобренное фото. Возвращает `storage_key` для проверки S3."""
    row = await session.execute(
        text(
            "INSERT INTO profiles (user_id, name, city, age, gender, bio, status) "
            "VALUES (:u, 'Имя', 'Томск', 28, 'female', 'о себе', 'complete') RETURNING id"
        ),
        {"u": user_id},
    )
    profile_id = int(row.scalar_one())
    key = f"photos/{uuid_lib.uuid4()}/{uuid_lib.uuid4()}.jpg"
    await session.execute(
        text(
            "INSERT INTO profile_photos (profile_id, storage_key, position, status, content_type, "
            "size_bytes, consent_given_at, moderated_at) VALUES (:p, :k, 1, 'approved', "
            "'image/jpeg', 1000, now(), now())"
        ),
        {"p": profile_id, "k": key},
    )
    return key


async def _card(session: AsyncSession, *, author_id: int, place_id: int) -> int:
    moment = datetime.now(UTC) - timedelta(hours=1)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, kind, activity) "
            "VALUES (:a, :p, 'tomsk', 'Встреча', NULL, 'cafe', :st, 2, 60, :e, 'full', 2, "
            "'approved', :pub, 'company', 'coffee') RETURNING id"
        ),
        {
            "a": author_id,
            "p": place_id,
            "st": moment,
            "e": moment - timedelta(hours=1),
            "pub": moment - timedelta(days=1),
        },
    )
    return int(row.scalar_one())


async def _chat_with_message(session: AsyncSession, *, card_id: int, sender_id: int) -> int:
    row = await session.execute(
        text(
            "INSERT INTO chats (event_card_id, uuid, status) VALUES (:c, :u, 'active') RETURNING id"
        ),
        {"c": card_id, "u": str(uuid_lib.uuid4())},
    )
    chat_id = int(row.scalar_one())
    await session.execute(
        text(
            "INSERT INTO messages (chat_id, sender_id, body, uuid, is_system) "
            "VALUES (:c, :s, :b, :u, false)"
        ),
        {"c": chat_id, "s": sender_id, "b": b"x" * 40, "u": str(uuid_lib.uuid4())},
    )
    return chat_id


async def _seed_full_graph(session: AsyncSession) -> dict[str, Any]:
    """Полный граф доменов D-10.2-5 для одной учётки.

    Строится весь перечень: анкета с фото, карточка, заявка, чат с сообщением,
    явка, уведомление, мьют, жалоба, санкция, обращение, запись аудита.
    """
    user_id, user_uuid = await _user(session)
    photo_key = await _profile_with_photo(session, user_id=user_id)
    place_id = await _place(session)
    card_id = await _card(session, author_id=user_id, place_id=place_id)
    chat_id = await _chat_with_message(session, card_id=card_id, sender_id=user_id)

    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:c, :a, '', 'pending', now(), now() + interval '1 day')"
        ),
        {"c": card_id, "a": user_id},
    )

    author_flag, participant_flag, status = PAIR_FLAGS["no_show"]
    await session.execute(
        text(
            "INSERT INTO attendance (card_id, participant_id, author_confirmed, "
            "participant_confirmed, status, confirmed_at) "
            "VALUES (:c, :p, :af, :pf, :s, now())"
        ),
        {"c": card_id, "p": user_id, "af": author_flag, "pf": participant_flag, "s": status},
    )

    await session.execute(
        text(
            "INSERT INTO notifications (user_id, type, payload) "
            "VALUES (:u, 'request.received', '{}'::jsonb)"
        ),
        {"u": user_id},
    )
    await session.execute(
        text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, 'request.received')"),
        {"u": user_id},
    )

    report = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, target_type, status, reason, reported_at, chat_id) "
            "VALUES (:r, 'message', 'open', 'спам', now(), :chat) RETURNING id"
        ),
        {"r": user_id, "chat": chat_id},
    )
    report_id = int(report.scalar_one())
    await session.execute(
        text(
            "INSERT INTO sanctions (user_id, report_id, type, reason) "
            "VALUES (:u, :r, 'warning', 'нарушение')"
        ),
        {"u": user_id, "r": report_id},
    )
    await session.execute(
        text(
            "INSERT INTO support_tickets (user_id, body, status) "
            "VALUES (:u, 'текст обращения', 'open')"
        ),
        {"u": user_id},
    )
    # ADR-14: журнал аудита без FK на `users` — обязан пережить удаление.
    await session.execute(
        text(
            "INSERT INTO access_logs (moderator_uuid, action, subject_uuid) "
            "VALUES (:m, 'card_read', :s)"
        ),
        {"m": str(uuid_lib.uuid4()), "s": str(user_uuid)},
    )

    await session.flush()
    return {
        "user_id": user_id,
        "user_uuid": user_uuid,
        "photo_key": photo_key,
        "card_id": card_id,
        "chat_id": chat_id,
    }


async def _other_user(session: AsyncSession) -> int:
    """Второй пользователь БЕЗ пометки удаления — автор чужих данных.

    Нужен, чтобы домены `SET NULL` вообще могли наблюдаться (P-2, CR 10.2):
    если и модератор, и обвиняемый, и автор обращения — одна и та же учётка,
    строки уходят каскадом и `SET NULL` не отличить от `CASCADE`.
    """
    phone = f"+7909{uuid_lib.uuid4().int % 10**7:07d}"
    row = await session.execute(
        text(
            "INSERT INTO users (provider, provider_user_id, phone, city_slug, "
            "role, status, gender, age_confirmed, consent_given_at, deletion_requested_at) "
            "VALUES ('phone', :p, :p, 'tomsk', 'user', 'active', 'male', true, now(), NULL) "
            "RETURNING id"
        ),
        {"p": phone},
    )
    return int(row.scalar_one())


async def _seed_third_party_refs(session: AsyncSession, *, subject_id: int) -> dict[str, Any]:
    """Домены `SET NULL` (перечень D-10.2-5) + жалоба, которая обязана выжить.

    Четыре ссылки на удаляемую учётку живут в строках ТРЕТЬИХ лиц:
    `event_cards.verified_by` (модератор), `sanctions.moderator_id`,
    `reports.accused_id`, `support_tickets.answered_by_id`. Ни одна не
    закреплялась тестом до CR 10.2 (P-2): перечень обещал `SET NULL`, а
    проверялся только каскад.

    Жалоба намеренно подана на ЧУЖОЙ чат чужой карточки: жалоба на карточку
    удаляемого ушла бы каскадом через `chats`/`attendance`, и `accused_id`
    нельзя было бы проверить — строки бы просто не стало.
    """
    other_id = await _other_user(session)
    place_id = await _place(session)

    moment = datetime.now(UTC) + timedelta(hours=2)
    card = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, verified_at, verified_by, kind, activity) "
            "VALUES (:a, :p, 'tomsk', 'Чужая встреча', NULL, 'cafe', :st, 2, 60, :e, "
            "'draft', 0, 'approved', now(), :v, 'company', 'coffee') RETURNING id"
        ),
        {
            "a": other_id,
            "p": place_id,
            "st": moment,
            "e": moment - timedelta(minutes=30),
            # P-2: удаляемая учётка — модератор, проверивший чужую карточку.
            "v": subject_id,
        },
    )
    other_card_id = int(card.scalar_one())
    chat_id = await _chat_with_message(session, card_id=other_card_id, sender_id=other_id)

    report = await session.execute(
        text(
            "INSERT INTO reports (reporter_id, accused_id, target_type, status, reason, "
            "reported_at, chat_id) VALUES (:r, :a, 'message', 'open', 'оскорбление', now(), :c) "
            "RETURNING id"
        ),
        # P-2: удаляемая учётка — обвиняемый; жалоба принадлежит третьему лицу.
        {"r": other_id, "a": subject_id, "c": chat_id},
    )
    report_id = int(report.scalar_one())

    sanction = await session.execute(
        text(
            "INSERT INTO sanctions (user_id, report_id, moderator_id, type, reason) "
            "VALUES (:u, :r, :m, 'warning', 'предупреждение') RETURNING id"
        ),
        # P-2: удаляемая учётка — модератор, вынесший санкцию другому.
        {"u": other_id, "r": report_id, "m": subject_id},
    )
    sanction_id = int(sanction.scalar_one())

    ticket = await session.execute(
        text(
            "INSERT INTO support_tickets (user_id, body, status, answer, answered_at, "
            "answered_by_id) VALUES (:u, 'вопрос', 'answered', 'ответ', now(), :b) "
            "RETURNING id"
        ),
        # P-2: удаляемая учётка — ответивший модератор поддержки.
        {"u": other_id, "b": subject_id},
    )
    ticket_id = int(ticket.scalar_one())

    await session.flush()
    return {
        "other_id": other_id,
        "card_id": other_card_id,
        "report_id": report_id,
        "sanction_id": sanction_id,
        "ticket_id": ticket_id,
    }


async def _count(session: AsyncSession, table: str, where: str, params: dict[str, Any]) -> int:
    row = await session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)
    return int(row.scalar_one())


# --- AC-02, AC-04: каскады по перечню D-10.2-5 ---


async def test_purge_removes_user_and_all_related_domains(session: AsyncSession) -> None:
    """AC-02, AC-04: после прогона нет строк ни в одной из 14 таблиц перечня.

    Проверяется КАЖДАЯ таблица перечня: «учётки нет» прошло бы и при живой
    анкете, фото, чате и сообщениях — то есть при полностью невыполненном
    NFR40/NFR41.

    **P-2 (CR 10.2): `reports` в ассертах.** До правки жалоба посеяна
    строкой `reports.reporter_id` и не проверялась: перечень обещал 14
    таблиц, тест закреплял 13. Жалоба удаляемого пользователя — его
    собственный домен (`reporter_id CASCADE`), и она обязана исчезнуть;
    жалоба, поданная НА него третьим лицом, обязана остаться — это проверяет
    отдельный тест `test_purge_keeps_report_against_deleted_user`.
    """
    seeded = await _seed_full_graph(session)
    user_id = seeded["user_id"]

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert result.purged == 1
    assert await _count(session, "users", "id = :u", {"u": user_id}) == 0
    assert await _count(session, "profiles", "user_id = :u", {"u": user_id}) == 0
    assert (
        await _count(
            session,
            "profile_photos",
            "storage_key = :k",
            {"k": seeded["photo_key"]},
        )
        == 0
    )
    assert await _count(session, "event_cards", "id = :c", {"c": seeded["card_id"]}) == 0
    assert await _count(session, "join_requests", "applicant_id = :u", {"u": user_id}) == 0
    assert await _count(session, "chats", "id = :c", {"c": seeded["chat_id"]}) == 0
    assert await _count(session, "messages", "sender_id = :u", {"u": user_id}) == 0
    assert await _count(session, "attendance", "participant_id = :u", {"u": user_id}) == 0
    assert await _count(session, "notifications", "user_id = :u", {"u": user_id}) == 0
    assert await _count(session, "notification_mutes", "user_id = :u", {"u": user_id}) == 0
    assert await _count(session, "sanctions", "user_id = :u", {"u": user_id}) == 0
    assert await _count(session, "support_tickets", "user_id = :u", {"u": user_id}) == 0
    # P-2: жалоба, где удаляемая учётка — REPORTER (CASCADE по reporter_id).
    assert await _count(session, "reports", "reporter_id = :u", {"u": user_id}) == 0
    # refresh_tokens пользователя тоже уходят каскадом (0001).
    assert await _count(session, "refresh_tokens", "user_id = :u", {"u": user_id}) == 0


async def test_purge_keeps_access_logs(session: AsyncSession) -> None:
    """AC-04, ADR-14, Ловушка 5: `access_logs` НЕ удаляется.

    FK на `users` там нет намеренно: журнал аудита переживает удаление.
    Красным станет, если кто-то добавит `CASCADE` в `0029`.
    """
    seeded = await _seed_full_graph(session)
    subject = str(seeded["user_uuid"])

    await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert await _count(session, "access_logs", "subject_uuid = :s", {"s": subject}) == 1


async def test_purge_keeps_report_against_deleted_user_and_nulls_accused(
    session: AsyncSession,
) -> None:
    """AC-04, P-2 (CR 10.2): `reports.accused_id` — `SET NULL`, строка ЖИВА.

    Жалоба подана ТРЕТЬИМ лицом на удаляемую учётку. `reports.accused_id`
    объявлен `ON DELETE SET NULL` (`0025`): история разбирательств обязана
    пережить удаление обвиняемого, иначе модератор теряет контекст, а
    жалобщик — след своего обращения.

    Красным станет, если кто-то сменит `SET NULL` на `CASCADE`: строка
    исчезнет и `count(*)` даст `0` вместо `1`. Проверяется и сам факт
    выживания строки, и обнуление ссылки — по отдельности, потому что
    `SET NULL` и «строка исчезла» дают одинаковый `count(*) = 0` по
    предикату `accused_id = :u`.
    """
    seeded = await _seed_full_graph(session)
    user_id = seeded["user_id"]
    refs = await _seed_third_party_refs(session, subject_id=user_id)

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert result.purged == 1
    # Строка жалобы жива...
    assert await _count(session, "reports", "id = :r", {"r": refs["report_id"]}) == 1
    # ...а ссылка на удалённую учётку обнулена.
    assert (
        await _count(session, "reports", "id = :r AND accused_id IS NULL", {"r": refs["report_id"]})
        == 1
    )


async def test_purge_nulls_verified_by_moderator_id_and_answered_by_id(
    session: AsyncSession,
) -> None:
    """AC-04, P-2 (CR 10.2): три `SET NULL`-домена модерации.

    `event_cards.verified_by`, `sanctions.moderator_id`,
    `support_tickets.answered_by_id` — все ссылаются на удаляемую учётку из
    строк ТРЕТЬИХ лиц. До правки ни один не закреплён тестом: перечень
    D-10.2-5 обещал `SET NULL`, ассертов было ноль (CR 10.2, P-2).

    Каждый проверяется парой «строка жива + ссылка NULL»: одного
    `IS NULL` мало — он пройдёт и на удалённой строке, если считать
    `count(*) WHERE col IS NULL` без привязки к `id`.
    """
    seeded = await _seed_full_graph(session)
    user_id = seeded["user_id"]
    refs = await _seed_third_party_refs(session, subject_id=user_id)

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)
    assert result.purged == 1

    # `event_cards.verified_by` — модератор, проверивший чужую карточку.
    assert await _count(session, "event_cards", "id = :c", {"c": refs["card_id"]}) == 1
    assert (
        await _count(
            session, "event_cards", "id = :c AND verified_by IS NULL", {"c": refs["card_id"]}
        )
        == 1
    )

    # `sanctions.moderator_id` — модератор, вынесший санкцию другому.
    assert await _count(session, "sanctions", "id = :s", {"s": refs["sanction_id"]}) == 1
    assert (
        await _count(
            session, "sanctions", "id = :s AND moderator_id IS NULL", {"s": refs["sanction_id"]}
        )
        == 1
    )

    # `support_tickets.answered_by_id` — модератор, ответивший на обращение.
    assert await _count(session, "support_tickets", "id = :t", {"t": refs["ticket_id"]}) == 1
    assert (
        await _count(
            session,
            "support_tickets",
            "id = :t AND answered_by_id IS NULL",
            {"t": refs["ticket_id"]},
        )
        == 1
    )


# --- AC-07: идемпотентность ---


async def test_second_run_purges_zero(session: AsyncSession) -> None:
    """AC-07: второй прогон тех же кандидатов возвращает `0` и не падает.

    CAS-предикат: строки уже нет, `RETURNING` пуст. Красным станет, если
    сервис считает «найденных», а не «удалённых».

    Идемпотентность проверяется на ЯДРЕ в одной сессии: `run_account_purges`
    открывает СВОЮ сессию через `get_session_factory()` и физически не может
    увидеть данные SAVEPOINT-транзакции теста. Это архитектурно верно (воркер
    владеет транзакцией, роутера над ним нет) — обёртка покрыта отдельным
    тестом ниже.
    """
    seeded = await _seed_full_graph(session)

    first = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)
    second = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert first.purged == 1
    assert second.purged == 0
    assert await _count(session, "users", "id = :u", {"u": seeded["user_id"]}) == 0


async def test_worker_wrapper_returns_purge_result(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-02, P-10 (CR 10.2): обёртка возвращает `PurgeResult`, не `int`.

    Фабрика сессий подменяется на тестовую: иначе воркер открыл бы отдельное
    соединение и не увидел бы SAVEPOINT-данные. Подменяется ИМЕННО фабрика,
    а не ядро: проверяется, что обёртка зовёт ядро и возвращает `purged`,
    а не «найденных».

    **P-10:** до правки обёртка отдавала `result.purged`, и `orphaned_objects`
    уничтожался — счётчик сирот S3 не доходил до лога ни в одном сценарии.
    Возврат `PurgeResult` проверяется здесь, а запись сироты в лог — в
    `test_worker_logs_orphaned_objects` ниже.
    """
    from contextlib import asynccontextmanager

    from app.workers import retention

    seeded = await _seed_full_graph(session)

    @asynccontextmanager
    async def _factory():
        yield session

    monkeypatch.setattr(retention, "get_session_factory", lambda: _factory)

    result = await retention.run_account_purges(now=datetime.now(UTC), limit=10)

    assert result.purged == 1
    assert result.orphaned_objects == 0
    assert await _count(session, "users", "id = :u", {"u": seeded["user_id"]}) == 0


async def test_worker_logs_orphaned_objects(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-10 (CR 10.2): `orphaned_objects` доходит до лога `account_purge_run`.

    До правки `main()` логировал только `purged`, а `orphaned_objects`
    считался в сервисе и выбрасывался: сбой S3 оставлял объекты без следа
    в наблюдаемости (ADR-14 при этом заявлен). Тест проверяет ИМЕННО запись
    в structlog, а не значение в `PurgeResult` — иначе дефект не отличается
    от исправления.

    `main()` крутится вечно, поэтому проверяется один проход: `sleep`
    подменяется на исключение-остановку, пойманное после вызова.
    """
    from contextlib import asynccontextmanager

    from app.workers import retention

    await _seed_full_graph(session)

    @asynccontextmanager
    async def _factory():
        yield session

    async def _boom(_storage_key: str) -> None:
        raise RuntimeError("S3 недоступен")

    captured: list[dict[str, Any]] = []

    class _StopWorker(Exception):
        pass

    async def _stop(_seconds: float) -> None:
        raise _StopWorker

    monkeypatch.setattr(retention, "get_session_factory", lambda: _factory)
    monkeypatch.setattr(media_service, "delete_object", _boom)
    monkeypatch.setattr(retention.asyncio, "sleep", _stop)

    # structlog в тестах пишет в stdlib: перехватываем событие через bound logger.
    original_info = retention.logger.info

    def _capture(event: str, **kwargs: Any) -> None:
        if event == "account_purge_run":
            captured.append({"event": event, **kwargs})
        original_info(event, **kwargs)

    monkeypatch.setattr(retention.logger, "info", _capture)

    with pytest.raises(_StopWorker):
        await retention.main()

    assert len(captured) == 1
    assert captured[0]["purged"] == 1
    # Главное: счётчик сирот попал в лог, а не потерялся между сервисом и логом.
    assert captured[0]["orphaned_objects"] == 1
    assert "duration_ms" in captured[0]


# --- Окно: кандидат берётся только с истёкшим окном ---


async def test_active_window_is_not_purged(session: AsyncSession) -> None:
    """AC-01: учётка с активным окном воркером НЕ удаляется.

    Красным станет, если предикат кандидатов потеряет сравнение с окном
    и заберёт всех, у кого пометка не NULL.
    """
    user_id, _uuid = await _user(session, expired=False)

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert result.purged == 0
    assert await _count(session, "users", "id = :u", {"u": user_id}) == 1


# --- AC-08: порядок «keys до DELETE, объект после commit» (18.11) ---


async def test_photo_keys_collected_before_delete_and_objects_after_commit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-08, 18.11: порядок «keys → DELETE → commit → S3» через ПЕРЕХВАТ.

    **Почему журнал порядка вызовов, а не видимость из другого соединения.**
    Тесты идут в SAVEPOINT-изоляции (§18.8), и второе соединение видит
    незакоммиченное состояние внешней транзакции теста — проверка видимости
    не отличает `commit` от `flush`. Мутация «заменить `commit` на `flush`»
    на ней выжила (M3 SURVIVED). Журнал фиксирует ФАКТ и МЕСТО вызова.

    Порядок обязателен: ключи фото собираются до `DELETE` (после удаления
    строк их негде взять), объекты S3 удаляются после `commit` (иначе откат
    оставит в БД битые ссылки).
    """
    seeded = await _seed_full_graph(session)
    user_id = seeded["user_id"]
    events: list[str] = []
    original_commit = session.commit

    async def _tracked_commit() -> None:
        events.append("commit")
        await original_commit()

    async def _fake_delete(_storage_key: str) -> None:
        events.append("s3_delete")

    monkeypatch.setattr(session, "commit", _tracked_commit)
    monkeypatch.setattr(media_service, "delete_object", _fake_delete)

    await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    # Мутация `commit -> flush` убирает событие 'commit': тест краснеет.
    assert events == ["commit", "s3_delete"]
    assert await _count(session, "users", "id = :u", {"u": user_id}) == 0


async def test_photo_keys_collected_before_delete(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-08: `storage_key` собраны ДО `DELETE FROM users`.

    Перехват `delete_user`: в момент его вызова ключи уже должны быть
    прочитаны, иначе после каскада брать их будет негде.
    """
    from app.repositories import account_deletion as repo_module

    seeded = await _seed_full_graph(session)
    collected: list[list[str]] = []
    original_collect = repo_module.AccountDeletionRepository.collect_photo_keys
    original_delete = repo_module.AccountDeletionRepository.delete_user

    async def _collect(self: Any, target_user_id: int) -> list[str]:
        keys = await original_collect(self, target_user_id)
        collected.append(keys)
        return keys

    async def _delete(self: Any, target_user_id: int, *, now: Any) -> int:
        # В момент удаления ключи УЖЕ собраны — иначе список пуст.
        assert collected and collected[-1] == [seeded["photo_key"]]
        return await original_delete(self, target_user_id, now=now)

    monkeypatch.setattr(repo_module.AccountDeletionRepository, "collect_photo_keys", _collect)
    monkeypatch.setattr(repo_module.AccountDeletionRepository, "delete_user", _delete)

    await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert collected == [[seeded["photo_key"]]]


async def test_cancel_between_select_and_delete_wins(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-07: CAS-предикат в `DELETE` не даёт удалить отменённую учётку.

    **Доказательство CAS, а не «повезло».** `collect_photo_keys` вызывается
    между `SELECT` кандидатов и `DELETE` — перехват снимает пометку ровно
    в этом окне, имитируя `DELETE /me/delete` от пользователя. Мутация
    «убрать предикат окна из `DELETE`» на обычном тесте выжила (M5 SURVIVED):
    между выборкой и удалением пометку никто не снимал, и разницы не было.
    """
    from app.repositories import account_deletion as repo_module

    seeded = await _seed_full_graph(session)
    user_id = seeded["user_id"]
    original = repo_module.AccountDeletionRepository.collect_photo_keys

    async def _cancel_then_collect(self: Any, target_user_id: int) -> list[str]:
        keys = await original(self, target_user_id)
        await session.execute(
            text("UPDATE users SET deletion_requested_at = NULL WHERE id = :u"),
            {"u": target_user_id},
        )
        return keys

    monkeypatch.setattr(
        repo_module.AccountDeletionRepository, "collect_photo_keys", _cancel_then_collect
    )

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert result.purged == 0
    assert await _count(session, "users", "id = :u", {"u": user_id}) == 1


async def test_storage_failure_does_not_rollback_deletion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-08, D-10 стори 2.4: ошибка S3 НЕ откатывает удаление аккаунта.

    Красным станет, если `delete_object` вызывать до `commit` или не ловить
    исключение: аккаунт остался бы в БД из-за недоступности хранилища.
    """
    seeded = await _seed_full_graph(session)
    user_id = seeded["user_id"]

    async def _boom(_storage_key: str) -> None:
        raise RuntimeError("S3 недоступен")

    monkeypatch.setattr(media_service, "delete_object", _boom)

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert result.purged == 1
    assert result.orphaned_objects == 1
    assert await _count(session, "users", "id = :u", {"u": user_id}) == 0


# --- P-9 (CR 10.2): `account_purged` несёт `duration_ms` ---


async def test_account_purged_event_carries_duration_ms(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-9 (CR 10.2): событие `account_purged` содержит `duration_ms`.

    D-10.2-8 требует `user_uuid` И `duration_ms`. До правки `duration_ms`
    жил только в `account_purge_run` — событии, которое логировалось
    `if purged:` и уже после цикла, поэтому на КАЖДУЮ учётку его не было.
    Докстринг `workers/retention.py` утверждал обратное коду.

    Проверяется запись в structlog, а не возврат значения: «есть поле в
    объекте» и «поле дошло до лога» — разные утверждения, и разрыв между
    ними был дефектом (тот же класс, что 6 из 25 находок CR 10.1).
    """
    from app.services import account_deletion_service as service_module

    await _seed_full_graph(session)

    captured: list[dict[str, Any]] = []
    original_info = service_module.logger.info

    def _capture(event: str, **kwargs: Any) -> None:
        if event == "account_purged":
            captured.append({"event": event, **kwargs})
        original_info(event, **kwargs)

    monkeypatch.setattr(service_module.logger, "info", _capture)

    result = await purge_due_accounts(session, now=datetime.now(UTC), limit=10)

    assert result.purged == 1
    assert len(captured) == 1
    assert captured[0]["event"] == "account_purged"
    # Главное: метрика длительности дошла до события.
    assert "duration_ms" in captured[0]
    assert isinstance(captured[0]["duration_ms"], int)
    assert captured[0]["duration_ms"] >= 0
    # Внутренний `id` наружу не выходит (project-context §7) — только `uuid`.
    assert "user_uuid" in captured[0]
    assert "user_id" not in captured[0]
