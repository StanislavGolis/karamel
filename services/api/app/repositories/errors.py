"""Разбор ошибок целостности БД, одинаковый для всех репозиториев.

Task QD A2 (CR 3.2, долг из `deferred-work.md`): имя нарушенного ограничения
в `IntegrityError` нужно и справочнику мест (`is_duplicate_name_error`), и фото
анкеты (`_is_position_race`). До вынесения в `photo_service` жила копия,
читавшая `error.orig.constraint_name` — под asyncpg этого атрибута там НЕТ,
поэтому ветка «гонка за позицию» была мёртвой: вместо `409 photo_limit_reached`
пользователь получал `500`.

Почему это отдельный модуль, а не метод репозитория: разбор формы ошибки
драйвера — не запрос к БД и не правило домена. Он нужен и сервисам, которые
репозиторий не создают (например, импорт мест), и тестам, которые проверяют
форму ошибки без базы.
"""

from __future__ import annotations

from typing import Any


def constraint_name_of(error: Any) -> str | None:
    """CR 3.1, патч 1: имя нарушенного ограничения из `IntegrityError`.

    Форма ошибки проверена на живом asyncpg 0.30 / PostgreSQL 18
    (`UniqueViolationError`), а не выведена рассуждением:

    - `error.orig` — это `sqlalchemy.dialects.postgresql.asyncpg
      .UniqueViolationError`, у которого **нет** атрибута
      `constraint_name`; его `__dict__` = `{orig, detail, pgcode, sqlstate}`;
    - имя ограничения лежит на два уровня глубже —
      `error.orig.orig.constraint_name` (внутренний объект
      `asyncpg.exceptions.UniqueViolationError`).

    Поэтому одиночный `getattr(error.orig, "constraint_name")` возвращал
    `None` ВСЕГДА, и дубль в гонке уходил наружу сырым `IntegrityError`
    (`500` вместо `409 PLACE_DUPLICATE`). Здесь перебираются обе формы:
    драйверы и версии SQLAlchemy кладут атрибут по-разному, а
    `sqlstate`/`pgcode` не различают ограничения между собой.

    `None` означает «имя не прочитано» и трактуется вызывающим как «не тот
    случай»: лучше честный `500` с диагностикой, чем ложный доменный `409`
    на нарушении CHECK, к гонке не относящемся.
    """
    candidates: list[Any] = [getattr(error, "orig", None)]
    original = candidates[0]
    if original is not None:
        candidates.append(getattr(original, "orig", None))
        candidates.append(getattr(original, "__cause__", None))
    for candidate in candidates:
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None
