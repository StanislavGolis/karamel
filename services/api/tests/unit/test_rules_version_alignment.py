"""Гард: версия правил в API совпадает с версией текста правил во фронтенде.

Стори 11.4 (AC-3, AC-6, NFR36).

**Зачем тест.** Источник истины версии — `MODERATION_RULES_VERSION` в
`apps/web/src/lib/moderation-rules.ts`: текст правил живёт в коде фронтенда
осознанно (D-10.4-1 — правила обязаны открываться до регистрации при любом
состоянии бэкенда). API не может импортировать TS-константу, поэтому значение
дублируется в `Settings.moderation_rules_version`.

Дублирование без гарда — это дефект отложенного действия: при бампе версии
правил один из двух литералов забывается, и пользователь «принимает» редакцию,
которой не читал, а владелец видит в карточке версию, которой нет на странице
правил. Тест читает TS-файл **как текст** — без node, npm и сборки Next.

Краснеет при правке любой из двух констант без второй.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import Settings

#: Корень репозитория от этого файла: `tests/unit/<файл>` → `services/api` → `services` → `karamel`.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: Файл-источник версии правил во фронтенде (D-10.4-1).
RULES_TS = REPO_ROOT / "apps" / "web" / "src" / "lib" / "moderation-rules.ts"

#: `export const MODERATION_RULES_VERSION = "1.0";`
_VERSION_RE = re.compile(r'MODERATION_RULES_VERSION\s*=\s*"([^"]+)"')


pytestmark = pytest.mark.skipif(
    not RULES_TS.is_file(),
    reason=(
        "нет apps/web/src/lib/moderation-rules.ts: в образе `api` копируется только "
        "`services/api`, и гард версии там неприменим (CR 11.4, P13). На хосте "
        "монорепо и в CI файл есть — тест работает"
    ),
)


def _frontend_version() -> str:
    """Извлекает версию правил из TS-константы.

    Читается именно исходник, а не сгенерированный артефакт: сгенерированного
    значения не существует, а `next build` в pytest не поднимается.
    """
    assert RULES_TS.is_file(), f"не найден источник версии правил: {RULES_TS}"
    source = RULES_TS.read_text(encoding="utf-8")
    match = _VERSION_RE.search(source)
    assert match is not None, (
        f'в {RULES_TS} нет `MODERATION_RULES_VERSION = "..."`: '
        "гард версии ослеп, а не «всё в порядке»"
    )
    return match.group(1)


def test_api_version_matches_frontend_rules_version() -> None:
    """AC-3.2: `Settings.moderation_rules_version` == версия в `moderation-rules.ts`.

    Краснеет, если бампнули версию правил (TS + хеш-гард), но забыли
    `MODERATION_RULES_VERSION` в `.env`/`config.py`. Тогда `POST /consent`
    записывает старую версию при новом тексте — то есть NFR36 фиксирует
    факт, которого не было.
    """
    api_version = Settings(_env_file=None).moderation_rules_version
    frontend_version = _frontend_version()

    assert api_version == frontend_version, (
        "Версия правил разошлась: "
        f"API={api_version!r}, фронтенд={frontend_version!r}. "
        "Обновите ОБЕ константы: `apps/web/src/lib/moderation-rules.ts` "
        "и `MODERATION_RULES_VERSION` в `.env`/`app/core/config.py`."
    )


def test_frontend_version_matches_declared_format() -> None:
    """AC-3: версия фронтенда проходит валидатор API-настройки.

    Краснеет, если во фронтенде появится `1.0-beta`: тогда дефолт API
    обязан измениться вместе с форматом, а не молча разойтись с валидатором.
    """
    frontend_version = _frontend_version()

    # Валидатор тот же, что на старте API: несовместимый формат — ValidationError.
    assert Settings(
        _env_file=None, moderation_rules_version=frontend_version
    ).moderation_rules_version == (frontend_version)
