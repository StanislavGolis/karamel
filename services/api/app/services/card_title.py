"""Стори 13.2 (AC-3, AC-4, AC-6; FR130, ADR-26): генерация заголовка карточки.

`title` остаётся `String(100)` и `NOT NULL` в БД, но **не вводится автором**:
сервер собирает его при создании из активности и названия места. Контракт API
для клиента не меняется — меняется источник значения (ADR-26).

**Почему отдельный модуль, а не метод модели.** Генерация — чистая функция:
вход (активность, категория места, имя места) → выход (строка). Её надо
проверять без БД и без HTTP, включая границу 100 символов. В сервисе карточки
(`event_card_service`) она вызывается дважды: при создании и при смене места
или активности — там же, где меняется `title`.

**Три уровня защиты от длинного заголовка (ADR-26).** `card_content_rules`
срабатывает при `len(title) > 100` → `verification_status = pending`. Ошибка
шаблона даёт не некрасивую строку, а карточку, молча ушедшую в ручную
очередь. Поэтому: (1) замер `max(length(places.name))` = 18 · (2) сокращение
**имени места** по построению · (3) тест на `<= 100` **и** `approved`.
"""

from __future__ import annotations

from app.models.enums import Activity, PlaceCategory
from app.services.card_content_rules import TITLE_MAX_LENGTH
from app.services.profile_normalize import normalize_text

#: AC-3: фраза активности — то, что человек выбрал плиткой. Значения совпадают
#: с `Activity`, кроме легаси `other` (AC-6: для него заголовок не генерируется).
_ACTIVITY_PHRASE: dict[Activity, str] = {
    Activity.COFFEE: "Кофе",
    Activity.WALK: "Прогулка",
    Activity.BAR: "Бар",
    Activity.CINEMA: "Кино",
    Activity.BOARDGAMES: "Настольные игры",
    Activity.SPORT: "Спорт",
}

#: AC-3: предлог и падеж — по КАТЕГОРИИ МЕСТА, а не по активности (ADR-26).
#: Ось разная намеренно: у места категория «кафе», а встреча может быть
#: «настольные игры в кафе». Если бы оборот выбирался активностью, «Кофе»
#: и «Настольные игры» получали бы один и тот же предлог, и строка
#: «Настольные игры за кофе» стала бы возможной.
_CATEGORY_PREPOSITION: dict[PlaceCategory, str] = {
    PlaceCategory.CAFE: "в кафе",
    PlaceCategory.BAR: "в баре",
    # Стори 13.2 (AC-3): категория места под плитку «настольные игры».
    # Без ключа генератор падал бы `KeyError` ровно на той паре, ради
    # которой миграция `0040` и делалась: до неё категории не существовало.
    PlaceCategory.BOARDGAMES: "в",
    PlaceCategory.CINEMA: "в кинотеатре",
    PlaceCategory.PARK: "в парке",
    # `walk` — не помещение: «в прогулке» бессмысленно, нужен творительный
    # падеж при предлоге «рядом с».
    PlaceCategory.WALK: "рядом с",
    PlaceCategory.QUIZ: "в",
    PlaceCategory.SKATING: "на катке",
    PlaceCategory.SPORT: "в",
    # `other` — категория места без уточнения. Общий предлог, потому что
    # конкретика неизвестна, а @property заголовок обязан быть непустым.
    PlaceCategory.OTHER: "в",
}

#: AC-3: переопределения для пар, где композиция даёт повтор корня.
#: `(bar, bar)` → «Бар в баре „X“»: слово «бар» дважды в одной короткой
#: строке. Переопределение убирает предлог целиком.
_OVERRIDES: dict[tuple[Activity, PlaceCategory], str] = {
    (Activity.BAR, PlaceCategory.BAR): "{activity}",
}

#: AC-4: символ усечения. Один кодпоинт (U+2026), не три точки: иначе
#: «...» съедало бы три символа бюджета вместо одного.
_ELLIPSIS = "…"

#: Кавычки-ёлочки: открывающая снизу, закрывающая сверху. Пара обязана быть
#: парной — тест `test_truncation_keeps_closing_quote` проверяет обе.
_QUOTE_OPEN = "„"
_QUOTE_CLOSE = "“"


def generate_card_title(
    *, activity: Activity, place_category: PlaceCategory, place_name: str
) -> str:
    """AC-3, AC-4, AC-6: собирает заголовок из активности и места.

    Порядок: фраза активности → оборот категории → имя места в кавычках.
    Сокращается ИМЯ, а не готовый заголовок (AC-4): обрезка готовой строки
    срезала бы закрывающую кавычку, и заголовок терял бы конец.

    `Activity.OTHER` — `ValueError`, а не тихая сборка «Другое в кафе»
    (AC-6): легаси-карточкам заголовок приходит от клиента, и генератор
    не должен притворяться, что у них есть активность.
    """
    # AC-6: `other` — не активность, а признак «активность неизвестна».
    # Заголовок для таких карточек не генерируется и не перетирается.
    if activity is Activity.OTHER:
        raise ValueError(
            "Для активности 'other' заголовок не генерируется: значение легаси, "
            "заголовок приходит от клиента и не перетирается (AC-6)."
        )

    phrase = _ACTIVITY_PHRASE[activity]
    cleaned_name = normalize_text(place_name)

    # AC-3: пары с повтором корня собираются по переопределению.
    override = _OVERRIDES.get((activity, place_category))
    if override is not None:
        head = override.format(activity=phrase) + " "
    else:
        preposition = _CATEGORY_PREPOSITION[place_category]
        head = f"{phrase} {preposition} "

    # AC-4: бюджет имени — всё, что осталось от 100 символов после «головы»
    # и закрывающей кавычки. Открывающая кавычка входит в «голову».
    open_quote = _QUOTE_OPEN
    budget = TITLE_MAX_LENGTH - len(head) - len(open_quote) - len(_QUOTE_CLOSE)

    # AC-4: имя влезает целиком — не трогаем. Иначе режем ИМЯ, ставя многоточие
    # ВНУТРЬ бюджета: `_ELLIPSIS` занимает один символ, поэтому хвост
    # `budget - 1` символов имени. Мутация «+1 без вычета многоточия» даёт
    # ровно 101 символ на имени 100 — тест `test_ellipsis_counts_toward_budget`.
    if len(cleaned_name) <= budget:
        name_part = cleaned_name
    elif budget > 0:
        name_part = cleaned_name[: budget - 1].rstrip() + _ELLIPSIS
    else:
        # Недостижимо на текущих константах (худшая «голова» ≈ 30 символов,
        # бюджет ≈ 69), но пустая строка вместо заголовка была бы нарушением
        # `NOT NULL` в БД и `min_length=1` в схеме.
        name_part = _ELLIPSIS

    return f"{head}{open_quote}{name_part}{_QUOTE_CLOSE}"
