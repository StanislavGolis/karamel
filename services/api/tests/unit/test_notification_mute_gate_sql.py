"""Единый строитель gate-предиката (стори 9.3; Task 2.2, 6.1; D-9.3-6).

**Почему проверяется ТЕКСТ предиката, а не поведение.** Поведение ловят
integration-тесты (4 пути × отключено/включено). Здесь закрывается другой
класс дефекта: предикат, который «работает» на одном пути и разошёлся на
другом. Копия в четырёх местах разошлась бы при первой правке — и самый
дорогой вариант (забытый `user_id`, мутация M13) заглушил бы тип ВСЕМ
пользователям из-за отключения одного.

**Обе колонки обязательны** (§18.2: каждый ассерт отвечает «какая мутация его
покраснит»):

- без `m.user_id = ...` предикат отключил бы тип всем (M13);
- без `m.type = ...` пользователь, отключивший один тип, потерял бы все
  уведомления (инверсия второго условия).

**`NOT EXISTS`, а не `JOIN`.** `JOIN`/`LEFT JOIN` по настройкам размножил бы
вставляемые строки при нескольких отключённых типах; ошибка тихая —
пользователь получил бы дубли уведомлений.
"""

from __future__ import annotations

from app.repositories.notification_settings import mute_gate_sql

#: Параметры, которыми пользуются РЕАЛЬНЫЕ точки вставки (см. вызовы
#: в `repositories/notifications.py`, `repositories/reports.py`,
#: `services/moderation_resolve_service.py`).
_CASES = (
    ("target.user_id", "CAST(:type AS varchar)"),
    ("CAST(:user_id AS bigint)", "CAST(:type AS varchar)"),
)


def test_gate_filters_by_user_and_type() -> None:
    """AC-01: предикат содержит оба условия — по `user_id` И по `type`.

    Краснеет, если из строителя убрать любую половину: без `user_id`
    отключение одного пользователя глушит всех (M13), без `type` — все типы
    одного пользователя.
    """
    for user_column, type_expression in _CASES:
        sql = mute_gate_sql(user_column, type_expression)
        assert f"m.user_id = {user_column}" in sql
        assert f"m.type = {type_expression}" in sql


def test_gate_is_not_exists_over_mutes_table() -> None:
    """AC-01: форма — `NOT EXISTS (SELECT 1 FROM notification_mutes m ...)`.

    Проверяется и таблица, и отрицание: `EXISTS` без `NOT` инвертировал бы
    смысл (уведомления приходили бы ТОЛЬКО отключённым типам — тихая ошибка,
    которую заметил бы не каждый тест на «строку не создали»).
    """
    sql = mute_gate_sql("target.user_id", "CAST(:type AS varchar)")
    assert sql.startswith("NOT EXISTS (SELECT 1 FROM notification_mutes m ")
    assert sql.count("NOT EXISTS") == 1


def test_all_four_insert_paths_use_the_builder() -> None:
    """AC-01, D-9.3-6: все четыре точки вставки зовут СТРОИТЕЛЯ, не копию.

    Краснеет, если в одном файле предикат написан руками: копия разошлась бы
    при следующей правке, и расхождение не заметил бы ни один поведенческий
    тест — все четыре пути работали бы «правильно», но по-разному.

    Проверка по импорту, а не по тексту SQL: текст каждого пути уже покрыт
    поведенческими тестами, а здесь важно именно то, что источник ОДИН.
    """
    from pathlib import Path

    api_root = Path(__file__).resolve().parents[2]
    sources = (
        api_root / "app" / "repositories" / "notifications.py",
        api_root / "app" / "repositories" / "reports.py",
        api_root / "app" / "services" / "moderation_resolve_service.py",
    )
    for path in sources:
        text_body = path.read_text(encoding="utf-8")
        assert "mute_gate_sql" in text_body, f"{path} не использует строитель gate"
        assert "FROM notification_mutes" not in text_body.replace(
            "SELECT 1 FROM notification_mutes m ", ""
        ), f"{path} содержит собственную копию предиката"


def test_every_insert_sql_embeds_the_gate() -> None:
    """AC-01, M1–M4: gate встроен в текст КАЖДОЙ из четырёх вставок.

    **Зачем текстовый ассерт, если есть поведенческие тесты.** Путь 8.4
    (`moderation_resolve_service`) пишет ТОЛЬКО неотключаемые типы
    (`report.resolved`, `sanction.issued`), и gate там сегодня поведенчески
    невидим: снятие предиката не меняет ни одного наблюдаемого исхода.
    Мутация M4 поэтому SURVIVED бы на любом поведенческом наборе.

    Гейт там нужен не «на всякий случай»: 8.4 — единственный путь, который
    пишет типы, решение об обязательности которых принимает продукт. Если
    `report.resolved` когда-нибудь станет отключаемым (или 8.4 начнёт писать
    ещё какой-то тип), отсутствие gate означало бы тихую доставку вопреки
    настройке — и обнаружилось бы это жалобой «мне приходит то, что я
    выключил».

    Проверка по ТЕКСТУ модулей-источников: SQL собирается f-строкой при
    импорте, и `mute_gate_sql` в тексте модуля обязан присутствовать в теле
    каждой константы.
    """
    from pathlib import Path

    api_root = Path(__file__).resolve().parents[2]
    expected = {
        api_root / "app" / "repositories" / "notifications.py": 2,
        api_root / "app" / "repositories" / "reports.py": 1,
        api_root / "app" / "services" / "moderation_resolve_service.py": 1,
    }
    for path, calls in expected.items():
        body = path.read_text(encoding="utf-8")
        # Импорт проверяется ОТДЕЛЬНО: он без скобки, и общий счёт
        # `mute_gate_sql(` его не видит. Смешивать два условия в одном
        # счётчике значило бы гадать, чего не хватает при падении.
        assert "from app.repositories.notification_settings import mute_gate_sql" in body, (
            f"{path.name}: строитель gate не импортирован"
        )
        assert body.count("mute_gate_sql(") == calls, (
            f"{path.name}: ожидалось {calls} встроенных gate, "
            f"найдено {body.count('mute_gate_sql(')}"
        )


#: Импорт ниже намеренно ПОСЛЕ тестов: он проверяет фактические константы,
#: а не пересказ. `_CREATE_ONE_SQL` и `_CREATE_ATTENDANCE_REQUESTS_SQL` —
#: приватные, но тест модуля вправе их читать (белый ящик для SQL-контракта).
def test_rendered_sql_contains_not_exists() -> None:
    """AC-01, M1/M2: собранный SQL содержит `NOT EXISTS` и обе колонки.

    Проверка на ОТРЕНДЕРЕННОЙ строке: если f-строка забыта (обычная строка
    без `f`), в тексте остались бы литералы `{mute_gate_sql(...)}` — запрос
    упал бы на синтаксисе, но текст-тест выше этого не заметил бы.
    """
    from app.repositories.notifications import (
        _CREATE_ATTENDANCE_REQUESTS_SQL,
        _CREATE_ONE_SQL,
    )

    for sql in (_CREATE_ONE_SQL, _CREATE_ATTENDANCE_REQUESTS_SQL):
        assert "NOT EXISTS (SELECT 1 FROM notification_mutes m" in sql
        assert "m.user_id =" in sql
        assert "m.type =" in sql
        assert "{mute_gate_sql" not in sql, "f-строка не отрендерена"
