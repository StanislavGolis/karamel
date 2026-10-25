"""Репозиторий адресных уведомлений (стори 7.3; AC-04, AC-11, AC-12).

**Один `INSERT ... SELECT` на карточку, не цикл вставок.** Адресаты —
производное от `attendance` (принятые заявки), и считать их в Python значило
бы сделать ещё один `SELECT` перед вставкой: воркер обязан укладываться в
контракт «4 запроса на карточку» (D-TD-4), а между чтением адресатов и
вставкой открылось бы окно, в котором состав компании меняется.

**Gate `EXISTS` живёт в SQL НАМЕРЕННО** (R6, самый дорогой риск TD 7.3).
Самая естественная реализация — `if await ensure_rows(...) > 0:` перед
вставкой — является ДЕФЕКТОМ: `ensure_rows` возвращает `0` в двух разных
состояниях:

1. у карточки нет принятых заявок — уведомлять некого (AC-12);
2. строки `attendance` уже создал ленивый путь 7.2 (`POST /cards/{uuid}/attendance`
   зовёт `ensure_rows` с CR 7.2 H-3) — адресаты ЕСТЬ, и уведомления обязаны уйти.

Различить эти состояния может только SQL, который видит `attendance`.
Поэтому `create_attendance_requests` вызывается ВСЕГДА после `ensure_rows`,
без условий в Python, а решение «есть ли кому слать» принимает `EXISTS`
внутри `INSERT`. Ошибка здесь тихая: маркер встал, уведомлений нет,
повторить нельзя (карточка вышла из выборки).

**`UNION`, а не `UNION ALL`** (AC-04, M5). `UNION` дедуплицирует адресатов:
у автора с тремя парами ровно один флаг на карточку (D-7.3-3), и наивный
проход по парам дал бы ему три идентичных уведомления. `UNION ALL` внутри
одной карточки дублей не даёт только потому, что `attendance` уникальна по
`(card_id, participant_id)`; но `UNION` сохраняет свойство и при смене ключа
(тест `A07`).

**`participant_id <> :author_id`** (AC-04, M6). Без него автор попал бы в
адресаты дважды: как владелец карточки и как «участник» собственной встречи.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.notification_settings import mute_gate_sql

__all__ = ["ATTENDANCE_REQUEST_TYPE", "NotificationsRepository"]

#: AC-10, NFR60: тип события/уведомления запроса подтверждения явки.
#: Одна константа на оба пути (строка в БД и конверт WS): расхождение
#: значений между ними было бы дефектом, который клиент не заметит —
#: он фильтрует по типу и молча проигнорировал бы «незнакомое» событие.
ATTENDANCE_REQUEST_TYPE = "attendance.request"


#: AC-04, AC-12: адресаты запроса — участники принятых заявок ПЛЮС автор.
#:
#: Форма `INSERT ... SELECT` выбрана не ради краткости: она делает адресность
#: свойством одного запроса. Цикл «прочитать пары → вставить по одной» открыл
#: бы окно между чтением и вставкой и добавил бы SQL сверх контракта (D-TD-4).
#:
#: Разбор по частям:
#:
#: - первая ветка `UNION` — участники: `participant_id <> :author_id` отсекает
#:   автора, если он (после подделки или будущей схемы) оказался в `attendance`;
#: - вторая ветка — автор: `EXISTS` пропускает его ТОЛЬКО когда у карточки есть
#:   хотя бы одна пара. Это и есть gate AC-12: карточка без компании не шлёт
#:   автору «подтвердите явку» (он получил бы `409 ATTENDANCE_NO_PAIR` по 7.2);
#: - `UNION` дедуплицирует: три пары → один адрес участника и один адрес автора.
#:   Внешний `SELECT DISTINCT` здесь БЫЛ и убран как мёртвый слой: `UNION`
#:   уже даёт уникальные строки, а вторая ветка — ровно один скаляр автора,
#:   который не может совпасть с участниками (`participant_id <> :author_id`).
#:   Два слоя дедупа делали мутации `UNION → UNION ALL` эквивалентными и
#:   прятали реальную адресность от тестов (обнаружено мутационным прогоном);
#: - `CAST(:payload AS jsonb)` — payload приходит JSON-строкой из сервиса,
#:   собранной ОДИН раз на карточку (Task 3.3, AC-11): побайтово одинаковый
#:   для всех адресатов (тест `A08`).
_CREATE_ATTENDANCE_REQUESTS_SQL = f"""
WITH target AS (
    SELECT a.participant_id AS user_id
      FROM attendance a
     WHERE a.card_id = :card_id
       AND a.participant_id <> :author_id
    UNION
    SELECT CAST(:author_id AS bigint)
     WHERE EXISTS (SELECT 1
                     FROM attendance a2
                    WHERE a2.card_id = :card_id
                      AND a2.participant_id <> :author_id)
), inserted AS (
    INSERT INTO notifications (user_id, type, payload)
    SELECT t.user_id, CAST(:type AS varchar), CAST(:payload AS jsonb)
      FROM target t
     WHERE {mute_gate_sql("t.user_id", "CAST(:type AS varchar)")}
    RETURNING notifications.uuid, notifications.user_id
)
SELECT i.uuid, u.uuid
  FROM inserted i
  JOIN users u ON u.id = i.user_id
"""

#: AC-01, граница эпика 9: единственное ЧТЕНИЕ состояния доставки, доступное
#: в 7.3. Записи `delivered_at`/`read_at` здесь нет — их ставит эпик 9, и
#: тесты 7.3 доказывают только «создано и ждёт доставки».
_COUNT_UNDELIVERED_SQL = """
SELECT count(*) FROM notifications WHERE user_id = :user_id AND delivered_at IS NULL
"""

#: 9.2, Task 2.1: одиночная вставка с возвратом публичного `uuid`.
#:
#: **Почему не пакетный `INSERT ... SELECT` 7.3.** Там адресат — производное
#: от `attendance`, и `uuid` не нужен никому: строки живут до отметки по
#: `chat_uuid`. Здесь адресат ровно один (автор или конкретный заявитель), и
#: `uuid` нужен вызывающему СРАЗУ — чтобы отметить доставку именно этой строки
#: после публикации в шину (D-9.2-5). Второй `SELECT` по
#: `(type, card_uuid)` был бы неоднозначен: у автора может быть несколько
#: уведомлений одного типа по одной карточке (`request.received` × N заявок).
_CREATE_ONE_SQL = f"""
INSERT INTO notifications (user_id, type, payload)
SELECT CAST(:user_id AS bigint), CAST(:type AS varchar), CAST(:payload AS jsonb)
 WHERE {mute_gate_sql("CAST(:user_id AS bigint)", "CAST(:type AS varchar)")}
RETURNING uuid
"""

#: 9.2, Task 2.2: отметка «ушло в шину». `delivered_at IS NULL` в предикате
#: делает операцию идемпотентной (§18.11): повторный вызов не сдвигает время
#: доставки и не нарушает `created_at <= delivered_at`.
_MARK_DELIVERED_SQL = """
UPDATE notifications
   SET delivered_at = now()
 WHERE uuid = :notification_uuid
   AND delivered_at IS NULL
"""


#: 9.4, Task 2.4: пакетная отметка доставки ОДНИМ запросом (§18.11).
#:
#: `uuid = ANY(:uuids)` — пачка помечается одним `UPDATE`, а не циклом: адресатов
#: у карточки может быть много, и N запросов нарушили бы контракт D-TD-4.
#: `delivered_at IS NULL` держит идемпотентность: повтор не сдвигает время
#: доставки и не ломает `created_at <= delivered_at`.
#:
#: **Неуспешные остаются `NULL`** (D-9.2-5): заполненное значение означает
#: «ушло в шину». Пометка всех строк «на всякий случай» соврала бы остальным
#: инстансам — они не получили событие и повтора не будет.
_MARK_DELIVERED_MANY_SQL = """
UPDATE notifications
   SET delivered_at = now()
 WHERE uuid = ANY(CAST(:uuids AS uuid[]))
   AND delivered_at IS NULL
"""


class NotificationsRepository:
    """Запись адресных уведомлений и чтение состояния доставки."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_attendance_requests(
        self, *, card_id: int, author_id: int, payload: str
    ) -> list[tuple[uuid_lib.UUID, uuid_lib.UUID]]:
        """AC-04, AC-12 7.3 + AC-02 9.4: создаёт строки запроса, отдаёт пары.

        Возвращает `[(notification_uuid, user_uuid), ...]` — по одной паре на
        КАЖДУЮ фактически вставленную строку. Форма изменилась в 9.4
        (D-9.4-7) ради доставки в личный канал: FR111 требует адресовать
        событие в `ws:user:{uuid}` и поставить `delivered_at` ИМЕННО этой
        строке. `int rowcount` для этого недостаточен — он не даёт ни `uuid`
        строки, ни адресата.

        **Почему `RETURNING`, а не второй `SELECT`** (D-9.4-7).
        `INSERT ... RETURNING` отдаёт оба идентификатора в том же запросе:
        SQL на карточку остаётся 4 (D-TD-4). Второй `SELECT` по
        `(type, card_uuid)` неоднозначен — у автора может быть несколько
        уведомлений ОДНОГО типа по ОДНОЙ карточке (вывод D-9.2-5) — и открыл
        бы окно между вставкой и чтением.

        **`join users` в самом запросе** (D-9.4-5, приём D-TD-5). Публичный
        `user_uuid` нужен для канала; читать его отдельным запросом значило бы
        добавить SQL и окно «адресат сменился». `join` не размножает строки:
        `users.id` — первичный ключ, соединение 1:1.

        Вызывается ВСЕГДА после `ensure_rows`, без Python-условий (см. docstring
        модуля: R6 — самая дорогая ошибка стори). Пустой список означает
        «некому слать» и НЕ является поводом не ставить маркер: маркер уже
        поставлен CAS выше по стеку, иначе карточка выбиралась бы каждые 60 с
        вечно (AC-12).

        `payload` — уже сериализованная JSON-строка: она собирается один раз
        на карточку, и все адресаты получают побайтово одинаковое содержимое
        (AC-11: ни `user_id`, ни имён — только `card_uuid` и `starts_at`).
        """
        result = await self._session.execute(
            text(_CREATE_ATTENDANCE_REQUESTS_SQL),
            {
                "card_id": card_id,
                "author_id": author_id,
                "type": ATTENDANCE_REQUEST_TYPE,
                "payload": payload,
            },
        )
        # `Result` из async-сессии не типизирован как `CursorResult`; строки
        # берутся методом `all()`, который есть у любого результата.
        return [(uuid_lib.UUID(str(row[0])), uuid_lib.UUID(str(row[1]))) for row in result.all()]

    async def create_one(self, *, user_id: int, type: str, payload: str) -> uuid_lib.UUID | None:
        """9.2, Task 2.1: вставляет ОДНО уведомление и возвращает его `uuid`.

        Адресация строки при отметке доставки — по `uuid`, а не по
        `(type, card_uuid)` (Task 2.3): старый предикат отметки
        (`notification_delivery`) не различает два уведомления ОДНОГО типа по
        ОДНОЙ карточке. У автора их ровно столько, сколько заявок пришло:
        отметка по `(type, card_uuid)` после второй заявки задела бы обе строки
        (или ни одной — в зависимости от формы запроса), и `delivered_at`
        перестал бы означать «эта строка ушла в шину».

        `payload` — готовая JSON-строка (как в 7.3): сериализация живёт в
        сервисе, где собирается форма события, а не в SQL-слое.
        `CAST(:payload AS jsonb)` сохраняет `CHECK jsonb_typeof(payload)='object'`
        из 9.1.

        **9.3, AC-01: возвращает `None`, если тип отключён** (D-9.3-6). Gate
        стоит ВНУТРИ вставки (`WHERE NOT EXISTS ... notification_mutes`), и
        отфильтрованный `INSERT ... SELECT` не возвращает строк — это
        нормальный исход настройки, а не ошибка. Вызывающий обязан ранне
        выйти без публикации WS (D-9.3-7) и без лога ошибки.
        """
        result = await self._session.execute(
            text(_CREATE_ONE_SQL),
            {"user_id": user_id, "type": type, "payload": payload},
        )
        # AC-01, D-9.3-6: `None` — тип отключён пользователем, строка не создана.
        # `scalar_one_or_none`, а не `scalar_one`: gate отфильтровал ВСЕ строки,
        # и `scalar_one` бросил бы `NoResultFound` на нормальном исходе
        # «пользователь не хочет этот тип» (мутация M1 ловит именно это).
        row = result.scalar_one_or_none()
        return None if row is None else uuid_lib.UUID(str(row))

    async def mark_delivered_by_uuid(self, *, notification_uuid: uuid_lib.UUID) -> int:
        """9.2, Task 2.2: `delivered_at = now()` — ТОЛЬКО после успешного `PUBLISH`.

        Возвращает число обновлённых строк (`1` — отметка поставлена, `0` —
        строка уже отмечена или её нет). Идемпотентность держит предикат
        `delivered_at IS NULL` (§18.11): повторный вызов не сдвигает время
        доставки, и `created_at <= delivered_at` остаётся верным.

        **Вызывается исключительно при `publish_to_user(...) is True`**
        (D-9.2-5): при локальной деградации событие дошло только до соединений
        этого процесса, и заполненный `delivered_at` соврал бы остальным
        инстансам (долг L-2). Пустое значение — честный сигнал «в шину не ушло».
        """
        result = await self._session.execute(
            text(_MARK_DELIVERED_SQL), {"notification_uuid": notification_uuid}
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def mark_delivered_many(self, *, notification_uuids: list[uuid_lib.UUID]) -> int:
        """AC-02 9.4, Task 2.4: `delivered_at` пачке ОДНИМ `UPDATE ... ANY`.

        Вызывается ТОЛЬКО с uuid строк, для которых `publish_to_user` вернул
        `True` (D-9.4-10). Возвращает число обновлённых строк.

        **Почему один запрос, а не цикл `mark_delivered_by_uuid`.** У карточки
        может быть много адресатов, а контракт D-TD-4 ограничивает число SQL
        на карточку. Кроме того, цикл дал бы N разных моментов доставки внутри
        одного события — расхождение, которое никто не заметит.

        **Предикат `delivered_at IS NULL`** (§18.11) держит идемпотентность:
        повторный прогон не сдвигает время доставки. Мутация M10 (снять
        предикат) краснит тест повторной отметки.

        Пустой список — no-op без обращения к БД: `publish` мог не уйти ни
        одному адресату (Redis недоступен), и тогда помечать нечего.
        """
        if not notification_uuids:
            return 0
        result = await self._session.execute(
            text(_MARK_DELIVERED_MANY_SQL),
            {"uuids": [str(item) for item in notification_uuids]},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def count_undelivered(self, *, user_id: int) -> int:
        """AC-01, граница эпика 9: сколько у пользователя не доставлено.

        Только чтение: в 7.3 колонки `delivered_at`/`read_at` создаются, но
        не заполняются. Тест `A05` проверяет оба значения (`0` до прогона,
        `N` после) — P-11.
        """
        result = await self._session.execute(text(_COUNT_UNDELIVERED_SQL), {"user_id": user_id})
        return int(result.scalar_one())

    # CR 7.3 LOW: `list_payloads` удалён. Он не вызывался ни в `app/`, ни в
    # `tests/` (тесты читают payload своим SQL в `attendance_prompt_helpers`),
    # то есть был мёртвым кодом с собственным представлением о форме доступа.
    # Удалили сразу: непокрытый метод разошёлся бы с реальным доступом молча.
