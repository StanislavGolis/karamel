"""Замок константы `HEAD_REVISION` (долг W-12.4-l, класс W-11.4-e).

**Зачем файл существует.** Константа `tests/conftest.py::HEAD_REVISION` заменяет
16 литералов `"0037"` в 8 файлах интеграционных тестов. Сама по себе замена
убирает дублирование, но создаёт новый риск: при появлении миграции `0038`
(стори 12.6) константу можно **забыть** обновить, и тогда 12 миграционных
ассертов будут сверяться с устаревшим ожиданием — зелень, не проверяющая
ничего.

**Почему константа — литерал, а не `get_heads()`.** Если бы `HEAD_REVISION`
вычислялась из `ScriptDirectory`, ассерты вида `assert _heads() == (HEAD_REVISION,)`
сравнивали бы объект сам с собой: удаление миграции, развилка голов или битый
`down_revision` прошли бы незамеченными (P-25 — гард проверяет форму, а не
эффект). Здесь стороны независимы: слева литерал из `conftest.py`, справа
реальная голова, прочитанная из `alembic/versions/`.

**Что ловит.** Два направления расхождения, оба — дефект:
- константа отстала (голова ушла вперёд) → тесты миграций ждут прошлую ревизию;
- константа угадана/опечатана (голова назад) → ассерты краснеют без причины.

Запускается без `DATABASE_URL`: читаются файлы миграций, БД не нужна.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from tests.conftest import HEAD_REVISION

API_ROOT = Path(__file__).resolve().parents[2]


def _actual_heads() -> tuple[str, ...]:
    """Головы по файлам миграций — независимо от константы."""
    config = AlembicConfig(str(API_ROOT / "alembic.ini"))
    return tuple(ScriptDirectory.from_config(config).get_heads())


def test_head_revision_matches_real_head() -> None:
    """W-12.4-l: константа равна РЕАЛЬНОЙ голове, а не запомненной.

    Краснеет при появлении новой миграции без правки `conftest.py`.
    Сообщение называет оба значения: иначе автор следующей миграции видит
    «ожидалось ('0038',), получено ('0037',)» и не знает, какое из них верно.
    """
    heads = _actual_heads()
    assert heads == (HEAD_REVISION,), (
        f"HEAD_REVISION = {HEAD_REVISION!r} разошлась с реальной головой {heads}. "
        "Обновите tests/conftest.py::HEAD_REVISION — она живёт в одном месте, "
        "миграционные тесты берут её импортом."
    )


def test_head_is_single() -> None:
    """Развилка голов сделала бы `upgrade head` недетерминированным.

    Отдельный тест, а не часть предыдущего: при двух головах предыдущий
    покраснеет с сообщением о расхождении константы, и настоящая причина
    (развилка) в тексте не появится.
    """
    heads = _actual_heads()
    assert len(heads) == 1, f"голов должно быть ровно одна, получено {heads}"


def test_head_revision_is_a_literal() -> None:
    """Константа — строковый литерал, а не вычисление из `ScriptDirectory`.

    Гард от «починки» предыдущего теста вычислимой константой: такая правка
    сделает оба теста зелёными навсегда и отключит их (P-25). Проверяется
    исходник модуля — здесь это допустимо, потому что защищаемое свойство
    само про форму записи, а не про поведение.
    """
    source = (API_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("HEAD_REVISION")
    ]
    assert lines == [f'HEAD_REVISION = "{HEAD_REVISION}"'], (
        "HEAD_REVISION должна быть простой строковой константой в tests/conftest.py, "
        f"получено: {lines}"
    )
