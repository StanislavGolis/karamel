"""Детерминированные правила автопроверки карточки (стори 4.3, Task 1).

AC-01, AC-02, FR101, FR91, ADR-08.

ADR-08: «Детерминированные правила (длина, стоп-слова, категория из белого
списка, отсутствие ссылок) переводят `pending → approved` синхронно при
создании. В `pending` остаются только сработавшие правила».

Почему чистая функция (D-4.3-1, D-4.3-6):

- результат воспроизводим, поэтому `auto_check` в очереди модератора
  пересчитывается на ЧТЕНИИ и не требует колонки под `failed_rules`. Кэш
  разошёлся бы с текстом после `PATCH` и потребовал бы инвалидации;
- правила тестируются без БД и без HTTP: каждая ветка — прямой вызов.

Порядок правил в `FAILED_RULES` — часть контракта (AC-02): клиент и тесты
сравнивают кортеж целиком, а не множество. Порядок задан списком правил ниже
(длина → категория → стоп-слова → ссылки → регистр → повторы): от самого
дешёвого и очевидного к эвристикам.

Правила НЕ выносят решение «отклонить»: автоматического `rejected` нет
(OQ-2), потому что FR113 требует причину от человека, а NFR54 — санкцию
человеком.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import PlaceCategory
from app.services.profile_normalize import normalize_bio, normalize_text

# AC-02, FR69: те же пределы, что в схеме (`schemas/event_card.py`) и в колонках
# (`event_card.title` 100, `description` Text). Импортировать схему нельзя:
# `schemas` → `models` → цикла нет, но `services` не должен зависеть от Pydantic-
# контракта HTTP; предел здесь — доменное правило, а не валидация формы.
TITLE_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 2000

# AC-02: порог правила `caps`. При длине меньше порога доля заглавных
# недостоверна («Питер» — 2 из 5 = 40 %, «КИНО» — 4 из 4 = 100 %).
CAPS_MIN_LENGTH = 20
CAPS_MAX_RATIO = 0.6

# AC-02: сколько раз подряд должен повториться символ или токен.
# 5 выбрано по живому примеру спама «Привет!!!!!» и «свидание свидание
# свидание»: 3–4 повтора встречаются в нормальной разговорной речи.
REPEAT_MIN_COUNT = 5

# AC-02, D-4.3-2: ссылки и `@`-упоминания. Один шаблон на все формы: домен
# с `www.`, схемы `http(s)://`, телеграм-инвайт и `@nick`. Регистр не важен.
# CR 4.3, патч 3 (AC-02, D-4.3-2): в `@`-упоминании допускается кириллица
# и точка. Прежний шаблон `@[a-z0-9_]{3,}` ловил только латиницу, и «пиши
# @никнейм» проходил как чистый текст — то есть правило, задуманное против
# увода общения из продукта (NFR41), не работало для русскоязычного пилота.
_LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@[\w.]{3,})", re.IGNORECASE | re.UNICODE)

# AC-02: повтор подряд. Для символов — `(.)\1{4,}`; для токенов — то же
# регулярное выражение на уровне целых слов (проверяется отдельно ниже).
_REPEAT_CHAR_RE = re.compile(rf"(.)\1{{{REPEAT_MIN_COUNT - 1},}}")
_REPEAT_WORD_RE = re.compile(rf"\b(\w{{2,}})(?:\s+\1\b){{{REPEAT_MIN_COUNT - 1},}}", re.IGNORECASE)

# AC-02: имена правил — закрытый набор. Порядок = порядок срабатывания
# в `failed_rules` (контракт AC-02, сравнивается кортежем).
RULES: tuple[str, ...] = ("length", "category", "stop_word", "link", "caps", "repeat")


@dataclass(frozen=True, slots=True)
class ContentCheckResult:
    """AC-01, AC-02: итог проверки без решения о модерации.

    `passed=True` означает «правила не сработали» → сервис ставит `approved`.
    `passed=False` → карточка остаётся `pending` и попадает в очередь.
    `failed_rules` — кортеж в порядке `RULES`, без дубликатов.
    """

    passed: bool
    failed_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CardContent:
    """AC-02: текст и категория — вход правила; отдельный тип для читаемости."""

    title: str
    description: str | None
    category: str


def evaluate_card_content(
    content: CardContent, *, stop_words: tuple[str, ...]
) -> ContentCheckResult:
    """AC-01, AC-02: применяет все правила и возвращает итог.

    Нормализация — та же, что при сохранении (`normalize_text` для заголовка,
    `normalize_bio` для описания): иначе стоп-слово с NBSP/uppercase прошло бы
    проверку, а в БД легло бы канонизированным (`schemas/event_card.py`).

    Стоп-слова тоже нормализуются здесь, а не только в конфиге: функция
    вызывается и воркером/скриптом, где `Settings` могла быть собрана
    с ненормализованным кортежем (D-4.3-1: значения — данные).
    """
    title = normalize_text(content.title)
    description = normalize_bio(content.description) if content.description else ""
    haystack = f"{title}\n{description}".strip()

    failed: list[str] = []

    # 1. `length`: длина после нормализации. NFC-нормализация может УДЛИНИТЬ
    # строку (1 кодовая точка → 3), и переполнение `varchar(100)` дало бы
    # `DBAPIError`, а не `IntegrityError` (урок CR 3.1).
    if not title or len(title) > TITLE_MAX_LENGTH or len(description) > DESCRIPTION_MAX_LENGTH:
        failed.append("length")

    # 2. `category`: значение из белого списка ADR-08. `PlaceCategory`
    # отбивает HTTP-вход в схеме, но воркер/импорт идут мимо Pydantic.
    try:
        PlaceCategory(content.category)
    except ValueError:
        failed.append("category")

    # 3. `stop_word`: подстрока в нормализованном тексте. Приведение к нижнему
    # регистру — с обеих сторон: «Казино» и «КАЗИНО» неотличимы для читателя.
    lowered = haystack.casefold()
    normalized_stop_words = tuple(
        word for word in (normalize_text(w).casefold() for w in stop_words) if word
    )
    if any(word in lowered for word in normalized_stop_words):
        failed.append("stop_word")

    # 4. `link`: схемы, `www.`, телеграм-инвайт, `@nick` (D-4.3-2).
    if _LINK_RE.search(haystack):
        failed.append("link")

    # 5. `caps`: доля заглавных букв среди БУКВ (пробелы/цифры не учитываются —
    # иначе «КИНО 18 2026» проходило бы порог).
    letters = [char for char in haystack if char.isalpha()]
    if len(letters) >= CAPS_MIN_LENGTH:
        uppercase = sum(1 for char in letters if char.isupper())
        if uppercase / len(letters) > CAPS_MAX_RATIO:
            failed.append("caps")

    # 6. `repeat`: один символ или одно слово повторено подряд.
    if _REPEAT_CHAR_RE.search(haystack) or _REPEAT_WORD_RE.search(haystack):
        failed.append("repeat")

    # Порядок `failed` соответствует порядку проверок = порядку `RULES` (AC-02).
    return ContentCheckResult(passed=not failed, failed_rules=tuple(failed))
