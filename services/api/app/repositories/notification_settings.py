"""Репозиторий настроек канала уведомлений и ЕДИНЫЙ строитель gate-предиката.

(стори 9.3; AC-01, AC-02; D-9.3-6)

**Почему gate — функция, а не строка в четырёх местах.** Точек вставки
уведомлений четыре (`notification_service.create_one`, 7.3-пакетник,
8.2-пакетник, 8.4-вставка). Копия предиката разошлась бы при первой правке
(класс дефекта 4.1): в одном месте забыли `user_id` — и отключение одного
человека заглушило бы уведомления ВСЕМ (мутация M13). Функция делает состав
предиката единственным по построению: изменить его можно только здесь.

**Почему gate живёт внутри `INSERT`, а не отдельным запросом.** Три из
четырёх путей — пакетные `INSERT ... SELECT`, где адресат производный от
`attendance`/`join_requests`. Чтение настроек в Python перед вставкой открыло
бы окно гонки: пользователь отключает тип между чтением и вставкой — и
уведомление уходит вопреки настройке (класс дефекта R6 стори 7.3).
`NOT EXISTS` разрешается в той же транзакции и в том же плане запроса.

**Хранится ОТКЛЮЧЁННОЕ** (D-9.3-3): строка есть → тип выключен. Пустая
таблица = «всё включено», поэтому новый пользователь не требует ни одной
строки, а новый тип автоматически включён.

**Идемпотентность** (D-9.3-12, §18.11): `ON CONFLICT DO NOTHING` на
`set_muted` и `DELETE` на `set_unmuted`. Повторный `PATCH` с тем же `enabled`
не даёт `500` и не плодит строк: `PRIMARY KEY (user_id, type)` — единственная
защита от дубля, а не предварительный `SELECT`."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["NotificationSettingsRepository", "mute_gate_sql"]


#: AC-01, D-9.3-6: `ON CONFLICT DO NOTHING` — повтор не 500 (§18.11).
#: Строка создаётся только при первом отключении типа; второе — no-op.
_MUTE_SQL = """
INSERT INTO notification_mutes (user_id, type)
VALUES (:user_id, :type)
ON CONFLICT (user_id, type) DO NOTHING
"""

#: AC-01, D-9.3-12: повторное включение уже включённого — no-op, не ошибка.
_UNMUTE_SQL = """
DELETE FROM notification_mutes WHERE user_id = :user_id AND type = :type
"""

#: AC-02: перечень отключённых типов пользователя. Один запрос на рендер
#: экрана настроек: 11 типов собираются в Python поверх этого множества.
_LIST_MUTED_SQL = """
SELECT m.type FROM notification_mutes m WHERE m.user_id = :user_id
"""


def mute_gate_sql(user_column: str, type_expression: str) -> str:
    """AC-01, D-9.3-6: предикат «тип НЕ отключён этим пользователем».

    Возвращает SQL-фрагмент, который вставляется в `WHERE` вставки:

        NOT EXISTS (SELECT 1 FROM notification_mutes m
                     WHERE m.user_id = <user_column>
                       AND m.type   = <type_expression>)

    **Почему `NOT EXISTS`, а не `LEFT JOIN ... IS NULL`.** `EXISTS`
    останавливается на первой найденной строке и не размножает вставляемые
    строки: `JOIN` по настройкам дал бы декартово произведение при нескольких
    отключённых типах, а `LEFT JOIN` без `DISTINCT` — дубли уведомлений.
    Ошибка была бы тихой: пользователь получил бы два одинаковых уведомления.

    **Почему `user_column` и `type_expression` — строки.** Четыре точки вставки
    адресуют пользователя по-разному (алиас `target.user_id` в пакетных
    `INSERT ... SELECT`, параметр `:user_id` в одиночной вставке), и попытка
    привести их к одному виду потребовала бы переписать рабочие запросы
    ради формы. Строки — минимум, который делает предикат ОДНИМ.

    **Обе колонки обязательны.** `m.user_id = <user_column>` без
    `m.type = <type_expression>` отключил бы ВСЕ типы человеку, отключившему
    один (M13); без `user_id` — заглушил бы тип ВСЕМ пользователям. Тест
    сверки текста предиката (§18.2, Task 6.1) проверяет наличие обоих условий:
    инверсия любого из них делает его красным.
    """
    return (
        "NOT EXISTS (SELECT 1 FROM notification_mutes m "
        f"WHERE m.user_id = {user_column} AND m.type = {type_expression})"
    )


class NotificationSettingsRepository:
    """Настройки канала: чтение отключённых типов и их идемпотентная запись."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_muted(self, *, user_id: int) -> frozenset[str]:
        """AC-02: типы, отключённые пользователем. Пустое множество — «всё включено».

        Возвращается `frozenset`, а не список: `enabled` для типа вычисляется
        проверкой вхождения, и порядок здесь не значит ничего (порядок 11 типов
        задаёт `NOTIFICATION_TYPES`, а не БД).
        """
        result = await self._session.execute(text(_LIST_MUTED_SQL), {"user_id": user_id})
        return frozenset(str(row[0]) for row in result.all())

    async def set_muted(self, *, user_id: int, type: str) -> None:
        """AC-01: отключает тип. Повторный вызов — no-op (`ON CONFLICT DO NOTHING`).

        Неотключаемые типы отвергает `ck_notification_mutes_required` в БД
        (D-9.3-5): гард API проверяется ПЕРВЫМ, но схема — единственная защита
        от прямой вставки мимо API.
        """
        await self._session.execute(text(_MUTE_SQL), {"user_id": user_id, "type": type})

    async def set_unmuted(self, *, user_id: int, type: str) -> None:
        """AC-01: включает тип обратно. `DELETE` уже идемпотентен (§18.11).

        `rowcount` НЕ проверяется: включение уже включённого типа — нормальный
        исход повторного `PATCH`, а не ошибка «нечего включать».
        """
        await self._session.execute(text(_UNMUTE_SQL), {"user_id": user_id, "type": type})
