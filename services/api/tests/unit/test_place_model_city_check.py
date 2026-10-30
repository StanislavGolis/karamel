"""CHECK-выражение `ck_places_city_slug` в МОДЕЛИ, а не только в БД (стори 12.1).

**Зачем отдельный файл.** `test_places_schema.py::test_bad_city_slug_rejected_by_database`
проверяет каталог БД: он краснеет, если CHECK снять **в базе**. Но модель и база —
две копии одного инварианта, и расходятся они молча: `alembic check` сравнивает
имена и наличие ограничений, **но не их тела**. Проверено мутацией CR 12.1 (C14):
`CheckConstraint("city_slug ~ '^[a-z0-9_-]{3,64}$'")` → `CheckConstraint("city_slug ~ '^.+$'")`
проходил `alembic check` с ответом «No new upgrade operations detected» и не ронял
ни один тест — ослабленный инвариант в модели уехал бы в следующую миграцию.

Краснеет при:
- ослаблении шаблона в модели (C14) — проверяется текст выражения;
- удалении CHECK из модели — `names` не содержит `city_slug`;
- переименовании ограничения обратно в `community_slug`.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint

from app.models.place import Place

#: Шаблон из `0032`/`0007`, повторённый в модели намеренно (конфиг проверяет
#: `.env` до подключения к БД, CHECK держит инвариант для сырых вставок).
_CITY_SLUG_PATTERN = "^[a-z0-9_-]{3,64}$"


#: Полное имя ограничения. В модели задано короткое `name="city_slug"`, но
#: naming convention (`ck_%(table_name)s_%(constraint_name)s`) даёт в метаданных
#: `ck_places_city_slug` — замер: `for c in Place.__table__.constraints`.
_CITY_CHECK_NAME = "ck_places_city_slug"


def _city_check_sql() -> str:
    """Текст CHECK-ограничения `city_slug` из модели."""
    checks = {
        str(constraint.name): str(constraint.sqltext)
        for constraint in Place.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert _CITY_CHECK_NAME in checks, (
        f"в модели Place нет CHECK `{_CITY_CHECK_NAME}`; есть: {sorted(checks)}"
    )
    return checks[_CITY_CHECK_NAME]


def test_model_check_repeats_pattern() -> None:
    """Модель держит тот же regex, что `ck_places_city_slug` в БД.

    Сверка ТЕКСТА, а не факта наличия: `alembic check` тело CHECK не видит,
    поэтому «ограничение есть» доказательством инварианта не является.
    """
    sql = _city_check_sql()

    assert _CITY_SLUG_PATTERN in sql, f"CHECK модели не содержит шаблон {_CITY_SLUG_PATTERN}: {sql}"


def test_model_check_is_not_weakened() -> None:
    """Модель не принимает всё подряд: ослабленные формы отвергаются по тексту.

    Отдельный ассерт от `test_model_check_repeats_pattern` намеренно: мутация
    «заменить шаблон на `^.+$`» удовлетворяет проверке «строка непустая», но
    обязана краснеть здесь.
    """
    sql = _city_check_sql()

    for weakened in ("^.+$", "^[a-zA-Z0-9_-]{3,64}$", ".*"):
        assert weakened not in sql, f"CHECK модели ослаблен до {weakened!r}: {sql}"
