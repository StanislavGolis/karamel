"""Воркер окон карточки: закрытие просроченных окон и архив начавшихся встреч.

Стори 4.6 (AC-05, AC-10; FR73, NFR20, NFR21). Два перехода состояний, у которых
нет HTTP-триггера:

1. `published → closed` по истечении `expires_at` (ADR-03). Ленивое закрытие
   уже работает в `GET /cards/{uuid}` (4.5), но карточку, которую никто не
   открывает, должен закрыть воркер: иначе окно живёт вечно, а участники видят
   «окно открыто» у встречи, где решение уже не принимается (OQ-19).
2. `published/full/closed → finished` по наступлении `starts_at` (FR73).
   Карточка обязана покинуть ленту и стать историей в момент начала встречи
   (NFR21: не позднее 5 минут).

**Идемпотентность (ADR-03).** Оба перехода — CAS на строке: второй прогон
(или второй воркер) видит предикат несработавшим и обновляет 0 строк.
Отдельных таблиц «что уже обработано» нет и не будет: статус карточки и есть
флаг.

**Почему не в API.** Прогон каждые 60 секунд в HTTP-процессе держал бы
блокировки и конкурировал с запросами пользователей; AR33 выносит воркеры
в отдельный процесс, AR34 — отдельный контейнер (`worker-cards` в compose).

**Наблюдаемость (ADR-14).** Structlog-события с `card_uuid` и `duration_ms`;
контроль зависших окон — прямой запрос `status='published' AND expires_at <
now() - interval '5 minutes'`, ненулевой результат = инцидент.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import get_session_factory
from app.core.redis import get_redis
from app.repositories.event_cards import EventCardRepository
from app.services.alert_service import send_alert
from app.services.card_decision_service import DecisionOutcome, close_due_card
from app.services.event_card_service import finish_started_cards
from app.workers.attendance_prompts import run_attendance_prompts
from app.workers.close_due_cards import run_decisions
from app.workers.decision_reminders import run_reminders

# NFR21: «не позднее 5 минут» — при цикле 60 с запас четырёхкратный.
WORKER_INTERVAL_SECONDS = 60
# Размер батча на один прогон (см. `CARDS_WORKER_BATCH` в сервисе): воркер
# обязан возвращать управление, даже если накопились тысячи карточек.
WORKER_BATCH_SIZE = 200
# Стори 10.7, Task 6.2 (AC-03, D-10.7-6): ключ дедупа алерта о зависшем окне.
# Отдельный от health-алерта: два разных инцидента не должны глушить друг
# друга в одном часовом окне. Без дедупа зависшее окно слало бы 1440 сообщений
# в сутки, и бот был бы замьючен — канал исчез бы, когда он нужен.
WINDOW_OVERDUE_ALERT_KEY = "alert:window_overdue:sent"

logger = structlog.get_logger(__name__)


async def close_due_windows(
    session: AsyncSession, *, now: datetime, limit: int = WORKER_BATCH_SIZE
) -> int:
    """AC-05 стори 4.6 + AC-06 стори 5б.3: закрывает истёкшие окна в готовой сессии.

    **Второй точки входа НЕТ** (D-5b.3-3): функция — тонкая обёртка над ядром
    `card_decision_service.close_due_card`, тем же, что зовёт воркер
    `close_due_cards.run_decisions` и ленивое чтение `GET /cards/{uuid}`.
    Раньше здесь стоял `close_if_due` — чистый CAS `published -> closed` без
    анализа заявок: на карточке `capacity=1` с живыми `pending` набор
    закрывался, и заявки навсегда оставались `pending` (FR80, AC-04).

    Сигнатура сохранена (`session`, `-> int`) намеренно (D-5b.3-6): её
    импортирует `tests/integration/test_feed_expired_full.py`, и смена формы
    на «фабрику сессий» дала бы `ImportError` в постороннем тесте. Возвращает
    число ФАКТИЧЕСКИ обработанных окон (выбор + безличное закрытие), не «сколько
    найдено».

    `commit` не делается: владелец транзакции — вызывающий (в `run_once` это
    отдельная сессия, в тесте — фикстура с SAVEPOINT).
    """
    repo = EventCardRepository(session)
    candidates = await repo.due_decision_ids(now=now, limit=limit)
    handled = 0
    for card_id, author_id, _card_uuid in candidates:
        result = await close_due_card(session, card_id=card_id, author_id=author_id, now=now)
        if result.outcome in (DecisionOutcome.SELECTED, DecisionOutcome.CLOSED_NO_CHOICE):
            handled += 1
    return handled


async def check_overdue_windows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    duration_ms: int,
) -> int:
    """AC-03 стори 10.7 (NFR64, ADR-14): контроль зависших окон + алерт.

    **Зачем отдельная функция.** `run_once` — триста строк переходов; контроль
    внутри него было бы нечем проверить иначе, как прогоном всех переходов
    (медленно и с побочными эффектами). Здесь проверяется ровно контроль:
    счётчик, лог, дедуп.

    **Лог — КАЖДЫЙ прогон, алерт — раз в час** (D-10.7-6). Лог отвечает на
    вопрос «инцидент продолжается?», алерт — «пора будить человека?». Без
    дедупа зависшее окно слало бы 1440 сообщений в сутки, и бот был бы
    замьючен в первый день — канал исчез бы ровно тогда, когда нужен.

    Возвращает число зависших окон: `run_once` не возвращает его наружу
    (словарь `run_once` — контракт существующих тестов), но тесту и будущему
    метрическому счётчику оно нужно.
    """
    async with session_factory() as session:
        overdue = await EventCardRepository(session).count_overdue_windows(now=now)
    if overdue <= 0:
        return 0

    logger.error("worker.window.overdue", count=overdue, duration_ms=duration_ms)
    # Ошибка отправки НЕ пробрасывается: контроль — диагностика, и его отказ
    # не должен отменять уже выполненные переходы прогона. `send_alert` сам
    # возвращает `False` на любом отказе, а `RedisError` из дедупа гасится
    # внутри него (D-10.7-3).
    await send_alert(
        f"env={settings.env} check=window_overdue count={overdue} duration_ms={duration_ms}",
        key=WINDOW_OVERDUE_ALERT_KEY,
        redis=get_redis(),
    )
    return overdue


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = WORKER_BATCH_SIZE,
    reminder_threshold_minutes: int | None = None,
) -> dict[str, int]:
    """Один прогон воркера: решения по окнам, затем архив. Возвращает счётчики.

    **Окно закрывается вместе с автовыбором, второй точки входа нет**
    (D-5b.3-3): первый шаг — `close_due_cards.run_decisions`, то же ядро, что
    зовёт ленивое чтение `GET /cards/{uuid}`. Карточка с истёкшим окном и
    живыми `pending` становится `full` с выбранным, без заявок — `closed`.
    Прежнее поведение (`published -> closed` без анализа заявок) нарушало
    FR80/AC-04.

    Порядок намеренный: карточка с истёкшим окном сначала получает решение,
    и только потом может уйти в `finished`, если встреча уже началась. Если
    `starts_at` ещё не наступил, второе условие не совпадёт, и карточка
    останется `full`/`closed` — как и требует FR74/FR80.

    Транзакции независимы: падение архива не откатывает решения, а падение
    одной карточки в пачке решений не отменяет остальные (AC-09).
    """
    moment = now if now is not None else datetime.now(UTC)
    started = time.monotonic()

    # Стори 5б.4 (AC-07): напоминание ОБЯЗАНО пройти раньше решений. Иначе
    # автовыбор 5б.3 закрывает окно первым, карточка уходит из `published`,
    # и напоминание «успей выбрать сам» не уходит никогда — FR103 стал бы
    # недостижим ровно в том сценарии, ради которого существует.
    threshold = (
        reminder_threshold_minutes
        if reminder_threshold_minutes is not None
        else settings.card_reminder_threshold_minutes
    )
    reminders = await run_reminders(
        session_factory, now=moment, threshold_minutes=threshold, limit=limit
    )

    decisions = await run_decisions(session_factory, now=moment, limit=limit)
    closed = decisions["selected"] + decisions["closed_no_choice"]

    async with session_factory() as session:
        finished = await finish_started_cards(session, now=moment, limit=limit)

    # Стори 7.3 (AC-03, NFR22): запрос подтверждения явки — ЧЕТВЁРТЫЙ шаг,
    # ПОСЛЕ архивации, и порядок не косметический. Предикат воркера —
    # `status IN ('full','closed','finished')`, а `published`-карточка с
    # наступившим `starts_at` становится `finished` именно шагом выше.
    # Поставь запрос раньше — карточка не попала бы в выборку этого прогона
    # и ждала бы следующего: `NFR22` в запас (цикл 60 с), но поведение стало
    # бы недетерминированным по фазе цикла.
    prompts = await run_attendance_prompts(session_factory, now=moment, limit=limit)

    duration_ms = int((time.monotonic() - started) * 1000)

    # Стори 10.7, Task 6.2 (AC-03, NFR64, ADR-14): контроль ЗАВИСШИХ окон.
    # Считается ПОСЛЕ всех переходов: карточка, закрытая этим же прогоном,
    # не должна попасть в контроль (иначе каждый прогон давал бы алерт
    # на собственную работу воркера). Предикат ADR-14 — `expires_at <
    # now() - 5 минут`: здоровый воркер закрывает окно за секунды, поэтому
    # ненулевой результат означает, что воркер не отработал.
    await check_overdue_windows(session_factory, now=moment, duration_ms=duration_ms)

    if closed:
        logger.info("worker.card.closed", count=closed, duration_ms=duration_ms)
    if finished:
        logger.info("worker.card.finished", count=finished, duration_ms=duration_ms)
    if prompts["prompted"] or prompts["failed"]:
        logger.info(
            "worker.attendance.run",
            prompted=prompts["prompted"],
            failed=prompts["failed"],
            duration_ms=duration_ms,
        )
    return {
        "closed": closed,
        "finished": finished,
        "reminded": reminders["reminded"],
        "reminder_failed": reminders["failed"],
        # Стори 7.3 (Task 4.5): новые ключи. Проверено: ни один существующий
        # тест не сравнивает словарь `run_once` целиком (единственное полное
        # сравнение — `run_reminders` в `test_decision_reminders.py:215`),
        # поэтому расширение безопасно (S49).
        "prompted": prompts["prompted"],
        "prompt_skipped": prompts["skipped"],
        "prompt_failed": prompts["failed"],
    }


async def main() -> None:
    """Цикл воркера: прогон, затем пауза. Ошибка прогона не убивает процесс.

    Падение одного прогона (БД недоступна, гонка с миграцией) не должно
    останавливать контейнер: `restart: unless-stopped` перезапустил бы его, но
    потерял бы уже выполненные переходы следующего прогона. Логируем и живём
    дальше — состояние карточек не теряется, они просто обработаются позже.
    """
    while True:
        try:
            await run_once(get_session_factory())
        except Exception as error:  # noqa: BLE001 — воркер обязан жить
            logger.error(
                "worker.run.failed",
                error=type(error).__name__,
                detail=str(error)[:500],
            )
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
