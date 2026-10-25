"""Фиксация подтверждения 18+ и согласия на обработку ПД (Task 2.2–2.6).

Порядок зафиксирован стори: сначала валидация целиком, затем запись. Иначе
отказ на втором чекбоксе оставлял бы уже записанный первый — учётка
с `age_confirmed=true` без согласия на ПД (нарушение 152-ФЗ, AC-03).

D-3: время 18+ и время согласия — один и тот же факт (`consent_given_at`).
D-4: время пишется один раз: повтор — `200`, но метка не перезаписывается.
D-5: пол фиксируется один раз; смена меняла бы права и обходила модерацию.

D-4 (стори 1.2): `commit()` здесь не вызывается — единица работы у роута,
сервис делает только `flush`.

Патчи 2 и 3 код-ревью 1.6 — гонки. Прежняя версия читала `user.gender`
из объекта, загруженного зависимостью `get_current_user`, без блокировки:

* два параллельных `POST /consent` (`female` и `male`) оба видели `NULL`
  и оба писали — `409 gender_locked` не срабатывал ни у одного;
* `is_new` считался по прочитанной строке, поэтому двойной клик
  перезаписывал `consent_given_at` (второй запрос видел уже заполненное
  поле только если успевал прочитать после коммита первого).

Решение — `SELECT ... FOR UPDATE` по `users.id` в начале транзакции:
второй запрос ждёт коммита первого и читает актуальные `gender`
и `consent_given_at`. Это закрывает обе гонки одним механизмом, и `is_new`
считается по заблокированной строке, а не по снимку, сделанному до неё.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Gender
from app.models.user import User
from app.schemas.consent import ConsentRequest

# Допустимые значения пола берутся из StrEnum модели, а не переписываются строкой:
# второй список разошёлся бы с CHECK-ограничением `gender` (миграция 0001).
ALLOWED_GENDERS = frozenset(str(value) for value in Gender)


class ConsentRequiredError(Exception):
    """AC-03: не подтверждены 18+ или согласие на обработку ПД."""


class GenderRequiredError(Exception):
    """AC-03: пол не выбран или выбрано недопустимое значение."""


class GenderLockedError(Exception):
    """AC-04/D-5: пол уже зафиксирован и отличается от запрошенного."""


class ConsentRaceError(Exception):
    """Патч 3: строку не удалось заблокировать — учётка исчезла между запросами.

    Практически недостижимо (пользователь пришёл из `get_current_user`,
    который только что прочитал эту же строку), но `SELECT ... FOR UPDATE`
    по удалённой записи возвращает `None`, и молча продолжать с устаревшим
    объектом нельзя: запись ушла бы в никуда.
    """


def lock_statement(user_id: int) -> Select[User]:
    """Строит запрос блокировки строки учётки (патчи 2 и 3).

    Вынесено отдельной функцией, потому что именно **этот запрос** — предмет
    проверки: `FOR UPDATE` без `populate_existing` бесполезен (ORM вернёт
    объект из identity map), а `populate_existing` без `FOR UPDATE` не защищает
    от гонки. Тест на текст запроса падает при потере любого из двух элементов,
    тогда как интеграционный тест через одну сессию этого не замечает:
    в одной транзакции второй запрос видит свои же изменения.
    """
    return (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _lock_user(session: AsyncSession, user_id: int) -> User | None:
    """Патч 2: читает строку учётки с блокировкой до конца транзакции.

    `FOR UPDATE` — не оптимизация, а условие корректности: без него проверка
    «пол уже зафиксирован» и запись разделены, и два параллельных запроса
    проходят проверку оба. Порядок блокировок в приложении один (сначала
    `users`), поэтому взаимных блокировок этот запрос не создаёт.

    `populate_existing=True` обязателен: `get_current_user` уже загрузил эту
    строку в ту же сессию, и без него ORM вернул бы объект из identity map
    со старыми атрибутами. Блокировка была бы взята, а решение «пол уже
    зафиксирован» принималось бы по снимку до неё — то есть гонка осталась бы.
    """
    result = await session.execute(lock_statement(user_id))
    return result.scalar_one_or_none()


async def record_consent(
    session: AsyncSession,
    user: User,
    payload: ConsentRequest,
) -> tuple[User, bool]:
    """Записывает подтверждение и пол; возвращает `(user, is_new)` (Task 2.2–2.6).

    `is_new` = согласие записано впервые (клиенту полезно отличить первый
    онбординг от повторной отправки формы).

    Satisfies: AC-01, AC-02, AC-03, AC-04.
    """
    # AC-03: оба чекбокса обязательны; отказ до любой записи.
    if not payload.age_confirmed or not payload.consent:
        raise ConsentRequiredError

    # AC-03: пол обязателен и входит в закрытый набор. `strip().lower()` —
    # нормализация ввода, а не расширение набора: `Female` даёт `female`,
    # `other` остаётся недопустимым.
    gender = (payload.gender or "").strip().lower()
    if gender not in ALLOWED_GENDERS:
        raise GenderRequiredError

    # Патч 2 (гонка на `gender`): перечитываем строку под блокировкой.
    # Объект из `get_current_user` — снимок на момент чтения; решение
    # «пол уже зафиксирован» должно приниматься по текущему значению в БД.
    locked = await _lock_user(session, user.id)
    if locked is None:
        raise ConsentRaceError

    # AC-04/D-5: пол уже зафиксирован и другой — конфликт состояния, не обновление.
    # Проверка идёт по заблокированной строке, поэтому второй параллельный
    # запрос увидит значение, записанное первым (или его же коммит).
    if locked.gender is not None and locked.gender != gender:
        raise GenderLockedError

    # AC-04/патч 3: `is_new` считается по заблокированной строке. Второй запрос
    # ждёт коммита первого, видит заполненный `consent_given_at` и не
    # перезаписывает время — двойной клик больше не двигает метку согласия.
    is_new = locked.consent_given_at is None

    # AC-04: `age_confirmed` выставляется всегда (идемпотентно), время — один раз.
    locked.age_confirmed = True
    if is_new:
        locked.consent_given_at = datetime.now(UTC)
    # AC-01, AC-02: пол фиксируется; `status` не трогается — он про санкции (AR30),
    # а мужчина получает `active` и не попадает в выборку из-за отсутствия анкеты.
    locked.gender = gender

    # Task 2.6: только `flush`; коммит — на роуте (единица работы).
    await session.flush()
    return locked, is_new
