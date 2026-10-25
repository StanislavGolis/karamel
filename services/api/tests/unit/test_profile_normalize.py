"""Нормализация полей анкеты (Task 3.5–3.6; AC-04).

§18.2: проверяется наблюдаемый результат (`вход → выход`), а не внутренние
вызовы. Таблица случаев — потому что дефект здесь всегда один и тот же:
две визуально одинаковые строки попадают в БД разными значениями, и выборка
`WHERE city = 'Томск'` (2.6) теряет половину анкет.
"""

from __future__ import annotations

import pytest

from app.services.profile_normalize import (
    normalize_bio,
    normalize_city,
    normalize_name,
    normalize_text,
)

PILOT_CITY = "Томск"

# --- normalize_text -------------------------------------------------------------


def test_normalize_text_strips_edges() -> None:
    """AC-04: края очищаются — `btrim` в БД режет только пробел, не NBSP/таб.

    Если сервис пропустит `'Томск\\t'`, CHECK `city = btrim(city, ...)` его
    отбьёт `IntegrityError`, то есть `500` на пользовательском вводе.
    """
    assert normalize_text("  Томск \t") == "Томск"


def test_normalize_text_replaces_nbsp_and_thin_space() -> None:
    """AC-04: NBSP и тонкий пробел — обычный пробел.

    NBSP не whitespace для `str.strip()` и не режется `btrim`: `'Томск\\u00a0'`
    прошёл бы CHECK и навсегда выпал из выборки по городу.
    """
    assert normalize_text("Томск\u00a0") == "Томск"
    assert normalize_text("Томск\u202f") == "Томск"
    assert normalize_text("Том\u00a0ск") == "Том ск"


def test_normalize_text_collapses_inner_spaces() -> None:
    """AC-04: `'Том   ск'` и `'Том ск'` — одно значение.

    Двойной пробел приходит из автозамены и копипаста; без схлопывания
    это две разные строки в `profiles.city`.
    """
    assert normalize_text("Том    ск") == "Том ск"


def test_normalize_text_applies_nfc() -> None:
    """AC-04: NFC обязателен — «й» бывает одной кодовой точкой и двумя.

    macOS присылает разложенную форму (`и` + комбинируемая бревис). Визуально
    строка та же, побайтово — другая, и сравнение с эталоном не сработает.
    """
    decomposed = "и\u0306"  # й в разложенной форме
    assert normalize_text(decomposed) == "й"
    assert normalize_text(decomposed) == normalize_text("й")


def test_normalize_text_keeps_emoji_and_yo() -> None:
    """AC-04: эмодзи и «ё» не уничтожаются нормализацией.

    Соблазн «упростить» текст (убрать не-буквы, свести ё→е) испортил бы
    живые анкеты: люди пишут «ёжик 🙂» и ожидают увидеть это же.
    """
    assert normalize_text("  ёжик 🙂  ") == "ёжик 🙂"


def test_normalize_text_result_has_no_nbsp_and_no_edges() -> None:
    """Task 3.6: инвариант результата, а не конкретное значение.

    Проверка обобщает AC-04 на любой ввод: после нормализации в строке не
    остаётся NBSP и краевых пробелов — ровно то, чего требует CHECK
    `city = btrim(city, ' \\t\\r\\n\\f\\v')`.
    """
    result = normalize_text("\u00a0 \t Том\u00a0ск \n\u00a0")

    assert "\u00a0" not in result
    assert "\u202f" not in result
    assert result == result.strip()


# --- normalize_city -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["томск", "Томск ", "ТОМСК", "Томск\t", "Томск\u00a0", "  тОмСк  ", "Томск"],
)
def test_normalize_city_canonizes_to_pilot_city(raw: str) -> None:
    """AC-04: любая форма города пилота → написание из `PILOT_CITY`.

    Эталон — конфиг, а не литерал в коде (FR16). Параметризация фиксирует,
    что канонизация не зависит от того, как именно пользователь набрал город.
    """
    assert normalize_city(raw, PILOT_CITY) == "Томск"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ростов-на-дону", "Ростов-на-Дону"),
        ("нижний новгород", "Нижний Новгород"),
        ("САНКТ-ПЕТЕРБУРГ", "Санкт-Петербург"),
        ("москва", "Москва"),
        ("петропавловск-камчатский", "Петропавловск-Камчатский"),
    ],
)
def test_normalize_city_capitalizes_non_pilot_cities(raw: str, expected: str) -> None:
    """AC-04: не-пилотный город — заглавная в каждом слове, дефис сохраняется.

    `str.title()` здесь непригоден: он даёт `'Ростов-На-Дону'` с заглавной
    после дефиса. Тест ловит именно это — иначе пользователь увидит
    «Санкт-Петербург» в одном написании, а «Ростов-На-Дону» в другом.
    """
    assert normalize_city(raw, PILOT_CITY) == expected


def test_normalize_city_keeps_pilot_case_from_config() -> None:
    """FR16: написание берётся из конфига, а не из алгоритма капитализации.

    Владелец может задать `PILOT_CITY=ТОМСК` — и тогда в БД должно лежать
    именно `ТОМСК`: эталон конфигурации главнее капитализации.
    """
    assert normalize_city("томск", "ТОМСК") == "ТОМСК"


def test_normalize_city_is_empty_when_input_is_blank() -> None:
    """AC-01: пустой город остаётся пустым — решение принимает сервис.

    Нормализация не бросает исключений: она чистая функция, а доменный код
    (`city_required`) выставляет сервис. Иначе один и тот же ввод давал бы
    разные ошибки в зависимости от точки вызова.
    """
    assert normalize_city("   ", PILOT_CITY) == ""
    assert normalize_city("\u00a0", PILOT_CITY) == ""


# --- normalize_name / normalize_bio ---------------------------------------------


def test_normalize_name_keeps_case() -> None:
    """AC-04: имя не капитализируется — «аня» и «Аня» решает пользователь.

    Автокапитализация испортила бы имена вроде «ван дер Саар» и ники.
    """
    assert normalize_name("  аня  ") == "аня"
    assert normalize_name("Аня\u00a0М") == "Аня М"


def test_normalize_bio_keeps_newlines() -> None:
    """AC-04: перевод строки в тексте анкеты сохраняется.

    Текст анкеты — шесть-семь строк (UX). Схлопывание `\\n` склеило бы абзацы
    в одну простыню, и пользователь увидел бы не то, что писал.
    """
    assert normalize_bio("люблю горы\nи лыжи") == "люблю горы\nи лыжи"


def test_normalize_bio_collapses_horizontal_spaces_only() -> None:
    """AC-04: горизонтальные пробелы схлопываются, переводы строк — нет.

    `'\\n\\n'` (пустая строка между абзацами) — осмысленный разделитель,
    а `'а    б'` — случайный. Один и тот же `normalize_text` здесь не годится.
    """
    assert normalize_bio("  люблю\u00a0\u00a0горы \n\n и лыжи  ") == "люблю горы\n\n и лыжи"


def test_normalize_bio_keeps_trailing_new_line_count() -> None:
    """AC-04: пустые строки внутри текста не схлопываются в одну.

    Разделение абзацев — часть смысла; иначе три абзаца превращаются в один
    сплошной блок при каждом сохранении.
    """
    assert normalize_bio("а\n\n\nб").count("\n") == 3


# --- CR 2.3, патчи 2–4: невидимый мусор и эталон города -----------------------


def test_normalize_text_removes_nul() -> None:
    """CR 2.3, патч 2: NUL-символ вычищается, а не уходит в БД.

    `\x00` не входит ни в `\s`, ни в `strip()`; PostgreSQL отвергает его
    в `text`/`varchar` с `CharacterNotInRepertoireError`, и пользователь
    получал `500` на копипасте из PDF или мессенджера. Тест краснеет, если
    фильтр убрать: строка с `\x00` доедет до базы.
    """
    assert normalize_text("Томск\x00") == "Томск"
    assert normalize_text("\x00Томск\x00") == "Томск"
    assert normalize_text("\x00") == ""


def test_normalize_text_removes_zero_width_and_bom() -> None:
    """CR 2.3, патч 3: ZWSP, BOM и управляющие символы не проходят.

    `U+200B` (ZWSP) и `U+FEFF` (BOM) относятся к Unicode `Cf`, `U+0001`
    и `U+001B` — к `Cc`. Ни один не матчится пробельным классом и не
    снимается `strip()`,
    поэтому CHECK в БД их пропускает: «город» из одного ZWSP выглядел бы
    непустым и навсегда выпал бы из выборки по городу.
    """
    assert normalize_text("Томск\u200b") == "Томск"
    assert normalize_text("\ufeffТомск") == "Томск"
    assert normalize_text("Том\u0001ск") == "Томск"
    assert normalize_text("Томск\u001b") == "Томск"
    # Город из одного ZWSP после очистки — пустая строка: сервис отдаст
    # `city_required`, а не сохранит «невидимый» город.
    assert normalize_text("\u200b") == ""
    assert normalize_text("\ufeff") == ""


def test_normalize_bio_keeps_newlines_but_drops_zero_width() -> None:
    """CR 2.3, патч 3: в тексте анкеты `\n` остаётся, невидимое — нет.

    Разделение абзацев — смысл, ZWSP из копипаста — шум. Если фильтр начнёт
    резать `\n`, три абзаца склеятся в один при каждом сохранении.
    """
    assert normalize_bio("горы\u200b\n\nлыжи") == "горы\n\nлыжи"
    assert "\n" in normalize_bio("а\u200b\nб")


def test_normalize_text_collapses_tab_without_gluing_words() -> None:
    """CR 2.3, патч 3: таб — пробел, а не удаление.

    Фильтр невидимых символов не должен удалять `\t`: `'Том\tск'` превратилось
    бы в `'Томск'` — склеенное слово, которого пользователь не писал.
    """
    assert normalize_text("Том\tск") == "Том ск"


def test_normalize_city_returns_normalized_pilot_city() -> None:
    """CR 2.3, патч 4: эталон отдаётся нормализованным, а не сырым.

    `PILOT_CITY="  Томск  "` в `.env` — обычное дело (пробел после `=`).
    Возврат сырого аргумента нарушал CHECK `city = btrim(city)` и давал
    `500` на корректном вводе пользователя.
    """
    assert normalize_city("томск", "  Томск  ") == "Томск"
    assert normalize_city("ТОМСК", "Томск\u00a0") == "Томск"


def test_normalize_city_with_blank_pilot_city_falls_back_to_capitalization() -> None:
    """CR 2.3, патч 4: пустой эталон не превращает любой город в пустую строку.

    Без проверки `canonical` пустой `PILOT_CITY` совпал бы с пустым вводом
    после нормализации и вернул `''` — то есть `city_required` на любой город.
    """
    assert normalize_city("ростов-на-дону", "   ") == "Ростов-на-Дону"
