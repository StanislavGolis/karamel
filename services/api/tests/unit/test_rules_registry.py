"""Реестр правил П1–П12 как ИСПОЛНЯЕМЫЙ гард (AC-1 стори 12.9).

**Зачем файл.** Правила П1–П12 живут текстом в PRD
(`prd-v2-action-first.md:815-832`). Текст не краснеет: тест можно удалить,
переименовать или оставить без покрытия, и никто не заметит — ровно так
возник долг `W-12.9-b`. Здесь каждое правило связано с КОНКРЕТНЫМ тестом,
и связь проверяется запуском, а не чтением.

**Что проверяется (эффект, не форма — P-25).** Не «в файле есть подстрока
`П5`», а: файл существует → `ast.parse` находит в нём функцию с заявленным
именем → докстринг этой функции содержит метку `П<N>`. Комментарий рядом с
тестом этого не заменит: метка обязана быть в докстринге функции, которую
находит `ast`.

**Почему `ast.parse`, а не импорт модуля.** Импорт интеграционного теста
тянул бы `app.main`, окружение, БД и порядок инициализации — реестр
перестал бы быть unit-тестом (AC-6: без сети, Redis и S3). `ast` разбирает
текст в дерево и отвечает на единственный нужный вопрос: есть ли функция и
что в её докстринге.

**Чем реестр НЕ является (долг `W-12.9-b`).** Это ручной список. Он ловит
удаление правила и потерю метки, но НЕ ловит обратное: новый тест с меткой
`П5`, не внесённый в `RULES`, останется невидимым. Автопоиск меток сделал
бы гард слепым к удалению записи — поэтому выбран явный список.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import pytest

#: Корень `services/api` — от него считаются пути реестра.
API_ROOT = Path(__file__).resolve().parents[2]

#: Правило → (путь к файлу теста относительно `services/api`, имя функции).
#:
#: 8 записей указывают на тесты, написанные эпиками 4–10 (правила
#: формализованы 2026-10-28, тесты писались раньше и номеров не знали —
#: им добавлена метка). 4 записи — новые тесты 12.9 (П3, П9, П10, П12).
RULES: dict[str, tuple[str, str]] = {
    "П1": (
        "tests/integration/test_cards_feed_api.py",
        "test_feed_returns_only_published_and_full_future",
    ),
    "П2": ("tests/unit/test_card_active_limit.py", "test_default_limit_is_two"),
    "П3": (
        "tests/integration/test_rule_p3_fields_before_join.py",
        "test_all_decision_fields_present_before_join",
    ),
    "П4": (
        "tests/integration/test_within_bounds_card_requests.py",
        "test_author_sees_within_bounds_for_each_applicant",
    ),
    "П5": (
        "tests/integration/test_my_requests_api.py",
        "test_not_selected_is_never_returned",
    ),
    "П6": (
        "tests/integration/test_auto_select_audience_guard.py",
        "test_guard_14_only_outsider_pending_keeps_card_published",
    ),
    "П7": (
        "tests/integration/test_request_cancel_api.py",
        "test_cancel_pending_returns_204_and_sets_cancelled",
    ),
    "П8": (
        "tests/integration/test_card_join_api.py",
        "test_full_card_returns_card_is_full_not_not_accepting",
    ),
    "П9": (
        "tests/integration/test_rule_p9_kind_vs_capacity.py",
        "test_open_set_joins_immediately_regardless_of_kind",
    ),
    "П10": (
        "tests/integration/test_rule_p10_cancel_reaches_chat.py",
        "test_cancel_card_reaches_participant_chat",
    ),
    "П11": (
        "tests/integration/test_card_author_profile_api.py",
        "test_archived_card_hides_author_profile",
    ),
    "П12": (
        "tests/unit/test_rule_p12_no_reason_anywhere.py",
        "test_no_forbidden_field_in_any_read_schema",
    ),
}

#: Полный набор правил PRD. Литерал, не `set(RULES)`: иначе проверка
#: «все 12 записей на месте» стала бы тавтологией (P-25).
ALL_RULES = frozenset(f"П{i}" for i in range(1, 13))


@functools.cache
def _module(path: str) -> ast.Module:
    """Разбирает файл в AST. Кеш: один файл может нести несколько правил."""
    return ast.parse((API_ROOT / path).read_text(encoding="utf-8"), filename=path)


def _carries_marker(docstring: str, rule: str) -> bool:
    """Есть ли в докстринге метка `rule` как ОТДЕЛЬНЫЙ токен (H-1).

    **Почему не `rule in docstring`.** «П1» — подстрока «П11», «П10», «П12»:
    правило П1 прошло бы с меткой чужого правила. CR 12.9 доказал это
    дважды: `П1`→`П11` и `П10`→`П100` давали 16 passed (SURVIVED).

    **Что считается отдельным токеном.** Справа метка не продолжается цифрой
    (`П1` не совпадает внутри `П11`), слева — не продолжается цифрой и не
    приклеена к букве (`П1` в `Правило П1`, но не в `XП1`). Формы `П5,`
    `П5.` `П5)` `П5 ` легальны: разделитель — любой не-цифровой символ.

    Границы заданы явно (`(?<![0-9A-Za-zА-Яа-яЁё_])`), а не через `\\b`:
    `\\b` в Python считает границей переход «буква↔цифра», поэтому `П1`
    внутри `П1x` дал бы ложное совпадение, а `П1` после подчёркивания — нет.
    """
    pattern = rf"(?<![0-9A-Za-zА-Яа-яЁё_]){re.escape(rule)}(?![0-9])"
    return re.search(pattern, docstring) is not None


def _function(path: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Ищет функцию верхнего уровня по имени (в т.ч. `async def`)."""
    for node in _module(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_every_rule_has_exactly_one_registry_entry() -> None:
    """AC-1: реестр содержит РОВНО 12 ключей — ни одного пропуска, ни лишнего."""
    assert set(RULES) == ALL_RULES, (
        f"пропущены: {sorted(ALL_RULES - set(RULES))}, "
        f"лишние: {sorted(set(RULES) - ALL_RULES)}"
    )


def test_registry_points_to_existing_tests() -> None:
    """AC-1: каждый путь существует, и в файле есть тест с заявленным именем."""
    missing_files = [
        rule for rule, (path, _) in RULES.items() if not (API_ROOT / path).is_file()
    ]
    assert missing_files == [], f"файлы тестов не найдены: {missing_files}"

    missing_tests = [
        f"{rule}: {path}::{name}"
        for rule, (path, name) in RULES.items()
        if _function(path, name) is None
    ]
    assert missing_tests == [], f"в файлах нет заявленных функций: {missing_tests}"


def test_registry_test_carries_rule_marker() -> None:
    """AC-1: докстринг теста содержит метку `П<N>` своего правила.

    Проверяется ИМЕННО докстринг функции из `ast`, а не текст файла:
    метка в комментарии рядом доказывала бы намерение автора, а не связь
    «этот тест покрывает это правило».
    """
    without_marker = []
    for rule, (path, name) in RULES.items():
        node = _function(path, name)
        assert node is not None, f"{rule}: функции {name} нет в {path}"
        docstring = ast.get_docstring(node) or ""
        if not _carries_marker(docstring, rule):
            without_marker.append(f"{rule}: {path}::{name}")
    assert without_marker == [], f"в докстринге нет метки правила: {without_marker}"


@pytest.mark.parametrize(
    ("docstring", "rule", "expected"),
    [
        # Легальные формы: метка отдельным токеном с любым разделителем.
        ("П5: проверка", "П5", True),
        ("П5, и ещё", "П5", True),
        ("П5.", "П5", True),
        ("(П5)", "П5", True),
        ("П5 ", "П5", True),
        ("Правило П5 — видимость", "П5", True),
        # Дефект H-1: П1 — подстрока П11/П10/П12, П5 — подстрока П51/П50.
        ("П11 (FR79)", "П1", False),
        ("П10 карточка", "П1", False),
        ("П12 причина", "П1", False),
        ("П51 расширенная", "П5", False),
        ("П100", "П10", False),
        # Приклееная к слову / подчёркиванию метка — не отдельный токен.
        ("XП5", "П5", False),
        ("_П5", "П5", False),
        # Нет метки вовсе.
        ("AC-1 без номера правила", "П5", False),
    ],
)
def test_marker_boundary_rejects_substring(
    docstring: str, rule: str, expected: bool
) -> None:
    """AC-1 (H-1 CR 12.9): метка ищется как ТОКЕН, а не как подстрока.

    **Дыра, которую закрывает тест.** CR 12.9 доказал дважды, что прежняя
    проверка `rule not in docstring` пропускала подмену: `П1`→`П11` и
    `П10`→`П100` давали **16 passed (SURVIVED)**. Гард проверял наличие
    ЛЮБОЙ метки, начинающейся с нужной, — класс `P-25`.

    **Почему этот тест нужен отдельно от проверки реального реестра.**
    На здоровом реестре обе реализации (`in` и граница) дают зелень: все
    12 меток корректны. Разницу видно только на подставных докстрингах —
    ровно поэтому дефект прожил до CR. Мутация «вернуть `in`» краснеет
    здесь и остаётся зелёной на `test_registry_test_carries_rule_marker`.
    """
    assert _carries_marker(docstring, rule) is expected, (
        f"граница метки {rule!r} в {docstring!r}: ожидалось {expected}"
    )


def test_registry_has_no_duplicate_test_names() -> None:
    """AC-1: один тест не может покрывать два правила (кроме реально общих).

    Краснеет от копипасты записи `П1 → тест`, `П2 → тот же тест`: один тест,
    объявленный двумя правилами, означает, что второе правило НЕ проверено
    своим утверждением. Дубль по паре «файл + имя» — всегда ошибка реестра.
    """
    seen: dict[tuple[str, str], str] = {}
    duplicates = []
    for rule, value in RULES.items():
        if value in seen:
            duplicates.append(f"{rule} и {seen[value]} → {value[1]}")
        seen[value] = rule
    assert duplicates == [], f"один тест на два правила: {duplicates}"


@pytest.mark.parametrize("rule", sorted(ALL_RULES))
def test_each_rule_has_its_own_test(rule: str) -> None:
    """AC-1: покомандная проверка — по одному падению на правило, не пачка."""
    assert rule in RULES, f"правило {rule} не внесено в реестр"
    path, name = RULES[rule]
    assert (API_ROOT / path).is_file(), f"{rule}: нет файла {path}"
    node = _function(path, name)
    assert node is not None, f"{rule}: нет теста {name} в {path}"
    docstring = ast.get_docstring(node) or ""
    assert _carries_marker(docstring, rule), f"{rule}: нет метки в докстринге {name}"
