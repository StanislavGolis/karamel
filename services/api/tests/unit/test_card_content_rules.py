"""Правила автопроверки карточки (стори 4.3, Task 1; AC-01, AC-02).

§18.1: каждый тест краснеет от снятия конкретного правила — в docstring
указано, какая правка делает его красным.

§18.2: проверяется НАБЛЮДАЕМЫЙ результат (`passed`, `failed_rules`),
а не факт вызова регулярного выражения.
"""

from __future__ import annotations

import pytest

from app.services.card_content_rules import (
    CAPS_MIN_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    REPEAT_MIN_COUNT,
    RULES,
    TITLE_MAX_LENGTH,
    CardContent,
    evaluate_card_content,
)

STOP_WORDS = ("казино", "займ", "свидание с продолжением")


def _check(
    title: str, description: str | None = None, category: str = "cinema"
) -> tuple[bool, tuple[str, ...]]:
    result = evaluate_card_content(
        CardContent(title=title, description=description, category=category),
        stop_words=STOP_WORDS,
    )
    return result.passed, result.failed_rules


def test_clean_text_passes() -> None:
    """AC-01: чистый текст — `passed=True`, пустой кортеж.

    Краснеет, если любое правило срабатывает на нормальном заголовке
    («Кино в пятницу»): ложное срабатывание отправило бы в очередь
    модератора вообще всё.
    """
    passed, failed = _check("Кино в пятницу", "Идём на вечерний сеанс")

    assert passed is True
    assert failed == ()


def test_rules_order_is_contract() -> None:
    """AC-02: порядок имён правил — часть контракта, сравнивается кортежем.

    Краснеет, если поменять порядок в `RULES`: клиент сравнивает кортеж
    целиком, и перестановка сломала бы совместимость без видимой причины.
    """
    assert RULES == ("length", "category", "stop_word", "link", "caps", "repeat")


@pytest.mark.parametrize("title", ["", "   ", "\u00a0\u2009"])
def test_length_rejects_empty_title(title: str) -> None:
    """AC-02: пустой заголовок после нормализации — `length`.

    Краснеет, если убрать `not title` из правила: пробелы и NBSP после
    `normalize_text` дают пустую строку, и без проверки карточка ушла бы
    в `approved` с пустым названием.
    """
    passed, failed = _check(title)

    assert passed is False
    assert failed == ("length",)


def test_length_rejects_too_long_title() -> None:
    """AC-02: предел заголовка — 100 символов после нормализации.

    Краснеет, если снять `len(title) > TITLE_MAX_LENGTH`: переполнение
    `varchar(100)` в БД дало бы `DBAPIError` (500), а не доменный отказ.
    """
    passed, failed = _check("я" * (TITLE_MAX_LENGTH + 1))

    assert passed is False
    assert "length" in failed


def test_length_rejects_too_long_description() -> None:
    """AC-02: предел описания — 2000 символов.

    Краснеет, если снять проверку описания: `description` уходит в `Text`
    без верхней границы на уровне БД, и «простыня на 50 КБ» попала бы
    в очередь модератора целиком.
    """
    passed, failed = _check("Кино", "я" * (DESCRIPTION_MAX_LENGTH + 1))

    assert passed is False
    assert "length" in failed


def test_length_accepts_boundary_description() -> None:
    """AC-02: ровно 2000 символов — ещё допустимо (`>` строгий).

    Текст РАЗНООБРАЗНЫЙ: 2000 одинаковых символов законно ловит правило
    `repeat`, и тест проверял бы не границу длины, а повтор.

    Краснеет, если заменить `>` на `>=`: граничное легальное значение
    отбрасывалось бы как ошибка.
    """
    filler = "идём в кино смотреть новый фильм вечером "
    description = (filler * (DESCRIPTION_MAX_LENGTH // len(filler) + 1))[:DESCRIPTION_MAX_LENGTH]
    assert len(description) == DESCRIPTION_MAX_LENGTH

    passed, failed = _check("Кино в пятницу", description)

    assert passed is True
    assert failed == ()


def test_category_outside_whitelist() -> None:
    """AC-02, ADR-08: категория вне `PlaceCategory` — правило `category`.

    Краснеет, если убрать проверку категории: воркер/импорт идут мимо
    Pydantic, и мусор из БД попал бы в очередь как «проверенный текст».
    """
    passed, failed = _check("Кино", category="не-категория")

    assert passed is False
    assert failed == ("category",)


@pytest.mark.parametrize(
    "title",
    ["Казино вечером", "КАЗИНО", "казино", "Казино\u00a0в центре"],
)
def test_stop_word_case_and_nbsp(title: str) -> None:
    """AC-02: стоп-слово срабатывает в любом регистре и с NBSP.

    Краснеет, если убрать `casefold()` или нормализацию: «КАЗИНО» и
    «Казино\\u00a0в центре» прошли бы как чистый текст.
    """
    passed, failed = _check(title)

    assert passed is False
    assert "stop_word" in failed


def test_stop_word_in_description() -> None:
    """AC-02: стоп-слово в ОПИСАНИИ тоже срабатывает.

    Краснеет, если проверять только заголовок: спам уводят в описание.
    """
    passed, failed = _check("Кино", "Подробности: займ под 0%")

    assert passed is False
    assert failed == ("stop_word",)


def test_stop_word_multiword_phrase() -> None:
    """AC-02: многословное стоп-слово ищется как подстрока.

    Краснеет, если сравнивать по отдельным словам: «свидание
    с продолжением» — одна фраза, а не два стоп-слова.
    """
    passed, failed = _check("Свидание с продолжением")

    assert passed is False
    assert "stop_word" in failed


@pytest.mark.parametrize(
    "text",
    ["Сайт https://spam.example", "Заходи www.spam.example", "Пиши t.me/spam", "Мой @spam_nick"],
)
def test_link_forms(text: str) -> None:
    """AC-02, D-4.3-2: все формы увода общения — правило `link`.

    Краснеет, если убрать любую альтернативу из `_LINK_RE`: `@nick` и
    `t.me/` — тот же вектор, что `http://` (увод из продукта до встречи).
    """
    passed, failed = _check("Кино", text)

    assert passed is False
    assert "link" in failed


def test_short_uppercase_word_is_not_caps() -> None:
    """AC-02: короткий заголовок заглавными — не нарушение.

    Краснеет, если снять `CAPS_MIN_LENGTH`: «КИНО» (4 буквы) дало бы 100%
    заглавных и отправило бы в очередь любую короткую карточку.
    """
    passed, failed = _check("КИНО")

    assert passed is True
    assert failed == ()


def test_caps_rule_fires_on_long_uppercase() -> None:
    """AC-02: длинный текст КАПСОМ — правило `caps`.

    Краснеет, если убрать правило или снять порог `CAPS_MAX_RATIO`.
    """
    text = "ПРИХОДИТЕ ВСЕ НА ВСТРЕЧУ ВЕЧЕРОМ"
    assert len([c for c in text if c.isalpha()]) >= CAPS_MIN_LENGTH

    passed, failed = _check(text)

    assert passed is False
    assert "caps" in failed


def test_caps_counts_letters_only() -> None:
    """AC-02: доля заглавных считается по БУКВАМ, цифры и пробелы не в счёт.

    Краснеет, если делить на длину строки: «КИНО 18 2026 55 777» прошло бы
    порог из-за цифр в тексте.
    """
    passed, failed = _check("кино 18 2026 55 777", "кино в пятницу вечером")

    assert passed is True
    assert failed == ()


@pytest.mark.parametrize(
    "text",
    ["Привет!!!!!", "Свидание свидание свидание свидание свидание"],
)
def test_repeat_rule(text: str) -> None:
    """AC-02: повтор символа или слова — правило `repeat`.

    Краснеет, если убрать `REPEAT_MIN_COUNT` из шаблона или одно из двух
    регулярных выражений: символьный и словесный повтор — разные векторы спама.
    """
    passed, failed = _check("Кино", text)

    assert passed is False
    assert "repeat" in failed


def test_repeat_amount_below_threshold_is_allowed() -> None:
    """AC-02: 3–4 повтора — нормальная разговорная речь.

    Краснеет, если снизить `REPEAT_MIN_COUNT` до 3: «Ура!!!» отправило бы
    в очередь модератора обычную радость.
    """
    assert REPEAT_MIN_COUNT == 5
    passed, failed = _check("Ура!!!")

    assert passed is True
    assert failed == ()


def test_multiple_rules_keep_declaration_order() -> None:
    """AC-02: несколько сработавших правил идут в порядке объявления.

    Краснеет, если собирать `failed_rules` как множество или сортировать
    по алфавиту: клиент сравнивает кортеж целиком.
    """
    passed, failed = _check("КАЗИНО ИДЁТ ВСЕМ ВЕЧЕРОМ!!!!!")

    assert passed is False
    # stop_word → caps → repeat (length и category чисты, link нет).
    assert failed == ("stop_word", "caps", "repeat")


def test_no_duplicate_rules() -> None:
    """AC-02: правило попадает в результат один раз.

    Краснеет, если добавлять имя в список на каждой найденной подстроке:
    три стоп-слова дали бы три `stop_word`, и клиент показал бы дубли.
    """
    passed, failed = _check("Казино и займ, свидание с продолжением")

    assert passed is False
    assert failed.count("stop_word") == 1


def test_empty_stop_words_list_does_not_fire() -> None:
    """AC-02: пустой словарь стоп-слов — правило не срабатывает.

    Краснеет, если искать `'' in text` (пустая строка есть в любой строке):
    без фильтра пустых элементов ЛЮБАЯ карточка ушла бы в очередь.
    """
    result = evaluate_card_content(
        CardContent(title="Кино в пятницу", description=None, category="cinema"),
        stop_words=("", "  "),
    )

    assert result.passed is True
    assert result.failed_rules == ()


def test_stop_words_are_normalized_inside_rule() -> None:
    """AC-02: стоп-слово из конфига нормализуется и в правиле.

    Краснеет, если убрать нормализацию стоп-слов: слово, вписанное в `.env`
    с NBSP или в верхнем регистре, не нашлось бы в нормализованном тексте.
    """
    result = evaluate_card_content(
        CardContent(title="Казино вечером", description=None, category="cinema"),
        stop_words=("  КАЗИНО\u00a0",),
    )

    assert result.passed is False
    assert result.failed_rules == ("stop_word",)


def test_description_none_is_allowed() -> None:
    """AC-02: `description=None` — легальное значение (поле необязательное).

    Краснеет, если нормализовать `None` как строку: описание без текста
    дало бы ложное срабатывание `length`.
    """
    passed, failed = _check("Кино в пятницу", None)

    assert passed is True
    assert failed == ()


@pytest.mark.parametrize("text", ["пиши @никнейм", "@ник", "@иван_петров"])
def test_link_cyrillic_mention(text: str) -> None:
    """AC-02, D-4.3-2 (CR 4.3, патч 3): кириллические `@`-упоминания ловятся.

    Краснеет, если вернуть `@[a-z0-9_]{3,}`: «пиши @никнейм» проходил как
    чистый текст, то есть правило против увода общения из продукта не работало
    для русскоязычного пилота.
    """
    passed, failed = _check("Кино", text)

    assert passed is False
    assert "link" in failed
