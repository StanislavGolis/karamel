"""Гонка публикаций карточки на двух соединениях (§18.3, стори 4.4; AC-03, AC-04, AC-06).

§18.3: гонку в одной сессии доказать нельзя — второй запрос видит
незакоммиченные изменения первого. Поэтому здесь НЕ используется фикстура
`session` из `conftest.py` (она живёт в одной транзакции, §18.8): данные
готовятся собственным движком и коммитятся по-настоящему, а публикации идут
через реальный сервис, каждая — в своей сессии со своим соединением.

**Доказательство — счётчик успешных `UPDATE`, а не коды ответов** (AC-03,
урок CR 2.2). Коды не различают ветки: без блокировки `users` второй запрос
всё равно упадёт — но упадёт на `CHECK`/CAS уже ПОСЛЕ записи, и `429`
получится в обоих случаях. Различитель — сколько раз CAS вернул `rowcount == 1`:
под блокировкой ровно один, без неё — два.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.enums import CardVerificationStatus, EventCardStatus
from app.repositories.event_cards import EventCardRepository
from app.repositories.users import UserRepository
from app.services.event_card_service import publish_card

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
COMMUNITY = "tomsk"
START_DELTA = timedelta(days=3)


class _RaceSettings:
    """Настройки для сервиса: минимум полей, лимит задаётся сценарием.

    `Settings` целиком не используется намеренно: тест должен проверять
    ПОВЕДЕНИЕ лимита, а не текущее значение `.env` стенда.
    """

    default_city_slug = COMMUNITY
    city_slugs = (COMMUNITY,)
    card_default_decision_window_minutes = 2880
    card_content_stop_words = ("казино", "займ")

    def __init__(self, limit: int = 2) -> None:
        self.card_active_limit = limit


class _OrderTrackingSession:
    """Прокси сессии: пишет текст каждого запроса в журнал (AC-04).

    Порядок блокировок PostgreSQL = порядок выполнения запросов. Поэтому
    журнал `execute` доказывает ADR-09 без чтения `pg_locks`: если первым
    уходит `SELECT users ... FOR UPDATE`, а `UPDATE event_cards` — позже,
    строки блокируются в правильном порядке. Краснеет при мутации 7
    (`COUNT` до `FOR UPDATE`) и при `FOR UPDATE` на карточке (тогда
    `UPDATE`/`SELECT ... FOR UPDATE event_cards` оказался бы первым).
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.log: list[str] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.log.append(str(statement))
        return await self._session.execute(statement, *args, **kwargs)

    async def commit(self) -> None:
        await self._session.commit()

    async def flush(self) -> None:
        await self._session.flush()


@pytest.fixture
async def engine() -> AsyncIterator[Any]:
    """Собственный движок: настоящие коммиты, видимые всем соединениям."""
    database_engine = create_async_engine(DATABASE_URL, poolclass=None)
    try:
        yield database_engine
    finally:
        await database_engine.dispose()


@pytest.fixture
async def prepared_author(engine: Any) -> AsyncIterator[int]:
    """Учётка автора, закоммиченная по-настоящему; чистится каскадом."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as preparation:
        phone = f"+7907{uuid_lib.uuid4().int % 10**7:07d}"
        user = await UserRepository(preparation).create(
            provider="phone", provider_user_id=phone, phone=phone, gender="female"
        )
        await preparation.commit()
    try:
        yield user.id
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await cleanup.commit()


# Места, созданные тестами: чистятся после каждого теста. Без этого прогон
# оставлял бы строки в базе разработчика, и интеграционные тесты мест
# (`test_places_list_api.py`) падали бы на «чужих» строках в своей выдаче —
# дефект, найденный полным прогоном (MultipleResultsFound).
_CREATED_PLACES: list[int] = []


@pytest.fixture(autouse=True)
async def _cleanup_places(engine: Any) -> AsyncIterator[None]:
    """Удаляет места, созданные тестом, вместе с их карточками (FK `RESTRICT`)."""
    _CREATED_PLACES.clear()
    yield
    if not _CREATED_PLACES:
        return
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as cleanup:
        # `ON DELETE RESTRICT` на `event_cards.place_id`: сначала карточки.
        await cleanup.execute(
            text("DELETE FROM event_cards WHERE place_id = ANY(:ids)"),
            {"ids": _CREATED_PLACES},
        )
        await cleanup.execute(
            text("DELETE FROM places WHERE id = ANY(:ids)"), {"ids": _CREATED_PLACES}
        )
        await cleanup.commit()
    _CREATED_PLACES.clear()


async def _insert_place(factory: Any) -> int:
    """Место под карточку: `place_id` обязателен (FK `RESTRICT`)."""
    async with factory() as preparation:
        row = await preparation.execute(
            text(
                "INSERT INTO places (name, category, address_text, city_slug, is_active) "
                "VALUES (:name, 'cafe', 'пр. Ленина, 1', :slug, true) RETURNING id"
            ),
            {"name": f"Кафе {uuid_lib.uuid4().hex[:8]}", "slug": COMMUNITY},
        )
        place_id = int(row.scalar_one())
        await preparation.commit()
    _CREATED_PLACES.append(place_id)
    return place_id


async def _insert_card(
    factory: Any,
    *,
    author_id: int,
    place_id: int,
    status: str = "draft",
    verification_status: str = "approved",
    rejection_reason: str | None = None,
) -> tuple[int, uuid_lib.UUID]:
    """Карточка в нужном состоянии, закоммиченная по-настоящему."""
    starts_at = datetime.now(UTC) + START_DELTA
    values: dict[str, Any] = {
        "author_id": author_id,
        "place_id": place_id,
        "city_slug": COMMUNITY,
        "title": "Настольные игры",
        "category": "cafe",
        "starts_at": starts_at,
        "capacity": 3,
        "decision_window_minutes": 2880,
        "expires_at": starts_at - timedelta(hours=48),
        "status": status,
        "verification_status": verification_status,
        # Стори 4.6 (AC-07, X-35): CHECK `ck_event_cards_full_capacity` стал
        # ДВУСТОРОННИМ — `full` при свободном месте запрещён базой. Без этого
        # сидирование `full` падало бы `IntegrityError`, и тест проверял бы
        # не лимит, а свежеисправленный дефект схемы.
        "filled_count": 3 if status == "full" else 0,
        # CHECK `ck_event_cards_published`: опубликованная карточка обязана
        # иметь момент публикации.
        "published_at": datetime.now(UTC) if status == "published" else None,
        # CHECK `ck_event_cards_rejection_reason`: отклонение без причины
        # запрещено (FR113).
        "rejection_reason": rejection_reason,
    }
    async with factory() as preparation:
        row = await preparation.execute(
            text(
                "INSERT INTO event_cards "
                "(author_id, place_id, city_slug, title, category, starts_at, "
                " capacity, decision_window_minutes, expires_at, status, "
                " verification_status, filled_count, published_at, rejection_reason,"
                "kind, activity) "
                "VALUES (:author_id, :place_id, :city_slug, :title, :category, "
                " :starts_at, :capacity, :decision_window_minutes, :expires_at, "
                " :status, :verification_status, :filled_count, :published_at, "
                " :rejection_reason, 'company', 'coffee') "
                "RETURNING id, uuid"
            ),
            values,
        )
        card_id, card_uuid = row.one()
        await preparation.commit()
    return int(card_id), card_uuid


class _LockWindow:
    """Держит `FOR UPDATE users` открытым, пока вторая транзакция ждёт (AC-03).

    Без окна таски выполняются почти последовательно: первый успевает
    закоммитить до `COUNT` второго, и тест зелёный ДАЖЕ без блокировки —
    проверено откатом (мутация 1 не краснела). Задержка ПОСЛЕ `execute`
    гарантирует, что вторая транзакция уже висит на `FOR UPDATE`, когда
    первая считает `COUNT`. С блокировкой это даёт 1 `UPDATE`, без неё — 2.
    """

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self._original_lock: Any = None
        self._original_count: Any = None

    def __enter__(self) -> _LockWindow:
        self._original_lock = EventCardRepository.lock_author
        self._original_count = EventCardRepository.count_active
        window = self

        async def _slow_lock(self_repo: EventCardRepository, author_id: int) -> None:
            await window._original_lock(self_repo, author_id)
            await asyncio.sleep(window.delay)

        async def _slow_count(self_repo: EventCardRepository, author_id: int) -> int:
            # Задержка НА ЗАДЕРЖКЕ COUNT, а не только на блокировке: без
            # `FOR UPDATE` (мутация 1) две транзакции иначе успевают
            # разойтись — вторая читает `count` уже после коммита первой,
            # и тест зелёный при сломанном коде. Здесь обе гарантированно
            # получают `count = limit - 1`.
            count = await window._original_count(self_repo, author_id)
            await asyncio.sleep(window.delay)
            return count

        EventCardRepository.lock_author = _slow_lock  # type: ignore[method-assign]
        EventCardRepository.count_active = _slow_count  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc_info: object) -> None:
        EventCardRepository.lock_author = self._original_lock  # type: ignore[method-assign]
        EventCardRepository.count_active = self._original_count  # type: ignore[method-assign]


class _CasCounter:
    """Единый счётчик успешных CAS на весь тест (AC-03).

    Патч ставится ОДИН раз до `asyncio.gather`, а не внутри каждого таска:
    при per-task патче второй таск перехватывал уже обёрнутый метод первого
    (`original` = первая обёртка), и один `UPDATE` считался дважды — тест
    становился красным независимо от кода (дефект первой редакции, найден
    прогоном).
    """

    def __init__(self) -> None:
        self.successes = 0
        self._original: Any = None

    def __enter__(self) -> _CasCounter:
        self._original = EventCardRepository.publish_if_draft
        counter = self

        async def _counting(self_repo: EventCardRepository, **kwargs: Any) -> int:
            rowcount = await counter._original(self_repo, **kwargs)
            if rowcount:
                counter.successes += rowcount
            return rowcount

        EventCardRepository.publish_if_draft = _counting  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc_info: object) -> None:
        EventCardRepository.publish_if_draft = self._original  # type: ignore[method-assign]


async def _publish_in_own_session(
    factory: Any,
    *,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    settings: Any,
) -> tuple[str, str]:
    """Публикует карточку в СВОЕЙ сессии; возвращает `(outcome, sql_log)`.

    `outcome` — `"ok"` либо имя класса доменной ошибки. Коды нужны лишь как
    диагностика; доказательством служит счётчик CAS из `_count_cas` (см.
    docstring модуля).
    """
    async with factory() as raw_session:
        tracked = _OrderTrackingSession(raw_session)
        try:
            await publish_card(
                tracked,  # type: ignore[arg-type]
                author_id=author_id,
                card_uuid=card_uuid,
                settings=settings,
            )
            outcome = "ok"
        except Exception as error:  # noqa: BLE001 — имя класса и есть результат
            outcome = type(error).__name__
            await raw_session.rollback()
        return outcome, "\n".join(tracked.log)


async def test_two_parallel_publishes_only_one_update(engine: Any, prepared_author: int) -> None:
    """AC-03, AC-01 (мутации 1 и 7): одна свободная позиция — ровно один `UPDATE`.

    Лимит 2, у автора одна `published` карточка и два черновика: свободна
    ровно одна позиция. Параллельная публикация обоих черновиков обязана дать
    `rowcount == 1` суммарно. Без `FOR UPDATE users` (мутация 1) или с `COUNT`
    до блокировки (мутация 7) обе транзакции видят `active_count = 1` и
    публикуют — счётчик равен 2, тест красный.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    await _insert_card(factory, author_id=prepared_author, place_id=place_id, status="published")
    _, first_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)
    _, second_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)
    settings = _RaceSettings(limit=2)

    with _CasCounter() as counter, _LockWindow():
        results = await asyncio.gather(
            _publish_in_own_session(
                factory, author_id=prepared_author, card_uuid=first_uuid, settings=settings
            ),
            _publish_in_own_session(
                factory, author_id=prepared_author, card_uuid=second_uuid, settings=settings
            ),
        )

    outcomes = [result[0] for result in results]
    assert counter.successes == 1, (
        f"без блокировки `users` обе публикации проходят: UPDATE {counter.successes}, "
        f"исходы {outcomes}"
    )
    assert outcomes.count("ok") == 1
    assert outcomes.count("CardLimitReachedError") == 1

    # Вторая карточка не тронута: лимит проверен ДО записи (AC-01).
    async with factory() as check:
        rows = await check.execute(
            text(
                "SELECT count(*) FROM event_cards WHERE author_id = :uid AND status = 'published'"
            ),
            {"uid": prepared_author},
        )
        assert rows.scalar_one() == 2


async def test_same_card_published_once(engine: Any, prepared_author: int) -> None:
    """AC-06 (мутации 2 и 6): два `publish` одной карточки — один `UPDATE`.

    `published_at` записывается ОДИН раз, второй публикатор получает
    `CARD_NOT_DRAFT`. Без `status = 'draft'` в CAS (мутация 2) второй `UPDATE`
    перезаписал бы `published_at`; без проверки `rowcount` (мутация 6)
    проигравший ответил бы успехом.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    card_id, card_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)
    settings = _RaceSettings(limit=2)

    with _CasCounter() as counter, _LockWindow():
        first, second = await asyncio.gather(
            _publish_in_own_session(
                factory, author_id=prepared_author, card_uuid=card_uuid, settings=settings
            ),
            _publish_in_own_session(
                factory, author_id=prepared_author, card_uuid=card_uuid, settings=settings
            ),
        )

    assert counter.successes == 1, f"двойная публикация: UPDATE {counter.successes}"
    assert [first[0], second[0]].count("ok") == 1
    assert [first[0], second[0]].count("CardNotDraftError") == 1

    # §18.2: сверка с СОХРАНЁННЫМ значением, а не «два чтения подряд».
    async with factory() as check:
        row = await check.execute(
            text("SELECT published_at FROM event_cards WHERE id = :id"), {"id": card_id}
        )
        stored_first = row.scalar_one()
    assert stored_first is not None

    # Ещё одна попытка публикации не имеет права переписать момент.
    third = await _publish_in_own_session(
        factory, author_id=prepared_author, card_uuid=card_uuid, settings=settings
    )
    assert third[0] == "CardNotDraftError"
    async with factory() as check:
        row = await check.execute(
            text("SELECT published_at FROM event_cards WHERE id = :id"), {"id": card_id}
        )
        assert row.scalar_one() == stored_first, "`published_at` перезаписан повторной публикацией"


async def test_users_row_locked_before_event_cards(engine: Any, prepared_author: int) -> None:
    """AC-04 (ADR-09, мутация 7): блокировка `users` уходит РАНЬШЕ записи карточки.

    Порядок блокировок PostgreSQL равен порядку запросов. Журнал `execute`
    показывает: `SELECT users ... FOR UPDATE` идёт до `UPDATE event_cards`,
    а `FOR UPDATE` на `event_cards` не встречается вовсе (CAS вместо него).
    Без блокировки (мутация 1) журнал не содержал бы `FOR UPDATE` вообще.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    _, card_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)
    settings = _RaceSettings(limit=2)

    outcome, sql_log = await _publish_in_own_session(
        factory, author_id=prepared_author, card_uuid=card_uuid, settings=settings
    )
    assert outcome == "ok"

    lock_position = sql_log.find("FOR UPDATE")
    update_position = sql_log.find("UPDATE event_cards")
    assert lock_position != -1, "блокировка `users` не выполнялась"
    assert update_position != -1, "CAS не выполнялся"
    assert lock_position < update_position, (
        "блокировка `users` ушла ПОСЛЕ записи карточки — нарушение ADR-09"
    )
    assert "FOR UPDATE" not in sql_log[update_position:], (
        "`FOR UPDATE` на `event_cards` запрещён: порядок `users` → `cards` (ADR-09)"
    )


async def test_draft_does_not_occupy_slot(engine: Any, prepared_author: int) -> None:
    """AC-01, AC-05 (регресс): черновики не занимают слот автора.

    Лимит 1, у автора пять черновиков и ни одной активной. Публикация должна
    пройти. Краснеет, если `draft` попадёт в `ACTIVE_STATUSES` — тогда автор
    не смог бы опубликовать ничего после создания первой заготовки.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    for _ in range(5):
        await _insert_card(factory, author_id=prepared_author, place_id=place_id)
    _, card_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)

    outcome, _ = await _publish_in_own_session(
        factory,
        author_id=prepared_author,
        card_uuid=card_uuid,
        settings=_RaceSettings(limit=1),
    )

    assert outcome == "ok"


async def test_finished_and_cancelled_release_slot(engine: Any, prepared_author: int) -> None:
    """AC-05 (мутация 5, FR73/FR100): `finished`/`cancelled` слот освобождают.

    Лимит 1, у автора по карточке в каждом терминальном состоянии — публикация
    проходит. Краснеет, если добавить `cancelled` (или `finished`) в
    `ACTIVE_STATUSES`: отменённая карточка навсегда съедала бы слот.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    for terminal in ("finished", "cancelled"):
        await _insert_card(factory, author_id=prepared_author, place_id=place_id, status=terminal)
    _, card_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)

    outcome, _ = await _publish_in_own_session(
        factory,
        author_id=prepared_author,
        card_uuid=card_uuid,
        settings=_RaceSettings(limit=1),
    )

    assert outcome == "ok"


async def test_closed_and_full_keep_slot(engine: Any, prepared_author: int) -> None:
    """AC-05 (мутация 4, FR74): `closed` и `full` слот НЕ освобождают.

    Лимит 2, у автора `closed` и `full` — набор ещё идёт, обе занимают слот.
    Краснеет, если убрать `closed` из `ACTIVE_STATUSES`: третья карточка
    публиковалась бы при двух идущих встречах.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    for active in ("closed", "full"):
        await _insert_card(factory, author_id=prepared_author, place_id=place_id, status=active)
    _, card_uuid = await _insert_card(factory, author_id=prepared_author, place_id=place_id)

    outcome, _ = await _publish_in_own_session(
        factory,
        author_id=prepared_author,
        card_uuid=card_uuid,
        settings=_RaceSettings(limit=2),
    )

    assert outcome == "CardLimitReachedError"


async def test_unverified_card_never_published_by_cas(engine: Any, prepared_author: int) -> None:
    """AC-06 (мутация 3): CAS не публикует карточку без `approved`.

    Карточка `rejected` с причиной: публикация обязана упасть на проверке
    Python, а CAS — не выполняться вовсе (иначе модератор, отклонивший
    карточку между проверкой и `UPDATE`, получил бы её опубликованной).
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    _, card_uuid = await _insert_card(
        factory,
        author_id=prepared_author,
        place_id=place_id,
        verification_status=CardVerificationStatus.REJECTED.value,
        rejection_reason="Спам",
    )

    outcome, _ = await _publish_in_own_session(
        factory,
        author_id=prepared_author,
        card_uuid=card_uuid,
        settings=_RaceSettings(limit=2),
    )

    assert outcome == "CardRejectedError"

    # Карточка осталась черновиком — CAS не тронул строку.
    async with factory() as check:
        row = await check.execute(
            text("SELECT status FROM event_cards WHERE uuid = :uuid"), {"uuid": card_uuid}
        )
        assert row.scalar_one() == EventCardStatus.DRAFT.value


async def test_cas_predicate_rejects_non_draft_and_unapproved(
    engine: Any, prepared_author: int
) -> None:
    """AC-06 (мутации 2 и 3): CAS не пишет поверх не-черновика и без `approved`.

    Прямая проверка ПРЕДИКАТА через репозиторий: сервис отсекает эти состояния
    в Python раньше, поэтому мутация предиката в сценарии двух `publish`
    ненаблюдаема (второй таск падает на проверке статуса). Здесь CAS зовётся
    напрямую — ровно как его вызовет транзакция, у которой состояние изменилось
    между проверкой и записью.

    Краснеет, если снять `status = 'draft'` (мутация 2) или
    `verification_status = 'approved'` (мутация 3): `rowcount` стал бы `1`,
    и карточка была бы перезаписана.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    place_id = await _insert_place(factory)
    published_id, published_uuid = await _insert_card(
        factory, author_id=prepared_author, place_id=place_id, status="published"
    )
    rejected_id, rejected_uuid = await _insert_card(
        factory,
        author_id=prepared_author,
        place_id=place_id,
        verification_status=CardVerificationStatus.REJECTED.value,
        rejection_reason="Спам",
    )
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=1)

    async with factory() as session:
        repo = EventCardRepository(session)
        await repo.lock_author(prepared_author)
        already_published = await repo.publish_if_draft(
            card_id=published_id,
            author_id=prepared_author,
            now=now,
            expires_at=expires_at,
        )
        not_approved = await repo.publish_if_draft(
            card_id=rejected_id,
            author_id=prepared_author,
            now=now,
            expires_at=expires_at,
        )
        await session.commit()

    assert already_published == 0, "CAS перезаписал уже опубликованную карточку"
    assert not_approved == 0, "CAS опубликовал карточку без `approved`"

    # Состояние не изменилось: `status` и `published_at` прежние.
    async with factory() as check:
        row = await check.execute(
            text("SELECT status FROM event_cards WHERE id = ANY(:ids)"),
            {"ids": [published_id, rejected_id]},
        )
        assert sorted(item[0] for item in row.all()) == ["draft", "published"]
    # `published_uuid`/`rejected_uuid` использованы как адреса вставки.
    assert published_uuid != rejected_uuid
