"""FR110 — уведомление автору о приближении окна решения (стори 9.4; AC-01, AC-03).

**Что доказывается.** До 9.4 `window.expiring` существовал только в схеме:
ни одного места в коде, которое его создаёт, не было. Здесь проверяется,
что воркер напоминаний 5б.4 доводит дело до КАССЫ, то есть до строки в
`notifications` и события в личном канале автора.

**Три свойства, каждое ловит свой класс дефекта:**

1. **Адресат — только автор** (D-9.4-3). Уведомление участнику вело бы в
   `403` на выборе заявки; проверка «участникам не пришло» — не украшение,
   а единственный способ поймать `M3`.
2. **Маркер в БД, не в памяти** (AC-03, ADR-15). Второй прогон даёт `SKIPPED`
   и не увеличивает число строк: маркер — `decision_reminded_at`, CAS.
3. **Безличность payload** (FR81). Рекурсивный обход ключей: вложенный объект
   не должен прятать `author_id`.

§18.3: `now` — параметр; `sleep` нет. §18.11: `delivered_at` проверяется в
обоих значениях (успех публикации и отсутствие Redis).
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.user import User
from app.repositories.users import UserRepository
from app.services import notification_service
from app.workers.decision_reminders import run_reminders

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
THRESHOLD = 15
WINDOW_EXPIRING_TYPE = "window.expiring"

_CREATED_USERS: list[int] = []
_CREATED_PLACES: list[int] = []


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL, pool_size=8, max_overflow=0)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    _CREATED_USERS.clear()
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_USERS and not _CREATED_PLACES:
        return
    async with factory() as session:
        if _CREATED_USERS:
            await session.execute(
                text("DELETE FROM notifications WHERE user_id = ANY(:ids)"),
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
    await session.flush()
    _CREATED_USERS.append(int(user.id))
    return user


async def _place(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO places (name, category, address_text, city_slug, is_active) "
            "VALUES (:name, 'cafe', 'ул. Мира, 1', :slug, true) RETURNING id"
        ),
        {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": settings.default_city_slug},
    )
    place_id = int(row.scalar_one())
    _CREATED_PLACES.append(place_id)
    return place_id


async def _insert_card(
    session: AsyncSession,
    *,
    author_id: int,
    place_id: int,
    expires_at: datetime,
    status: str = "published",
) -> tuple[uuid_lib.UUID, int]:
    """Карточка `published` с ЯВНЫМ `expires_at`: порог проверяется точно.

    Набор колонок повторяет `attendance_prompt_helpers.insert_card` (единственный
    проверенный список): `request_mode` в схеме нет, а `decision_window_minutes`
    и `verification_status` обязательны по CHECK-ам.
    """
    starts = expires_at + timedelta(days=1)
    row = await session.execute(
        text(
            "INSERT INTO event_cards (author_id, place_id, city_slug, title, description, "
            "category, starts_at, capacity, decision_window_minutes, expires_at, status, "
            "filled_count, verification_status, published_at, chat_uuid, attendance_prompted_at,"
            "kind, activity) "
            "VALUES (:author, :place, :slug, 'Встреча', NULL, 'cafe', :starts, 3, 60, "
            ":expires, :status, 1, 'approved', :published_at, NULL, NULL, 'company', 'coffee') "
            "RETURNING uuid, id"
        ),
        {
            "author": author_id,
            "place": place_id,
            "slug": settings.default_city_slug,
            "starts": starts,
            "expires": expires_at,
            "status": status,
            "published_at": expires_at - timedelta(days=1),
        },
    )
    row = row.one()
    return uuid_lib.UUID(str(row[0])), int(row[1])


async def _add_selected_participant(session: AsyncSession, *, card_id: int, user_id: int) -> None:
    """Принятая заявка: `join_requests.status='selected'` (домен 5а.1)."""
    moment = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO join_requests (card_id, applicant_id, message, status, submitted_at, "
            "expires_at) VALUES (:c, :u, '', 'selected', :submitted, :expires)"
        ),
        {
            "c": card_id,
            "u": user_id,
            "submitted": moment - timedelta(hours=2),
            "expires": moment + timedelta(days=1),
        },
    )


async def _rows(session: AsyncSession, card_uuid: uuid_lib.UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT user_id, type, payload, delivered_at FROM notifications "
            "WHERE type = :t AND payload->>'card_uuid' = :u"
        ),
        {"t": WINDOW_EXPIRING_TYPE, "u": str(card_uuid)},
    )
    return [
        {
            "user_id": int(row[0]),
            "type": str(row[1]),
            "payload": dict(row[2]),
            "delivered_at": row[3],
        }
        for row in result.all()
    ]


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.add(str(key))
            keys |= _all_keys(inner)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# ——— AC-01: ровно одна строка, адресат — автор ————————————————————————


async def test_window_expiring_goes_to_author_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01 (D-9.4-3, M3): уведомление получает ТОЛЬКО автор карточки.

    Краснеет при мутации M3 (адресовать участникам): участник выбор заявки
    сделать не может, ссылка из уведомления вернула бы `403`.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        participant = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await _add_selected_participant(seed, card_id=card_id, user_id=int(participant.id))
        await seed.commit()

    await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        rows = await _rows(check, card_uuid)

    assert len(rows) == 1, rows
    assert rows[0]["user_id"] == int(author.id), "уведомление ушло не автору (M3)"
    assert rows[0]["type"] == WINDOW_EXPIRING_TYPE
    # AC-01: публикация удалась — отметка доставки стоит (оба значения, §18.11).
    assert rows[0]["delivered_at"] is not None


async def test_no_notification_before_threshold(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01: до порога строки НЕТ — иначе уведомление приходило бы за сутки.

    Обратное значение признака к предыдущему тесту: без него «создаём всегда»
    проходило бы зелёным.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, _ = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(hours=10),
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        rows = await _rows(check, card_uuid)

    assert counters["reminded"] == 0
    assert counters["notified"] == 0
    assert rows == []


async def test_notification_published_to_author_channel(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01 (M3): событие уходит в ЛИЧНЫЙ канал автора, и только туда.

    До этого теста адрес проверялся только по строке в БД. Мутация «взять
    `card_uuid` вместо `author_uuid`» оставляла БД нетронутой и проходила
    зелёной — то есть адресация канала не была покрыта вовсе (D-98: усилить
    тест, а не ослаблять мутацию).

    Проверяются оба значения (§18.11): канал автора получил ровно одно
    событие, канал участника — ни одного.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        participant = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await _add_selected_participant(seed, card_id=card_id, user_id=int(participant.id))
        await seed.commit()

    channels: list[str] = []
    events: list[str] = []

    async def _spy(*, user_uuid: Any, envelope: Any) -> bool:
        channels.append(str(user_uuid))
        events.append(envelope.type)
        return True

    monkeypatch.setattr(notification_service, "publish_to_user", _spy)

    await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    assert channels == [str(author.uuid)], channels
    assert events == [WINDOW_EXPIRING_TYPE], events


async def test_payload_has_no_personal_keys(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-01 (M11, FR81): payload без `author_id`/`user_id` и имён.

    Обход рекурсивный: вложенный объект не должен прятать ключ.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, _ = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.commit()

    await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        rows = await _rows(check, card_uuid)

    assert len(rows) == 1
    keys = _all_keys(rows[0]["payload"])
    forbidden = {"user_id", "author_id", "participant_id", "applicant_id", "name", "phone"}
    assert keys & forbidden == set(), keys
    # Форма: ровно два поля, `expires_at` в ISO-Z (как курсор сообщений).
    assert set(rows[0]["payload"]) == {"card_uuid", "expires_at"}
    assert str(rows[0]["payload"]["expires_at"]).endswith("Z")


# ——— AC-03: дедупликация маркером в БД ————————————————————————————————


async def test_second_run_creates_no_duplicate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC-03 (M1, M2, ADR-15): второй прогон — `SKIPPED`, строк не прибавилось.

    Краснеет при M2 (создавать уведомление до CAS): маркер не проверен,
    и второй прогон создал бы вторую строку.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, _ = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.commit()

    first = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)
    second = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        rows = await _rows(check, card_uuid)

    assert first["reminded"] == 1
    assert first["notified"] == 1
    assert second["reminded"] == 0
    assert second["notified"] == 0
    assert len(rows) == 1, "маркер не в БД: уведомление продублировано"


async def test_muted_type_creates_no_row_and_no_ws(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-01 9.3 + Task 3.3: `window.expiring` ОТКЛЮЧАЕМ — строки и WS нет.

    Тип не входит в `REQUIRED_NOTIFICATION_TYPES` (там только
    `report.resolved` и `sanction.issued`), поэтому отключение обязано работать:
    gate 9.3 стоит внутри `create_one` и отфильтровывает строку целиком.
    Проверяются ОБА следствия: строка не создана И событие не опубликовано
    (иначе клиент получил бы уведомление, которого нет в списке).
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, _ = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.execute(
            text("INSERT INTO notification_mutes (user_id, type) VALUES (:u, :t)"),
            {"u": int(author.id), "t": WINDOW_EXPIRING_TYPE},
        )
        await seed.commit()

    published: list[Any] = []

    async def _spy(*, user_uuid: Any, envelope: Any) -> bool:
        published.append(envelope.type)
        return True

    monkeypatch.setattr(notification_service, "publish_to_user", _spy)

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        rows = await _rows(check, card_uuid)

    # Маркер FR103 всё равно встал: отключение настройки не отменяет доменное
    # напоминание (D-9.3-7 — нормальный исход, а не сбой).
    assert counters["reminded"] == 1
    assert counters["notified"] == 0
    assert counters["notify_failed"] == 1
    assert rows == [], "отключённому типу создана строка"
    assert published == [], "отключённый тип опубликован в WS"


async def test_cas_loser_creates_no_notification(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03 (M2, CR r1 MEDIUM-2): проигранный CAS не создаёт уведомления.

    **Почему прежняя формулировка M2 выживала.** Второй прогон воркера вообще
    не доходит до цикла: карточка уже не попадает в `due_reminder_ids`
    (`decision_reminded_at IS NULL`), поэтому guard воркера не исполняется и
    мутация «не выходить на SKIPPED» ничего не меняет. Проверка была слепой
    не к дефекту, а к СЦЕНАРИЮ: гонка двух прогонов.

    Здесь гонка воспроизводится детерминированно, как в 5б.4: `reminder_state`
    подменён так, что возвращает `decision_reminded_at = NULL` ПРИ РЕАЛЬНО
    СТОЯЩЕМ маркере. Значит шаги 2–5 проходят, и решение принимает только CAS.

    Краснеет при снятии проверки `marked_uuid is None`: проигравший прогон
    счёл бы себя победителем и создал ВТОРОЕ уведомление FR110 — то есть
    дубль, который AC-03 запрещает.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        # Маркер уже стоит: «другой прогон успел первым».
        await seed.execute(
            text("UPDATE event_cards SET decision_reminded_at = :m WHERE id = :id"),
            {"m": moment - timedelta(seconds=30), "id": card_id},
        )
        await seed.commit()

    from app.repositories.event_cards import EventCardRepository

    original_state = EventCardRepository.reminder_state

    async def _stale_due(
        self: EventCardRepository, *, now: Any, threshold_minutes: int, limit: int
    ) -> list[tuple[int, int, Any]]:
        # Карточка ПОПАЛА в выборку: «первый прогон прочитал кандидатов до
        # того, как второй поставил маркер». Без этой подмены SQL-предикат
        # `decision_reminded_at IS NULL` отфильтровал бы её, воркер не вошёл
        # бы в цикл, и тест был бы слеп к самому сценарию (CR MEDIUM-2).
        del now, threshold_minutes, limit
        return [(card_id, int(author.id), card_uuid)]

    async def _stale_state(self: EventCardRepository, *, card_id: int) -> Any:
        real = await original_state(self, card_id=card_id)
        assert real is not None
        # «Прочитали до того, как другой прогон поставил маркер».
        return (real[0], real[1], real[2], None, real[4], real[5])

    monkeypatch.setattr(EventCardRepository, "due_reminder_ids", _stale_due)
    monkeypatch.setattr(EventCardRepository, "reminder_state", _stale_state)

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        rows = await _rows(check, card_uuid)

    assert counters["reminded"] == 0, "проигранный CAS засчитан как напоминание"
    assert counters["notified"] == 0
    assert rows == [], "проигравший CAS создал уведомление — дубль FR110 (M2)"


async def test_marker_survives_notification_failure(
    factory: async_sessionmaker[AsyncSession], dead_redis: Any
) -> None:
    """AC-03, D-9.4-4 (M4): сбой доставки НЕ откатывает маркер.

    Иначе карточка с недоступным Redis выбиралась бы каждые 60 с вечно, а
    маркер FR103 (`decision_reminded_at`) откатывался бы из-за вторичного
    следствия. Краснеет при M4.
    """
    moment = datetime.now(UTC)
    async with factory() as seed:
        author = await _new_user(seed)
        place_id = await _place(seed)
        card_uuid, card_id = await _insert_card(
            seed,
            author_id=int(author.id),
            place_id=place_id,
            expires_at=moment + timedelta(minutes=5),
        )
        await seed.commit()

    counters = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)
    again = await run_reminders(factory, now=moment, threshold_minutes=THRESHOLD)

    async with factory() as check:
        marker = await check.execute(
            text("SELECT decision_reminded_at FROM event_cards WHERE id = :id"), {"id": card_id}
        )
        rows = await _rows(check, card_uuid)

    assert counters["reminded"] == 1
    assert counters["notify_failed"] == 1, "сбой доставки не посчитан"
    assert counters["notified"] == 0
    # Маркер FR103 стоит, несмотря на провал канала.
    assert marker.scalar_one() is not None
    # D-9.2-5: строка осталась без отметки доставки.
    assert len(rows) == 1
    assert rows[0]["delivered_at"] is None, "отметка без успешной публикации (M9)"
    # Повторного напоминания нет: карточка вышла из выборки.
    assert again["reminded"] == 0
