"""add bar and boardgames to place category whitelist (story 13.2)

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-01

Стори 13.2 (AC-1, AC-2; FR131, ADR-12 amended).

**Почему `0040`, а не иначе.** Замер `alembic heads` перед первой строкой
кода (2026-11-01): голова — `0039` (`0039_event_cards_audience_skipped_at.py`).
`down_revision = "0039"`. Эпик (`epic-13.md:60`) назначал `0040` — первый
случай совпадения за пять стори подряд (12.1 → `0035`, 12.2 → `0036`,
12.4 → `0037`, 12.6 → `0038` — каждая писала не тот номер).

**Зачем категории.** Шесть плиток создания (FR131, решение владельца №3)
открывают карту, отфильтрованную по категории места. Маппинга
«плитка → категории» в вебе нет, и завести его нечем: `PlaceCategory`
содержал 8 значений, среди которых `bar` и `boardgames` отсутствовали.
Без миграции две из шести плиток не работают.

**Риск на данных нулевой.** Новых значений в существующих строках нет и
быть не может: `ck_places_category` их не пропускал. На стенде `places` —
2 строки, `event_cards` — по `category` те же 8 значений. Миграция только
РАСШИРЯЕТ ограничение; ни одна существующая строка не перестаёт ему
соответствовать, таблицы не переписываются.

**Оба CHECK, а не один.** Список категорий общий для `places.category` и
`event_cards.category` (ADR-12). Пересоздание только одного оставило бы
вторую таблицу узкой: карточка с `category='bar'` при валидном справочнике
получала бы `IntegrityError` — то есть дефект, который выглядит как ошибка
клиента.

**Список продублирован строкой осознанно.** Миграция обязана оставаться
исполнимой сама по себе: `alembic` зовёт ревизию в отдельном процессе, а
импорт `app.models.enums` связал бы ревизию с текущим кодом приложения —
при следующем рефакторинге откат старой ревизии сломался бы (то же решение,
что в `0037`). Совпадение с `PlaceCategory` проверяется тестом, а не
надеждой: `tests/integration/test_place_category_bar_api.py`.

**Порядок в `downgrade` обязателен: сначала `drop_constraint`, потом
`create_check_constraint`.** Создание узкого ограничения при живом широком
дало бы два CHECK на одной колонке; PostgreSQL проверяет оба, и удаление
широкого после этого уже не спасает. Порядок обратный `upgrade` — там
сначала снимается узкое.
"""

from __future__ import annotations

import logging
import os

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

#: Имена ограничений целиком: у `ck`-конвенции есть токен `%(constraint_name)s`,
#: но `places`/`event_cards` объявляют `CheckConstraint(..., name="category")`,
#: и полное имя строится как `ck_<table>_category`. Передавать в `op.f`
#: сокращённое имя здесь НЕЛЬЗЯ: оба ограничения называются одинаково
#: (`category`) и различаются только таблицей.
PLACES_CATEGORY_CHECK = "ck_places_category"
EVENT_CARDS_CATEGORY_CHECK = "ck_event_cards_category"

#: AC-1: белый список ПОСЛЕ миграции. Порядок значений — как в `PlaceCategory`,
#: чтобы `pg_get_constraintdef` совпадал с моделью и `alembic check` был чист.
_CATEGORY_CHECK = (
    "category IN ('cafe', 'bar', 'cinema', 'boardgames', 'quiz', 'skating', "
    "'park', 'sport', 'walk', 'other')"
)

#: Откат: список ДО миграции. Без `bar` и `boardgames`.
_CATEGORY_CHECK_BEFORE = (
    "category IN ('cafe', 'cinema', 'quiz', 'skating', 'park', 'sport', 'walk', 'other')"
)

#: AC-2: значения, которые откат делает недопустимыми.
NEW_CATEGORIES: tuple[str, ...] = ("bar", "boardgames")

#: Ценность, в которую откат переводит строки с новыми категориями при
#: осознанном подтверждении. `other` — легаси-значение, существующее и до,
#: и после миграции, поэтому оно проходит ЛЮБОЕ из двух ограничений.
#: Строки не удаляются: `places` связаны с карточками внешним ключом, и
#: удаление места сломало бы карточку (класс CR 12.4 H-1, но хуже).
FALLBACK_CATEGORY = "other"

#: Переменная осознанного выбора: оператор знает, что категория будет
#: заменена на `other`, и подтверждает это явно. Без неё откат
#: останавливается ДО операции.
ALLOW_MEANING_LOSS_ENV = "ALEMBIC_ALLOW_MEANING_LOSS"

#: Класс CR 12.2 H-1: в offline-режиме (`--sql`) соединения нет, посчитать
#: строки невозможно. Отдать скрипт без защиты там, где её не видно, — тот же
#: дефект, что гард `0024` закрывал.
_OFFLINE_REFUSAL = (
    "Откат 0040 в offline-режиме (`--sql`) запрещён: посчитать места и карточки, "
    "у которых откат заменит категорию на 'other', невозможно без соединения. "
    "Выполните откат онлайн."
)


def upgrade() -> None:
    # AC-1: расширение белого списка на обеих таблицах. Порядок внутри пары
    # «drop → create» обязателен: пока жив старый CHECK, добавление нового
    # ничего не меняет, а удаление старого после создания нового оставило бы
    # на секунду оба — в транзакции это безопасно, но имя одно, и второй
    # `create` упал бы на существующем.
    op.drop_constraint(op.f(PLACES_CATEGORY_CHECK), "places", type_="check")
    op.create_check_constraint("category", "places", _CATEGORY_CHECK)

    op.drop_constraint(op.f(EVENT_CARDS_CATEGORY_CHECK), "event_cards", type_="check")
    op.create_check_constraint("category", "event_cards", _CATEGORY_CHECK)


def downgrade() -> None:
    """Откат с гардом против МОЛЧАЛИВОЙ потери смысла (AC-2).

    **Что произошло бы без гарда.** `downgrade` возвращает узкое ограничение.
    Строка `places.category = 'bar'` перестаёт ему соответствовать, и
    PostgreSQL отказывает в `ADD CONSTRAINT` с `check constraint "..." is
    violated by some row` — то есть откат ПАДАЕТ, но падает на этапе DDL,
    когда транзакция уже начала менять схему. Сообщение говорит о нарушении
    ограничения, а не о том, сколько строк и в какой таблице виноваты;
    оператор вынужден искать их сам.

    **Решение.** Гард считает строки в ОБЕИХ таблицах до изменения схемы и
    останавливается с числом. Отказ виден до операции, а не после.

    **Обход.** `ALEMBIC_ALLOW_MEANING_LOSS=1` — осознанный выбор оператора
    (например, откат пилота, где категории «бар» и «настольные игры» не
    нужны). Тогда строки переводятся в `other` ДО возврата узкого
    ограничения, и откат проходит. Предупреждение печатается: молчаливого
    стирания не остаётся ни в одном пути.

    **Почему `other`, а не удаление строк.** `event_cards.place_id` ссылается
    на `places.id`; удаление места сломало бы карточку. Смысл категории
    теряется, но сущности остаются, и повторный `upgrade` не восстановит
    категорию — это записано в предупреждении.
    """
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    bind = op.get_bind()
    places_at_risk = bind.execute(
        sa.text("SELECT count(*) FROM places WHERE category = ANY(:values)"),
        {"values": list(NEW_CATEGORIES)},
    ).scalar_one()
    cards_at_risk = bind.execute(
        sa.text("SELECT count(*) FROM event_cards WHERE category = ANY(:values)"),
        {"values": list(NEW_CATEGORIES)},
    ).scalar_one()
    rows_at_risk = places_at_risk + cards_at_risk

    if rows_at_risk:
        if os.environ.get(ALLOW_MEANING_LOSS_ENV) != "1":
            raise RuntimeError(
                f"Откат 0040 остановлен: у {places_at_risk} мест и {cards_at_risk} "
                "карточек категория — 'bar' или 'boardgames', а откат возвращает "
                "белый список без них. Эти строки перестанут проходить "
                "ограничение, и `ADD CONSTRAINT` упадёт на нарушении. Если замена "
                f"категории на '{FALLBACK_CATEGORY}' допустима, повторите откат с "
                f"`{ALLOW_MEANING_LOSS_ENV}=1`."
            )
        # Прецедент `0021`/`0037`: stdlib-logger, а не `structlog` — миграция не
        # должна зависеть от конфигурации логирования приложения.
        logging.getLogger("alembic.runtime.migration").warning(
            "осознанный откат 0040: у %s мест и %s карточек категория будет "
            "заменена на '%s' (%s=1)",
            places_at_risk,
            cards_at_risk,
            FALLBACK_CATEGORY,
            ALLOW_MEANING_LOSS_ENV,
        )
        # Порядок обязателен: сначала данные, потом схема. Если переставить,
        # `ADD CONSTRAINT` упадёт на строках, которые ещё не исправлены.
        bind.execute(
            sa.text("UPDATE places SET category = :fallback WHERE category = ANY(:values)"),
            {"fallback": FALLBACK_CATEGORY, "values": list(NEW_CATEGORIES)},
        )
        bind.execute(
            sa.text(
                "UPDATE event_cards SET category = :fallback WHERE category = ANY(:values)"
            ),
            {"fallback": FALLBACK_CATEGORY, "values": list(NEW_CATEGORIES)},
        )

    # AC-2: возврат узкого списка. `drop` первым — иначе на колонке окажутся
    # два CHECK, и PostgreSQL будет проверять оба.
    op.drop_constraint(op.f(PLACES_CATEGORY_CHECK), "places", type_="check")
    op.create_check_constraint("category", "places", _CATEGORY_CHECK_BEFORE)

    op.drop_constraint(op.f(EVENT_CARDS_CATEGORY_CHECK), "event_cards", type_="check")
    op.create_check_constraint("category", "event_cards", _CATEGORY_CHECK_BEFORE)
