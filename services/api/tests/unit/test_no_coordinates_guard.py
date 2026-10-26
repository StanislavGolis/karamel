"""Гарды на исходники: модели, миграции, контракт (Tasks 2–5; AC-03, AC-04).

Четыре теста, ни один не требует `DATABASE_URL`. Это главное свойство файла:
`information_schema` (см. `tests/integration/test_no_coordinates_schema.py`)
видит только ПРИМЕНЁННУЮ схему, поэтому в CI без базы гард схемы молча
скипается, а класс дефекта «модель есть, миграции нет» (ретро эпика 3,
`ProfilePhoto`) не ловится вообще ничем. Здесь проверяются ИСХОДНИКИ:
`app/models/*.py`, `alembic/versions/*.py` и `app.openapi()`.

Почему `ast`, а не импорт моделей: импорт тянет `app.core.config`, которому
нужен `DATABASE_URL`, — тест стал бы skip ровно там, где он нужен. `ast` —
stdlib, работает по тексту файла, база и сеть не требуются.

Что НЕ проверяется и почему (D-10.3-7): `PILOT_CITY_BBOX` в `.env.example` —
прямоугольник ГОРОДА, одинаков для всех и публично известен; человека он не
идентифицирует. `geo_service.locate_city` принимает `lat`/`lng` аргументами
и ни в БД, ни в лог их не пишет.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from app.main import app

# Набор импортируется, а не копируется: расхождение двух списков и было
# причиной Н-2 (наборы 3/4/6 имён в пяти гардах). Проверка равенства —
# `test_forbidden_names_match_integration_file` ниже.
from tests.integration.test_no_coordinates_schema import FORBIDDEN_COLUMN_NAMES

API_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = API_ROOT / "app" / "models"
VERSIONS_DIR = API_ROOT / "alembic" / "versions"

# D-10.3-5, правило 2: типы PostGIS хранят ту же точность, что пара `numeric`,
# и имеют безобидное имя колонки (`location geometry`). Ловятся типом.
_POSTGIS_TYPE_TOKENS = ("geometry", "geography", "geoalchemy2")

# P-4 CR 10.3: имя колонки из сырого DDL. `ADD COLUMN` и `ADD` без `COLUMN` —
# оба валидны в PostgreSQL (`ADD [COLUMN] имя тип`), поэтому `COLUMN` опционален.
# `IF NOT EXISTS` между ними допускается.
_ADD_COLUMN_RE = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\"?\[?`?(\w+)",
    re.IGNORECASE,
)


def _call_name(node: ast.expr) -> str | None:
    """Имя вызываемого объекта: `sa.Column` → `Column`, `mapped_column` → оно."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _string_arg(node: ast.expr) -> str | None:
    """Первый строковый литерал в узле — имя колонки."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _postgis_tokens_in(node: ast.AST) -> list[str]:
    """Токены PostGIS-типов внутри поддерева (P-3, P-6 CR 10.3).

    Проверяются только `ast.Name`/`ast.Attribute` (имена классов и модулей:
    `Geometry`, `geoalchemy2.Geometry`) и строковые литералы в позиции ТИПА.
    Докстринги и комментарии в поддерево не попадают, потому что вызов
    ограничивает обход аргументами `Column`/`mapped_column`, а не файлом.

    Раньше тип искался по ВСЕМ строковым литералам файла, и миграция с текстом
    «эта миграция НЕ использует geometry» краснела на легитимном коде — гард,
    падающий на честной правке, отключают (находка P-3).
    """
    tokens: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            token = child.id
        elif isinstance(child, ast.Attribute):
            token = child.attr
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            token = child.value
        else:
            continue
        lowered = token.lower()
        if any(postgis in lowered for postgis in _POSTGIS_TYPE_TOKENS):
            tokens.append(token)
    return tokens


def _raw_sql_statements(tree: ast.AST) -> list[tuple[int, str]]:
    """Строковые аргументы `op.execute(...)` — сырой SQL миграции (P-4 CR 10.3).

    Первый позиционный аргумент, если это строковый литерал (в т.ч. f-строка
    собирается из констант). `op.execute(some_constant)` не раскрывается:
    значение константы лежит в другой области видимости, статический анализ
    без исполнения модуля его не получит — это долг W-10.3-b, не дыра здесь.
    """
    statements: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) != "execute":
            continue
        if not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            statements.append((node.lineno, argument.value))
    return statements


def _added_columns_in_sql(sql: str) -> list[str]:
    """Имена колонок из `ADD COLUMN <имя>` сырого SQL (P-4 CR 10.3).

    Разбирается регулярным выражением, а не полноценным парсером SQL: задача —
    найти запрещённые имена и типы, а не понять произвольный DDL. Регистр
    нормализуется: `ADD COLUMN LAT` и `add column lat` — одно и то же.
    Кавычки снимаются: `ADD COLUMN "lat"` — тоже координата.
    """
    found: list[str] = []
    for match in _ADD_COLUMN_RE.finditer(sql):
        name = match.group(1).strip('"`[]')
        lowered = name.lower()
        if lowered in FORBIDDEN_COLUMN_NAMES or any(
            postgis in sql[match.start() : match.end()].lower() for postgis in _POSTGIS_TYPE_TOKENS
        ):
            found.append(name)
    return found


def _iter_column_definitions(tree: ast.AST) -> list[tuple[int, str, ast.Call]]:
    """Все объявления колонок: `sa.Column("имя", …)` и `mapped_column(…)`.

    Возвращает `(строка, имя_колонки, узел_вызова)`. Для `mapped_column` имя
    берётся из имени атрибута (`lat: Mapped[float] = mapped_column(...)`),
    поэтому имя передаётся отдельно в `_column_name_for_annotation`.
    """
    found: list[tuple[int, str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name not in {"Column", "mapped_column"}:
            continue
        column_name = _string_arg(node.args[0]) if node.args else None
        if column_name is None and node.keywords:
            for keyword in node.keywords:
                if keyword.arg == "name":
                    column_name = _string_arg(keyword.value)
        if column_name is None:
            # `mapped_column()` без имени — имя приходит из аннотации;
            # подставляем пустую строку, нарушение найдётся через AnnAssign.
            column_name = ""
        found.append((node.lineno, column_name, node))
    return found


def _scan_model_source(filename: str, source: str) -> list[str]:
    """Нарушения в ОДНОМ исходнике модели: по имени и по типу колонки.

    Вынесено из теста, чтобы проверку можно было испытать на синтетическом
    исходнике: гард, который нельзя покрасить в тесте, нельзя и отличить
    от отключённого (P-3, P-6 CR 10.3).
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id.lower() in FORBIDDEN_COLUMN_NAMES:
            violations.append(f"{filename}:{node.lineno} → {target.id}")

    for lineno, column_name, call in _iter_column_definitions(tree):
        if column_name and column_name.lower() in FORBIDDEN_COLUMN_NAMES:
            violations.append(f"{filename}:{lineno} → {column_name}")
        for token in _postgis_tokens_in(call):
            violations.append(
                f"{filename}:{lineno} → {column_name or '<из аннотации>'} (тип {token})"
            )
    return violations


def _scan_migration_source(filename: str, source: str) -> tuple[list[str], list[str], list[str]]:
    """Нарушения в ОДНОМ исходнике миграции: `(по имени, по типу, сырой SQL)`.

    AST, а не поиск по тексту файла: слово `geometry` в докстринге — не
    координата (P-3 CR 10.3). Сырой SQL — отдельный канал (P-4).
    """
    by_name: list[str] = []
    by_type: list[str] = []
    by_raw_sql: list[str] = []

    tree = ast.parse(source, filename=filename)
    for lineno, column_name, call in _iter_column_definitions(tree):
        if column_name and column_name.lower() in FORBIDDEN_COLUMN_NAMES:
            by_name.append(f"{filename}:{lineno} → {column_name}")
        for token in _postgis_tokens_in(call):
            by_type.append(f"{filename}:{lineno} → {token}")

    for lineno, sql in _raw_sql_statements(tree):
        for column_name in _added_columns_in_sql(sql):
            by_raw_sql.append(f"{filename}:{lineno} → ADD COLUMN {column_name}")
        lowered_sql = sql.lower()
        for postgis in _POSTGIS_TYPE_TOKENS:
            if postgis in lowered_sql:
                by_raw_sql.append(f"{filename}:{lineno} → тип {postgis} в сыром SQL")
                break
    return by_name, by_type, by_raw_sql


def _scan_models_dir(directory: Path) -> list[str]:
    """Нарушения по всем моделям каталога; пустой каталог — ошибка (P-5 CR 10.3).

    `assert scanned` стоит ВНУТРИ обхода намеренно: без него неверный путь
    (переименованный пакет, другой корень) даёт 0 файлов, 0 нарушений и
    зелёный тест — гард слепой, но отчитывается «нарушений нет». Вынесено
    в функцию, чтобы это можно было доказать тестом на пустом каталоге.
    """
    violations: list[str] = []
    scanned: list[str] = []

    for path in sorted(directory.rglob("*.py")):
        scanned.append(path.name)
        violations.extend(_scan_model_source(path.name, path.read_text(encoding="utf-8")))

    assert scanned, (
        f"не найдено ни одного файла моделей в {directory} — проверка слепа "
        "(P-5 CR 10.3): переименование пакета отключает гард молча"
    )
    return violations


def _scan_versions_dir(directory: Path) -> tuple[list[str], list[str], list[str]]:
    """Нарушения по всем миграциям каталога; пустой каталог — ошибка (P-5).

    Файлы без слова `revision` пропускаются: это вспомогательные модули
    пакета, а не миграции. Если таких окажется ВСЕ — `scanned` пуст и
    `assert` краснеет, а не молчит.
    """
    by_name: list[str] = []
    by_type: list[str] = []
    by_raw_sql: list[str] = []
    scanned: list[str] = []

    for path in sorted(directory.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "revision" not in source:
            continue
        scanned.append(path.name)
        n, t, r = _scan_migration_source(path.name, source)
        by_name.extend(n)
        by_type.extend(t)
        by_raw_sql.extend(r)

    assert scanned, (
        f"не найдено ни одного файла миграций в {directory} — проверка слепа (P-5 CR 10.3)"
    )
    return by_name, by_type, by_raw_sql


def test_no_coordinate_columns_in_models() -> None:
    """AC-01, D-10.3-4: в моделях нет колонок координат, даже без миграции.

    Ловит класс дефекта «модель есть, миграции нет»: `lat: Mapped[float]`
    в `place.py` не попадёт в `information_schema` (миграция не создана),
    но означает намерение хранить координаты и рано или поздно станет
    колонкой. Проверка по исходнику видит намерение ДО миграции.

    §18.1: краснеет при добавлении атрибута. Мутация M-2 — ровно это.
    Файлы `__pycache__` не берутся: `rglob("*.py")` их пропускает по имени.
    """
    # P-5 CR 10.3: `_scan_models_dir` сама падает на пустом каталоге —
    # проверено `test_empty_catalog_is_not_silently_green_p5` ниже.
    violations = _scan_models_dir(MODELS_DIR)

    assert not violations, (
        "NFR34: в моделях объявлены колонки координат — "
        f"{'; '.join(sorted(violations))}. Колонка в модели без миграции — "
        "намерение хранить точные координаты; место встречи хранится "
        "названием, категорией и адресом текстом (FR67)."
    )


def test_no_coordinate_columns_or_postgis_types_in_migrations() -> None:
    """AC-04, D-10.3-5: исходники миграций не вводят координаты — двумя правилами.

    Правило 1 (ИМЯ): `sa.Column("lat", sa.Numeric)` и
    `op.add_column(…, sa.Column("lat", …))`. Краснеет, если координата
    приходит безобидным типом `numeric` — сценарий мутации M-1.

    Правило 2 (ТИП): любой узел с `Geometry`/`GEOGRAPHY`/`geoalchemy2`.
    Краснеет, если PostGIS-тип приходит безобидным именем
    (`location geometry`): такое имя правило 1 не ловит.

    Два правила в одном тесте, потому что проверяют ОДИН артефакт и одну
    гарантию AC-04: миграция не вводит хранение координат. Разделение дало бы
    пятый тест и лишний прогон того же обхода; сообщения различают правило.

    §18.1: краснеет при добавлении файла. Проверка идёт по исходнику,
    а не по применённой схеме: миграция может быть написана и не применена.
    ``__pycache__`` не попадает в `glob("*.py")`; файлы без `revision`
    пропускаются — это не миграции, а вспомогательные модули пакета.
    """
    # P-5 CR 10.3: `_scan_versions_dir` сама падает на пустом каталоге.
    # AST, а не поиск по тексту файла: слово `geometry` в докстринге —
    # не координата (P-3). Сырой SQL — отдельный канал (P-4).
    (
        violations_by_name,
        violations_by_type,
        violations_by_raw_sql,
    ) = _scan_versions_dir(VERSIONS_DIR)

    assert not violations_by_name, (
        "AC-04, FR59: миграция вводит колонку координат по имени — "
        f"{'; '.join(sorted(violations_by_name))}. Точные координаты "
        "не хранятся; нужен адрес текстом (FR67)."
    )
    assert not violations_by_type, (
        "AC-04, FR59: миграция использует тип PostGIS — "
        f"{'; '.join(sorted(set(violations_by_type)))}. Тип `geometry` хранит "
        "точность координат с точностью до сантиметра независимо от имени "
        "колонки; расширение PostGIS в проекте не устанавливается вовсе."
    )
    assert not violations_by_raw_sql, (
        "AC-04, FR59: сырой SQL миграции добавляет колонку координат — "
        f"{'; '.join(sorted(violations_by_raw_sql))}. `op.execute` обходит "
        "и `sa.Column`, и `op.add_column`; миграция с таким ALTER вводит "
        "хранение координат теми же правами, что и описанная декларативно."
    )


def _collect_forbidden_properties(
    node: Any, schema: dict[str, Any], scope: str, seen: set[str]
) -> list[str]:
    """Рекурсивно собирает свойства-координаты, раскрывая `$ref`.

    Спуск по `$ref` обязателен (D-10.3-3): координата может лежать внутри
    вложенного объекта (`EventCardPlace`) и в самой операции её не видно.
    `seen` защищает от циклов ссылок (`Node → Node`).
    """
    found: list[str] = []

    def visit(value: Any, where: str) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, where)
            return
        if not isinstance(value, dict):
            return

        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                visit(schema["components"]["schemas"].get(name, {}), f"{where}→{name}")

        # P-2 CR 10.3: у параметра операции (`parameters[*]`) имя лежит в ключе
        # `name`, а не в `properties`. Прежний код читал только `properties`,
        # и `[{'name': 'lat', 'in': 'query'}]` давал `[]` — при том что докстринг
        # теста обещал поимку `?lat=…`. Признак параметра: `in` + `name`.
        if isinstance(value.get("in"), str) and isinstance(value.get("name"), str):
            param_name = value["name"]
            if param_name.lower() in FORBIDDEN_COLUMN_NAMES:
                found.append(f"{where}[{param_name}] ({value['in']})")

        properties = value.get("properties")
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                if prop_name.lower() in FORBIDDEN_COLUMN_NAMES:
                    found.append(f"{where}.{prop_name}")
                # D-10.3-3: пара чисел фиксированной длины — единственная причина
                # держать в контракте `[широта, долгота]` без имени.
                if isinstance(prop_schema, dict):
                    items = prop_schema.get("items")
                    if (
                        prop_schema.get("type") == "array"
                        and isinstance(items, dict)
                        and items.get("type") == "number"
                        and prop_schema.get("minItems") == 2
                        and prop_schema.get("maxItems") == 2
                    ):
                        found.append(f"{where}.{prop_name} (array[2] of number)")
                visit(prop_schema, where)

        for key, child in value.items():
            if key in {"properties", "$ref"}:
                continue
            visit(child, where)

    visit(node, scope)
    return found


def test_no_coordinate_fields_in_openapi_responses() -> None:
    """AC-03, D-10.3-3: в ОТВЕТАХ контракта нет полей координат.

    Обход `app.openapi()["paths"]` → операция → `responses[*].content[*].schema`
    и `parameters[*]`, рекурсивно по `$ref`.

    §18.1: краснеет, если поле добавлено в ответ. Мутации: M-3 (`lat: float`
    в `LocateCityResponse`) и M-4 (`location: list[float]` с `minItems=2`).

    `requestBody` НЕ проверяется — сознательное исключение (Н-1):
    `LocateCityRequest.lat/lng/accuracy` в теле `POST /profiles/locate-city`
    легитимны. FR59 запрещает ХРАНИТЬ координаты, не принимать их; ответ
    `LocateCityResponse` = `{city, reason}`, координат в нём нет. Гард,
    проверяющий `requestBody`, покраснел бы на штатном эндпоинте и был бы
    отключён — то есть перестал бы защищать.

    `parameters` проверяются тем же набором: координата в query-параметре
    (`?lat=…&lng=…`) попадает в логи доступа и в историю, то есть хранится.
    """
    schema = app.openapi()
    paths = schema["paths"]

    # Эндпоинт-исключение виден в контракте: если его переименуют, исключение
    # перестанет быть исключением, и ослабленный гард надо пересмотреть.
    assert "/api/v1/profiles/locate-city" in paths, (
        "эндпоинт транзита координат исчез из контракта — пересмотри исключение requestBody"
    )
    assert "LocateCityRequest" in schema["components"]["schemas"]

    violations: list[str] = []
    for path, operations in paths.items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            seen: set[str] = set()
            violations.extend(
                _collect_forbidden_properties(
                    operation.get("responses", {}),
                    schema,
                    f"{method.upper()} {path} response",
                    seen,
                )
            )
            violations.extend(
                _collect_forbidden_properties(
                    operation.get("parameters", []),
                    schema,
                    f"{method.upper()} {path} parameters",
                    seen,
                )
            )

    assert not violations, (
        "AC-03, NFR34: в ответах API есть поля координат — "
        f"{'; '.join(sorted(violations))}. Координата в ответе равносильна "
        "её публикации: клиент кэширует тело, прокси и логи его сохраняют."
    )


def test_forbidden_names_match_integration_file() -> None:
    """D-10.3-6: набор запрещённых имён един — файлов два, список один.

    §18.1: краснеет, если в одном файле добавить имя, а в другом забыть.
    Именно это и было причиной Н-2: пять гардов держали наборы 6/6/6/3/4
    имени, скопированные вручную, и расхождение делало защиту неравномерной.

    Набор выше — тот же объект, что импортирован в этом файле (проверяется
    идентичность значения, не имени): тест страхует от появления второго
    литерала в будущем.
    """
    expected = frozenset(
        {
            "lat",
            "lng",
            "lon",
            "latitude",
            "longitude",
            "coords",
            "coordinates",
            "geo",
            "geom",
            "geography",
            "geometry",
            "point",
        }
    )

    assert expected == FORBIDDEN_COLUMN_NAMES
    assert len(FORBIDDEN_COLUMN_NAMES) == 12


# --- Доказательство исправлений CR 10.3 -----------------------------------
#
# Каждый тест ниже красит САМ ГАРД на синтетическом входе и проверяет текст
# сообщения. Это требование методической находки CR: «красный тест» ≠ «гард
# сработал». Тест на реальном дереве краснеет и от импорта, и от синтаксиса;
# синтетический вход изолирует ровно проверяемое правило.


def test_query_parameter_is_detected_by_name_p2() -> None:
    """P-2 CR 10.3: `?lat=` ловится по ключу `name`, а не `properties`.

    До исправления `[{'name': 'lat', 'in': 'query'}]` давал `[]` — пустой
    список при докстринге, обещавшем обратное. Тест утверждает не «красный»,
    а конкретный элемент: имя параметра и его место (`query`).
    """
    parameters = [
        {"name": "lat", "in": "query", "schema": {"type": "number"}},
        {"name": "lng", "in": "query", "schema": {"type": "number"}},
        {"name": "city", "in": "query", "schema": {"type": "string"}},
    ]
    found = _collect_forbidden_properties(parameters, {"components": {"schemas": {}}}, "X", set())

    assert found == ["X[lat] (query)", "X[lng] (query)"], (
        f"ожидались только координатные параметры, получено: {found}"
    )


def test_migration_docstring_with_geometry_is_green_p3() -> None:
    """P-3 CR 10.3: слово `geometry` в докстринге миграции — не координата.

    До исправления обход `ast.walk(tree)` по всем строковым литералам краснел
    на докстринге «эта миграция НЕ использует PostGIS/geometry». Гард,
    падающий на честном комментарии, отключают — он перестаёт защищать.
    """
    source = '''
"""Эта миграция НЕ использует PostGIS/geometry и geography."""

revision = "0098"


def upgrade() -> None:
    # geometry в комментарии — тоже не координата
    op.add_column("places", sa.Column("address_text", sa.Text()))
'''
    by_name, by_type, by_raw_sql = _scan_migration_source("0098.py", source)

    assert (by_name, by_type, by_raw_sql) == ([], [], []), (
        "докстринг и комментарий не должны попадать в нарушения: "
        f"{by_name} / {by_type} / {by_raw_sql}"
    )


def test_migration_docstring_with_geometry_is_red_under_old_rule() -> None:
    """P-3, контрольная половина: настоящий тип краснеет, докстринг не мешает.

    Без этой пары «зелёный на докстринге» мог бы означать, что правило типа
    сломано целиком и больше ничего не ловит.
    """
    source = '''
"""Миграция, где geometry упомянута и использована."""

revision = "0099"


def upgrade() -> None:
    op.add_column("places", sa.Column("location", Geometry("POINT", srid=4326)))
'''
    by_name, by_type, by_raw_sql = _scan_migration_source("0099.py", source)

    assert by_name == [], "имя `location` не запрещено (FR67: текстовый адрес)"
    assert any("Geometry" in item for item in by_type), (
        f"тип PostGIS обязан краснеть, получено: {by_type}"
    )


def test_raw_alter_execute_is_detected_p4() -> None:
    """P-4 CR 10.3: `op.execute("ALTER TABLE … ADD COLUMN lat …")` ловится.

    До исправления такой ALTER не видел никто: он не `sa.Column` и не
    `op.add_column`. Ревью доказало фактом — 1 passed при реальной колонке.
    """
    source = """
revision = "0100"


def upgrade() -> None:
    op.execute("ALTER TABLE places ADD COLUMN lat numeric(9,6)")
"""
    by_name, by_type, by_raw_sql = _scan_migration_source("0100.py", source)

    assert by_raw_sql == ["0100.py:6 → ADD COLUMN lat"], (
        f"сырой ADD COLUMN lat обязан краснеть, получено: {by_raw_sql}"
    )


def test_raw_sql_without_coordinate_name_is_green_p4() -> None:
    """P-4, контрольная половина: сырой SQL без координат не краснеет.

    В проекте 27 вызовов `op.execute` (индексы, backfill), и ложное
    срабатывание на них отключило бы гард так же, как докстринг в P-3.
    """
    source = """
revision = "0101"


def upgrade() -> None:
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx ON places (name)")
    op.execute("SET LOCAL lock_timeout = '3s'")
"""
    by_name, by_type, by_raw_sql = _scan_migration_source("0101.py", source)

    assert (by_name, by_type, by_raw_sql) == ([], [], []), (
        f"индексы и таймауты — не координаты: {by_raw_sql}"
    )


def test_postgis_type_in_model_is_detected_p6() -> None:
    """P-6 CR 10.3: `mapped_column(Geometry(...))` при безобидном имени ловится.

    Прежний гард моделей смотрел только имена. `shape: Mapped[str]` с
    `mapped_column(Geometry("POINT"))` проходил зелёным, хотя хранит точность
    координат — ровно та же дыра, что `location geometry` в миграции.
    """
    source = """
class Place(Base):
    __tablename__ = "places"
    shape: Mapped[str] = mapped_column(Geometry("POINT", srid=4326))
"""
    violations = _scan_model_source("place.py", source)

    assert violations == ["place.py:4 → <из аннотации> (тип Geometry)"], (
        f"тип PostGIS в модели обязан краснеть, получено: {violations}"
    )


def test_model_with_text_column_is_green_p6() -> None:
    """P-6, контрольная половина: `address_text` и `name` не краснеют.

    Гард, который краснеет на каждой модели, отключают в первый же день.
    """
    source = """
class Place(Base):
    __tablename__ = "places"
    name: Mapped[str] = mapped_column(sa.Text())
    address_text: Mapped[str] = mapped_column(sa.Text())
"""
    violations = _scan_model_source("place.py", source)

    assert violations == [], f"легитимные колонки не координаты: {violations}"


def test_empty_models_dir_raises_instead_of_green_p5(tmp_path: Path) -> None:
    """P-5 CR 10.3: пустой каталог моделей — падение, а не «нарушений нет».

    Проверяется ПОВЕДЕНИЕ гарда на реальном входе: `tmp_path` существует,
    но файлов в нём нет — ровно то, что даёт переименованный пакет или
    неверный корень. До исправления тест был бы зелёным и слепым.

    `pytest.raises` с `match` привязывает падение к нужной строке: без
    `match` сошло бы любое исключение, включая `NameError` в самом тесте
    (методическая находка CR 10.3 — «красный тест ≠ гард сработал»).
    """
    with pytest.raises(AssertionError, match="проверка слепа"):
        _scan_models_dir(tmp_path)


def test_empty_versions_dir_raises_instead_of_green_p5(tmp_path: Path) -> None:
    """P-5 CR 10.3: пустой каталог миграций — падение, а не «нарушений нет».

    Второй хелпер проверяется отдельно: у него свой путь (`glob`, не `rglob`)
    и свой `assert`, скопированный, а не общий — копия могла разойтись.
    """
    with pytest.raises(AssertionError, match="проверка слепа"):
        _scan_versions_dir(tmp_path)
