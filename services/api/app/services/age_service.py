"""Домен возраста (стори 12.2; ADR-17, ADR-18; AC-2, AC-4, AC-5).

Возраст больше не хранится — он **вычисляется** из `profiles.birth_date`
на сервере. Причина в ADR-17: число лет устаревает в день рождения, и 18+
по нему не проверяется, а декларируется.

**Три источника, порядок фиксирован:**

1. `birth_date` — источник истины (AC-2);
2. `age` — legacy-fallback для 40 008 учёток, где даты рождения нет
   и восстановить её из числа лет математически невозможно (AC-5);
3. `None` — возраст неизвестен.

Складывать, усреднять или «уточнять» источники нельзя: два числа на одну
сущность разойдутся на первом дне рождения (ADR-17, «Отвергнуто» а).

**`today` — параметр, не `date.today()` внутри.** Граница 18 лет — единственное
место в продукте, где ошибка стоит реального доступа, и она обязана
проверяться детерминированно (`tests/unit/test_age_service.py`, AC-6).

Модуль чистый: ни SQLAlchemy, ни FastAPI. Его импортируют сервисы, схемы
и гард `deps.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

#: Стори 12.3, Task 10 (долг `W-12.2-b`): «сегодня» ОДНО на запрос.
#:
#: Гард 18+ (`deps.py`) и сериализация ответа (`event_card_service` и др.)
#: живут в одном HTTP-запросе и вызывают `compute_age` каждый со своим
#: `date.today()`. На границе суток и в день рождения числа расходятся:
#: гард пропустит пользователя, а в ответе окажется на год меньше. Здесь
#: значение фиксируется один раз на запрос (middleware `TodayMiddleware`)
#: и переиспользуется всеми вызывающими.
#:
#: `ContextVar` вместо аргумента в каждом вызове: сигнатура `compute_age`
#: НЕ меняется (AC-6 стори 12.2 — `today` остаётся обязательным параметром,
#: граница 18 лет проверяется детерминированно), а место вызова лечится
#: без протаскивания `today` через пять слоёв.
_today_override: ContextVar[date | None] = ContextVar("karamel_today_override", default=None)


def today_or_now() -> date:
    """Единственный источник «сегодня» для гарда и сериализации (Task 10).

    Возвращает инъецированное значение, если оно установлено (тесты, middleware),
    иначе `date.today()`. Все восемь мест Замера 6 ходят сюда, а не в `date.today()`
    напрямую: иначе в одном запросе окажется два разных «сегодня».
    """
    override = _today_override.get()
    return date.today() if override is None else override


@contextmanager
def override_today(value: date) -> Iterator[date]:
    """Фиксирует «сегодня» на время блока — для тестов и middleware.

    `ContextVar` копируется в дочерние задачи (asyncio), поэтому значение
    видно во всех корутинах одного запроса и не течёт между запросами.
    """
    token = _today_override.set(value)
    try:
        yield value
    finally:
        _today_override.reset(token)


#: Нижняя граница возраста. Совпадает с CHECK `profiles.age` в БД и с
#: `AGE_MIN` схем профиля: расхождение дало бы `IntegrityError` → `500`
#: вместо `422 underage` (сервис пропустил бы значение, которое БД не примет).
AGE_MIN = 18

#: Верхняя граница. Не продуктовая: защита от опечатки в годе (1900 вместо 1990).
AGE_MAX = 99

#: Код отказа при несовершеннолетнем. Отдельный от `invalid_birth_date`: клиент
#: показывает у поля «дата рождения» разный текст — «нужно 18 лет» и «проверьте дату».
UNDERAGE_CODE = "underage"

#: Код отказа при некорректной дате (в будущем, старше `AGE_MAX`).
INVALID_BIRTH_DATE_CODE = "invalid_birth_date"


def compute_age(birth_date: date | None, legacy_age: int | None, today: date) -> int | None:
    """Полных лет на `today` или `None`, если возраст неизвестен.

    Satisfies: AC-2 (вычисление вместо хранения), AC-5 (legacy-fallback).

    `birth_date` приоритетнее `legacy_age`: у учётки, заполнившей дату поверх
    старого числа, число устарело, и отдавать его значило бы врать.

    **День рождения сегодня — уже полный год.** Сравнение `(month, day)` строго
    меньше даты рождения вычитает единицу только до наступления дня рождения
    включительно; в сам день рождения вычитается 0. Это не мелочь: `<=`
    отняло бы год у человека в его собственный день рождения (AC-6).

    29 февраля: в невисокосный год день рождения «наступает» 1 марта — так
    работает сравнение кортежем, и это ожидаемое поведение `date`.
    """
    if birth_date is None:
        return legacy_age
    # AC-6: граница в одну сторону — `(month, day) < (month, day)`.
    before_birthday = (today.month, today.day) < (birth_date.month, birth_date.day)
    return today.year - birth_date.year - int(before_birthday)


def is_adult(age: int | None) -> bool:
    """Совершеннолетний ли (AC-4).

    `None` — **не** совершеннолетний: неизвестный возраст не пропуск.
    Обратное решение превратило бы отсутствие данных в разрешение
    на доступ к UGC.
    """
    return age is not None and age >= AGE_MIN


def validate_birth_date(value: date, today: date) -> date:
    """Проверяет дату рождения до записи в БД (AC-2, AC-4).

    Возвращает значение без изменений, если оно допустимо. Иначе `ValueError`
    с кодом в тексте — сервис профиля превращает его в `422` с этим кодом.

    Порядок проверок важен: будущая дата отсекается **до** вычисления возраста,
    иначе она дала бы отрицательный возраст и ушла бы в ветку `underage`
    с неверным сообщением.
    """
    if value > today:
        raise ValueError(INVALID_BIRTH_DATE_CODE)

    age = compute_age(value, None, today)
    if age is not None and age > AGE_MAX:
        raise ValueError(INVALID_BIRTH_DATE_CODE)
    if not is_adult(age):
        raise ValueError(UNDERAGE_CODE)
    return value
