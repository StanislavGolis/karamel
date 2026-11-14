"""Стори 13.2 (AC-3, AC-4, AC-6): генерация заголовка карточки.

AC-3 требует, чтобы предлог и падеж выбирались по КАТЕГОРИИ МЕСТА, а не по
активности. Поэтому ключевые тесты — пары, в которых одна активность
складывается с разными категориями и даёт разные обороты. Тест, проверяющий
только «кофе → Кофе в кафе», прошёл бы и на реализации, игнорирующей
категорию: для `(coffee, cafe)` оба подхода дают одно и то же.
"""

from __future__ import annotations

import pytest

from app.models.enums import Activity, PlaceCategory
from app.schemas.event_card import TITLE_MAX_LENGTH
from app.services.card_title import generate_card_title


class TestPrepositionByPlaceCategory:
    """AC-3: предлог и падеж — по категории места, не по активности."""

    def test_coffee_at_cafe(self) -> None:
        title = generate_card_title(
            activity=Activity.COFFEE, place_category=PlaceCategory.CAFE, place_name="Прага"
        )
        assert title == "Кофе в кафе „Прага“"

    def test_same_activity_different_category_gives_different_preposition(self) -> None:
        """Одна активность, две категории — разные обороты.

        Мутация «предлог по активности» этот тест НЕ ловит: она даёт один и
        тот же оборот для обеих категорий, и первая проверка пройдёт. Ловит
        вторая: строки обязаны различаться.
        """
        at_cafe = generate_card_title(
            activity=Activity.COFFEE, place_category=PlaceCategory.CAFE, place_name="Прага"
        )
        at_park = generate_card_title(
            activity=Activity.COFFEE, place_category=PlaceCategory.PARK, place_name="Прага"
        )
        assert at_cafe != at_park
        assert "кафе" in at_cafe
        assert "парке" in at_park

    def test_boardgames_at_bar(self) -> None:
        title = generate_card_title(
            activity=Activity.BOARDGAMES, place_category=PlaceCategory.BAR, place_name="Прага"
        )
        assert title == "Настольные игры в баре „Прага“"

    def test_cinema_at_cinema(self) -> None:
        title = generate_card_title(
            activity=Activity.CINEMA, place_category=PlaceCategory.CINEMA, place_name="Прага"
        )
        assert title == "Кино в кинотеатре „Прага“"

    def test_walk_at_skating_rink(self) -> None:
        title = generate_card_title(
            activity=Activity.WALK, place_category=PlaceCategory.SKATING, place_name="Прага"
        )
        assert title == "Прогулка на катке „Прага“"

    def test_walk_near_place_uses_instrumental(self) -> None:
        """`walk` категория — «рядом с», а не «в»: там нет помещения."""
        title = generate_card_title(
            activity=Activity.WALK, place_category=PlaceCategory.WALK, place_name="Прага"
        )
        assert title == "Прогулка рядом с „Прага“"

    def test_bar_tile_at_bar_place_avoids_root_repetition(self) -> None:
        """Переопределение `(bar, bar)`: «Бар в баре» — повтор корня.

        Мутация «удалить переопределение» даёт именно эту строку.
        """
        title = generate_card_title(
            activity=Activity.BAR, place_category=PlaceCategory.BAR, place_name="Прага"
        )
        assert title == "Бар „Прага“"
        assert "в баре" not in title

    def test_unknown_combination_falls_back_to_generic_preposition(self) -> None:
        """Композиция без переопределения не падает: любой пары достаточно."""
        title = generate_card_title(
            activity=Activity.SPORT, place_category=PlaceCategory.QUIZ, place_name="Прага"
        )
        assert title.startswith("Спорт")
        assert "Прага" in title


class TestTruncation:
    """AC-4: сокращается ИМЯ МЕСТА, не готовый заголовок."""

    def test_short_name_kept_intact(self) -> None:
        title = generate_card_title(
            activity=Activity.COFFEE, place_category=PlaceCategory.CAFE, place_name="Прага"
        )
        assert "Прага" in title
        assert "…" not in title

    def test_18_char_name_kept_intact(self) -> None:
        """Замер `max(length(places.name))` = 18 на стенде — имя влезает целиком."""
        name = "П" * 18
        title = generate_card_title(
            activity=Activity.COFFEE, place_category=PlaceCategory.CAFE, place_name=name
        )
        assert len(title) <= TITLE_MAX_LENGTH
        assert name in title

    def test_100_char_name_truncated_with_ellipsis(self) -> None:
        """Граница AC-5: имя 100 символов — самый длинный замеренный вход."""
        name = "П" * 100
        title = generate_card_title(
            activity=Activity.COFFEE, place_category=PlaceCategory.CAFE, place_name=name
        )
        assert len(title) <= TITLE_MAX_LENGTH
        assert title.endswith("“")
        assert "…" in title

    def test_truncation_keeps_closing_quote(self) -> None:
        """Мутация `title[:100]` срезает кавычку — заголовок без конца.

        Проверяется наличие ОБЕИХ кавычек: открывающей и закрывающей.
        """
        name = "П" * 100
        title = generate_card_title(
            activity=Activity.BOARDGAMES, place_category=PlaceCategory.BOARDGAMES, place_name=name
        )
        assert title.count("„") == 1
        assert title.count("“") == 1
        assert title.endswith("“")

    def test_ellipsis_counts_toward_budget(self) -> None:
        """Мутация `name[:budget] + '…'` даёт `budget + 1` символов — перебор."""
        for length in (99, 100, 120):
            name = "П" * length
            title = generate_card_title(
                activity=Activity.COFFEE, place_category=PlaceCategory.CAFE, place_name=name
            )
            assert len(title) <= TITLE_MAX_LENGTH, f"длина имени {length} дала {len(title)}"

    def test_title_never_empty(self) -> None:
        """Худший случай бюджета не должен дать пустую строку."""
        title = generate_card_title(
            activity=Activity.BOARDGAMES,
            place_category=PlaceCategory.CINEMA,
            place_name="П" * 200,
        )
        assert title
        assert len(title) <= TITLE_MAX_LENGTH


class TestLegacyOther:
    """AC-6: `other` — легаси-активность, заголовок не генерируется."""

    def test_other_raises(self) -> None:
        """Генератор отказывается, а не молча собирает «Другое в кафе»."""
        with pytest.raises(ValueError, match="other"):
            generate_card_title(
                activity=Activity.OTHER,
                place_category=PlaceCategory.CAFE,
                place_name="Прага",
            )


def _neutral_name(length: int) -> str:
    """Имя места нужной длины, нейтральное для правил контента.

    Первая редакция теста брала `"П" * 18` и падала на `('caps', 'repeat')`:
    18 одинаковых заглавных — это 100 % заглавных (> `CAPS_MAX_RATIO` = 0.6)
    и 18 повторов одного символа подряд (> `REPEAT_MIN_COUNT` = 5). Правила
    сработали ВЕРНО, тест был неправ: AC-5 говорит про нейтральное имя, а не
    про любое. Здесь — строчные слова через пробел, без повторов.
    """
    words = ("кафе", "прага", "томск", "уфа", "лента", "дом")
    name = ""
    index = 0
    while len(name) < length:
        name += ("" if not name else " ") + words[index % len(words)]
        index += 1
    return name[:length].strip()


class TestContentRulesInteraction:
    """AC-5: сгенерированный заголовок проходит автопроверку."""

    def test_generated_title_passes_length_rule(self) -> None:
        """Правило `length` — первое и единственное, что зависит от генерации.

        Проверяется напрямую, без БД: если генерация вернёт 101 символ,
        `evaluate_card_content` начнёт с `length` и карточка уйдёт в `pending`.
        """
        from app.services.card_content_rules import CardContent, evaluate_card_content

        title = generate_card_title(
            activity=Activity.BOARDGAMES,
            place_category=PlaceCategory.BOARDGAMES,
            place_name="П" * 100,
        )
        result = evaluate_card_content(
            CardContent(title=title, description=None, category="boardgames"),
            stop_words=(),
        )
        assert "length" not in result.failed_rules

    def test_neutral_name_is_approved(self) -> None:
        """Замер 18 и 100 символов на нейтральном имени → `approved`.

        AC-5: имя места нейтрально, значит единственное правило, которое
        может сработать, — `length`. Оно не срабатывает.
        """
        from app.services.card_content_rules import CardContent, evaluate_card_content

        for length in (18, 100):
            title = generate_card_title(
                activity=Activity.COFFEE,
                place_category=PlaceCategory.CAFE,
                place_name=_neutral_name(length),
            )
            result = evaluate_card_content(
                CardContent(title=title, description=None, category="cafe"), stop_words=()
            )
            assert result.passed, f"имя {length}: провалены {result.failed_rules}"

    def test_stop_word_in_place_name_is_not_silently_approved(self) -> None:
        """Находка CS: имя места из OSM может нарушать правила контента.

        Поведение фиксируется: карточка уходит в `pending`, а не «тихо
        approved». Генерация длину гарантирует, остальные правила — нет
        (долг `W-13.2-a`).
        """
        from app.services.card_content_rules import CardContent, evaluate_card_content

        title = generate_card_title(
            activity=Activity.BOARDGAMES, place_category=PlaceCategory.BAR, place_name="Казино"
        )
        result = evaluate_card_content(
            CardContent(title=title, description=None, category="bar"),
            stop_words=("казино",),
        )
        assert not result.passed
        assert "stop_word" in result.failed_rules


class TestNameNormalization:
    """CR 13.2 H-3 M09: имя места нормализуется ПЕРЕД сборкой заголовка.

    **Почему тест появился после CR.** Мутация «убрать `normalize_text`»
    ВЫЖИВАЛА: все существующие тесты подавали уже чистые имена («Прага»,
    «П»*100), на которых нормализация ничего не меняет. При этом имя места
    приходит от автора (13.1: из OSM без модерации), и NBSP/ZWSP в нём дают
    заголовок с невидимым мусором, который проходит `CHECK` в БД.
    """

    def test_nbsp_in_name_is_collapsed(self) -> None:
        """NBSP схлопывается до обычного пробела — заголовок читаем глазами."""
        title = generate_card_title(
            activity=Activity.COFFEE,
            place_category=PlaceCategory.CAFE,
            place_name="Прага\u00a0Бар",
        )
        assert "\u00a0" not in title, f"NBSP остался в заголовке: {title!r}"
        assert title == "Кофе в кафе „Прага Бар“"

    def test_zwsp_in_name_is_removed(self) -> None:
        """ZWSP (U+200B) удаляется: он не пробел и `strip()` его не снимает.

        Краснеет, если убрать `normalize_text`: ноль-ширинный символ остался бы
        в заголовке, и человек увидел бы «ПрагаБар» без пробела.
        """
        title = generate_card_title(
            activity=Activity.COFFEE,
            place_category=PlaceCategory.CAFE,
            place_name="Прага\u200bБар",
        )
        assert "\u200b" not in title, f"ZWSP остался в заголовке: {title!r}"

    def test_extra_spaces_are_collapsed(self) -> None:
        """Двойные пробелы схлопываются — заголовок не «дырявый»."""
        title = generate_card_title(
            activity=Activity.COFFEE,
            place_category=PlaceCategory.CAFE,
            place_name="  Прага   Бар  ",
        )
        assert title == "Кофе в кафе „Прага Бар“"


class TestBudgetExhausted:
    """CR 13.2 H-3 M10: ветка «бюджет имени исчерпан» не пустая.

    **Почему тест появился после CR.** Мутация `name_part = _ELLIPSIS` → `""`
    ВЫЖИВАЛА: ветка недостижима на текущих константах (худшая «голова»
    ≈ 30 символов при `TITLE_MAX_LENGTH = 100`, бюджет ≈ 69). Но константа —
    доменное правило, а не физическая величина: её уменьшение делает ветку
    живой, и тогда пустой `name_part` дал бы заголовок вида `Кофе в кафе „“`
    — с пустыми кавычками и без имени места.

    Константа подменяется в МОДУЛЕ генератора: `card_title` импортирует её
    по имени, и подмена в `card_content_rules` на уже загруженный модуль
    не подействовала бы.
    """

    def test_title_with_exhausted_budget_is_not_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Бюджет < 1: заголовок остаётся непустым и с закрытой кавычкой."""
        import app.services.card_title as title_module

        # Голова «Кофе в кафе „» = 13 символов; 10 заведомо меньше.
        monkeypatch.setattr(title_module, "TITLE_MAX_LENGTH", 10)

        title = generate_card_title(
            activity=Activity.COFFEE,
            place_category=PlaceCategory.CAFE,
            place_name="Прага",
        )

        assert "„“" not in title, f"пустые кавычки в заголовке: {title!r}"
        assert title.endswith("“"), f"заголовок без закрывающей кавычки: {title!r}"
        # Имя не влезло целиком — оно обязано быть представлено многоточием,
        # а не исчезнуть: пустая строка вместо `…` = молчаливая потеря места.
        assert "…" in title, f"имя места исчезло без многоточия: {title!r}"
